// ORP data model: the records conforming implementations exchange (spec §3). Field names match the wire format
// and the oracle's Memory.to_json(), so a store or bundle written by one engine reads in the other.
import { createHash, randomBytes } from "node:crypto";
import { isoformat, parseTime, type Micros } from "./time.js";
import { pyDumps } from "./pyjson.js";

export const KINDS = ["event", "document", "fact", "preference", "procedure", "observation", "summary", "profile",
  "entity"] as const;
export const ORIGINS = ["observed", "extracted", "inferred", "human", "imported", "seed"] as const;
export const STATUSES = ["active", "superseded", "retracted", "draft", "expired"] as const;
export const TOKEN_NAMESPACES = ["group", "channel", "role", "label", "team", "org"] as const;

export function newId(): string {
  return "mem_" + randomBytes(12).toString("hex");
}

export class Principal {
  readonly id: string;
  readonly groups: ReadonlySet<string>;
  readonly tenant: string;
  private _tokens?: ReadonlySet<string>;

  constructor(id: string, groups: Iterable<string> = [], tenant = "default") {
    this.id = id;
    this.groups = new Set(groups);
    this.tenant = tenant;
  }

  /** Access tokens (spec §6.1): principal:<id>, public, group:<g> for every group, and a group that already carries
   * a declared namespace (channel:C07, role:auditor, label:secret) is also a token as written. */
  tokens(): ReadonlySet<string> {
    if (!this._tokens) {
      const out = new Set<string>([`principal:${this.id}`, "public"]);
      for (const g of this.groups) {
        out.add(`group:${g}`);
        const i = g.indexOf(":");
        if (i >= 0 && (TOKEN_NAMESPACES as readonly string[]).includes(g.slice(0, i))) out.add(g);
      }
      this._tokens = out;
    }
    return this._tokens;
  }

  get isStar(): boolean {
    return this.groups.has("*");
  }

  withTenant(tenant: string): Principal {
    return new Principal(this.id, this.groups, tenant);
  }
}

export const SYSTEM = new Principal("system", ["*"]);

export interface Claim { subject: string; predicate: string; object: string; cardinality: string }
export interface Source { uri: string; span: string; hash: string; observed_at: string }

export function claimKey(c: Claim): string {
  return `${c.subject.toLowerCase().trim()}|${c.predicate.toLowerCase().trim()}`;
}

export interface Memory {
  content: string;
  kind: string;
  id: string;
  tenant: string;
  scope: string;
  readers: ReadonlySet<string> | null;
  writers: ReadonlySet<string> | null;
  origin: string;
  load: string;
  version: number;
  support: number;
  actor: string;
  role: string;
  thread: string;
  seq: number;
  observed_at: Micros | null;
  event_time: Micros | null;
  valid_from: Micros | null;
  valid_to: Micros | null;
  expires_at: Micros | null;
  status: string;
  supersedes: string[];
  superseded_by: string | null;
  claim: Claim | null;
  sources: Source[];
  derived_from: string[];
  labels: string[];
  confidence: number;
  meta: Record<string, any>;
}

export function makeMemory(init: Partial<Memory> & { content: string }): Memory {
  return {
    kind: "event", id: newId(), tenant: "default", scope: "", readers: null, writers: null, origin: "observed",
    load: "on_query", version: 1, support: 1, actor: "", role: "", thread: "", seq: 0, observed_at: null,
    event_time: null, valid_from: null, valid_to: null, expires_at: null, status: "active", supersedes: [],
    superseded_by: null, claim: null, sources: [], derived_from: [], labels: [], confidence: 1.0, meta: {}, ...init,
  };
}

/** Python str.strip(), then sha256 of tenant|thread|actor|content, first 32 hex chars (Memory.content_hash). */
export function contentHash(m: Memory): string {
  return createHash("sha256").update(`${m.tenant}|${m.thread}|${m.actor}|${m.content}`).digest("hex").slice(0, 32);
}

const sorted = (s: Iterable<string>) => [...s].sort(pyCmp);

/** Python's default string ordering (by code point), which differs from JS sort for astral characters. */
export function pyCmp(a: string, b: string): number {
  const A = [...a], B = [...b];
  const n = Math.min(A.length, B.length);
  for (let i = 0; i < n; i++) {
    const x = A[i]!.codePointAt(0)!, y = B[i]!.codePointAt(0)!;
    if (x !== y) return x < y ? -1 : 1;
  }
  return A.length - B.length;
}

/** De-duplication key (spec §4.1, M17): scoped to tenant AND writer AND audience. JSON form, as the store keeps it. */
export function dedupKey(m: Memory, writer: string): string {
  const rd = m.readers !== null ? sorted(m.readers) : null;
  return pyDumps(["content", m.tenant, writer, rd, sorted(m.labels), contentHash(m)]);
}

export function idemDedupKey(tenant: string, writer: string, key: string): string {
  return pyDumps(["key", tenant, writer, key]);
}

const TIME_FIELDS = ["observed_at", "event_time", "valid_from", "valid_to", "expires_at"] as const;

