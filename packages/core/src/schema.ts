// The Smartware 0.1 wire schemas (JSON Schema 2020-12), written once from spec §3, §4 and §8 and used for everything else:
// tools/gen-schemas.mjs writes schema/smartware-0.1/*.json, schema/openapi.json and schema/mcp-tools.json from this
// module, and drift tests check it against the spec's field table and against both engines' validators.
type S = Record<string, any>;

const str: S = { type: "string" };
const strs: S = { type: "array", items: { type: "string" } };
const ts: S = { type: "string", description: "ISO 8601 timestamp; offsets are converted to UTC" };
const nullable = (s: S): S => ({ anyOf: [s, { type: "null" }] });
const obj = (properties: S, required: string[] = [], extra: S = {}): S => ({ type: "object", properties, required, additionalProperties: false, ...extra });

export const KIND: S = { type: "string", description: "event | document | fact | preference | procedure | observation | summary | profile | entity, or x-<name>",
  anyOf: [{ enum: ["event", "document", "fact", "preference", "procedure", "observation", "summary", "profile", "entity"] }, { pattern: "^x-" }] };
export const ORIGIN: S = { enum: ["observed", "extracted", "inferred", "human", "imported", "seed"] };
export const ROLE: S = { enum: ["user", "assistant", "system", "tool", "human", "connector"] };
export const LOAD: S = { enum: ["always", "on_scope", "on_query"] };
export const STATUS: S = { enum: ["active", "superseded", "retracted", "draft", "expired"] };

export const Claim: S = obj({ subject: { type: "string", minLength: 1 }, predicate: { type: "string", minLength: 1 },
  object: { type: "string", minLength: 1 }, cardinality: { enum: ["one", "many"], default: "one" } }, ["subject", "predicate", "object"]);
export const Source: S = obj({ uri: str, span: str, hash: str, observed_at: str }, ["uri"]);
export const EventTime: S = obj({ start: nullable(ts), end: nullable(str), granularity: nullable(str) });

/** A record as engines return it (get, export). */
export const Memory: S = obj({
  id: str, tenant: str, kind: KIND, content: str, claim: nullable(Claim), scope: str, readers: nullable(strs), writers: nullable(strs),
  actor: str, role: { type: "string" }, origin: ORIGIN, thread: str, seq: { type: "integer" }, observed_at: nullable(ts),
  event_time: nullable(ts), valid_from: nullable(ts), valid_to: nullable(ts), expires_at: nullable(ts), status: STATUS,
  supersedes: strs, superseded_by: nullable(str), version: { type: "integer", minimum: 1 }, sources: { type: "array", items: Source },
  derived_from: strs, support: { type: "integer", minimum: 0 }, confidence: { type: "number", minimum: 0, maximum: 1 }, labels: strs,
  load: LOAD, meta: { type: "object" }, written_by: { type: ["string", "null"], description: "authenticated writer (actor is asserted)" },
}, ["id", "tenant", "kind", "content", "readers", "origin", "observed_at", "status", "version"]);

/** One memory in a remember request (engine-set fields such as observed_at, status and version are not authored). */
export const MemoryInput: S = obj({
  id: { type: "string", minLength: 1, pattern: "^[^~]+$" }, kind: KIND, content: { type: "string", minLength: 1 }, claim: nullable(Claim),
  scope: str, readers: nullable(strs), writers: nullable(strs), actor: str, role: nullable(ROLE), origin: ORIGIN, thread: str,
  seq: { type: "integer" }, event_time: { anyOf: [ts, EventTime, { type: "null" }] }, valid_from: nullable(ts), valid_to: nullable(ts),
  expires_at: nullable(ts), sources: { type: "array", items: Source }, derived_from: strs, labels: strs, load: LOAD,
  confidence: { type: "number", minimum: 0, maximum: 1 }, support: { type: "integer", minimum: 0 },
  meta: { type: "object", description: "free-form; keys starting with _ are reserved for engines" }, supersedes: strs,
}, ["content"]);

export const RememberRequest: S = obj({
  memories: { type: "array", items: MemoryInput }, mode: { enum: ["append", "upsert", "supersede", "propose"], default: "append" },
  idempotency_key: str, if_version: nullable({ type: "integer" }), wait: { enum: ["accepted", "stored", "indexed"], default: "indexed" },
  allow_reingest: { type: "boolean" },
}, ["memories"]);

const Envelope: S = { degraded: strs, request_id: str };

export const RememberResponse: S = obj({ ids: strs, replayed: { type: "boolean" }, stage: { enum: ["accepted", "stored", "indexed"] }, ...Envelope },
  ["ids", "replayed", "stage", "degraded", "request_id"]);

