// A minimal HTTP client for the Smartware HTTP binding (spec §8.2), just enough for connectors and the test kit.
// It never logs request bodies or tokens. The principal is whoever the bearer token resolves to on the server.

export type Json = Record<string, any>;

export class SmartwareHttpError extends Error {
  constructor(readonly status: number, readonly code: string, message: string, readonly details: Json = {}) {
    super(message);
    this.name = "SmartwareHttpError";
  }
}

export interface SmartwareHttpOptions {
  url: string;
  token: string;
  tenant?: string;
  /** Per request timeout in milliseconds (default 30 s). */
  timeoutMs?: number;
}

export class SmartwareHttp {
  readonly base: string;
  readonly tenant: string;
  readonly #token: string; // private field: never enumerable, never serialised
  readonly timeoutMs: number;

  constructor(opt: SmartwareHttpOptions) {
    if (!opt.url) throw new Error("url is required");
    if (!opt.token) throw new Error("token is required");
    this.base = opt.url.replace(/\/+$/, "");
    this.tenant = opt.tenant ?? "default";
    this.#token = opt.token;
    this.timeoutMs = opt.timeoutMs ?? 30_000;
  }

  toJSON() {
    return { base: this.base, tenant: this.tenant };
  }

  private path(verb: string): string {
    return `/v1/${encodeURIComponent(this.tenant)}/${verb}`;
  }

  async call(method: string, path: string, body?: unknown): Promise<Json> {
    const r = await fetch(this.base + path, {
      method,
      headers: { authorization: `Bearer ${this.#token}`, "content-type": "application/json", accept: "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    const text = await r.text();
    let json: any = null;
    try { json = text ? JSON.parse(text) : {}; } catch { json = null; }
    if (!r.ok) {
      const e = json?.error ?? {};
      throw new SmartwareHttpError(r.status, String(e.code ?? `http_${r.status}`), String(e.message ?? `HTTP ${r.status}`), e.details ?? {});
    }
    if (json === null || typeof json !== "object") throw new SmartwareHttpError(r.status, "bad_response", "server did not return JSON");
    return json as Json;
  }

  whoami(): Promise<Json> { return this.call("GET", this.path("whoami")); }
  capabilities(): Promise<Json> { return this.call("GET", this.path("capabilities")); }
  remember(body: Json): Promise<Json> { return this.call("POST", this.path("remember"), body); }
  forget(body: Json): Promise<Json> { return this.call("POST", this.path("forget"), body); }
  recall(body: Json): Promise<Json> { return this.call("POST", this.path("recall"), body); }
  reflect(body: Json): Promise<Json> { return this.call("POST", this.path("reflect"), body); }
  export(): Promise<Json> { return this.call("GET", this.path("export")); }
  changes(cursor = 0, limit = 10_000): Promise<Json> { return this.call("GET", `${this.path("changes")}?cursor=${cursor}&limit=${limit}`); }

  /** One record by client id, or null when it does not exist or this principal cannot read it (the two look the same). */
  async get(id: string): Promise<Json | null> {
    try {
      return await this.call("GET", `${this.path("memories")}/${encodeURIComponent(id)}`);
    } catch (e) {
      if (e instanceof SmartwareHttpError && e.status === 404) return null;
      throw e;
    }
  }

  async list(q: { kind?: string; thread?: string; scope?: string; limit?: number; cursor?: number } = {}): Promise<Json> {
    const sp = new URLSearchParams();
    for (const [k, v] of Object.entries(q)) if (v !== undefined) sp.set(k, String(v));
    const qs = sp.toString();
    return this.call("GET", this.path("memories") + (qs ? `?${qs}` : ""));
  }
}
