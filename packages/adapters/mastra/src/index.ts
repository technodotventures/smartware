// Smartware memory for Mastra. Nothing here imports Mastra: pass its createTool (and Zod) in, and add the processors to
// an Agent's inputProcessors / outputProcessors. The shapes below are structural types for the Mastra interfaces
// this adapter targets (see README for the release and what to check).
import type { MemoryApi } from "@technodotventures/smartware-client";
import { createMemoryTools, memoryHooks, toZod, type HookOptions, type MemoryHooks, type MemoryTool, type ToolOptions, type ZodLike } from "@technodotventures/smartware-tools";

export { memoryHooks as mastraHooks } from "@technodotventures/smartware-tools";

/** The config object Mastra's createTool accepts (the fields this adapter sets). */
export interface MastraToolConfig {
  id: string;
  description: string;
  inputSchema: unknown;
  execute: (arg: any, ctx?: any) => Promise<unknown>;
}
export type CreateTool<T = unknown> = (config: MastraToolConfig) => T;

export interface MastraToolOptions extends ToolOptions {
  /** Zod, for Mastra releases whose createTool only accepts Zod schemas. Without it the JSON Schema is passed. */
  z?: ZodLike;
}

/** The tool's own arguments, from either execute shape: ({ context, ... }) in Mastra 0.x, (inputData, ctx) later. */
export function toolInput(arg: unknown): unknown {
  if (arg && typeof arg === "object" && !Array.isArray(arg) && "context" in arg) {
    const c = (arg as { context: unknown }).context;
    if (c && typeof c === "object" && !Array.isArray(c)) return c;
  }
  return arg;
}

/** Mastra tools keyed by id: memoryRecall, memoryRemember, memoryForget, memoryProfile. Identity is the client's
 * credentials; Mastra's resourceId and runtime context are never used as a principal. */
export function mastraTools<T>(client: MemoryApi, createTool: CreateTool<T>, opts: MastraToolOptions = {}): Record<string, T> {
  const { z, ...toolOpts } = opts;
  const tools = createMemoryTools(client, toolOpts);
  const out: Record<string, T> = {};
  for (const [k, t] of Object.entries(tools) as Array<[string, MemoryTool]>) {
    const id = "memory" + k[0]!.toUpperCase() + k.slice(1);
    out[id] = createTool({
      id,
      description: t.description,
      inputSchema: z ? toZod(t.inputSchema, z) : t.inputSchema,
      execute: async (arg: unknown) => t.execute(toolInput(arg)),
    });
  }
  return out;
}

// ---------------------------------------------------------------- processors

/** The message fields this adapter reads (MastraMessageV2-like; plain { role, content } also works). */
export interface MastraMessageLike {
  id?: string;
  role: string;
  content: unknown;
  threadId?: string;
  resourceId?: string;
  createdAt?: Date;
  [k: string]: unknown;
}

export interface MastraProcessor {
  name: string;
  processInput?(args: { messages: MastraMessageLike[]; abort?: (reason?: string) => never; [k: string]: unknown }): Promise<MastraMessageLike[]>;
  processOutputResult?(args: { messages: MastraMessageLike[]; abort?: (reason?: string) => never; [k: string]: unknown }): Promise<MastraMessageLike[]>;
}

export interface ProcessorOptions extends Omit<HookOptions, "thread" | "scope"> {
  /** Thread for recorded turns. Default: the messages' threadId. */
  thread?: string;
  /** Scope for recall and recorded turns. */
  scope?: string;
  /** Map a Mastra resourceId to a scope segment: with "users", resourceId "u1" scopes to "users/u1". It narrows
   * recall and labels writes; it is never treated as a principal. Off by default. */
  resourceScope?: string;
  /** Build the system message that carries recalled context. Default: a MastraMessageV2-shaped system message. */
  toMessage?: (text: string, like: MastraMessageLike | undefined) => MastraMessageLike;
}

function defaultMessage(text: string, like: MastraMessageLike | undefined): MastraMessageLike {
  return {
    id: `memory-context-${globalThis.crypto.randomUUID()}`, role: "system", createdAt: new Date(),
    ...(like?.threadId ? { threadId: like.threadId } : {}), ...(like?.resourceId ? { resourceId: like.resourceId } : {}),
    content: { format: 2, parts: [{ type: "text", text }], content: text },
  };
}

/** An input processor that recalls for the last user message (and records it), and an output processor that
 * records the assistant's reply. Use with Agent({ inputProcessors: [input], outputProcessors: [output] }). */
export function memoryProcessors(client: MemoryApi, opts: ProcessorOptions = {}): { input: MastraProcessor; output: MastraProcessor } {
  const ctx = (messages: MastraMessageLike[]): { hooks: MemoryHooks; like: MastraMessageLike | undefined } => {
    const like = [...messages].reverse().find((m) => m.role === "user") ?? messages.at(-1);
    const scope = opts.resourceScope && like?.resourceId
      ? [opts.scope, opts.resourceScope, encodeURIComponent(String(like.resourceId))].filter(Boolean).join("/") : opts.scope;
    const thread = opts.thread ?? (typeof like?.threadId === "string" ? like.threadId : undefined);
    return { hooks: memoryHooks(client, { ...opts, ...(scope ? { scope } : {}), ...(thread ? { thread } : {}) }), like };
  };
  return {
    input: {
      name: "memory-recall",
      async processInput({ messages }) {
        const { hooks, like } = ctx(messages);
        const text = await hooks.before(messages);
        if (opts.recordUser !== false) await hooks.after(messages);
        if (!text) return messages;
        const sys = (opts.toMessage ?? defaultMessage)(text, like);
        const firstNonSystem = messages.findIndex((m) => m.role !== "system");
        const at = firstNonSystem < 0 ? messages.length : firstNonSystem;
        return [...messages.slice(0, at), sys, ...messages.slice(at)];
      },
    },
    output: {
      name: "memory-record",
      async processOutputResult({ messages }) {
        if (opts.recordReply === false) return messages;
        const reply = [...messages].reverse().find((m) => m.role === "assistant");
        if (reply) {
          const { hooks } = ctx(messages);
          await hooks.after([], reply);
        }
        return messages;
      },
    },
  };
}
