// ORP service layer (spec §4): the verbs with request validation, principal binding, concurrency control,
// governance (§6) and lifecycle (§7). A port of orp/api.py; the conformance suite and the doctor test both.
//
// Transports call Service methods with a principal resolved from credentials, never from request bodies.
// Every response carries request_id and degraded[]. Client-supplied ids are unique per tenant (M02): they are
// stored as `<id>~<tenant>` and every response shows the client's own id.
import { createHash, randomBytes } from "node:crypto";
import { Engine, newJournal } from "./engine.js";
import type { Encoder } from "./encoders.js";
import { claimKey, contentHash, KINDS, makeMemory, memoryFromJson, memoryToJson, newId, ORIGINS, Principal,
  pyCmp, recallRequest, type Claim, type Memory, type Source } from "./model.js";
import type { Params } from "./params.js";
import { FEATURES } from "./params.js";
import { pyDumps } from "./pyjson.js";
import { scanSecrets } from "./secrets.js";
import { emptyDelta, SqliteStore } from "./store.js";
import { countTokens, sentences, terms } from "./text.js";
import { epochSeconds, isoformat, MIN_TIME, nowMicros, parseTime, type Micros } from "./time.js";

export const SPEC_VERSION = "0.1";
export const ENGINE_VERSION = "0.2.0";
export const ROLES = ["user", "assistant", "system", "tool", "human", "connector"];
export const LOADS = ["always", "on_scope", "on_query"];
export const WAITS = ["accepted", "stored", "indexed"];
export const DEFAULT_RECALL_BUDGET = 2000;
export const MAX_ITEMS = 200, MAX_BUDGET = 32000;
const ENGINE_ID = /^mem_[0-9a-f]{24}$/;

export class ORPError extends Error {
  constructor(readonly code: string, message: string, readonly details: Record<string, any> = {}) {
    super(message);
  }

  toJSON() {
    return { error: { code: this.code, message: this.message, details: this.details } };
  }
}

type Json = Record<string, any>;

function checkKeys(body: unknown, allowed: Set<string>, where: string): asserts body is Json {
  if (body === null || typeof body !== "object" || Array.isArray(body)) {
    throw new ORPError("invalid_parameter", `${where}: expected an object`);
  }
  const extra = Object.keys(body).filter((k) => !allowed.has(k)).sort(pyCmp);
  if (extra.length) {
    throw new ORPError("unsupported_parameter", `${where}: unsupported parameter(s) ${pyList(extra)}`, { parameters: extra });
  }
}

const pyList = (xs: string[]) => "[" + xs.map((x) => `'${x}'`).join(", ") + "]";

function enumv<T extends string>(v: unknown, allowed: readonly T[], name: string): T {
  if (!(allowed as readonly unknown[]).includes(v)) {
    throw new ORPError("unsupported_parameter", `${name} ${pyRepr(v)} not in ${pyList([...allowed])}`);
  }
  return v as T;
}

const pyRepr = (v: unknown) => (typeof v === "string" ? `'${v}'` : v === undefined || v === null ? "None" : String(v));

function ts(v: unknown): Micros | null {
  if (v === undefined || v === null) return null;
  if (typeof v === "number") return v;
  try {
    return parseTime(String(v));
  } catch {
    throw new ORPError("invalid_parameter", `bad timestamp ${pyRepr(v)}`);
  }
}

function strs(v: unknown, name: string, nullable = false): string[] | null {
  if ((v === null || v === undefined) && nullable) return null;
  if (!Array.isArray(v) || !v.every((x) => typeof x === "string")) {
    throw new ORPError("invalid_parameter", `${name} must be a list of strings`);
  }
  return [...v];
}

function int(v: unknown, name: string, lo?: number, hi?: number): number {
  if (typeof v !== "number" || !Number.isFinite(v) || !Number.isInteger(v)) {
    throw new ORPError("invalid_parameter", `${name} must be an integer`);
  }
  if ((lo !== undefined && v < lo) || (hi !== undefined && v > hi)) {
    throw new ORPError("invalid_parameter", `${name} must be within [${lo ?? "None"}, ${hi ?? "None"}]`);
  }
  return v;
}

function num(v: unknown, name: string, lo?: number, hi?: number): number {
  if (typeof v !== "number" || !Number.isFinite(v)) throw new ORPError("invalid_parameter", `${name} must be a number`);
  if ((lo !== undefined && v < lo) || (hi !== undefined && v > hi)) {
    throw new ORPError("invalid_parameter", `${name} must be within [${lo ?? "None"}, ${hi ?? "None"}]`);
  }
  return v;
}

const REMEMBER_KEYS = new Set(["memories", "mode", "idempotency_key", "if_version", "wait", "allow_reingest"]);
const MEM_KEYS = new Set(["id", "kind", "content", "claim", "scope", "readers", "writers", "actor", "role", "origin",
  "thread", "seq", "event_time", "valid_from", "valid_to", "expires_at", "sources", "derived_from", "labels", "load",
  "confidence", "support", "meta", "supersedes"]);
export const RECALL_KEYS = new Set(["query", "queries", "as_of", "known_at", "scope", "kinds", "budget", "granularity",
  "include", "time", "mode", "min_coverage", "cursor"]);
const FORGET_KEYS = new Set(["ids", "mode", "cascade", "dry_run", "reason", "allow_reingest"]);
const LINK_TYPES = ["about", "mentions", "part_of", "follows", "causes", "supports", "contradicts", "supersedes",
  "derived_from", "relates"];

export function isAdmin(pr: Principal): boolean {
  return pr.groups.has("*") || pr.groups.has("admin");
}

const rid = () => "req_" + randomBytes(8).toString("hex");
const env = (out: Json): Json => {
  if (!("degraded" in out)) out.degraded = [];
  out.request_id = rid();
  return out;
};

export interface ServiceOptions {
  params?: Params;
  tokenizer?: string;
  store?: SqliteStore | null;
  /** Label content that looks like a credential as secret (default true). */
  secretScan?: boolean;
  encoderName?: string;
  rankerName?: string;
  notes?: string[];
  /** Persist remote-encoder vectors in the store so restarts do not re-embed. */
  cacheVectors?: boolean;
}

interface ChangeEntry extends Json { cursor: number; _iid: string; tenant: string }

/** One async lock: verbs run one at a time, so engine state is never observed half-written. */
class Mutex {
  private tail: Promise<void> = Promise.resolve();
  run<T>(fn: () => Promise<T> | T): Promise<T> {
    const r = this.tail.then(fn);
    this.tail = r.then(() => undefined, () => undefined);
    return r;
  }
}

export class Service {
  readonly engine: Engine;
  readonly tokenizer: string;
  readonly store: SqliteStore | null;
  readonly secretScan: boolean;
  encoderName: string;
  rankerName: string;
  notes: string[];
  keys = new Map<string, string[]>();
  jobs = new Map<string, Json>();
  feedbackLog: Json[] = [];
  edgesMap = new Map<string, Json>();
  tombstones = new Map<string, Json>();
  blocked = new Map<string, string[]>();
  changeFeed = new Map<string, ChangeEntry[]>();
  changeCount = new Map<string, number>();
  private loaded = new Set<string>();
  private lock = new Mutex();
  // per-verb persistence journal (service side; the engine keeps its own)
  private jIdem = new Set<string>();
  private jJobs = new Set<string>();
  private jEdges = new Set<string>();
  private jTomb = new Set<string>();
  private jBlocked = new Set<string>();
  private jChanges: ChangeEntry[] = [];
  private jFeedback: Json[] = [];

  constructor(readonly enc: Encoder, opts: ServiceOptions = {}) {
    this.engine = new Engine(enc, opts.params);
    this.tokenizer = opts.tokenizer ?? "orp-approx-1";
    this.store = opts.store ?? null;
    this.secretScan = opts.secretScan ?? true;
    this.encoderName = opts.encoderName ?? enc.name;
    this.rankerName = opts.rankerName ?? `orp-${this.engine.p.ranker}-1`;
    this.notes = opts.notes ?? [];
    if (this.store && opts.cacheVectors !== false && "persist" in enc) {
      const st = this.store;
      (enc as any).persist = { get: (e: string, h: string) => st.getVector(e, h), put: (e: string, h: string, v: Float32Array) => st.putVector(e, h, v) };
    }
    if (this.store) {
      const meta = this.store.meta();
      if (meta.secret) this.engine.secret = Buffer.from(meta.secret, "hex");
      else this.store.flush({ ...emptyDelta(), meta: { secret: this.engine.secret.toString("hex") } });
      this.engine.epoch = Number(meta.epoch ?? 0);
      this.engine.wseq = Number(meta.wseq ?? 0);
      this.engine.mutations = Number(meta.mutations ?? 0);
      this.engine.ordSeq = Number(meta.max_ord ?? 0);
    }
  }

