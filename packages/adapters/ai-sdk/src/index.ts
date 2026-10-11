// Smartware memory for the Vercel AI SDK (language-model middleware), plus helpers for the OpenAI Agents SDK and the
// Claude Agent SDK. Structural types only: nothing here imports those packages.
import { SmartwareError, type MemoryApi, type RememberResponse } from "@technodotventures/smartware-client";
import {
  createMemoryTools, memoryHooks, toZodShape,
  type HookOptions, type MemoryTool, type ToolOptions, type ZodLike,
} from "@technodotventures/smartware-tools";

export { formatContext, lastUserText, messageText } from "@technodotventures/smartware-tools";

// ---------------------------------------------------------------- AI SDK middleware

/** The parts of LanguageModelV2CallOptions (AI SDK 5) / V1 (AI SDK 4) this middleware reads and writes. */
export interface CallParams {
  prompt: Array<{ role: string; content: unknown; [k: string]: unknown }>;
  providerOptions?: Record<string, Record<string, unknown> | undefined>;
  providerMetadata?: Record<string, Record<string, unknown> | undefined>;
  [k: string]: unknown;
}

export interface Middleware {
  middlewareVersion?: "v2";
  transformParams(args: { type: "generate" | "stream"; params: CallParams; model?: unknown }): Promise<CallParams>;
  wrapGenerate(args: { doGenerate: () => Promise<any>; doStream?: () => Promise<any>; params: CallParams; model?: unknown }): Promise<any>;
  wrapStream(args: { doGenerate?: () => Promise<any>; doStream: () => Promise<{ stream: ReadableStream<any>; [k: string]: unknown }>; params: CallParams; model?: unknown }): Promise<any>;
}

export interface MiddlewareOptions {
  /** Thread for recorded turns. A per-call providerOptions[key].thread wins. */
  thread?: string;
  /** Scope for recall and recorded turns. A per-call providerOptions[key].scope wins. */
  scope?: string;
  /** Recall budget per call. Default 1500. */
  budgetTokens?: number;
  /** Also inject profile items within this budget. Default 0 (off). */
  profileBudget?: number;
  /** Record the user's turn. Default true. */
  recordUser?: boolean;
  /** Record the assistant's reply. Default false: replies are the model's words, not the user's. */
  recordReply?: boolean;
  /** Who can read recorded turns. Default "self". */
  audience?: "self" | "tenant";
  /** providerOptions key this middleware reads. Default "memory". */
  providerKey?: string;
  /** When recall fails: true (default) carries on without memory and reports degraded "ai-sdk:recall-failed";
   * false fails the call. */
  failOpen?: boolean;
  /** Called when recording a turn fails (the reply is still returned). Default: console.warn with the error code. */
  onError?: (err: unknown) => void;
  /** AI SDK middleware version. "v2" (AI SDK 5, default) or "v1" (AI SDK 4). */
  version?: "v1" | "v2";
  header?: string;
}

/** Per-call settings a caller may pass in providerOptions[providerKey]. Identity fields are refused. */
export interface CallMemoryOptions { thread?: string; scope?: string; asOf?: string }
const CALL_KEYS = new Set(["thread", "scope", "asOf"]);

function callOptions(params: CallParams, key: string): CallMemoryOptions {
  const raw = params.providerOptions?.[key] ?? params.providerMetadata?.[key];
  if (raw === undefined) return {};
  if (!raw || typeof raw !== "object") throw new SmartwareError("invalid_parameter", `providerOptions.${key} must be an object`);
  const extra = Object.keys(raw).filter((k) => !CALL_KEYS.has(k));
  if (extra.length) {
    // principal, user, tenant, readers ...: identity and audience come from credentials and config, never a call
    throw new SmartwareError("unsupported_parameter", `providerOptions.${key}: unsupported ${extra.join(", ")}`, 0, { parameters: extra });
  }
  for (const k of Object.keys(raw)) if (typeof raw[k] !== "string") throw new SmartwareError("invalid_parameter", `providerOptions.${key}.${k} must be a string`);
  return raw as CallMemoryOptions;
}

function replyText(result: any): string {
  if (!result || typeof result !== "object") return "";
  if (typeof result.text === "string") return result.text; // V1
  if (Array.isArray(result.content)) return result.content.filter((p: any) => p?.type === "text").map((p: any) => p.text).join(""); // V2
  return "";
}

