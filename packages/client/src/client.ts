// The ORP HTTP client. Identity is bound at construction (a token, or a getToken callback) and is never a call
// argument: there is no principal, tenant or actor parameter on any verb (M56). Writes fail closed: a memory is
// readable by its writer alone unless the caller widens it explicitly.
import { OrpError } from "./errors.js";
import type {
  Bundle, Capabilities, Change, ChangesOptions, ChangesResponse, ContextPack, Edge, FeedbackRequest, ForgetOptions,
  ForgetResponse, HistoryResponse, JobResponse, LinkRequest, ListOptions, ListPage, Memory, MemoryInput,
  ProfileOptions, ProfilePack, RecallOptions, ReflectRequest, RememberOptions, RememberResponse, Time, UnitsResponse,
  Whoami, Envelope,
} from "./types.js";

type Json = Record<string, any>;
type FetchLike = (input: string, init: RequestInit) => Promise<Response>;

export interface DegradedInfo {
  /** The verb or adapter that reported it, e.g. "recall" or "langgraph". */
  source: string;
}

export interface ClientOptions {
  /** Gateway base URL, e.g. http://127.0.0.1:7477 */
  url: string;
  /** A bearer token (API key or OIDC access token). Use getToken instead when it rotates. */
  token?: string;
  /** Called before every request; return the current bearer token. */
  getToken?: () => string | Promise<string>;
  /** The tenant in the URL path. Leave it out and the client asks the gateway (whoami) once and caches the answer. */
  tenant?: string;
  /** Inject a fetch implementation (tests, proxies, edge runtimes). Default: globalThis.fetch. */
  fetch?: FetchLike;
  /** Called with degraded[] whenever a response carries any. Default: console.warn once per distinct value,
   * naming the value only, never memory content. */
  onDegraded?: (values: string[], info: DegradedInfo) => void;
  /** Extra attempts after a 429, a 503 or a network error. Only reads and writes with an idempotency key are
   * retried. Default 2. */
  retries?: number;
  /** Base delay for jittered exponential backoff, in ms. Default 200. */
  retryDelayMs?: number;
  /** Upper bound for one backoff delay, in ms. Default 5000. */
  maxRetryDelayMs?: number;
  /** Per-request timeout in ms. Default 30000. */
  timeoutMs?: number;
}

interface Shared {
  url: string;
  fetch: FetchLike;
  onDegraded: (values: string[], info: DegradedInfo) => void;
  retries: number;
  retryDelayMs: number;
  maxRetryDelayMs: number;
  timeoutMs: number;
}

interface RequestOptions {
  body?: unknown;
  query?: Record<string, string | number | undefined>;
  /** Safe to send again: a read, or a write carrying an idempotency key. */
  retry: boolean;
  verb: string;
  /** Override the tenant in the path (used for discovery). */
  tenant?: string;
}

/** The everyday verbs. OrpClient and RunClient both implement it, so adapters accept either. */
export interface MemoryApi {
  recall(query: string, opts?: RecallOptions): Promise<ContextPack>;
  remember(input: string | MemoryInput | Array<string | MemoryInput>, opts?: RememberOptions): Promise<RememberResponse>;
  forget(ids: string | string[], opts?: ForgetOptions): Promise<ForgetResponse>;
  profile(opts: ProfileOptions): Promise<ProfilePack>;
  /** Report degraded[] values that did not come from a response (for example an adapter that cannot carry as_of). */
  reportDegraded(values: string[], source: string): void;
}

const DEFAULT_RECALL_BUDGET = 2000;
const sleep = (ms: number, signal?: AbortSignal) => new Promise<void>((ok, fail) => {
  if (signal?.aborted) return fail(signal.reason);
  const t = setTimeout(() => { signal?.removeEventListener("abort", onAbort); ok(); }, ms);
  const onAbort = () => { clearTimeout(t); fail(signal!.reason); };
  signal?.addEventListener("abort", onAbort, { once: true });
});
const iso = (t: Time): string => (t instanceof Date ? t.toISOString() : t);

function defaultOnDegraded(): (values: string[], info: DegradedInfo) => void {
  const seen = new Set<string>();
  return (values, info) => {
    for (const v of values) {
      if (seen.has(v)) continue;
      seen.add(v);
      console.warn(`[orp] degraded (${info.source}): ${v}`);
    }
  };
}

