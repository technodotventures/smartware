// Importers from other memory projects' formats (the "migration in" half of bundles). Each reads a file as data,
// maps it onto ORP records exactly as the matching compatibility adapter would have written them, and writes through
// the ordinary verbs (validation, secret scan, de-duplication, closure). Writes carry idempotency keys derived from
// their content, so importing the same file twice writes nothing new.
//
//   mcp-memory  the reference MCP memory server's memory.json (JSON lines of entities and relations, or the older
//               single object with entities[] and relations[]); entities, observations and relations land exactly
//               as the mcp-memory toolset writes them
//   mem0        a mem0 export: a list of memories, or {results: [...]} / {memories: [...]}, as returned by get_all;
//               user_id / agent_id / run_id become the same scopes the mem0 shim uses
//   letta       a Letta agent file (.af): core memory blocks become always-loaded profile records, messages become
//               events in order, archival passages become documents (same mapping as the Python client's importer)
import { createHash } from "node:crypto";
import { ORPError, type Principal, type Service } from "@orp/core";
import { McpSession } from "./mcp.js";
import { scopeOf } from "./mem0.js";

type Json = Record<string, any>;
export const IMPORT_FORMATS = ["mcp-memory", "mem0", "letta"] as const;
export type ImportFormat = (typeof IMPORT_FORMATS)[number];

export interface ImportOptions {
  /** Readers for every record: a token list, or null for everyone in the tenant. Default: the importing principal. */
  readers?: string[] | null;
  batchSize?: number;
  /** letta: keep assistant reasoning alongside replies. */
  includeReasoning?: boolean;
}

const digest = (x: unknown) => createHash("sha256").update(JSON.stringify(x)).digest("hex").slice(0, 24);
const enc = (s: unknown) => encodeURIComponent(String(s)).replace(/~/g, "%7E");

async function writeBatches(svc: Service, pr: Principal, mems: Json[], keyPrefix: string, batch: number): Promise<{ ids: string[]; deduplicated: number }> {
  const ids: string[] = [];
  let deduplicated = 0;
  for (let i = 0; i < mems.length; i += batch) {
    const chunk = mems.slice(i, i + batch);
    const r = await svc.remember({ memories: chunk, idempotency_key: `${keyPrefix}:${i / batch}:${digest(chunk)}` }, pr);
    ids.push(...r.ids);
    deduplicated += (r.deduplicated ?? []).length;
  }
  return { ids, deduplicated };
}

function parseJson(text: string, what: string): any {
  try {
    return JSON.parse(text);
  } catch (e) {
    throw new ORPError("invalid_parameter", `${what} is not valid JSON: ${(e as Error).message}`);
  }
}

