// A LangGraph JS BaseStore-compatible store backed by ORP. Structural types only: nothing imports LangGraph.
//   namespace ["users", "u1", "prefs"]  ->  scope "<prefix>/users/u1/prefs"
//   key                                 ->  client id (with the namespace and owner, so keys never collide)
//   value                               ->  meta.kv.value (round-trips exactly); content is a text rendering for recall
//   search(query)                       ->  recall within the namespace scope
// The BaseStore interface has no place for as_of, budgets or principals: as_of and budget come from this store's
// config, the principal from the client's credentials, and the store reports what it cannot carry as degraded[].
import { OrpError, type OrpClient, type Memory, type MemoryInput, type Time } from "@orp/client";

export interface Item {
  value: Record<string, any>;
  key: string;
  namespace: string[];
  createdAt: Date;
  updatedAt: Date;
}
export interface SearchItem extends Item { score?: number }

export interface GetOperation { namespace: string[]; key: string }
export interface PutOperation { namespace: string[]; key: string; value: Record<string, any> | null; index?: false | string[] }
export interface SearchOperation { namespacePrefix: string[]; filter?: Record<string, any>; limit?: number; offset?: number; query?: string }
export interface MatchCondition { matchType: "prefix" | "suffix"; path: string[] }
export interface ListNamespacesOperation { matchConditions?: MatchCondition[]; maxDepth?: number; limit?: number; offset?: number }
export type Operation = GetOperation | PutOperation | SearchOperation | ListNamespacesOperation;
export type OperationResult = Item | null | SearchItem[] | string[][] | void;

export interface OrpStoreConfig {
  /** Root scope for every namespace. Default "langgraph". */
  prefix?: string;
  /** Who can read what the graph writes: "self" (default, the client's principal) or "tenant". */
  audience?: "self" | "tenant";
  /** Pinned as_of for searches with a query (the interface has no per-call clock). Default: now, per call. */
  asOf?: Time;
  /** Recall budget for searches with a query, in tokens. Default 4000. */
  budgetTokens?: number;
  /** Default fields to render into the searchable text (dot paths). Default: the whole value as JSON. */
  index?: string[];
}

const KIND = "x-kv" as const;
const KIND_NOINDEX = "x-kv-noindex" as const;
const enc = (s: string) => encodeURIComponent(s).replace(/~/g, "%7E");

interface KvMeta { namespace: string[]; key: string; value: Record<string, any>; updated_at: string }

export class OrpStore {
  /** What this adapter can and cannot carry, for doctor and for humans. */
  static readonly capabilities = {
    level: "L2",
    carries: ["namespace -> scope", "key -> client id", "value -> meta (lossless)", "search query -> recall", "principal from client credentials"],
    loses: ["per-call as_of (config only)", "per-call budget (config only)", "principals and readers per call (audience from config)",
      "filter is applied after recall's top-k cut", "as_of on searches without a query (list has no clock over HTTP)"],
  } as const;

  private readonly prefix: string;
  private reported = new Set<string>();

  constructor(readonly client: OrpClient, readonly config: OrpStoreConfig = {}) {
    this.prefix = (config.prefix ?? "langgraph").replace(/^\/+|\/+$/g, "");
    if (!this.prefix) throw new OrpError("invalid_parameter", "prefix must not be empty");
  }

  start(): void {}
  stop(): void {}

  private degrade(...values: string[]) {
    const fresh = values.filter((v) => !this.reported.has(v));
    for (const v of fresh) this.reported.add(v);
    if (fresh.length) this.client.reportDegraded(fresh, "langgraph");
  }

  private scopeOf(ns: string[]): string {
    return [this.prefix, ...ns.map(enc)].join("/");
  }

  private checkNs(ns: unknown, allowEmpty: boolean): string[] {
    if (!Array.isArray(ns) || !ns.every((s) => typeof s === "string" && s.length > 0) || (!allowEmpty && ns.length === 0)) {
      throw new OrpError("invalid_parameter", "namespace must be an array of non-empty strings");
    }
    return ns as string[];
  }

  private async idOf(ns: string[], key: string): Promise<string> {
    const owner = this.config.audience === "tenant" ? "_tenant" : (await this.client.me()).id;
    return `kv:${[owner, this.prefix, ...ns, key].map(enc).join(":")}`; // ":" is always percent-encoded inside a part
  }