export class OrpClient implements MemoryApi {
  /** Verbs most applications never need directly. */
  readonly advanced: AdvancedApi;
  private readonly shared: Shared;
  private readonly token?: string;
  private readonly getTokenFn?: () => string | Promise<string>;
  private tenantP: Promise<string> | null;
  private meP: Promise<Whoami> | null = null;
  private readonly tenantGuess: string | Promise<string>;

  constructor(opts: ClientOptions);
  /** @internal derived clients (forUser) share transport settings and the degraded de-duplication. */
  constructor(opts: ClientOptions, shared: Shared, tenantGuess: string | Promise<string>);
  constructor(opts: ClientOptions, shared?: Shared, tenantGuess?: string | Promise<string>) {
    if (!opts || typeof opts.url !== "string" || !opts.url) throw new OrpError("invalid_parameter", "url is required");
    if (opts.token === undefined && opts.getToken === undefined) throw new OrpError("invalid_parameter", "token or getToken is required");
    if (opts.token !== undefined && opts.getToken !== undefined) throw new OrpError("invalid_parameter", "pass token or getToken, not both");
    const f = opts.fetch ?? (globalThis.fetch as FetchLike | undefined);
    if (!f) throw new OrpError("invalid_parameter", "no fetch implementation available");
    this.shared = shared ?? {
      url: opts.url.replace(/\/+$/, ""), fetch: f, onDegraded: opts.onDegraded ?? defaultOnDegraded(),
      retries: Math.max(0, Math.trunc(opts.retries ?? 2)), retryDelayMs: opts.retryDelayMs ?? 200,
      maxRetryDelayMs: opts.maxRetryDelayMs ?? 5000, timeoutMs: opts.timeoutMs ?? 30_000,
    };
    this.token = opts.token;
    this.getTokenFn = opts.getToken;
    this.tenantP = opts.tenant ? Promise.resolve(opts.tenant) : null;
    this.tenantGuess = tenantGuess ?? "default";
    this.advanced = new AdvancedApi(this);
  }

  // ------------------------------------------------------------ transport
  private async bearer(): Promise<string> {
    const t = this.token ?? (await this.getTokenFn!());
    if (typeof t !== "string" || !t) throw new OrpError("unauthorized", "getToken returned no token");
    return t;
  }

  /** The tenant this client's token belongs to. */
  tenant(): Promise<string> {
    if (!this.tenantP) {
      this.tenantP = this.discoverTenant();
      this.tenantP.catch(() => { this.tenantP = null; }); // a failed discovery is retried on the next call
    }
    return this.tenantP;
  }

  private async discoverTenant(): Promise<string> {
    // The gateway checks the path tenant before answering whoami, so discovery starts from a guess: the parent
    // client's tenant for forUser clients, otherwise "default".
    try {
      const me = await this.request<Whoami>("GET", "whoami", { retry: true, verb: "whoami", tenant: await this.tenantGuess });
      this.meP = Promise.resolve(me);
      return me.tenant;
    } catch (e) {
      if (!(e instanceof OrpError && e.code === "forbidden")) throw e;
      const viaMcp = await this.tenantFromMcp().catch(() => null);
      if (viaMcp) return viaMcp;
      throw new OrpError("forbidden", "could not discover the tenant for this token; pass `tenant` to the client", e.status, {}, e.requestId);
    }
  }