  // ------------------------------------------------------------ tenancy and persistence
  /** Load a tenant from the store on first use. */
  ensureTenant(t: string) {
    if (this.loaded.has(t)) return;
    this.loaded.add(t);
    if (!this.store) return;
    const rows = this.store.loadTenant(t);
    const E = this.engine;
    for (const r of rows.memories) {
      const m = memoryFromJson(JSON.parse(r.record));
      E.mem.set(r.iid, m);
      let o = E.order.get(t);
      if (!o) E.order.set(t, (o = []));
      o.push(r.iid);
      E.ord.set(r.iid, r.ord);
      if (m.expires_at !== null) E.hasExpiry = true;
    }
    for (const c of rows.claims) {
      const k = t + "\0" + c.key;
      let a = E.claims.get(k);
      if (!a) E.claims.set(k, (a = []));
      a.push(c.iid);
    }
    for (const d of rows.dedup) E.loadHash(d.hkey, d.iid);
    for (const x of rows.idem) this.keys.set(`${t}\0${x.principal}\0${x.key}`, JSON.parse(x.ids));
    for (const b of rows.blocked) {
      const k = `${t}\0${b.chash}`;
      let a = this.blocked.get(k);
      if (!a) this.blocked.set(k, (a = []));
      a.push(b.iid);
    }
    for (const e of rows.edges) this.edgesMap.set(e.id, JSON.parse(e.body));
    for (const x of rows.tombstones) this.tombstones.set(x.iid, JSON.parse(x.body));
    for (const j of rows.jobs) this.jobs.set(j.id, JSON.parse(j.body));
    if (rows.counters) {
      E.tmut.set(t, rows.counters.tmut);
      E.tcount.set(t, rows.counters.tcount);
    }
    this.changeCount.set(t, rows.changeCount);
    E.invalidate(t);
  }

  /** Write everything the last verb changed, in one transaction. */
  private flush() {
    const E = this.engine, J = E.journal;
    if (!this.store) {
      E.journal = newJournal();
      this.jIdem.clear(); this.jJobs.clear(); this.jEdges.clear(); this.jTomb.clear(); this.jBlocked.clear();
      this.jChanges = []; this.jFeedback = [];
      return;
    }
    const d = emptyDelta();
    d.meta = { epoch: String(E.epoch), wseq: String(E.wseq), mutations: String(E.mutations) };
    for (const i of J.mem) {
      const m = E.mem.get(i);
      if (!m) continue;
      d.memories.push({ iid: i, tenant: m.tenant, ord: E.ord.get(i) ?? 0, record: pyDumps(memoryToJson(m), { sortKeys: true, floatKeys: FLOATS }) });
    }
    d.deleted = [...J.deleted];
    for (const k of J.claims) {
      const [tenant, key] = k.split("\0") as [string, string];
      d.claims.push({ tenant, key, iids: [...(E.claims.get(k) ?? [])] });
    }
    for (const [h, iid] of J.dedupAdd) d.dedupAdd.push({ hkey: h, tenant: E.mem.get(iid)?.tenant ?? JSON.parse(h)[1], iid });
    d.dedupDel = [...J.dedupDel];
    for (const k of this.jIdem) {
      const [tenant, principal, key] = k.split("\0") as [string, string, string];
      d.idem.push({ tenant, principal, key, ids: pyDumps(this.keys.get(k)) });
    }
    for (const k of this.jBlocked) {
      const [tenant, chash] = k.split("\0") as [string, string];
      d.blocked.push({ tenant, chash, iids: [...(this.blocked.get(k) ?? [])] });
    }
    for (const id of this.jEdges) { const e = this.edgesMap.get(id)!; d.edges.push({ id, tenant: e.tenant, body: pyDumps(e, { sortKeys: true }) }); }
    for (const i of this.jTomb) { const x = this.tombstones.get(i)!; d.tombstones.push({ iid: i, tenant: x.tenant, body: pyDumps(x, { sortKeys: true }) }); }
    for (const c of this.jChanges) d.changes.push({ tenant: c.tenant, cursor: c.cursor, body: pyDumps(c, { sortKeys: true }) });
    E.audit.forEach((a, n) => d.audit.push({ tenant: E.auditTenant[n] ?? "", body: pyDumps(a, { sortKeys: true }) }));
    for (const id of this.jJobs) { const j = this.jobs.get(id)!; d.jobs.push({ id, tenant: j.tenant, body: pyDumps(j, { sortKeys: true }) }); }
    for (const f of this.jFeedback) d.feedback.push({ tenant: f._tenant ?? "", body: pyDumps(stripUnderscore(f), { sortKeys: true }) });
    for (const t of J.tenants) d.counters.push({ tenant: t, tmut: E.tmut.get(t) ?? 0, tcount: E.tcount.get(t) ?? 0 });
    this.store.flush(d);
    E.audit = [];
    E.auditTenant = [];
    E.journal = newJournal();
    this.jIdem.clear(); this.jJobs.clear(); this.jEdges.clear(); this.jTomb.clear(); this.jBlocked.clear();
    this.jChanges = []; this.jFeedback = [];
    for (const t of this.changeFeed.keys()) this.changeFeed.set(t, []);
  }

  /** Run a verb under the lock, loading the caller's tenant and persisting what changed (or nothing, on error). */
  private async verb<T>(pr: Principal, write: boolean, fn: () => T | Promise<T>, extra: string[] = []): Promise<T> {
    return this.lock.run(async () => {
      this.ensureTenant(pr.tenant);
      if (this.enc.warm) await this.enc.warm([...this.engine.pendingTexts(pr.tenant), ...extra]);
      try {
        const out = await fn();
        if (write) this.flush();
        return out;
      } catch (e) {
        if (write) this.rollbackJournal();
        throw e;
      }
    });
  }

  /** On an error mid-verb, reload the tenant from the store so memory matches what was committed. */
  private rollbackJournal() {
    const E = this.engine;
    const dirty = E.journal.mem.size || E.journal.deleted.size || this.jIdem.size || this.jChanges.length;
    E.journal = newJournal();
    this.jIdem.clear(); this.jJobs.clear(); this.jEdges.clear(); this.jTomb.clear(); this.jBlocked.clear();
    this.jChanges = []; this.jFeedback = [];
    if (dirty && this.store) {
      // validation happens before any mutation, so this only runs on an internal error: reload from the store
      for (const t of this.loaded) this.unloadTenant(t);
    }
    E.audit = []; E.auditTenant = [];
  }

  private unloadTenant(t: string) {
    const E = this.engine;
    for (const i of E.order.get(t) ?? []) { E.mem.delete(i); E.ord.delete(i); }
    E.order.delete(t);
    for (const k of [...E.claims.keys()]) if (k.startsWith(t + "\0")) E.claims.delete(k);
    for (const [h, i] of [...E.hash]) if (!E.mem.has(i)) E.hash.delete(h);
    for (const k of [...this.keys.keys()]) if (k.startsWith(t + "\0")) this.keys.delete(k);
    for (const k of [...this.blocked.keys()]) if (k.startsWith(t + "\0")) this.blocked.delete(k);
    for (const [k, e] of [...this.edgesMap]) if (e.tenant === t) this.edgesMap.delete(k);
    for (const [k, x] of [...this.tombstones]) if (x.tenant === t) this.tombstones.delete(k);
    for (const [k, j] of [...this.jobs]) if (j.tenant === t) this.jobs.delete(k);
    this.loaded.delete(t);
    E.invalidate(t);
  }

  // ------------------------------------------------------------ ids
  private in_(tenant: string, cid: unknown): string {
    if (typeof cid !== "string") throw new ORPError("invalid_parameter", "ids are strings");
    const q = `${cid}~${tenant}`;
    return this.engine.mem.has(q) ? q : cid;
  }

  out(tenant: string, iid: string): string;
  out(tenant: string, iid: string | null): string | null;
  out(tenant: string, iid: string | null): string | null {
    if (typeof iid === "string" && iid.endsWith("~" + tenant)) return iid.slice(0, -tenant.length - 1);
    return iid;
  }

  private outl(t: string, ids: string[]): string[] {
    return ids.map((i) => this.out(t, i));
  }