  private static kv(m: Memory): KvMeta | null {
    const kv = (m.meta as Record<string, any> | undefined)?.kv;
    return kv && typeof kv === "object" && Array.isArray(kv.namespace) && typeof kv.key === "string" ? (kv as KvMeta) : null;
  }

  private static item(m: Memory, kv: KvMeta): Item {
    const t = (s: string | null | undefined) => (s ? new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + "Z") : new Date(0));
    return { value: kv.value, key: kv.key, namespace: [...kv.namespace], createdAt: t(m.observed_at), updatedAt: new Date(kv.updated_at) };
  }

  private async record(ns: string[], key: string): Promise<Memory | null> {
    try {
      const m = await this.client.advanced.get(await this.idOf(ns, key));
      return m.status === "active" && OrpStore.kv(m) ? m : null;
    } catch (e) {
      if (e instanceof OrpError && e.code === "not_found") return null;
      throw e;
    }
  }

  // ------------------------------------------------------------ BaseStore
  async get(namespace: string[], key: string): Promise<Item | null> {
    const m = await this.record(this.checkNs(namespace, false), key);
    return m ? OrpStore.item(m, OrpStore.kv(m)!) : null;
  }

  async put(namespace: string[], key: string, value: Record<string, any> | null, index?: false | string[]): Promise<void> {
    const ns = this.checkNs(namespace, false);
    if (typeof key !== "string" || !key) throw new OrpError("invalid_parameter", "key must be a non-empty string");
    if (value === null) return this.delete(ns, key);
    if (typeof value !== "object" || Array.isArray(value)) throw new OrpError("invalid_parameter", "value must be an object");
    const id = await this.idOf(ns, key);
    const fields = index === false ? null : index ?? this.config.index;
    const input: MemoryInput = {
      id, kind: index === false ? KIND_NOINDEX : KIND, content: `${key}: ${renderValue(value, fields ?? undefined)}`,
      scope: this.scopeOf(ns), thread: this.scopeOf(ns),
      meta: { kv: { namespace: ns, key, value, updated_at: new Date().toISOString() } satisfies KvMeta },
      ...(this.config.audience === "tenant" ? {} : { readers: [`principal:${(await this.client.me()).id}`] }),
    };
    const audience = this.config.audience === "tenant" ? { audience: "tenant" as const } : {};
    for (let attempt = 0; ; attempt++) {
      let existing: Memory | null = null;
      try {
        existing = await this.client.advanced.get(id);
      } catch (e) {
        if (!(e instanceof OrpError && e.code === "not_found")) throw e;
      }
      try {
        if (existing) await this.client.remember(input, { mode: "upsert", ifVersion: existing.version, allowReingest: true, ...audience });
        else await this.client.remember(input, { allowReingest: true, ...audience });
        return;
      } catch (e) {
        // a concurrent put won the race: read again and retry against the new version
        if (e instanceof OrpError && e.code === "version_conflict" && attempt < 3) continue;
        throw e;
      }
    }
  }

  async delete(namespace: string[], key: string): Promise<void> {
    const id = await this.idOf(this.checkNs(namespace, false), key);
    try {
      await this.client.forget(id, { mode: "purge", allowReingest: true, reason: "langgraph store delete" });
    } catch (e) {
      if (e instanceof OrpError && e.code === "not_found") return;
      throw e;
    }
  }

  async search(namespacePrefix: string[], options: { filter?: Record<string, any>; limit?: number; offset?: number; query?: string } = {}): Promise<SearchItem[]> {
    const ns = this.checkNs(namespacePrefix, true);
    const limit = options.limit ?? 10, offset = options.offset ?? 0;
    const scope = this.scopeOf(ns);
    const keep = (kv: KvMeta) => !options.filter || matches(kv.value, options.filter);
    if (options.query && options.query.trim()) {
      this.degrade("langgraph:as_of-from-config", "langgraph:budget-from-config");
      if (options.filter) this.degrade("langgraph:filter-after-recall");
      const want = Math.min(200, (offset + limit) * (options.filter ? 3 : 1));
      const pack = await this.client.recall(options.query, { scope, kinds: [KIND], budgetTokens: this.config.budgetTokens ?? 4000,
        maxItems: want, granularity: "unit", ...(this.config.asOf !== undefined ? { asOf: this.config.asOf } : {}) });
      const order: string[] = [], score = new Map<string, number>();
      for (const it of pack.items) {
        const id = it.memory_ids[0];
        if (id && !score.has(id)) { order.push(id); score.set(id, it.score); }
      }
      const { memories } = await this.client.advanced.getMany(order);
      const out: SearchItem[] = [];
      for (const m of memories) {
        const kv = OrpStore.kv(m);
        if (kv && m.status === "active" && keep(kv)) out.push({ ...OrpStore.item(m, kv), score: score.get(m.id) });
      }
      return out.slice(offset, offset + limit);
    }
    if (this.config.asOf !== undefined) this.degrade("langgraph:no-as_of-on-list");
    const out: SearchItem[] = [];
    let skipped = 0;
    for await (const m of this.client.advanced.all({ scope, kinds: [KIND, KIND_NOINDEX], limit: 200 })) {
      const kv = OrpStore.kv(m);
      if (!kv || !keep(kv)) continue;
      if (skipped < offset) { skipped++; continue; }
      out.push(OrpStore.item(m, kv));
      if (out.length >= limit) break;
    }
    return out;
  }

  async listNamespaces(options: { prefix?: string[]; suffix?: string[]; maxDepth?: number; limit?: number; offset?: number } = {}): Promise<string[][]> {
    const conds: MatchCondition[] = [];
    if (options.prefix) conds.push({ matchType: "prefix", path: options.prefix });
    if (options.suffix) conds.push({ matchType: "suffix", path: options.suffix });
    return this.namespaces({ matchConditions: conds, maxDepth: options.maxDepth, limit: options.limit ?? 100, offset: options.offset ?? 0 });
  }

  private async namespaces(op: ListNamespacesOperation): Promise<string[][]> {
    const seen = new Map<string, string[]>();
    const pre = op.matchConditions?.find((c) => c.matchType === "prefix")?.path ?? [];
    const literal = pre.every((s) => s !== "*") ? pre : [];
    for await (const m of this.client.advanced.all({ scope: this.scopeOf(literal), kinds: [KIND, KIND_NOINDEX], limit: 200 })) {
      const kv = OrpStore.kv(m);
      if (!kv) continue;
      if (!(op.matchConditions ?? []).every((c) => matchNs(kv.namespace, c))) continue;
      const ns = op.maxDepth !== undefined ? kv.namespace.slice(0, op.maxDepth) : kv.namespace;
      seen.set(JSON.stringify(ns), ns);
    }
    const all = [...seen.values()].sort((a, b) => a.join("\u0000").localeCompare(b.join("\u0000")));
    const offset = op.offset ?? 0;
    return all.slice(offset, offset + (op.limit ?? 100));
  }

  async batch(operations: Operation[]): Promise<OperationResult[]> {
    const out: OperationResult[] = [];
    for (const op of operations) {
      if ("namespacePrefix" in op) out.push(await this.search(op.namespacePrefix, op));
      else if ("key" in op && "value" in op) out.push(await this.put(op.namespace, op.key, op.value, op.index));
      else if ("key" in op) out.push(await this.get(op.namespace, op.key));
      else out.push(await this.namespaces(op));
    }
    return out;
  }
}