  /** Fallback discovery: the MCP endpoint has no tenant in its path, and its capabilities resource URI names the
   * token's tenant. Opens a session, lists resources, and closes the session again. */
  private async tenantFromMcp(): Promise<string | null> {
    const S = this.shared;
    const auth = { authorization: `Bearer ${await this.bearer()}` };
    const post = (body: Json, sid?: string) => S.fetch(`${S.url}/mcp`, { method: "POST", signal: AbortSignal.timeout(S.timeoutMs),
      headers: { ...auth, "content-type": "application/json", accept: "application/json, text/event-stream", ...(sid ? { "mcp-session-id": sid } : {}) },
      body: JSON.stringify(body) });
    const init = await post({ jsonrpc: "2.0", id: 1, method: "initialize",
      params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "orp-client", version: "0.2.0" } } });
    const sid = init.headers.get("mcp-session-id");
    await init.text();
    if (!init.ok || !sid) return null;
    try {
      const res = await post({ jsonrpc: "2.0", id: 2, method: "resources/list" }, sid);
      const body = (await res.json()) as Json;
      for (const r of (body?.result?.resources ?? []) as Json[]) {
        const m = /^orp:\/\/([^/]+)\/capabilities$/.exec(String(r.uri ?? ""));
        if (m) return decodeURIComponent(m[1]!);
      }
      return null;
    } finally {
      await S.fetch(`${S.url}/mcp`, { method: "DELETE", headers: { ...auth, "mcp-session-id": sid } }).then((r) => r.text(), () => undefined);
    }
  }

  /** Who this client's credentials resolve to (cached). */
  me(): Promise<Whoami> {
    if (!this.meP) {
      this.meP = this.request<Whoami>("GET", "whoami", { retry: true, verb: "whoami" });
      this.meP.catch(() => { this.meP = null; });
    }
    return this.meP;
  }

  reportDegraded(values: string[], source: string): void {
    if (values.length) this.shared.onDegraded([...values], { source });
  }

  /** @internal */
  async request<T>(method: "GET" | "POST" | "DELETE", path: string, o: RequestOptions): Promise<T> {
    const tenant = o.tenant ?? (await this.tenant());
    let url = `${this.shared.url}/v1/${encodeURIComponent(tenant)}/${path}`;
    if (o.query) {
      const qs = new URLSearchParams();
      for (const [k, v] of Object.entries(o.query)) if (v !== undefined) qs.set(k, String(v));
      const s = qs.toString();
      if (s) url += "?" + s;
    }
    const payload = o.body === undefined ? undefined : JSON.stringify(o.body);
    const S = this.shared;
    for (let attempt = 0; ; attempt++) {
      const headers: Record<string, string> = { authorization: `Bearer ${await this.bearer()}`, accept: "application/json" };
      if (payload !== undefined) headers["content-type"] = "application/json";
      let res: Response;
      try {
        res = await S.fetch(url, { method, headers, body: payload, signal: AbortSignal.timeout(S.timeoutMs) });
      } catch (e) {
        // The request may or may not have reached the gateway: only safe requests go again.
        if (o.retry && attempt < S.retries) { await sleep(this.backoff(attempt)); continue; }
        const timeout = (e as Error)?.name === "TimeoutError";
        throw new OrpError("network", timeout ? `${o.verb}: timed out` : `${o.verb}: network error`, 0);
      }
      if ((res.status === 429 || res.status === 503) && o.retry && attempt < S.retries) {
        await res.body?.cancel().catch(() => undefined);
        await sleep(this.backoff(attempt, res.headers.get("retry-after")));
        continue;
      }
      const text = await res.text();
      let body: unknown = undefined;
      if (text) { try { body = JSON.parse(text); } catch { body = undefined; } }
      const rid = res.headers.get("x-request-id") ?? undefined;
      if (!res.ok) throw OrpError.fromResponse(res.status, body, rid);
      if (body === undefined || body === null || typeof body !== "object") {
        throw new OrpError("internal", `${o.verb}: response is not JSON`, res.status, {}, rid);
      }
      const deg = (body as Json).degraded;
      if (Array.isArray(deg) && deg.length) this.reportDegraded(deg.map(String), o.verb);
      return body as T;
    }
  }

  private backoff(attempt: number, retryAfter?: string | null): number {
    const S = this.shared;
    const cap = Math.min(S.maxRetryDelayMs, S.retryDelayMs * 2 ** attempt);
    const jittered = Math.random() * cap; // full jitter
    const ra = retryAfter && /^\d+$/.test(retryAfter) ? Math.min(S.maxRetryDelayMs, Number(retryAfter) * 1000) : 0;
    return Math.max(jittered, ra);
  }

  // ------------------------------------------------------------ everyday verbs
  /** A budgeted, cited context pack for a question. */
  async recall(query: string, opts: RecallOptions = {}): Promise<ContextPack> {
    if (typeof query !== "string" || !query.trim()) throw new OrpError("invalid_parameter", "query is required (an empty query never means 'everything')");
    const body: Json = { query, budget: { tokens: opts.budgetTokens ?? DEFAULT_RECALL_BUDGET } };
    if (opts.maxItems !== undefined) body.budget.items = opts.maxItems;
    if (opts.asOf !== undefined) body.as_of = iso(opts.asOf);
    if (opts.knownAt !== undefined) body.known_at = iso(opts.knownAt);
    if (opts.scope !== undefined || opts.prefer !== undefined) {
      body.scope = opts.prefer ? { ...(opts.scope !== undefined ? { within: opts.scope } : {}), prefer: opts.prefer } : opts.scope;
    }
    if (opts.kinds !== undefined) body.kinds = opts.kinds;
    if (opts.granularity !== undefined) body.granularity = opts.granularity;
    if (opts.mode !== undefined) body.mode = opts.mode;
    if (opts.minCoverage !== undefined) body.min_coverage = opts.minCoverage;
    if (opts.include !== undefined) body.include = { ...opts.include };
    return this.request<ContextPack>("POST", "recall", { body, retry: true, verb: "recall" });
  }

  /** Store one or more memories. Fail-closed defaults: readers is [you] unless you pass readers or
   * audience: "tenant"; procedures go in as drafts that need approval unless you set mode. */
  async remember(input: string | MemoryInput | Array<string | MemoryInput>, opts: RememberOptions = {}): Promise<RememberResponse> {
    const list = (Array.isArray(input) ? input : [input]).map((m) => (typeof m === "string" ? { content: m } : m));
    if (!list.length) throw new OrpError("invalid_parameter", "nothing to remember");
    if (opts.audience !== undefined && opts.audience !== "self" && opts.audience !== "tenant") {
      throw new OrpError("unsupported_parameter", `audience ${String(opts.audience)} (use "self" or "tenant", or explicit readers)`);
    }
    let selfReaders: string[] | null = null;
    const memories: Json[] = [];
    for (const m of list) {
      if (!m || typeof m !== "object" || typeof m.content !== "string") throw new OrpError("invalid_parameter", "each memory needs string content");
      const out: Json = { ...m };
      if (out.kind === undefined) out.kind = opts.kind ?? (out.claim ? "fact" : "event");
      if (out.thread === undefined && opts.thread !== undefined) out.thread = opts.thread;
      if (out.scope === undefined && opts.scope !== undefined) out.scope = opts.scope;
      if ("readers" in m && m.readers !== undefined) {
        if (m.readers === null) throw new OrpError("invalid_parameter", "readers: null means the whole tenant; say so with audience: \"tenant\"");
        if (opts.audience === "tenant") throw new OrpError("invalid_parameter", "pass explicit readers or audience: \"tenant\", not both");
      } else if (opts.audience === "tenant") {
        out.readers = null;
      } else {
        selfReaders ??= [`principal:${(await this.me()).id}`];
        out.readers = [...selfReaders];
      }
      memories.push(out);
    }
    let mode = opts.mode;
    if (mode === undefined) {
      const procs = memories.filter((m) => m.kind === "procedure").length;
      if (procs && procs !== memories.length) {
        throw new OrpError("invalid_parameter", "procedures default to drafts (mode propose); send them in their own call or set mode");
      }
      if (procs) mode = "propose";
    }
    const body: Json = { memories };
    if (mode !== undefined) body.mode = mode;
    if (opts.idempotencyKey !== undefined) body.idempotency_key = opts.idempotencyKey;
    if (opts.ifVersion !== undefined) body.if_version = opts.ifVersion;
    if (opts.wait !== undefined) body.wait = opts.wait;
    if (opts.allowReingest !== undefined) body.allow_reingest = opts.allowReingest;
    return this.request<RememberResponse>("POST", "remember", { body, retry: opts.idempotencyKey !== undefined, verb: "remember" });
  }

  /** Retract (default), redact or purge memories, cascading to what was derived from them. dryRun shows the
   * affected ids and changes nothing. */
  async forget(ids: string | string[], opts: ForgetOptions = {}): Promise<ForgetResponse> {
    const list = Array.isArray(ids) ? ids : [ids];
    if (!list.length || !list.every((x) => typeof x === "string" && x)) throw new OrpError("invalid_parameter", "ids must be non-empty strings");
    const body: Json = { ids: list };
    if (opts.mode !== undefined) body.mode = opts.mode;
    if (opts.cascade !== undefined) body.cascade = opts.cascade;
    if (opts.dryRun) body.dry_run = true;
    if (opts.reason !== undefined) body.reason = opts.reason;
    if (opts.allowReingest !== undefined) body.allow_reingest = opts.allowReingest;
    return this.request<ForgetResponse>("POST", "forget", { body, retry: Boolean(opts.dryRun), verb: "forget" });
  }

  /** Always-loaded context (rules, preferences, profile facts) within a required token budget. */
  async profile(opts: ProfileOptions): Promise<ProfilePack> {
    if (!opts || typeof opts.budgetTokens !== "number" || !(opts.budgetTokens > 0)) {
      throw new OrpError("invalid_parameter", "profile requires budgetTokens (no unbounded ambient context)");
    }
    const body: Json = { budget: { tokens: Math.trunc(opts.budgetTokens) } };
    if (opts.subject !== undefined) body.subject = opts.subject;
    if (opts.asOf !== undefined) body.as_of = iso(opts.asOf);
    return this.request<ProfilePack>("POST", "profile", { body, retry: true, verb: "profile" });
  }

  /** Widen who can read a memory: adds reader tokens (e.g. "principal:bob", "group:eng") to an active record.
   * Reads the record, then upserts it with if_version, so a concurrent edit is never overwritten. */
  async share(id: string, readers: string | string[]): Promise<{ id: string; version: number; readers: string[] | null; changed: boolean }> {
    const add = Array.isArray(readers) ? readers : [readers];
    if (!add.length || !add.every((r) => typeof r === "string" && r.trim())) throw new OrpError("invalid_parameter", "readers must be non-empty token strings");
    for (let attempt = 0; ; attempt++) {
      const rec = await this.advanced.get(id);
      if (rec.status !== "active") throw new OrpError("invalid_parameter", `only active records can be shared (this one is ${rec.status})`);
      if (rec.readers === null) return { id: rec.id, version: rec.version, readers: null, changed: false };
      const next = [...new Set([...rec.readers, ...add])].sort();
      if (next.length === rec.readers.length) return { id: rec.id, version: rec.version, readers: rec.readers, changed: false };
      try {
        await this.remember([recordToInput(rec, next)], { mode: "upsert", ifVersion: rec.version });
        return { id: rec.id, version: rec.version + 1, readers: next, changed: true };
      } catch (e) {
        if (e instanceof OrpError && e.code === "version_conflict" && attempt < 2) continue;
        throw e;
      }
    }
  }

  /** The version chain of a claim (subject, predicate). */
  history(subject: string, predicate: string): Promise<HistoryResponse> {
    return this.request<HistoryResponse>("GET", "history", { query: { subject, predicate }, retry: true, verb: "history" });
  }

  // ------------------------------------------------------------ derived clients
  /** A client for one agent run: as_of is resolved once now (or from `asOf`) and sent on every recall and
   * profile, and writes carry idempotency keys run:<runId>:<step>, so a retried or restarted run replays
   * instead of writing twice (M17, M20, M52). */
  run(opts: { runId?: string; asOf?: Time } = {}): RunClient {
    return new RunClient(this, opts.runId ?? globalThis.crypto.randomUUID(), iso(opts.asOf ?? new Date()));
  }

  /** A client acting as one user, authenticated by that user's bearer (an OIDC token the gateway verifies).
   * Nothing about the user is asserted by this client: the gateway maps the token to a principal. Tenant
   * discovery starts from this client's tenant, where a platform's users usually live. */
  forUser(assertionToken: string): OrpClient {
    if (typeof assertionToken !== "string" || !assertionToken) throw new OrpError("invalid_parameter", "an assertion token is required");
    const guess = this.tenantP ? this.tenantP.catch(() => this.tenantGuess) : this.tenantGuess;
    return new OrpClient({ url: this.shared.url, token: assertionToken }, this.shared, guess);
  }
}