  private readableId(i: string, pr: Principal): boolean {
    const m = this.engine.mem.get(i);
    return m !== undefined && this.canRead(m, pr);
  }

  // ------------------------------------------------------------ helpers
  private log(op: string, memId: string, pr: Principal, kw: Json = {}) {
    const m = this.engine.mem.get(memId);
    const tenant = m ? m.tenant : pr.tenant;
    const n = (this.changeCount.get(tenant) ?? 0) + 1;
    this.changeCount.set(tenant, n);
    const c: ChangeEntry = { cursor: n, at: isoformat(nowMicros()), op, id: this.out(tenant, memId), _iid: memId,
      tenant, by: pr.id, version: m ? m.version : null, status: m ? m.status : null, ...kw };
    if (this.store) this.jChanges.push(c);
    else {
      let feed = this.changeFeed.get(tenant);
      if (!feed) this.changeFeed.set(tenant, (feed = []));
      feed.push(c);
    }
  }

  canRead(m: Memory, pr: Principal, labels: readonly string[] = []): boolean {
    return this.engine.readable(m, pr, labels);
  }

  canWrite(m: Memory, pr: Principal): boolean {
    return this.engine.canWrite(m, pr);
  }

  /** Ids admissible for pr under the same rules as recall (§5.1, §5.2). */
  view(pr: Principal, asOf?: Micros | null, labels: string[] = []): Set<string> {
    return new Set(this.engine.admissible(recallRequest({ query: "", principal: pr, tenant: pr.tenant,
      as_of: asOf ?? nowMicros(), filters: { labels } })));
  }

  /** Wire form of a record, with client-facing ids. Links to records the caller cannot read are dropped. */
  record(m: Memory, pr: Principal, internal = false): Json {
    const t = m.tenant;
    const d = memoryToJson(m);
    d.id = this.out(t, m.id);
    const keep = internal ? () => true : (i: string) => this.readableId(i, pr);
    d.supersedes = m.supersedes.filter(keep).map((i) => this.out(t, i));
    d.superseded_by = m.superseded_by && keep(m.superseded_by) ? this.out(t, m.superseded_by) : null;
    d.derived_from = m.derived_from.filter((i) => internal || keep(i)).map((i) => this.out(t, i));
    if (internal) {
      const meta = { ...d.meta };
      if ("_supersedable_by" in meta) meta._supersedable_by = (meta._supersedable_by as string[]).map((i) => this.out(t, i));
      d.meta = meta;
    } else {
      d.written_by = m.meta._creator ?? null;
      d.meta = stripUnderscore(d.meta);
    }
    return d;
  }

  // ------------------------------------------------------------ capabilities
  async capabilities(pr: Principal): Promise<Json> {
    const p = this.engine.p;
    const learned = p.ranker === "linear" || p.ranker === "gbdt";
    const inactive = new Set([...p.drop_features, "rr_qexp", ...(p.w_sub <= 0 ? ["rr_sub"] : [])]);
    return env({
      spec: SPEC_VERSION, engine: `orp-ts/${ENGINE_VERSION}`,
      conformance: { claimed: ["L1", "L2", "L3", "L4"], suite: "orp doctor (spec/MUSTS.md)" },
      kinds: [...KINDS], origins: [...ORIGINS], roles: ROLES,
      tokens: ["principal:<id>", "public", "group:<g>", "channel:<c>", "role:<r>", "label:<l>", "team:<t>", "org:<o>"],
      channels: ["lexical:bm25(k1=1.2,b=0.75, admissible-set statistics)", "lexical:window(unit+admissible neighbours)",
        "lexical:segment-maxpool", "dense:unit", "dense:window", "dense:segment-maxpool", "thread(admissible units)",
        "entity", "temporal", "prf:rm3"],
      fusion: `weighted-rrf(k=${p.rrf_k})`, encoder: this.encoderName, notes: [...this.notes],
      ranker: {
        name: this.rankerName,
        features: learned ? FEATURES.filter((x) => !inactive.has(x)) : ["coverage", "actor", "role", "order", "current", "prefer"],
        post_ranker_multipliers: ["prefer", ...(learned ? [] : ["order"])],
        fitted_on: "public dev splits (LoCoMo-10 conv 0-2; LME-R dev split)",
      },
      score_scale: learned ? "probability-like 0..1 from the learned ranker" : "fused RRF normalised to top=1, times feature multipliers",
      coverage_method: learned ? "top item's ranker probability" : "top score / feature ceiling",
      recall_parameters: [...RECALL_KEYS].filter((k) => !["queries", "cursor", "time"].includes(k)).sort(pyCmp),
      rejected_parameters: ["queries", "cursor", "time"],
      granularity: ["unit", "span", "thread", "auto"], tokenizer: this.tokenizer,
      granularity_auto: "units; a short reply carries the prompt it answers" + (p.expand_next ? "; questions carry their reply" : ""),
      kinds_quota: "a map restricts recall to the listed kinds and caps each; a list only restricts",
      modes: { fast: "no pseudo-relevance feedback, rrf ranker", balanced: "full pipeline",
        deep: "same as balanced in this engine (no model-based reranking)" },
      dedup: { idempotency_key: "per (tenant, writer, key), unbounded window",
        content: "per (tenant, writer, readers, labels, thread, actor, content), unbounded window" },
      secret_scan: this.secretScan ? "content that looks like a credential is labelled secret on remember" : "off",
      defaults: { "budget.tokens": DEFAULT_RECALL_BUDGET, "budget.items": 50 },
      limits: { max_items: MAX_ITEMS, max_budget_tokens: MAX_BUDGET },
    });
  }

  // ------------------------------------------------------------ remember
  private parseTimeField(raw: Json, meta: Json): Micros | null {
    const et = raw.event_time;
    if (et !== null && typeof et === "object" && !Array.isArray(et)) {
      checkKeys(et, new Set(["start", "end", "granularity"]), "event_time");
      meta.event_end = et.end ?? null;
      meta.event_granularity = et.granularity ?? null;
      return ts(et.start);
    }
    return ts(et);
  }

