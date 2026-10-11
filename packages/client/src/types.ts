// Wire types for the ORP 0.1 HTTP binding, written by hand to match schema/orp-0.1/*.schema.json.
// Identity fields (principal, tenant, actor of the request) are deliberately absent from every request type:
// the gateway takes them from the bearer token (M56).

export type Kind =
  | "event" | "document" | "fact" | "preference" | "procedure" | "observation" | "summary" | "profile" | "entity"
  | `x-${string}`;
export type Origin = "observed" | "extracted" | "inferred" | "human" | "imported" | "seed";
export type Role = "user" | "assistant" | "system" | "tool" | "human" | "connector";
export type Load = "always" | "on_scope" | "on_query";
export type Status = "active" | "superseded" | "retracted" | "draft" | "expired";
export type Validity = "current" | "superseded" | "historical";
export type RememberMode = "append" | "upsert" | "supersede" | "propose";
export type ForgetMode = "retract" | "redact" | "purge";
export type Granularity = "unit" | "span" | "thread" | "auto";
export type RecallMode = "fast" | "balanced" | "deep";
export type FeedbackSignal = "used" | "helpful" | "wrong" | "outdated" | "draft_final";
export type LinkType =
  | "about" | "mentions" | "part_of" | "follows" | "causes" | "supports" | "contradicts" | "supersedes"
  | "derived_from" | "relates" | `x-${string}`;
export type ErrorCode =
  | "unsupported_parameter" | "invalid_parameter" | "version_conflict" | "forbidden" | "not_found"
  | "budget_too_small" | "degraded" | "rate_limited" | "unauthorized" | "payload_too_large" | "internal"
  | "unavailable" | "network";

/** A timestamp: an ISO 8601 string (offsets are converted to UTC by the engine) or a Date. */
export type Time = string | Date;

export interface Claim {
  subject: string;
  predicate: string;
  object: string;
  cardinality?: "one" | "many";
}

export interface Source {
  uri: string;
  span?: string;
  hash?: string;
  observed_at?: string;
}

export interface EventTime {
  start?: string | null;
  end?: string | null;
  granularity?: string | null;
}

/** A memory to write (schema MemoryInput). `readers` is an explicit audience; leave it out and the client writes
 * for you alone. Tenant-wide writes need `audience: "tenant"` on the call, never `readers: null`. */
export interface MemoryInput {
  id?: string;
  kind?: Kind;
  content: string;
  claim?: Claim | null;
  scope?: string;
  readers?: string[];
  writers?: string[] | null;
  actor?: string;
  role?: Role | null;
  origin?: Origin;
  thread?: string;
  seq?: number;
  event_time?: string | EventTime | null;
  valid_from?: string | null;
  valid_to?: string | null;
  expires_at?: string | null;
  sources?: Source[];
  derived_from?: string[];
  labels?: string[];
  load?: Load;
  confidence?: number;
  support?: number;
  meta?: Record<string, unknown>;
  supersedes?: string[];
}

/** A stored record as the gateway returns it (schema Memory). */
export interface Memory {
  id: string;
  tenant: string;
  kind: Kind;
  content: string;
  claim?: Claim | null;
  scope?: string;
  readers: string[] | null;
  writers?: string[] | null;
  actor?: string;
  role?: string;
  origin: Origin;
  thread?: string;
  seq?: number;
  observed_at: string | null;
  event_time?: string | null;
  valid_from?: string | null;
  valid_to?: string | null;
  expires_at?: string | null;
  status: Status;
  supersedes?: string[];
  superseded_by?: string | null;
  version: number;
  sources?: Source[];
  derived_from?: string[];
  support?: number;
  confidence?: number;
  labels?: string[];
  load?: Load;
  meta?: Record<string, unknown>;
  /** The authenticated writer (actor is client-asserted, this is not). */
  written_by?: string | null;
}

/** Fields every response envelope carries. */
export interface Envelope {
  degraded: string[];
  request_id: string;
}

export interface ContextItem {
  ref?: string;
  memory_ids: string[];
  kind?: Kind;
  scope?: string;
  text: string;
  tokens: number;
  score: number;
  why: Record<string, unknown>;
  validity: Validity;
  event_time?: unknown;
  citations: string[];
  superseded?: string[];
}

export interface ContextPack extends Envelope {
  items: ContextItem[];
  tokens: number;
  budget: { tokens?: number };
  coverage: number;
  abstain_hint?: boolean;
  snapshot: string;
  notes?: string[];
  tokenizer?: string;
}