/** meta keys the engine derives from content on write. */
export const ENGINE_DERIVED_META: ReadonlySet<string> = new Set(["entities", "time_refs"]);

const INPUT_KEYS = ["id", "kind", "content", "claim", "scope", "writers", "actor", "origin", "thread", "seq", "event_time",
  "valid_from", "valid_to", "expires_at", "sources", "derived_from", "labels", "load", "confidence", "support", "meta"] as const;

/** The writable fields of a stored record, for an upsert that keeps everything but the audience. */
function recordToInput(rec: Memory, readers: string[]): MemoryInput {
  const out: Json = {};
  for (const k of INPUT_KEYS) {
    const v = (rec as Json)[k];
    if (v !== undefined) out[k] = v;
  }
  if (rec.role) out.role = rec.role;
  if (out.meta) { // entities and time_refs are derived by the engine; the server rejects them on input
    const meta: Json = {};
    for (const [k, v] of Object.entries(out.meta as Json)) if (!ENGINE_DERIVED_META.has(k) && !k.startsWith("_")) meta[k] = v;
    out.meta = meta;
  }
  out.readers = readers;
  return out as MemoryInput;
}

/** Run-scoped client: see OrpClient.run(). */
export class RunClient implements MemoryApi {
  private step = 0;
  constructor(readonly client: OrpClient, readonly runId: string, readonly asOf: string) {}