  private buildMemory(raw: unknown, pr: Principal, mode: string): Memory {
    checkKeys(raw, MEM_KEYS, "memory");
    const t = pr.tenant;
    if (typeof raw.content !== "string" || !raw.content) throw new ORPError("invalid_parameter", "content must be a non-empty string");
    const kind = raw.kind ?? "event";
    if (!(KINDS as readonly string[]).includes(kind) && !String(kind).startsWith("x-")) {
      throw new ORPError("unsupported_parameter", `kind ${pyRepr(kind)}`);
    }
    const origin = enumv(raw.origin ?? (kind === "event" || kind === "document" ? "observed" : "human"), ORIGINS, "origin");
    if (raw.role !== undefined && raw.role !== null) enumv(raw.role, ROLES, "role");
    const load = enumv(raw.load ?? "on_query", LOADS, "load");
    for (const fld of ["scope", "thread", "actor"]) {
      if (fld in raw && typeof raw[fld] !== "string") throw new ORPError("invalid_parameter", `${fld} must be a string`);
    }
    if ("meta" in raw && (raw.meta === null || typeof raw.meta !== "object" || Array.isArray(raw.meta))) {
      throw new ORPError("invalid_parameter", "meta must be an object");
    }
    if (Object.keys(raw.meta ?? {}).some((k) => k.startsWith("_"))) {
      throw new ORPError("invalid_parameter", "meta keys starting with '_' are reserved for the engine");
    }
    const readers = strs(raw.readers, "readers", true);
    const writers = strs(raw.writers, "writers", true);
    let labels = strs(raw.labels ?? [], "labels")!;
    const derived = strs(raw.derived_from ?? [], "derived_from")!.map((s) => this.in_(t, s));
    if ((origin === "extracted" || origin === "inferred") && !derived.length) {
      throw new ORPError("invalid_parameter", "extracted/inferred memories require derived_from");
    }
    for (const s of derived) {
      const src = this.engine.mem.get(s);
      if (!src || !this.canRead(src, pr, ["secret"])) throw new ORPError("forbidden", `cannot derive from ${this.out(t, s)}`);
    }
    let claim: Claim | null = null;
    if (raw.claim !== undefined && raw.claim !== null) {
      const c = raw.claim;
      checkKeys(c, new Set(["subject", "predicate", "object", "cardinality"]), "claim");
      if (!["subject", "predicate", "object"].every((k) => typeof c[k] === "string" && c[k])) {
        throw new ORPError("invalid_parameter", "claim needs string subject, predicate and object");
      }
      enumv(c.cardinality ?? "one", ["one", "many"], "claim.cardinality");
      claim = { subject: c.subject, predicate: c.predicate, object: c.object, cardinality: c.cardinality ?? "one" };
    }
    let sources: Source[] = [];
    for (const s of (raw.sources ?? []) as unknown[]) {
      checkKeys(s, new Set(["uri", "span", "hash", "observed_at"]), "source");
      if (typeof s.uri !== "string") throw new ORPError("invalid_parameter", "source.uri is required");
      sources.push({ uri: String(s.uri), span: s.span !== undefined ? String(s.span) : "", hash: s.hash !== undefined ? String(s.hash) : "",
        observed_at: s.observed_at !== undefined ? String(s.observed_at) : "" });
    }
    const meta: Json = { ...(raw.meta ?? {}) };
    const eventTime = this.parseTimeField(raw, meta);
    if (origin !== "observed" && !sources.length) {
      sources = derived.length ? derived.map((s) => src(`orp://${t}/mem/${this.out(t, s)}`)) : [src(`orp://${t}/principal/${pr.id}`)];
    }
    if (this.secretScan) {
      const hits = scanSecrets(raw.content);
      if (hits.length && !labels.includes("secret")) {
        labels = [...labels, "secret"];
        meta._secret_scan = hits;
      }
    }
    const m = makeMemory({
      content: raw.content, kind, tenant: t, scope: raw.scope ?? "",
      readers: readers !== null ? new Set(readers) : null, writers: writers !== null ? new Set(writers) : null,
      actor: raw.actor ?? pr.id, role: raw.role ?? "", origin, thread: raw.thread ?? "",
      seq: int(raw.seq ?? 0, "seq"), observed_at: nowMicros(), event_time: eventTime,
      valid_from: ts(raw.valid_from), valid_to: ts(raw.valid_to), expires_at: ts(raw.expires_at),
      claim, sources, derived_from: derived, labels, load,
      confidence: num(raw.confidence ?? 1.0, "confidence", 0, 1), support: int(raw.support ?? 1, "support", 0), meta,
    });
    if (raw.id !== undefined && raw.id !== null) {
      const cid = raw.id;
      if (typeof cid !== "string" || !cid || cid.includes("~")) {
        throw new ORPError("invalid_parameter", "client ids are non-empty strings without '~'");
      }
      if (ENGINE_ID.test(cid)) { // an engine id may only name an existing record of this tenant (upsert)
        const ex = this.engine.mem.get(cid);
        if (!ex || ex.tenant !== t) throw new ORPError("invalid_parameter", "ids of the form mem_<24 hex> are engine-assigned");
        m.id = cid;
      } else {
        m.id = `${cid}~${t}`;
      }
    }
    m.meta._creator = pr.id;
    if (m.actor !== pr.id) m.meta._actor_asserted = true; // `actor` is client-asserted; `written_by` is authenticated
    if (mode === "propose") m.status = "draft";
    return m;
  }

  async remember(body: unknown, pr: Principal): Promise<Json> {
    checkKeys(body, REMEMBER_KEYS, "remember");
    const mode = enumv(body.mode ?? "append", ["append", "upsert", "supersede", "propose"], "mode");
    enumv(body.wait ?? "indexed", WAITS, "wait");
    if (!Array.isArray(body.memories ?? [])) throw new ORPError("invalid_parameter", "memories must be a list");
    return this.verb(pr, true, async () => {
      const raws = (body.memories ?? []) as Json[];
      const mems = raws.map((raw) => this.buildMemory(raw, pr, mode)); // validate all first
      if (this.enc.warm) await this.enc.warm(mems.flatMap((m) => this.engine.textsFor(m)));
      return env(this.rememberSync(body, pr, mode, mems));
    });
  }

  private rememberSync(body: Json, pr: Principal, mode: string, prebuilt?: Memory[]): Json {
    const t = pr.tenant;
    const key = body.idempotency_key;
    const kk = `${t}\0${pr.id}\0${key}`;
    if (key && this.keys.has(kk)) return { ids: this.outl(t, this.keys.get(kk)!), replayed: true, stage: "indexed" };
    const raws = (body.memories ?? []) as Json[];
    const mems = prebuilt ?? raws.map((raw) => this.buildMemory(raw, pr, mode)); // validate all first
    const out: string[] = [];
    const E = this.engine;
    raws.forEach((raw, idx) => {
      const m = mems[idx]!;
      if (!(body.allow_reingest || m.meta.allow_reingest)) {
        // blocked only if the writer could read the retracted record: never reveals others' content
        for (const bid of this.blocked.get(`${t}\0${contentHash(m)}`) ?? []) {
          const bm = E.mem.get(bid);
          if (bm && (this.canRead(bm, pr, ["secret"]) || bm.meta._creator === pr.id)) {
            throw new ORPError("forbidden", "content was retracted; re-ingest blocked");
          }
        }
      }
      const existing = E.mem.get(m.id);
      if (existing) {
        if (mode !== "upsert") throw new ORPError("version_conflict", `${this.out(t, m.id)} exists; use mode=upsert with if_version`);
        if (!this.canWrite(existing, pr)) throw new ORPError("forbidden", `no write access to ${this.out(t, m.id)}`);
        if (body.if_version === undefined || body.if_version === null) {
          throw new ORPError("invalid_parameter", "upsert of an existing record requires if_version");
        }
        if (existing.status === "retracted" && !body.allow_reingest) {
          throw new ORPError("forbidden", "record was retracted; upsert would restore it (set allow_reingest)");
        }
        if (int(body.if_version, "if_version") !== existing.version) {
          throw new ORPError("version_conflict", "version mismatch", { current: existing.version });
        }
        m.version = existing.version + 1;
        m.observed_at = existing.observed_at;
        m.meta._creator = existing.meta._creator ?? pr.id;
        for (const k of ["_supersedable_by", "_wseq"]) if (k in existing.meta) m.meta[k] = existing.meta[k];
        E.dropHashes(m.id);
        E.mem.delete(m.id);
        E.removeOrder(m.id, existing.tenant);
        E.tcount.set(existing.tenant, (E.tcount.get(existing.tenant) ?? 0) - 1);
      }
      if (mode === "supersede") {
        if (m.kind === "event" || m.kind === "document") throw new ORPError("invalid_parameter", "evidence (event/document) is never superseded");
        for (const old of strs(raw.supersedes ?? [], "supersedes")!) {
          const oi = this.in_(t, old);
          const om = E.mem.get(oi);
          if (!om || !this.canRead(om, pr, ["secret"]) || !this.canWrite(om, pr)) throw new ORPError("forbidden", `cannot supersede ${old}`);
          if (om.kind === "event" || om.kind === "document") throw new ORPError("invalid_parameter", "evidence (event/document) is never superseded");
          om.status = "superseded"; om.superseded_by = m.id;
          om.valid_to = m.valid_from ?? m.event_time ?? nowMicros();
          om.meta._explicit_superseded = true;
          om.version++;
          E.dirty(oi);
          m.supersedes.push(oi);
          this.log("supersede", oi, pr);
        }
      }
      const ids = E.remember(m, pr);
      if (existing) E.reclose(m.id);
      out.push(...ids);
      this.log("remember", ids[0]!, pr);
    });
    if (key) {
      this.keys.set(kk, out);
      this.jIdem.add(kk);
    }
    return { ids: this.outl(t, out), replayed: false, stage: "indexed" };
  }