export const RecallRequest: S = obj({
  query: { type: "string", minLength: 1, pattern: "\\S" },
  queries: { type: "array", items: obj({ text: str, weight: { type: "number" } }, ["text"]), description: "engines MAY reject (must say so)" },
  as_of: nullable(ts), known_at: nullable(ts),
  scope: { anyOf: [str, obj({ within: str, prefer: strs })] },
  kinds: { anyOf: [strs, { type: "object", additionalProperties: { type: "integer", minimum: 0 } }, { type: "null" }] },
  budget: obj({ tokens: { type: "integer", minimum: 50 }, items: { type: "integer", minimum: 1 } }),
  granularity: { enum: ["unit", "span", "thread", "auto"] },
  include: obj({ history: { type: "boolean" }, drafts: { type: "boolean" }, neighbours: { anyOf: [{ type: "integer", minimum: 0, maximum: 10 }, { const: "auto" }, { type: "null" }] },
    explain: { type: "boolean" }, labels: strs }),
  time: { description: "engines MAY reject (must say so)" }, mode: { enum: ["fast", "balanced", "deep"] },
  min_coverage: nullable({ type: "number", minimum: 0, maximum: 1 }), cursor: { description: "engines MAY reject (must say so)" },
}, ["query"]);

export const ContextItem: S = obj({
  ref: str, memory_ids: strs, kind: KIND, scope: str, text: str, tokens: { type: "integer" }, score: { type: "number" },
  why: { type: "object" }, validity: { enum: ["current", "superseded", "historical"] }, event_time: {}, citations: strs, superseded: strs,
}, ["memory_ids", "text", "tokens", "score", "why", "validity", "citations"]);

export const ContextPack: S = obj({
  items: { type: "array", items: ContextItem }, tokens: { type: "integer" }, budget: obj({ tokens: { type: "integer" } }),
  coverage: { type: "number" }, abstain_hint: { type: "boolean" }, snapshot: str, notes: strs, tokenizer: str, ...Envelope,
}, ["items", "tokens", "budget", "coverage", "snapshot", "degraded", "request_id"]);

export const ProfileRequest: S = obj({ subject: nullable(str), budget: obj({ tokens: { type: "integer", minimum: 1 } }, ["tokens"]), as_of: nullable(ts) }, ["budget"]);
export const ForgetRequest: S = obj({ ids: strs, mode: { enum: ["retract", "redact", "purge"] }, cascade: { type: "boolean" },
  dry_run: { type: "boolean" }, reason: str, allow_reingest: { type: "boolean" } }, ["ids"]);
export const ForgetResponse: S = obj({ dry_run: { type: "boolean" }, affected: strs, ...Envelope }, ["dry_run", "affected"]);
export const FeedbackRequest: S = obj({ target: str, signal: { enum: ["used", "helpful", "wrong", "outdated", "draft_final"] }, draft: str,
  final: str, note: str }, ["target", "signal"]);
export const ReflectRequest: S = obj({ scope: str, threads: strs, kinds: strs });
export const LinkRequest: S = obj({ type: { anyOf: [{ enum: ["about", "mentions", "part_of", "follows", "causes", "supports", "contradicts",
  "supersedes", "derived_from", "relates"] }, { type: "string", pattern: "^x-" }] }, from: str, to: str, valid_from: nullable(ts),
valid_to: nullable(ts), sources: { type: "array", items: Source } }, ["type", "from", "to"]);
export const Change: S = obj({ cursor: { type: "integer" }, at: ts, op: str, id: str, tenant: str, by: str, version: nullable({ type: "integer" }),
  status: nullable(STATUS), reason: str, edge: str }, ["cursor", "op", "id"]);
export const ChangesResponse: S = obj({ changes: { type: "array", items: Change }, cursor: { type: "integer" }, ...Envelope }, ["changes", "cursor"]);
export const Error_: S = obj({ error: obj({ code: { enum: ["unsupported_parameter", "invalid_parameter", "version_conflict", "forbidden",
  "not_found", "budget_too_small", "degraded", "rate_limited", "unauthorized", "payload_too_large", "internal"] }, message: str,
details: { type: "object" } }, ["code", "message"]) }, ["error"]);
export const Bundle: S = obj({ manifest: obj({ spec: str, tenant: str, count: { type: "integer" }, sha256: str, tombstones: { type: "array" },
  id_map: { type: "object" } }, ["spec", "tenant", "count", "sha256"]), "memories.jsonl": str, "edges.jsonl": str, ...Envelope }, ["memories.jsonl"]);

export const SCHEMAS: Record<string, S> = { Memory, MemoryInput, Claim, Source, EventTime, RememberRequest, RememberResponse, RecallRequest,
  ContextItem, ContextPack, ProfileRequest, ForgetRequest, ForgetResponse, FeedbackRequest, ReflectRequest, LinkRequest, Change,
  ChangesResponse, Error: Error_, Bundle };