// ------------------------------------------------------------ MCP memory server
export async function importMcpMemory(svc: Service, pr: Principal, text: string, opts: ImportOptions = {}): Promise<Json> {
  const entities: Json[] = [], relations: Json[] = [];
  const trimmed = text.trim();
  const add = (x: any, line?: number) => {
    if (!x || typeof x !== "object") throw new ORPError("invalid_parameter", `memory.json${line ? ` line ${line}` : ""}: expected an object`);
    if (x.type === "entity" || (x.type === undefined && "entityType" in x)) {
      if (typeof x.name !== "string" || !x.name) throw new ORPError("invalid_parameter", "an entity needs a name");
      entities.push({ name: x.name, entityType: String(x.entityType ?? ""), observations: (Array.isArray(x.observations) ? x.observations : []).filter((o: unknown) => typeof o === "string" && o.trim()) });
    } else if (x.type === "relation" || (x.type === undefined && "relationType" in x)) {
      if (typeof x.from !== "string" || typeof x.to !== "string" || typeof x.relationType !== "string") throw new ORPError("invalid_parameter", "a relation needs from, to and relationType");
      relations.push({ from: x.from, to: x.to, relationType: x.relationType });
    }
  };
  if (trimmed.startsWith("{") && !trimmed.includes("\n{")) {
    const o = parseJson(trimmed, "memory.json");
    if (Array.isArray(o.entities) || Array.isArray(o.relations)) {
      for (const e of o.entities ?? []) add({ type: "entity", ...e });
      for (const r of o.relations ?? []) add({ type: "relation", ...r });
    } else add(o, 1);
  } else {
    trimmed.split("\n").forEach((l, i) => { if (l.trim()) add(parseJson(l, `memory.json line ${i + 1}`), i + 1); });
  }
  const readers = opts.readers === undefined ? "self" : opts.readers === null ? "tenant" : opts.readers;
  const session = new McpSession(svc, pr, { toolsets: ["mcp-memory"], defaultReaders: readers });
  let createdEntities = 0, createdRelations = 0, addedObservations = 0;
  const skipped: Json[] = [];
  const batch = opts.batchSize ?? 100;
  const created = new Set<string>();
  for (let i = 0; i < entities.length; i += batch) {
    const r = await session.call("create_entities", { entities: entities.slice(i, i + batch) });
    for (const e of r.structuredContent.entities as Json[]) created.add(e.name);
    createdEntities += r.structuredContent.entities.length;
  }
  // an entity that already existed (a re-import of a grown file) gets the observations it lacks; one that was
  // forgotten on purpose stays forgotten (the re-ingest block applies, unlike the add_observations tool)
  const obsReaders = readers === "self" ? [`principal:${pr.id}`] : readers === "tenant" ? null : readers;
  for (const e of entities) {
    if (created.has(e.name) || !e.observations.length) continue;
    const have = new Set<string>();
    let cursor: number | null = 0;
    while (cursor !== null) {
      const page: Json = await svc.list({ kinds: ["observation"], thread: `entity:${e.name}`, limit: 1000, cursor }, pr);
      for (const m of page.memories as Json[]) have.add(m.content);
      cursor = page.cursor;
    }
    for (const o of new Set<string>(e.observations)) {
      if (have.has(o)) continue;
      try {
        await svc.remember({ memories: [{ kind: "observation", content: o, readers: obsReaders, thread: `entity:${e.name}`, meta: { entity: e.name } }] }, pr);
        addedObservations++;
      } catch (err) {
        if (err instanceof ORPError && err.code === "forbidden") skipped.push({ entity: e.name, observation: o, reason: "previously forgotten" });
        else throw err;
      }
    }
  }
  for (const rel of relations) {
    try {
      const r = await session.call("create_relations", { relations: [rel] });
      createdRelations += r.structuredContent.relations.length;
    } catch (e) {
      if (e instanceof ORPError && e.code === "not_found") skipped.push({ relation: rel, reason: "endpoint entity not found" });
      else throw e;
    }
  }
  return { format: "mcp-memory", entities: entities.length, created_entities: createdEntities, added_observations: addedObservations,
    relations: relations.length, created_relations: createdRelations, skipped };
}

// ------------------------------------------------------------ mem0
export async function importMem0(svc: Service, pr: Principal, text: string, opts: ImportOptions = {}): Promise<Json> {
  const o = parseJson(text, "mem0 export");
  const rows: any[] = Array.isArray(o) ? o : Array.isArray(o?.results) ? o.results : Array.isArray(o?.memories) ? o.memories : null;
  if (!rows) throw new ORPError("invalid_parameter", "a mem0 export is a list of memories, or an object with results[] or memories[]");
  const readers = opts.readers === undefined ? [`principal:${pr.id}`] : opts.readers;
  const mems: Json[] = [];
  const skipped: Json[] = [];
  for (const [n, r] of rows.entries()) {
    const content = r?.memory ?? r?.text ?? r?.data?.memory;
    if (typeof content !== "string" || !content.trim()) { skipped.push({ index: n, reason: "no memory text" }); continue; }
    const ids = { user: r.user_id ?? undefined, agent: r.agent_id ?? undefined, run: r.run_id ?? undefined };
    if (ids.user === undefined && ids.agent === undefined && ids.run === undefined) { skipped.push({ index: n, reason: "no user_id, agent_id or run_id" }); continue; }
    const m: Json = { kind: "fact", origin: "imported", content, scope: scopeOf(ids).full, readers, actor: String(ids.user ?? ids.agent ?? "mem0"),
      sources: [{ uri: `mem0://memories/${enc(r.id ?? n)}` }],
      meta: { via: "mem0", ...(r.metadata && typeof r.metadata === "object" ? { mem0: r.metadata } : {}), ...(r.hash ? { mem0_hash: String(r.hash) } : {}),
        ...(r.id ? { mem0_id: String(r.id) } : {}) } };
    if (typeof r.created_at === "string" && r.created_at) m.event_time = r.created_at;
    if (Array.isArray(r.categories) && r.categories.every((c: unknown) => typeof c === "string")) m.meta.mem0_categories = r.categories;
    mems.push(m);
  }
  const w = await writeBatches(svc, pr, mems, "mem0", opts.batchSize ?? 100);
  return { format: "mem0", memories: rows.length, written: w.ids.length, deduplicated: w.deduplicated, skipped };
}