  // ------------------------------------------------------------ recall
  async recall(body: unknown, pr: Principal): Promise<Json> {
    checkKeys(body, RECALL_KEYS, "recall");
    if (typeof body.query !== "string" || !body.query.trim()) {
      throw new ORPError("invalid_parameter", "query is required (an empty query never means 'everything')");
    }
    const rejected = ["queries", "cursor", "time"].filter((k) => k in body);
    if (rejected.length) throw new ORPError("unsupported_parameter", `not supported by this engine: ${pyList(rejected)}`, { parameters: rejected });
    const budget = body.budget ?? { tokens: DEFAULT_RECALL_BUDGET };
    checkKeys(budget, new Set(["tokens", "items"]), "budget");
    const inc = body.include ?? {};
    checkKeys(inc, new Set(["history", "drafts", "neighbours", "explain", "labels"]), "include");
    let scope = body.scope ?? {};
    if (typeof scope === "string") scope = { within: scope };
    checkKeys(scope, new Set(["within", "prefer"]), "scope");
    if (typeof (scope.within ?? "") !== "string") throw new ORPError("invalid_parameter", "scope.within must be a string");
    const prefer = strs(scope.prefer ?? [], "scope.prefer")!;
    const mode = enumv(body.mode ?? "balanced", ["fast", "balanced", "deep"], "mode");
    const gran = enumv(body.granularity ?? "auto", ["unit", "span", "thread", "auto"], "granularity");
    let kinds: string[] | null = null;
    let quotas: Record<string, number> = {};
    if (body.kinds !== null && typeof body.kinds === "object" && !Array.isArray(body.kinds)) {
      quotas = Object.fromEntries(Object.entries(body.kinds).map(([k, v]) => [k, int(v, `kinds.${k}`, 0)]));
      kinds = Object.keys(body.kinds);
    } else if (body.kinds !== undefined && body.kinds !== null) {
      kinds = strs(body.kinds, "kinds");
    }
    for (const k of kinds ?? []) {
      if (!(KINDS as readonly string[]).includes(k) && !k.startsWith("x-")) throw new ORPError("unsupported_parameter", `kind ${pyRepr(k)}`);
    }
    const tokens = int(budget.tokens ?? DEFAULT_RECALL_BUDGET, "budget.tokens", undefined, MAX_BUDGET);
    if (tokens < 50) throw new ORPError("budget_too_small", "budget.tokens must be >= 50");
    const itemsMax = int(budget.items ?? 50, "budget.items", 1, MAX_ITEMS);
    const nbh = inc.neighbours ?? 0;
    if (!(nbh === 0 || nbh === null || nbh === "auto") && (typeof nbh !== "number" || !Number.isInteger(nbh) || nbh < 0 || nbh > 10)) {
      throw new ORPError("invalid_parameter", "include.neighbours must be an integer 0..10 or 'auto'");
    }
    let minCov: number | null = null;
    if (body.min_coverage !== undefined && body.min_coverage !== null) minCov = num(body.min_coverage, "min_coverage", 0, 1);
    const labels = strs(inc.labels ?? [], "include.labels")!;
    const asOf = ts(body.as_of), knownAt = ts(body.known_at);
    return this.verb(pr, false, () => {
      const req = recallRequest({
        query: body.query, principal: pr, tenant: pr.tenant, as_of: asOf ?? nowMicros(), known_at: knownAt,
        scope: scope.within ?? "", kinds, budget_tokens: tokens, max_items: itemsMax, include_history: Boolean(inc.history),
        filters: { labels, drafts: Boolean(inc.drafts), granularity: gran, neighbours: typeof nbh === "number" ? nbh : 0, quotas, prefer },
      });
      const E = this.engine;
      const p = E.p;
      if (mode === "fast") E.p = { ...p, w_prf: 0, ranker: "rrf" }; // declared: no second pass, no learned ranker
      let pack;
      try {
        pack = E.recall(req);
      } finally {
        E.p = p;
      }
      const t = pr.tenant;
      const viewSup = new Map(E.viewSup);
      const items: Json[] = [];
      let used = 0;
      for (const it of pack.items) {
        if (used + it.tokens > tokens) continue;
        used += it.tokens;
        const m0 = E.mem.get(it.memory_ids[0]!);
        const sup: string[] = [];
        for (const i of it.memory_ids) {
          const mi = E.mem.get(i);
          const nxt = mi && Engine.inChain(mi) ? viewSup.get(i) : mi?.superseded_by ?? null;
          if (nxt && this.readableId(nxt, pr)) sup.push(this.out(t, nxt));
        }
        items.push({
          ref: `orp://${t}/mem/${this.out(t, it.memory_ids[0]!)}`, memory_ids: this.outl(t, it.memory_ids),
          kind: m0 ? m0.kind : "event", scope: m0 ? m0.scope : "", text: it.text, tokens: it.tokens, score: it.score,
          why: (inc.explain ?? true) ? it.why : {}, validity: it.valid,
          citations: it.citations.map((c) => this.out(t, c)), superseded: sup,
        });
      }
      const cov = pack.coverage;
      const abstain = minCov !== null && cov < minCov;
      const outItems = abstain ? [] : items;
      const degraded = pack.notes.filter((n) => n.startsWith("degraded:")).map((n) => n.split(":").slice(1).join(":"));
      return env({ items: outItems, tokens: abstain ? 0 : used, budget: { tokens }, coverage: cov, abstain_hint: abstain,
        snapshot: pack.snapshot, degraded, notes: pack.notes.filter((n) => !n.startsWith("degraded:")), tokenizer: this.tokenizer });
    }, [body.query]);
  }

  // ------------------------------------------------------------ profile (load: always)
  async profile(body: unknown, pr: Principal): Promise<Json> {
    checkKeys(body, new Set(["subject", "budget", "as_of"]), "profile");
    const b = body.budget;
    if (b === null || typeof b !== "object" || !b.tokens) {
      throw new ORPError("invalid_parameter", "profile requires budget.tokens (no unbounded ambient context)");
    }
    checkKeys(b, new Set(["tokens"]), "budget");
    const tokens = int(b.tokens, "budget.tokens", 1, MAX_BUDGET);
    const asOf = ts(body.as_of) ?? nowMicros();
    const subj = body.subject;
    if (subj !== undefined && subj !== null && typeof subj !== "string") throw new ORPError("invalid_parameter", "subject must be a string");
    return this.verb(pr, false, () => {
      const view = this.view(pr, asOf);
      const prio: Record<string, number> = { procedure: 0, preference: 1, profile: 2, fact: 3 };
      let cands = [...view].map((i) => this.engine.mem.get(i)!).filter((m) => m.load === "always");
      cands = cands.filter((m) => !(subj && m.claim && m.claim.subject.toLowerCase() !== subj.toLowerCase() && m.actor !== subj));
      const key = (m: Memory) => [prio[m.kind] ?? 9, -epochSeconds(m.valid_from ?? m.observed_at ?? 0)] as const;
      cands.sort((a, c) => key(a)[0] - key(c)[0] || key(a)[1] - key(c)[1] || pyCmp(a.id, c.id));
      const lines: Json[] = [];
      let used = 0;
      for (const m of cands) {
        const n = countTokens(m.content) + 2;
        if (used + n > tokens) continue;
        used += n;
        lines.push({ id: this.out(m.tenant, m.id), kind: m.kind, text: m.content });
      }
      return env({ items: lines, tokens: used, budget: { tokens } });
    });
  }

  // ------------------------------------------------------------ get / neighbours / thread / history
  async get(ids: unknown, pr: Principal, labels: string[] = []): Promise<Json> {
    const list = strs(ids, "ids")!;
    return this.verb(pr, false, () => {
      const out: Json[] = [], missing: string[] = [];
      for (const cid of list) {
        const m = this.engine.mem.get(this.in_(pr.tenant, cid));
        if (!m || !this.canRead(m, pr, labels)) { missing.push(cid); continue; } // forbidden and absent look the same
        out.push(this.record(m, pr));
      }
      return env({ memories: out, not_found: missing });
    });
  }

  async neighbours(memId: unknown, before: unknown, after: unknown, pr: Principal): Promise<Json> {
    const b = int(before, "before", 0, 50), a = int(after, "after", 0, 50);
    return this.verb(pr, false, () => {
      const iid = this.in_(pr.tenant, memId);
      const view = this.view(pr);
      const m = this.engine.mem.get(iid);
      if (!m || !view.has(iid)) throw new ORPError("not_found", String(memId));
      const same = !m.thread ? [m] : this.threadSorted(view, m.thread, m.tenant);
      const idx = same.findIndex((x) => x.id === iid);
      const sel = same.slice(Math.max(0, idx - b), idx + a + 1);
      return env({ memories: sel.map((x) => ({ id: this.out(x.tenant, x.id), actor: x.actor, content: x.content, seq: x.seq })) });
    });
  }

  private threadSorted(view: Set<string>, thread: string, tenant: string): Memory[] {
    return [...view].map((i) => this.engine.mem.get(i)!).filter((x) => x.thread === thread && x.tenant === tenant)
      .sort((x, y) => x.seq - y.seq || (x.observed_at ?? MIN_TIME) - (y.observed_at ?? MIN_TIME) || pyCmp(x.id, y.id));
  }