  /** The idempotency key the next write without an explicit step will use. */
  get nextKey(): string {
    return `run:${this.runId}:${this.step + 1}`;
  }

  private pinned<T extends { asOf?: Time }>(opts: T): T {
    if (opts.asOf !== undefined && iso(opts.asOf) !== this.asOf) {
      throw new OrpError("invalid_parameter", "a run pins as_of once; start another run for a different time");
    }
    return { ...opts, asOf: this.asOf };
  }

  recall(query: string, opts: RecallOptions = {}): Promise<ContextPack> {
    return this.client.recall(query, this.pinned(opts));
  }

  profile(opts: ProfileOptions): Promise<ProfilePack> {
    return this.client.profile(this.pinned(opts));
  }

  /** Writes carry run:<runId>:<step>. Steps count up from 1 per write; pass `step` to name one explicitly
   * (an explicit number also moves the counter past it, so later automatic steps never reuse it). */
  remember(input: string | MemoryInput | Array<string | MemoryInput>, opts: RememberOptions & { step?: number | string } = {}): Promise<RememberResponse> {
    const { step, ...rest } = opts;
    let s: number | string;
    if (step === undefined) s = ++this.step;
    else {
      s = step;
      if (typeof step === "number") this.step = Math.max(this.step, step);
    }
    return this.client.remember(input, { ...rest, idempotencyKey: rest.idempotencyKey ?? `run:${this.runId}:${s}` });
  }