// ------------------------------------------------------------ Letta agent file
const SKIP_USER_TYPES = new Set(["heartbeat", "login", "system_alert"]);

function lettaText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.filter((p) => p && typeof p === "object" && (p.type ?? "text") === "text" && typeof p.text === "string").map((p) => p.text).join("\n");
  return "";
}

function lettaMessage(m: Json, reasoning: boolean): [string | null, string, string] {
  const role = m.role;
  let text = lettaText(m.content ?? m.text);
  if (role === "system") return [null, "system", "system_messages"];
  if (role === "tool") return [null, "tool", "tool_messages"];
  if (role === "user") {
    let wrapped: any = null;
    try { wrapped = JSON.parse(text); } catch { /* plain text */ }
    if (wrapped && typeof wrapped === "object" && !Array.isArray(wrapped) && "type" in wrapped) {
      if (SKIP_USER_TYPES.has(wrapped.type)) return [null, "user", "heartbeats"];
      if (typeof wrapped.message === "string") text = wrapped.message;
    }
    return [text.trim() ? text : null, "user", "empty_messages"];
  }
  if (role === "assistant") {
    let reply: string | null = null;
    const calls = Array.isArray(m.tool_calls) ? m.tool_calls : [];
    for (const tc of calls) {
      const fn = tc?.function ?? {};
      if (fn.name === "send_message") {
        let args: any = {};
        try { args = JSON.parse(fn.arguments || "{}"); } catch { args = {}; }
        if (typeof args.message === "string") reply = args.message;
      }
    }
    if (reply === null && !calls.length) reply = text;
    const parts: string[] = [];
    if (reasoning && text.trim() && reply !== text) parts.push(`(thinking) ${text}`);
    if (reply) parts.push(reply);
    const body = parts.join("\n");
    return [body.trim() ? body : null, "assistant", "tool_calls_without_reply"];
  }
  return [null, String(role), "other_roles"];
}