  /** Admissible units of one thread in order (spec §8.2 /threads/{thread}/neighbours). */
  async thread(thread: unknown, pr: Principal): Promise<Json> {
    if (typeof thread !== "string" || !thread) throw new ORPError("invalid_parameter", "thread is a non-empty string");
    return this.verb(pr, false, () => {
      const ms = this.threadSorted(this.view(pr), thread, pr.tenant);
      return env({ memories: ms.map((x) => ({ id: this.out(x.tenant, x.id), actor: x.actor, content: x.content, seq: x.seq })) });
    });
  }

  async history(subject: unknown, predicate: unknown, pr: Principal): Promise<Json> {
    if (typeof subject !== "string" || typeof predicate !== "string") {
      throw new ORPError("invalid_parameter", "subject and predicate are strings");
    }
    return this.verb(pr, false, () => {
      const t = pr.tenant;
      const chain = this.engine.history(subject, predicate, t).filter((m) => this.canRead(m, pr));
      return env({ versions: chain.map((m) => ({
        id: this.out(t, m.id), object: m.claim!.object, status: m.status, version: m.version,
        valid_from: m.valid_from !== null ? isoformat(m.valid_from) : null, valid_to: m.valid_to !== null ? isoformat(m.valid_to) : null,
        superseded_by: m.superseded_by && this.readableId(m.superseded_by, pr) ? this.out(t, m.superseded_by) : null,
      })) });
    });
  }

  // ------------------------------------------------------------ link
  async link(body: unknown, pr: Principal): Promise<Json> {
    checkKeys(body, new Set(["type", "from", "to", "valid_from", "valid_to", "sources"]), "link");
    if (!String(body.type ?? "").startsWith("x-")) enumv(body.type, LINK_TYPES, "type");
    return this.verb(pr, true, () => {
      const e: Json = { ...body };
      for (const end of ["from", "to"]) {
        const iid = this.in_(pr.tenant, body[end]);
        const m = this.engine.mem.get(iid);
        if (!m || !this.canRead(m, pr)) throw new ORPError("not_found", String(body[end]));
        e[end] = iid;
      }
      const eid = "edge_" + randomBytes(8).toString("hex");
      this.edgesMap.set(eid, { ...e, id: eid, tenant: pr.tenant, _by: pr.id });
      this.jEdges.add(eid);
      return env({ id: eid });
    });
  }

  /** Extension (x-unlink): remove an edge. Allowed for its creator, a writer of either endpoint, or an admin. */
  async unlink(edgeId: unknown, pr: Principal): Promise<Json> {
    return this.verb(pr, true, () => {
      const e = this.edgesMap.get(String(edgeId));
      if (!e || !this.edgeVisible(e, pr)) throw new ORPError("not_found", String(edgeId));
      const a = this.engine.mem.get(e.from)!, b = this.engine.mem.get(e.to)!;
      if (!(isAdmin(pr) || e._by === pr.id || this.canWrite(a, pr) || this.canWrite(b, pr))) {
        throw new ORPError("forbidden", "no write access to this edge");
      }
      this.edgesMap.delete(String(edgeId));
      if (this.store) this.store.db.prepare("DELETE FROM edges WHERE id = ?").run(String(edgeId));
      this.log("unlink", e.from, pr, { edge: String(edgeId) });
      return env({ id: String(edgeId), removed: true });
    });
  }

  /** Extension (x-list): admissible records, newest first, filtered by kind/thread/scope, with a cursor. Same
   * admissibility as recall, so it never shows what recall would hide. */
  async list(q: Json, pr: Principal): Promise<Json> {
    checkKeys(q, new Set(["kinds", "thread", "scope", "limit", "cursor", "as_of", "include_history"]), "list");
    const kinds = q.kinds ? strs(q.kinds, "kinds") : null;
    const limit = int(q.limit ?? 100, "limit", 1, 1000);
    const cursor = int(q.cursor ?? 0, "cursor", 0);
    const asOf = ts(q.as_of);
    return this.verb(pr, false, () => {
      const req = recallRequest({ query: "", principal: pr, tenant: pr.tenant, as_of: asOf ?? nowMicros(),
        scope: typeof q.scope === "string" ? q.scope : "", kinds, include_history: Boolean(q.include_history) });
      let ids = this.engine.admissible(req);
      if (typeof q.thread === "string") ids = ids.filter((i) => this.engine.mem.get(i)!.thread === q.thread);
      ids.reverse(); // newest first
      const page = ids.slice(cursor, cursor + limit);
      return env({ memories: page.map((i) => this.record(this.engine.mem.get(i)!, pr)),
        cursor: cursor + page.length < ids.length ? cursor + page.length : null });
    });
  }

  private edgeVisible(e: Json, pr: Principal): boolean {
    const a = this.engine.mem.get(e.from), b = this.engine.mem.get(e.to);
    return Boolean(e.tenant === pr.tenant && a && b && this.canRead(a, pr) && this.canRead(b, pr));
  }

  async edges(memId: unknown, pr: Principal): Promise<Json[]> {
    return this.verb(pr, false, () => {
      const iid = this.in_(pr.tenant, memId);
      const t = pr.tenant;
      return [...this.edgesMap.values()].filter((e) => (e.from === iid || e.to === iid) && this.edgeVisible(e, pr))
        .map((e) => ({ ...stripUnderscore(e), from: this.out(t, e.from), to: this.out(t, e.to) }));
    });
  }

  // ------------------------------------------------------------ forget
  async forget(body: unknown, pr: Principal): Promise<Json> {
    checkKeys(body, FORGET_KEYS, "forget");
    const mode = enumv(body.mode ?? "retract", ["retract", "redact", "purge"], "mode");
    const cascade = body.cascade ?? true;
    if (typeof cascade !== "boolean") throw new ORPError("invalid_parameter", "cascade must be a boolean");
    const list = strs(body.ids, "ids")!;
    return this.verb(pr, !body.dry_run, () => {
      const t = pr.tenant;
      const E = this.engine;
      const ids: string[] = [];
      for (const cid of list) {
        const i = this.in_(t, cid);
        const m = E.mem.get(i);
        if (!m || !this.canRead(m, pr, ["secret"])) throw new ORPError("not_found", cid);
        if (!this.canWrite(m, pr)) throw new ORPError("forbidden", `no write access to ${cid}`);
        ids.push(i);
      }
      const affected = E.cascadeSet(ids, cascade);
      // the caller sees only what it may read; cascaded records it cannot read are still forgotten (M28)
      const visible = affected.filter((i) => this.readableId(i, pr) || ids.includes(i)).map((i) => this.out(t, i)).sort(pyCmp);
      if (body.dry_run) return env({ dry_run: true, affected: visible });
      if (!body.allow_reingest) {
        for (const i of affected) {
          const m = E.mem.get(i);
          if (!m) continue;
          const k = `${m.tenant}\0${contentHash(m)}`;
          let a = this.blocked.get(k);
          if (!a) this.blocked.set(k, (a = []));
          a.push(i);
          this.jBlocked.add(k);
        }
      }
      const done: string[] = [];
      for (const i of ids) if (E.mem.has(i)) done.push(...E.forget(i, mode, cascade, pr, String(body.reason ?? "")));
      for (const i of new Set(done)) {
        if (mode === "purge") {
          this.tombstones.set(i, { id: this.out(t, i), _iid: i, tenant: t, at: isoformat(nowMicros()) });
          this.jTomb.add(i);
        }
        this.log(`forget:${mode}`, i, pr, { reason: String(body.reason ?? "") });
      }
      return env({ dry_run: false, affected: visible });
    });
  }

  // ------------------------------------------------------------ feedback / approval
  async feedback(body: unknown, pr: Principal): Promise<Json> {
    checkKeys(body, new Set(["target", "signal", "draft", "final", "note"]), "feedback");
    enumv(body.signal, ["used", "helpful", "wrong", "outdated", "draft_final"], "signal");
    return this.verb(pr, true, () => {
      const t = pr.tenant;
      const m = this.engine.mem.get(this.in_(t, body.target));
      if (!m || !this.canRead(m, pr)) throw new ORPError("not_found", String(body.target));
      const fb = { ...body, target: m.id, actor: pr.id, at: isoformat(nowMicros()), _tenant: t };
      this.feedbackLog.push(fb);
      if (this.store) this.jFeedback.push(fb);
      const out: Json = { recorded: true };
      if (body.signal === "draft_final" && body.draft && body.final) {
        const rule = ruleFromDiff(String(body.draft), String(body.final));
        if (rule) {
          const r = this.rememberSync({ memories: [{ kind: "procedure", content: rule, origin: "inferred",
            derived_from: [this.out(t, m.id)], load: "on_scope", scope: m.scope }], mode: "propose" }, pr, "propose");
          out.rule_candidate = r.ids[0];
        }
      }
      return env(out);
    });
  }