  /** Not keyed (the protocol has no idempotency key on forget), so it is never retried. */
  forget(ids: string | string[], opts: ForgetOptions = {}): Promise<ForgetResponse> {
    return this.client.forget(ids, opts);
  }

  share(id: string, readers: string | string[]) {
    return this.client.share(id, readers);
  }

  history(subject: string, predicate: string): Promise<HistoryResponse> {
    return this.client.history(subject, predicate);
  }

  reportDegraded(values: string[], source: string): void {
    this.client.reportDegraded(values, source);
  }
}

/** Verbs for tools, operators and adapters. */
export class AdvancedApi {
  constructor(private readonly c: OrpClient) {}

  /** One record by id. Throws OrpError not_found when it is absent or you may not read it (they look the same). */
  get(id: string): Promise<Memory> {
    return this.c.request<Memory>("GET", `memories/${encodeURIComponent(id)}`, { retry: true, verb: "get" });
  }

  /** Several records by id; missing or unreadable ids are listed in not_found. */
  async getMany(ids: string[]): Promise<{ memories: Memory[]; not_found: string[] }> {
    const out = await Promise.all(ids.map((id) => this.get(id).catch((e) => {
      if (e instanceof OrpError && e.code === "not_found") return null;
      throw e;
    })));
    return { memories: out.filter((m): m is Memory => m !== null), not_found: ids.filter((_, i) => out[i] === null) };
  }

  /** One page of admissible records, newest first (extension x-list). */
  list(opts: ListOptions = {}): Promise<ListPage> {
    return this.c.request<ListPage>("GET", "memories", { retry: true, verb: "list", query: {
      kind: opts.kinds?.length ? opts.kinds.join(",") : undefined, thread: opts.thread, scope: opts.scope,
      limit: opts.limit, cursor: opts.cursor } });
  }