export interface ProfileItem { id: string; kind: Kind; text: string }
export interface ProfilePack extends Envelope { items: ProfileItem[]; tokens: number; budget: { tokens: number } }

export interface RememberResponse extends Envelope {
  ids: string[];
  replayed: boolean;
  stage: "accepted" | "stored" | "indexed";
  note?: string;
}

export interface ForgetResponse extends Envelope { dry_run: boolean; affected: string[] }

export interface HistoryVersion {
  id: string;
  object: string;
  status: Status;
  version: number;
  valid_from: string | null;
  valid_to: string | null;
  superseded_by: string | null;
}
export interface HistoryResponse extends Envelope { versions: HistoryVersion[] }

export interface Whoami { id: string; tenant: string; groups: string[]; tokens: string[] }

export interface ListPage extends Envelope { memories: Memory[]; cursor: number | null }

export interface ThreadUnit { id: string; actor: string; content: string; seq: number }
export interface UnitsResponse extends Envelope { memories: ThreadUnit[] }

export interface Edge {
  id: string;
  type: LinkType;
  from: string;
  to: string;
  valid_from?: string | null;
  valid_to?: string | null;
  sources?: Source[];
  tenant?: string;
}

export interface LinkRequest {
  type: LinkType;
  from: string;
  to: string;
  valid_from?: string | null;
  valid_to?: string | null;
  sources?: Source[];
}

export interface FeedbackRequest {
  target: string;
  signal: FeedbackSignal;
  draft?: string;
  final?: string;
  note?: string;
}

export interface ReflectRequest { scope?: string; threads?: string[]; kinds?: string[] }
export interface JobResponse extends Envelope { job?: string; status: string; created: string[] }

export interface Change {
  cursor: number;
  at?: string;
  op: string;
  id: string;
  tenant?: string;
  by?: string;
  version?: number | null;
  status?: Status | null;
  reason?: string;
  edge?: string;
}
export interface ChangesResponse extends Envelope { changes: Change[]; cursor: number }

export interface Bundle {
  manifest?: { spec: string; tenant: string; count: number; sha256: string; tombstones?: unknown[]; id_map?: Record<string, string> };
  "memories.jsonl": string;
  "edges.jsonl"?: string;
  degraded?: string[];
  request_id?: string;
}

export type Capabilities = Envelope & Record<string, unknown>;

// ---------------------------------------------------------------- call options

export interface RecallOptions {
  /** Hard cap on the pack, in the engine's tokeniser (declared in capabilities). Default 2000. */
  budgetTokens?: number;
  /** Most items to return (budget.items). */
  maxItems?: number;
  /** The clock: what was valid at this time. A run() client pins this once. */
  asOf?: Time;
  /** What the store knew at this time (corrections learned later do not hide older beliefs). */
  knownAt?: Time;
  /** Restrict to a scope path and its children. */
  scope?: string;
  /** Scopes to rank first. */
  prefer?: string[];
  /** Kinds to include, or a map of kind to a per-kind item quota. */
  kinds?: Kind[] | Record<string, number>;
  granularity?: Granularity;
  mode?: RecallMode;
  /** Return no items (abstain_hint: true) when coverage is under this. */
  minCoverage?: number;
  include?: {
    history?: boolean;
    drafts?: boolean;
    neighbours?: number | "auto" | null;
    explain?: boolean;
    labels?: string[];
  };
}

export interface RememberOptions {
  mode?: RememberMode;
  /** Makes the write safe to retry: a replay returns the original ids. */
  idempotencyKey?: string;
  /** Optimistic concurrency for mode "upsert". */
  ifVersion?: number;
  /** "self" (default): only you can read it. "tenant": everyone in the tenant (sends readers: null). */
  audience?: "self" | "tenant";
  wait?: "accepted" | "stored" | "indexed";
  allowReingest?: boolean;
  /** Defaults applied to string inputs and to memories that leave these out. */
  kind?: Kind;
  thread?: string;
  scope?: string;
}

export interface ForgetOptions {
  mode?: ForgetMode;
  cascade?: boolean;
  dryRun?: boolean;
  reason?: string;
  allowReingest?: boolean;
}

export interface ProfileOptions {
  /** Required: profile never returns unbounded ambient context. */
  budgetTokens: number;
  subject?: string;
  asOf?: Time;
}

export interface ListOptions {
  kinds?: Kind[];
  thread?: string;
  scope?: string;
  limit?: number;
  cursor?: number;
}

export interface ChangesOptions {
  cursor?: number;
  limit?: number;
  /** Keep polling for new changes instead of stopping at the end of the feed. */
  follow?: boolean;
  pollMs?: number;
  signal?: AbortSignal;
}