  async approve(memId: unknown, pr: Principal): Promise<Json> {
    return this.verb(pr, true, () => {
      const E = this.engine;
      const iid = this.in_(pr.tenant, memId);
      const m = E.mem.get(iid);
      if (!m || m.status !== "draft" || !this.canRead(m, pr, ["secret"])) throw new ORPError("not_found", String(memId));
      if (!(this.canWrite(m, pr) || pr.groups.has("approver"))) throw new ORPError("forbidden", "approval requires write access or the approver role");
      m.status = "active";
      m.version++;
      E.dirty(iid);
      if (m.claim && m.kind !== "event" && m.kind !== "document") E.supersede(m, pr); // an approved claim supersedes (M48)
      E.touch(m.tenant);
      this.log("approve", iid, pr);
      return env({ id: memId, status: "active", version: m.version });
    });
  }

  // ------------------------------------------------------------ reflect (deterministic consolidation)
  async reflect(body: unknown, pr: Principal): Promise<Json> {
    checkKeys(body, new Set(["scope", "threads", "kinds"]), "reflect");
    const want = body.threads ? new Set(strs(body.threads, "threads")!) : null;
    return this.verb(pr, true, () => {
      const job = "job_" + randomBytes(6).toString("hex");
      const made: string[] = [];
      const threads = new Map<string, Memory[]>();
      for (const m of this.engine.mem.values()) {
        if ((m.kind === "event" || m.kind === "document") && m.status === "active" && this.canRead(m, pr)) {
          if (want && !want.has(m.thread)) continue;
          let a = threads.get(m.thread);
          if (!a) threads.set(m.thread, (a = []));
          a.push(m);
        }
      }
      const sysp = new Principal("system", ["*"], pr.tenant);
      for (const th of [...threads.keys()].sort(pyCmp)) {
        const ms = threads.get(th)!.sort((a, b) => a.seq - b.seq || pyCmp(a.id, b.id));
        const key = "reflect:" + createHash("sha1").update(th + "|" + ms.map((m) => m.id).join(",")).digest("hex").slice(0, 16);
        if (this.keys.has(`${pr.tenant}\0system\0${key}`)) continue;
        let acl: Set<string> | null = null;
        for (const m of ms) {
          const rd = m.readers;
          if (rd !== null) acl = acl === null ? new Set<string>(rd) : new Set<string>([...acl].filter((x: string) => rd.has(x)));
        }
        const prev = [...this.engine.mem.values()].filter((x) => x.kind === "summary" && x.thread === th && x.status === "active" &&
          x.tenant === pr.tenant && sameReaders(x.readers, acl)).map((x) => x.id);
        const last = ms[ms.length - 1]!;
        const r = this.rememberSync({ memories: [{ kind: "summary", content: extractiveSummary(ms), origin: "extracted",
          derived_from: ms.map((m) => m.id), thread: th, supersedes: prev, sources: [{ uri: `orp://${pr.tenant}/thread/${th}` }],
          event_time: isoformat((last.event_time ?? last.observed_at)!) }], mode: prev.length ? "supersede" : "append",
          idempotency_key: key }, sysp, prev.length ? "supersede" : "append");
        made.push(...r.ids);
      }
      this.jobs.set(job, { status: "completed", created: made, tenant: pr.tenant });
      this.jJobs.add(job);
      return env({ job, status: "completed", created: made });
    });
  }

  async job(jobId: unknown, pr: Principal): Promise<Json> {
    return this.verb(pr, false, () => {
      const j = this.jobs.get(String(jobId));
      if (!j || j.tenant !== pr.tenant) throw new ORPError("not_found", String(jobId));
      return env({ ...j });
    });
  }

  // ------------------------------------------------------------ changes / audit
  async changes(cursor: unknown, pr: Principal, limit: unknown = 500): Promise<Json> {
    const c = int(cursor, "cursor", 0), l = int(limit, "limit", 1, 100_000);
    return this.verb(pr, false, () => {
      const t = pr.tenant;
      const total = this.changeCount.get(t) ?? 0;
      const rows: Json[] = this.store ? this.store.changes(t, c, l)
        : (this.changeFeed.get(t) ?? []).slice(c, c + l);
      const auditor = pr.groups.has("auditor");
      const out: Json[] = [];
      for (const row of rows) {
        const m = this.engine.mem.get(row._iid);
        if (!auditor && (!m || !this.canRead(m, pr))) continue;
        out.push(stripUnderscore(row));
      }
      return env({ changes: out, cursor: Math.min(total, c + l) });
    });
  }

  async auditLog(pr: Principal): Promise<Json> {
    if (!pr.groups.has("auditor")) throw new ORPError("forbidden", "audit requires the auditor role");
    return this.verb(pr, false, () => {
      const t = pr.tenant;
      if (this.store) return env({ entries: this.store.audit(t).map((a) => ({ ...a, id: this.out(t, a.id) })) });
      const ids = new Set<string>();
      for (const [i, m] of this.engine.mem) if (m.tenant === t) ids.add(i);
      for (const x of this.tombstones.values()) if (x.tenant === t) ids.add(x._iid);
      return env({ entries: this.engine.audit.filter((a) => ids.has(a.id)).map((a) => ({ ...a, id: this.out(t, a.id) })) });
    });
  }

  // ------------------------------------------------------------ export / import (§8.3)
  async export(pr: Principal): Promise<Json> {
    return this.verb(pr, false, () => {
      const t = pr.tenant;
      const mems = [...this.engine.mem.values()].filter((m) => m.tenant === t && this.canRead(m, pr, ["secret"]));
      mems.sort((a, b) => pyCmp(this.out(t, a.id), this.out(t, b.id)));
      const lines = mems.map((m) => pyDumps(this.record(m, pr, true), { sortKeys: true, floatKeys: FLOATS }));
      const edges = [...this.edgesMap.values()].filter((e) => this.edgeVisible(e, pr))
        .map((e) => pyDumps({ ...stripUnderscore(e), from: this.out(t, e.from), to: this.out(t, e.to) }, { sortKeys: true }));
      const manifest = { spec: SPEC_VERSION, tenant: t, count: lines.length,
        sha256: createHash("sha256").update(lines.join("\n")).digest("hex"),
        tombstones: [...this.tombstones.values()].filter((x) => x.tenant === t).map(stripUnderscore) };
      return env({ manifest, "memories.jsonl": lines.join("\n"), "edges.jsonl": edges.join("\n") });
    });
  }