/** The oracle's Memory.to_json(): readers/writers sorted, times as naive ISO strings. */
export function memoryToJson(m: Memory): Record<string, any> {
  const d: Record<string, any> = {
    content: m.content, kind: m.kind, id: m.id, tenant: m.tenant, scope: m.scope,
    readers: m.readers !== null ? sorted(m.readers) : null, writers: m.writers !== null ? sorted(m.writers) : null,
    origin: m.origin, load: m.load, version: m.version, support: m.support, actor: m.actor, role: m.role,
    thread: m.thread, seq: m.seq,
    observed_at: null, event_time: null, valid_from: null, valid_to: null, expires_at: null,
    status: m.status, supersedes: [...m.supersedes], superseded_by: m.superseded_by,
    claim: m.claim ? { subject: m.claim.subject, predicate: m.claim.predicate, object: m.claim.object,
      cardinality: m.claim.cardinality } : null,
    sources: m.sources.map((s) => ({ uri: s.uri, span: s.span, hash: s.hash, observed_at: s.observed_at })),
    derived_from: [...m.derived_from], labels: [...m.labels], confidence: m.confidence,
    meta: structuredClone(m.meta),
  };
  for (const k of TIME_FIELDS) d[k] = m[k] !== null ? isoformat(m[k]!) : null;
  return d;
}

export function memoryFromJson(d0: Record<string, any>): Memory {
  const d = { ...d0 };
  const t = (v: unknown) => (v ? parseTime(String(v)) : null);
  const str = (v: unknown, dflt = "") => (v === undefined || v === null ? dflt : String(v));
  if (typeof d.content !== "string") throw new TypeError("content must be a string");
  const known = new Set(["content", "kind", "id", "tenant", "scope", "readers", "writers", "origin", "load", "version",
    "support", "actor", "role", "thread", "seq", "observed_at", "event_time", "valid_from", "valid_to", "expires_at",
    "status", "supersedes", "superseded_by", "claim", "sources", "derived_from", "labels", "confidence", "meta"]);
  for (const k of Object.keys(d)) if (!known.has(k)) throw new TypeError(`unexpected keyword argument '${k}'`);
  const strList = (v: unknown, name: string): string[] => {
    if (v === undefined || v === null) return [];
    if (!Array.isArray(v) || !v.every((x) => typeof x === "string")) throw new TypeError(`${name} must be strings`);
    return [...v];
  };
  return makeMemory({
    content: d.content, kind: str(d.kind, "event"), id: str(d.id) || newId(), tenant: str(d.tenant, "default"),
    scope: str(d.scope), readers: d.readers == null ? null : new Set(strList(d.readers, "readers")),
    writers: d.writers == null ? null : new Set(strList(d.writers, "writers")), origin: str(d.origin, "observed"),
    load: str(d.load, "on_query"), version: Number(d.version ?? 1), support: Number(d.support ?? 1),
    actor: str(d.actor), role: str(d.role), thread: str(d.thread), seq: Number(d.seq ?? 0),
    observed_at: t(d.observed_at), event_time: t(d.event_time), valid_from: t(d.valid_from), valid_to: t(d.valid_to),
    expires_at: t(d.expires_at), status: str(d.status, "active"), supersedes: strList(d.supersedes, "supersedes"),
    superseded_by: d.superseded_by ? String(d.superseded_by) : null,
    claim: d.claim ? { subject: str(d.claim.subject), predicate: str(d.claim.predicate), object: str(d.claim.object),
      cardinality: str(d.claim.cardinality, "one") } : null,
    sources: (Array.isArray(d.sources) ? d.sources : []).map((s: any) => ({ uri: str(s.uri), span: str(s.span),
      hash: str(s.hash), observed_at: str(s.observed_at) })),
    derived_from: strList(d.derived_from, "derived_from"), labels: strList(d.labels, "labels"),
    confidence: Number(d.confidence ?? 1), meta: d.meta && typeof d.meta === "object" ? structuredClone(d.meta) : {},
  });
}

export interface RecallRequest {
  query: string;
  principal: Principal;
  tenant: string;
  as_of: Micros | null;
  known_at: Micros | null;
  scope: string;
  kinds: string[] | null;
  budget_tokens: number | null;
  max_items: number;
  include_history: boolean;
  mode: string;
  filters: {
    labels?: string[]; drafts?: boolean; granularity?: string; neighbours?: number;
    quotas?: Record<string, number>; prefer?: string[];
  };
}

export function recallRequest(init: Partial<RecallRequest> & { query: string }): RecallRequest {
  return { principal: SYSTEM, tenant: "default", as_of: null, known_at: null, scope: "", kinds: null,
    budget_tokens: 2000, max_items: 50, include_history: false, mode: "balanced", filters: {}, ...init };
}

export interface ContextItem {
  memory_ids: string[];
  text: string;
  score: number;
  why: Record<string, any>;
  tokens: number;
  citations: string[];
  valid: string;
  turn_ids: string[];
}

export interface ContextPack {
  items: ContextItem[];
  tokens: number;
  budget: number | null;
  coverage: number;
  snapshot: string;
  notes: string[];
}