export async function importLetta(svc: Service, pr: Principal, text: string, opts: ImportOptions = {}): Promise<Json> {
  const data = parseJson(text, "agent file");
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new ORPError("invalid_parameter", "an agent file is a JSON object");
  let fmt: string;
  let agents: Array<[Json, Json[]]>;
  if (Array.isArray(data.agents)) {
    const blocks = new Map<string, Json>();
    for (const b of data.blocks ?? []) if (b && typeof b === "object") blocks.set(b.id, b);
    agents = data.agents.filter((a: any) => a && typeof a === "object").map((a: Json) => [a, [
      ...(a.block_ids ?? []).filter((i: string) => blocks.has(i)).map((i: string) => blocks.get(i)!),
      ...(a.core_memory ?? []).filter((b: any) => b && typeof b === "object")]]);
    fmt = "multi";
  } else if ("core_memory" in data || "messages" in data) {
    agents = [[data, (data.core_memory ?? []).filter((b: any) => b && typeof b === "object")]];
    fmt = "agent";
  } else throw new ORPError("invalid_parameter", "not a Letta agent file: expected core_memory/messages or agents/blocks");
  const readers = opts.readers === undefined ? [`principal:${pr.id}`] : opts.readers;
  const batch = opts.batchSize ?? 100;
  const summaries: Json[] = [];
  for (const [agent, blocks] of agents) {
    const name = String(agent.name ?? agent.id ?? "letta-agent");
    const agentKey = enc(agent.id ?? name);
    const scope = `letta/${enc(name)}`;
    const skipped: Record<string, number> = {};
    const skip = (r: string) => { skipped[r] = (skipped[r] ?? 0) + 1; };
    const profiles: Json[] = [];
    for (const b of blocks) {
      const value = b.value, label = b.label || "block";
      if (typeof value !== "string" || !value.trim()) { skip("empty_blocks"); continue; }
      const meta: Json = { block_label: label, letta_agent: name };
      if (b.limit !== undefined && b.limit !== null) meta.block_limit = b.limit;
      if (b.description) meta.block_description = b.description;
      profiles.push({ kind: "profile", load: "always", origin: "imported", content: value, scope, thread: `${name}/core-memory`, readers,
        sources: [{ uri: `letta-af://${enc(name)}/blocks/${enc(label)}` }], meta });
    }
    const events: Json[] = [];
    (Array.isArray(agent.messages) ? agent.messages : []).forEach((m: any, n: number) => {
      if (!m || typeof m !== "object") return;
      const [t, role, reason] = lettaMessage(m, Boolean(opts.includeReasoning));
      if (t === null) { skip(reason); return; }
      const ev: Json = { kind: "event", origin: "imported", content: t, role, actor: role === "assistant" ? name : (m.name || "user"),
        thread: name, seq: n, scope, readers, sources: [{ uri: `letta-af://${enc(name)}/messages/${enc(m.id || n)}` }], meta: { letta_agent: name } };
      if (m.id) ev.meta.letta_message_id = m.id;
      if (m.created_at) ev.event_time = m.created_at;
      events.push(ev);
    });
    const docs: Json[] = [];
    const passages: any[] = [...(agent.passages ?? []), ...(agent.archival_memory ?? [])];
    if (agent !== data) passages.push(...(data.passages ?? []).filter((x: any) => x && typeof x === "object" && x.agent_id === agent.id));
    passages.forEach((p, n) => {
      const t = p && typeof p === "object" ? p.text : typeof p === "string" ? p : null;
      if (typeof t !== "string" || !t.trim()) { skip("empty_passages"); return; }
      const d: Json = { kind: "document", origin: "imported", content: t, scope, thread: `${name}/archival`, readers,
        sources: [{ uri: `letta-af://${enc(name)}/passages/${enc((p && typeof p === "object" ? p.id : null) ?? n)}` }], meta: { letta_agent: name } };
      if (p && typeof p === "object" && p.created_at) d.event_time = p.created_at;
      docs.push(d);
    });
    const a = profiles.length ? await writeBatches(svc, pr, profiles, `letta:${agentKey}:blocks`, batch) : { ids: [], deduplicated: 0 };
    const b = events.length ? await writeBatches(svc, pr, events, `letta:${agentKey}:messages`, batch) : { ids: [], deduplicated: 0 };
    const c = docs.length ? await writeBatches(svc, pr, docs, `letta:${agentKey}:passages`, batch) : { ids: [], deduplicated: 0 };
    summaries.push({ agent: name, thread: name, scope, profile_ids: a.ids, event_ids: b.ids, document_ids: c.ids,
      deduplicated: a.deduplicated + b.deduplicated + c.deduplicated, skipped,
      not_imported: ["tools", "tool_rules", "llm_config", "embedding_config", "system"].filter((k) => agent[k]) });
  }
  const md = data.metadata && typeof data.metadata === "object" ? data.metadata : {};
  return { format: "letta", layout: fmt, version: data.version ?? md.revision_id ?? null, level: "L2", agents: summaries };
}

export async function importFrom(format: string, svc: Service, pr: Principal, text: string, opts: ImportOptions = {}): Promise<Json> {
  if (format === "mcp-memory") return importMcpMemory(svc, pr, text, opts);
  if (format === "mem0") return importMem0(svc, pr, text, opts);
  if (format === "letta") return importLetta(svc, pr, text, opts);
  throw new ORPError("invalid_parameter", `format must be one of ${IMPORT_FORMATS.join(", ")}`);
}