  /** Lossless import (M62), tenant administrators only (M71). Every record and edge must belong to the importing
   * tenant; closure, labels, de-duplication and claim chains are re-applied; engine ids that collide with another
   * tenant's are remapped (id_map). */
  async import(bundle: unknown, pr: Principal): Promise<Json> {
    if (!isAdmin(pr)) throw new ORPError("forbidden", "import requires the tenant admin role");
    const t = pr.tenant;
    if (bundle === null || typeof bundle !== "object" || typeof ((bundle as Json)["memories.jsonl"] ?? "") !== "string") {
      throw new ORPError("invalid_parameter", "bundle needs memories.jsonl");
    }
    const b = bundle as Json;
    const man = (b.manifest ?? {}) as Json;
    if ((man.tenant ?? t) !== t) throw new ORPError("forbidden", "bundle tenant mismatch");
    let rows: Json[];
    try {
      rows = String(b["memories.jsonl"]).split("\n").filter((l) => l.trim()).map((l) => JSON.parse(l));
    } catch (e) {
      throw new ORPError("invalid_parameter", `bad bundle line: ${(e as Error).message}`);
    }
    for (const d of rows) if ((d.tenant ?? t) !== t) throw new ORPError("forbidden", "bundle tenant mismatch");
    const edgeRows: Json[] = String(b["edges.jsonl"] ?? "").split("\n").filter((l) => l.trim()).map((l) => JSON.parse(l));
    for (const e of edgeRows) if ((e.tenant ?? t) !== t) throw new ORPError("forbidden", "edge tenant mismatch");
    return this.verb(pr, true, () => {
      const E = this.engine;
      // a bundle can name engine ids of tenants not loaded yet; check collisions against the store too
      const idMap = new Map<string, string>();
      const conv = (x: string): string => {
        const hit = idMap.get(x);
        if (hit) return hit;
        if (ENGINE_ID.test(x)) {
          const o = E.mem.get(x);
          if (o === undefined && !this.storeHasOtherTenant(x, t)) return x;
          if (o !== undefined && o.tenant === t) return x;
          const n = newId();
          idMap.set(x, n);
          return n;
        }
        return `${x}~${t}`;
      };
      let n = 0;
      const keys = new Set<string>();
      const mems: Memory[] = [];
      for (const d0 of rows) {
        const d = { ...d0 };
        delete d.written_by;
        d.id = conv(d.id);
        d.supersedes = (d.supersedes ?? []).map(conv);
        d.superseded_by = d.superseded_by ? conv(d.superseded_by) : null;
        d.derived_from = (d.derived_from ?? []).map(conv);
        const meta = { ...(d.meta ?? {}) };
        if ("_supersedable_by" in meta) meta._supersedable_by = meta._supersedable_by.map(conv);
        d.meta = meta;
        try {
          mems.push(memoryFromJson(d));
        } catch (e) {
          throw new ORPError("invalid_parameter", `bad record: ${(e as Error).message}`);
        }
      }
      for (const m of mems) {
        if (m.tenant !== t) throw new ORPError("forbidden", "bundle tenant mismatch");
        if (E.mem.has(m.id)) continue;
        const got = E.remember(m, new Principal((m.meta._creator as string) ?? "system", [], t), true);
        if (got.length && got[0] === m.id) {
          n++;
          if (m.claim) keys.add(t + "\0" + claimKey(m.claim));
        }
      }
      for (const k of keys) E.resolveChain(k);
      for (const e0 of edgeRows) {
        const e: Json = { ...e0, from: conv(e0.from), to: conv(e0.to) };
        const a = E.mem.get(e.from), bb = E.mem.get(e.to);
        if (!(a && bb && a.tenant === t && bb.tenant === t)) throw new ORPError("invalid_parameter", "edge endpoints must exist in this tenant");
        let eid = e.id || "edge_" + randomBytes(8).toString("hex");
        if (this.edgesMap.has(eid) && this.edgesMap.get(eid)!.tenant !== t) eid = "edge_" + randomBytes(8).toString("hex");
        this.edgesMap.set(eid, { ...e, id: eid, tenant: t });
        this.jEdges.add(eid);
      }
      for (const x of (man.tombstones ?? []) as Json[]) {
        const iid = conv(x.id);
        this.tombstones.set(iid, { ...x, _iid: iid, tenant: t });
        this.jTomb.add(iid);
      }
      E.touch(t);
      return env({ imported: n, id_map: Object.fromEntries([...idMap].map(([k, v]) => [k, this.out(t, v)])) });
    });
  }

  private storeHasOtherTenant(iid: string, t: string): boolean {
    if (!this.store) return false;
    const r = this.store.db.prepare("SELECT tenant FROM memories WHERE iid = ?").get(iid) as Json | undefined;
    return Boolean(r && r.tenant !== t);
  }

  /** A full snapshot of the service state into a new store file (backups, and moving an in-memory service to disk).
   * With a store attached, SQLite's online backup copies the file consistently. */
  async saveTo(path: string): Promise<void> {
    return this.lock.run(async () => {
      if (this.store) {
        const { backup } = (await import("node:sqlite")) as any;
        await backup(this.store.db, path);
        return;
      }
      const E = this.engine;
      const out = new SqliteStore(path);
      const d = emptyDelta();
      d.meta = { secret: E.secret.toString("hex"), epoch: String(E.epoch), wseq: String(E.wseq), mutations: String(E.mutations) };
      for (const [i, m] of E.mem) d.memories.push({ iid: i, tenant: m.tenant, ord: E.ord.get(i) ?? 0, record: pyDumps(memoryToJson(m), { sortKeys: true, floatKeys: FLOATS }) });
      for (const [k, iids] of E.claims) { const [tenant, key] = k.split("\0") as [string, string]; d.claims.push({ tenant, key, iids: [...iids] }); }
      for (const [h, iid] of E.hash) d.dedupAdd.push({ hkey: h, tenant: E.mem.get(iid)?.tenant ?? JSON.parse(h)[1], iid });
      for (const [k, ids] of this.keys) { const [tenant, principal, key] = k.split("\0") as [string, string, string]; d.idem.push({ tenant, principal, key, ids: pyDumps(ids) }); }
      for (const [k, iids] of this.blocked) { const [tenant, chash] = k.split("\0") as [string, string]; d.blocked.push({ tenant, chash, iids: [...iids] }); }
      for (const [id, e] of this.edgesMap) d.edges.push({ id, tenant: e.tenant, body: pyDumps(e, { sortKeys: true }) });
      for (const [i, x] of this.tombstones) d.tombstones.push({ iid: i, tenant: x.tenant, body: pyDumps(x, { sortKeys: true }) });
      for (const feed of this.changeFeed.values()) for (const c of feed) d.changes.push({ tenant: c.tenant, cursor: c.cursor, body: pyDumps(c, { sortKeys: true }) });
      E.audit.forEach((a, n) => d.audit.push({ tenant: E.auditTenant[n] ?? "", body: pyDumps(a, { sortKeys: true }) }));
      for (const [id, j] of this.jobs) d.jobs.push({ id, tenant: j.tenant, body: pyDumps(j, { sortKeys: true }) });
      for (const f of this.feedbackLog) d.feedback.push({ tenant: f._tenant ?? "", body: pyDumps(stripUnderscore(f), { sortKeys: true }) });
      const ts = new Set([...E.tmut.keys(), ...E.tcount.keys()]);
      for (const t of ts) d.counters.push({ tenant: t, tmut: E.tmut.get(t) ?? 0, tcount: E.tcount.get(t) ?? 0 });
      out.flush(d);
      out.close();
    });
  }

  close() {
    this.store?.close();
  }
}

const FLOATS = new Set(["confidence"]);

function src(uri: string): Source {
  return { uri, span: "", hash: "", observed_at: "" };
}

function stripUnderscore(o: Json): Json {
  return Object.fromEntries(Object.entries(o).filter(([k]) => !k.startsWith("_")));
}

function sameReaders(a: ReadonlySet<string> | null, b: ReadonlySet<string> | null): boolean {
  if (a === null || b === null) return a === b;
  if (a.size !== b.size) return false;
  for (const x of a) if (!b.has(x)) return false;
  return true;
}

// ---------------------------------------------------------------- deterministic helpers
export function extractiveSummary(ms: Memory[], k = 5): string {
  const sents: Array<[number, string]> = [];
  for (const m of ms) {
    for (const s of sentences(m.content)) {
      if (s.split(/\s+/).filter(Boolean).length >= 5) sents.push([m.seq, m.actor ? `${m.actor}: ${s}` : s]);
    }
  }
  if (!sents.length) return [...ms.map((m) => m.content).join(" ")].slice(0, 400).join("");
  const df = new Map<string, number>(), cent = new Map<string, number>();
  for (const [, s] of sents) {
    for (const t of new Set(terms(s))) df.set(t, (df.get(t) ?? 0) + 1);
    for (const t of terms(s)) cent.set(t, (cent.get(t) ?? 0) + 1);
  }
  const scored = sents.map(([, s], i) => {
    const tset = new Set(terms(s));
    let sc = 0;
    for (const t of tset) sc += cent.get(t)! / (1 + df.get(t)!);
    return [sc / (1 + Math.sqrt(tset.size)), i] as [number, number];
  });
  scored.sort((a, b) => b[0] - a[0] || b[1] - a[1]);
  const keep = scored.slice(0, k).map((x) => x[1]).sort((a, b) => a - b);
  return keep.map((i) => sents[i]![1]).join(" ");
}

export function ruleFromDiff(draft: string, final: string): string | null {
  const wordsOf = (s: string) => new Set(s.toLowerCase().match(/[\p{L}\p{N}_]+/gu) ?? []);
  const d = wordsOf(draft), f = wordsOf(final);
  const removed = [...d].filter((w) => !f.has(w)).sort(pyCmp).slice(0, 8);
  const added = [...f].filter((w) => !d.has(w)).sort(pyCmp).slice(0, 8);
  if (!removed.length && !added.length) return null;
  const parts: string[] = [];
  if (removed.length) parts.push("avoid: " + removed.join(", "));
  if (added.length) parts.push("prefer: " + added.join(", "));
  const ratio = [...final].length / Math.max(1, [...draft].length);
  if (ratio < 0.8) parts.push(`keep it about ${Math.trunc(ratio * 100)}% of the draft length`);
  return "When drafting similar content, " + parts.join("; ") + ".";
}