function matchNs(ns: string[], c: MatchCondition): boolean {
  if (c.path.length > ns.length) return false;
  const part = c.matchType === "prefix" ? ns.slice(0, c.path.length) : ns.slice(ns.length - c.path.length);
  return c.path.every((p, i) => p === "*" || p === part[i]);
}

function getPath(v: any, path: string): unknown {
  return path.split(".").reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), v);
}

function renderValue(value: Record<string, any>, fields?: string[]): string {
  if (!fields?.length) return JSON.stringify(value);
  return fields.map((f) => getPath(value, f)).filter((x) => x !== undefined && x !== null)
    .map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join("\n") || JSON.stringify(value);
}

/** LangGraph filter semantics: equality, or an operator object ($eq, $ne, $gt, $gte, $lt, $lte). */
function matches(value: Record<string, any>, filter: Record<string, any>): boolean {
  return Object.entries(filter).every(([k, want]) => {
    const got = getPath(value, k) as any;
    if (want && typeof want === "object" && !Array.isArray(want) && Object.keys(want).some((x) => x.startsWith("$"))) {
      return Object.entries(want).every(([op, w]: [string, any]) => {
        switch (op) {
          case "$eq": return deepEq(got, w);
          case "$ne": return !deepEq(got, w);
          case "$gt": return got > w;
          case "$gte": return got >= w;
          case "$lt": return got < w;
          case "$lte": return got <= w;
          default: throw new OrpError("unsupported_parameter", `filter operator ${op}`);
        }
      });
    }
    return deepEq(got, want);
  });
}

function deepEq(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}