  /** Every page, following the cursor. */
  async *pages(opts: ListOptions = {}): AsyncGenerator<ListPage> {
    let cursor: number | null = opts.cursor ?? 0;
    while (cursor !== null) {
      const page: ListPage = await this.list({ ...opts, cursor });
      yield page;
      cursor = page.cursor;
    }
  }

  /** Every record, following the cursor. */
  async *all(opts: ListOptions = {}): AsyncGenerator<Memory> {
    for await (const p of this.pages(opts)) yield* p.memories;
  }

  neighbours(id: string, opts: { before?: number; after?: number } = {}): Promise<UnitsResponse> {
    return this.c.request<UnitsResponse>("GET", `memories/${encodeURIComponent(id)}/neighbours`, { retry: true, verb: "neighbours",
      query: { before: opts.before, after: opts.after } });
  }

  thread(thread: string): Promise<UnitsResponse> {
    return this.c.request<UnitsResponse>("GET", `threads/${encodeURIComponent(thread)}/neighbours`, { retry: true, verb: "thread" });
  }

  link(req: LinkRequest): Promise<Envelope & { id: string }> {
    return this.c.request("POST", "link", { body: req, retry: false, verb: "link" });
  }

  unlink(edgeId: string): Promise<Envelope & { id: string; removed: boolean }> {
    return this.c.request("DELETE", `edges/${encodeURIComponent(edgeId)}`, { retry: false, verb: "unlink" });
  }

  async edges(id: string): Promise<Edge[]> {
    return (await this.c.request<{ edges: Edge[] }>("GET", `memories/${encodeURIComponent(id)}/edges`, { retry: true, verb: "edges" })).edges;
  }

  feedback(req: FeedbackRequest): Promise<Envelope & { recorded: boolean; rule_candidate?: string }> {
    return this.c.request("POST", "feedback", { body: req, retry: false, verb: "feedback" });
  }

  approve(id: string): Promise<Envelope & { id: string; status: string; version: number }> {
    return this.c.request("POST", `memories/${encodeURIComponent(id)}/approve`, { retry: false, verb: "approve" });
  }

  reflect(req: ReflectRequest = {}): Promise<JobResponse> {
    return this.c.request("POST", "reflect", { body: req, retry: false, verb: "reflect" });
  }

  job(id: string): Promise<JobResponse> {
    return this.c.request("GET", `jobs/${encodeURIComponent(id)}`, { retry: true, verb: "job" });
  }

  /** One page of the change feed (only changes to records you can read). */
  changesPage(cursor = 0, limit?: number): Promise<ChangesResponse> {
    return this.c.request("GET", "changes", { retry: true, verb: "changes", query: { cursor, limit } });
  }

  /** Changes from `cursor`, following the feed. Stops at the end unless `follow` is set, in which case it polls
   * until `signal` aborts. */
  async *changes(opts: ChangesOptions = {}): AsyncGenerator<Change> {
    let cursor = opts.cursor ?? 0;
    for (;;) {
      if (opts.signal?.aborted) return;
      const page = await this.changesPage(cursor, opts.limit);
      for (const c of page.changes) yield c;
      const moved = page.cursor !== cursor;
      cursor = page.cursor;
      if (moved) continue;
      if (!opts.follow) return;
      try { await sleep(opts.pollMs ?? 1000, opts.signal); } catch { return; }
    }
  }

  export(): Promise<Bundle> {
    return this.c.request("GET", "export", { retry: true, verb: "export" });
  }

  /** Tenant administrators only (M71). */
  import(bundle: Bundle): Promise<Envelope & { imported: number; id_map: Record<string, string> }> {
    return this.c.request("POST", "import", { body: bundle, retry: false, verb: "import" });
  }

  capabilities(): Promise<Capabilities> {
    return this.c.request("GET", "capabilities", { retry: true, verb: "capabilities" });
  }

  /** Who the token resolves to (always asks the gateway). */
  whoami(): Promise<Whoami> {
    return this.c.request("GET", "whoami", { retry: true, verb: "whoami" });
  }

  /** The audit log (auditor role). */
  audit(): Promise<Envelope & { entries: Json[] }> {
    return this.c.request("GET", "audit", { retry: true, verb: "audit" });
  }
}