/** Recall before the model call, record the turn after. Use with wrapLanguageModel({ model, middleware }). */
export function memoryMiddleware(client: MemoryApi, opts: MiddlewareOptions = {}): Middleware {
  const key = opts.providerKey ?? "memory";
  const onError = opts.onError ?? ((e: unknown) => console.warn(`[smartware] ai-sdk: could not record the turn (${e instanceof SmartwareError ? e.code : "error"})`));
  const hooksFor = (params: CallParams): ReturnType<typeof memoryHooks> => {
    const c = callOptions(params, key);
    const h: HookOptions = { budgetTokens: opts.budgetTokens ?? 1500, profileBudget: opts.profileBudget ?? 0,
      recordUser: opts.recordUser ?? true, recordReply: opts.recordReply ?? false, audience: opts.audience ?? "self",
      ...(opts.header ? { header: opts.header } : {}) };
    const thread = c.thread ?? opts.thread, scope = c.scope ?? opts.scope;
    if (thread !== undefined) h.thread = thread;
    if (scope !== undefined) h.scope = scope;
    if (c.asOf !== undefined) h.asOf = c.asOf;
    return memoryHooks(client, h);
  };
  const record = async (params: CallParams, reply: string) => {
    try {
      await hooksFor(params).after(params.prompt, reply ? { role: "assistant", content: reply } : undefined);
    } catch (e) {
      onError(e);
    }
  };
  return {
    ...(opts.version === "v1" ? {} : { middlewareVersion: "v2" as const }),
    async transformParams({ params }) {
      const hooks = hooksFor(params); // validates providerOptions first, even on failOpen
      let text = "";
      try {
        text = await hooks.before(params.prompt);
      } catch (e) {
        if (opts.failOpen === false) throw e;
        client.reportDegraded(["ai-sdk:recall-failed"], "ai-sdk");
      }
      if (!text) return params;
      const prompt = [...params.prompt];
      let at = prompt.findIndex((m) => m.role !== "system");
      if (at < 0) at = prompt.length;
      prompt.splice(at, 0, { role: "system", content: text });
      return { ...params, prompt };
    },
    async wrapGenerate({ doGenerate, params }) {
      const result = await doGenerate();
      await record(params, replyText(result));
      return result;
    },
    async wrapStream({ doStream, params }) {
      const { stream, ...rest } = await doStream();
      let text = "";
      const tap = new TransformStream<any, any>({
        transform(chunk, ctl) {
          if (chunk && chunk.type === "text-delta") text += typeof chunk.delta === "string" ? chunk.delta : typeof chunk.textDelta === "string" ? chunk.textDelta : "";
          ctl.enqueue(chunk);
        },
        async flush() { await record(params, text); },
      });
      return { stream: stream.pipeThrough(tap), ...rest };
    },
  };
}

// ---------------------------------------------------------------- generic helpers (any SDK)

/** A context block for one question: recall (and optionally profile), formatted as evidence. "" when empty. */
export async function recallContext(client: MemoryApi, query: string, opts: HookOptions = {}): Promise<string> {
  return memoryHooks(client, opts).before([{ role: "user", content: query }]);
}

/** Record one turn as events in a thread. */
export async function recordTurn(client: MemoryApi, turn: { user?: string; reply?: string }, opts: HookOptions = {}): Promise<RememberResponse | null> {
  const msgs = turn.user ? [{ role: "user", content: turn.user }] : [];
  return memoryHooks(client, { ...opts, recordUser: Boolean(turn.user), recordReply: Boolean(turn.reply) })
    .after(msgs, turn.reply ? { role: "assistant", content: turn.reply } : undefined);
}

// ---------------------------------------------------------------- OpenAI Agents SDK

/** The config @openai/agents' tool() accepts (the fields set here). */
export interface OpenAIToolConfig {
  name: string;
  description: string;
  parameters: unknown;
  strict: boolean;
  execute: (input: any) => Promise<string>;
}

/** Memory tools for the OpenAI Agents SDK: pass its tool() function in. Parameters are JSON Schema with
 * strict: false, because the memory tools have optional fields. */
export function openAIAgentsTools<T>(client: MemoryApi, tool: (config: OpenAIToolConfig) => T, opts: ToolOptions = {}): T[] {
  return (Object.values(createMemoryTools(client, opts)) as MemoryTool[]).map((t) => tool({
    name: t.name, description: t.description, parameters: t.inputSchema, strict: false,
    execute: async (input: unknown) => JSON.stringify(await t.execute(typeof input === "string" ? JSON.parse(input) : input)),
  }));
}

// ---------------------------------------------------------------- Claude Agent SDK

export type ClaudeToolFn<T> = (name: string, description: string, shape: Record<string, any>,
  handler: (args: any, extra?: unknown) => Promise<{ content: Array<{ type: "text"; text: string }>; isError?: boolean }>) => T;

/** Memory tools for the Claude Agent SDK: pass its tool() function and Zod in, then hand the result to
 * createSdkMcpServer({ name: "memory", version: "0.2.0", tools }). Errors come back as tool results with
 * isError, so the model can read them. */
export function claudeAgentTools<T>(client: MemoryApi, tool: ClaudeToolFn<T>, z: ZodLike, opts: ToolOptions = {}): T[] {
  return (Object.values(createMemoryTools(client, opts)) as MemoryTool[]).map((t) => tool(t.name, t.description, toZodShape(t.inputSchema, z),
    async (args: unknown) => {
      try {
        return { content: [{ type: "text", text: JSON.stringify(await t.execute(args)) }] };
      } catch (e) {
        if (e instanceof SmartwareError) return { content: [{ type: "text", text: `${e.code}: ${e.message}` }], isError: true };
        throw e;
      }
    }));
}

/** A UserPromptSubmit hook for the Claude Agent SDK: recalls for the prompt and returns it as additionalContext,
 * and records the prompt. Use as options.hooks = claudeAgentHooks(client, { thread }). The SDK's hooks do not
 * carry the assistant's reply: record it with recordTurn() after the query loop if you want it. */
export function claudeAgentHooks(client: MemoryApi, opts: HookOptions = {}) {
  const hooks = memoryHooks(client, { ...opts, recordReply: false });
  const onPrompt = async (input: { prompt?: unknown; session_id?: string; [k: string]: unknown }) => {
    const prompt = typeof input?.prompt === "string" ? input.prompt : "";
    if (!prompt.trim()) return {};
    const msgs = [{ role: "user", content: prompt }];
    const [context] = await Promise.all([hooks.before(msgs), opts.recordUser === false ? null : hooks.after(msgs)]);
    return context ? { hookSpecificOutput: { hookEventName: "UserPromptSubmit" as const, additionalContext: context } } : {};
  };
  return { UserPromptSubmit: [{ hooks: [onPrompt] }] };
}
