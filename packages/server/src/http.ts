// HTTP gateway: the ORP HTTP binding (spec §8.2), MCP over Streamable HTTP (spec §8.1) as an OAuth 2.1 resource
// server, health and metrics. Every request authenticates first; the principal comes from the token only.
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { randomBytes, randomUUID } from "node:crypto";
import { ORPError, type Principal, type Service } from "@orp/core";
import { AuthError, type PrincipalSource } from "./auth.js";
import { McpSession, PROTOCOLS, type SessionOptions } from "./mcp.js";
import { Log } from "./log.js";
import { mem0Route } from "./mem0.js";

type Json = Record<string, any>;

export interface HttpOptions {
  host: string;
  port: number;
  publicUrl?: string; // e.g. https://memory.example.com (for OAuth metadata); default http://host:port
  authorizationServers?: string[];
  maxBody?: number; // bytes, default 1 MiB (import: 64 MiB)
  rate?: { rps: number; burst: number }; // per principal
  authFailRate?: { rps: number; burst: number }; // per client address, for failed authentication (default 1/s, burst 20)
  allowedOrigins?: string[]; // for browser callers and MCP DNS-rebinding protection
  mcp: Omit<SessionOptions, "notify" | "elicit">;
  metricsToken?: string;
  sessionTtlMs?: number;
}

const ERR_STATUS: Record<string, number> = { unauthorized: 401, forbidden: 403, not_found: 404, version_conflict: 409,
  rate_limited: 429, payload_too_large: 413, unavailable: 503 };

/** A bounded label for metrics and logs: ids and other caller-chosen path segments become placeholders, so a caller
 * cannot grow the metrics table or put arbitrary text in log labels. */
export function routeTemplate(method: string, parts: string[]): string {
  const p0 = parts[0];
  if (p0 === "v1") {
    if (parts.length === 2 && parts[1] === "whoami") return `${method} /v1/whoami`;
    const verb = parts[2];
    if (verb === undefined) return `${method} /v1`;
    const known = ["capabilities", "whoami", "recall", "remember", "forget", "feedback", "reflect", "profile", "link", "import",
      "memories", "history", "threads", "changes", "audit", "jobs", "export", "edges"];
    if (!known.includes(verb)) return `${method} /v1/:tenant/other`;
    const rest = parts.slice(3).map((x, i) => (i === 0 ? ":id" : ["neighbours", "edges", "history", "approve"].includes(x) ? x : ":x"));
    return `${method} /v1/:tenant/${[verb, ...rest].join("/")}`;
  }
  if (p0 === "mcp" && parts.length === 1) return `${method} /mcp`;
  if (p0 === "mem0") return `${method} /mem0`;
  if (p0 === ".well-known") return `${method} /.well-known`;
  if (["healthz", "readyz", "metrics"].includes(p0 ?? "") && parts.length === 1) return `${method} /${p0}`;
  return `${method} other`;
}

class Bucket {
  tokens: number;
  at = Date.now();
  constructor(readonly rps: number, readonly burst: number) { this.tokens = burst; }
  /** Refill without spending; true when at least one token is available. */
  peek(): boolean {
    const now = Date.now();
    this.tokens = Math.min(this.burst, this.tokens + ((now - this.at) / 1000) * this.rps);
    this.at = now;
    return this.tokens >= 1;
  }
  take(): boolean {
    const now = Date.now();
    this.tokens = Math.min(this.burst, this.tokens + ((now - this.at) / 1000) * this.rps);
    this.at = now;
    if (this.tokens < 1) return false;
    this.tokens -= 1;
    return true;
  }
}

class Metrics {
  counts = new Map<string, number>();
  latency = new Map<string, number[]>(); // per route: histogram buckets
  static BUCKETS = [5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, Infinity];
  observe(route: string, status: number, ms: number) {
    const k = `${route}|${status}`;
    this.counts.set(k, (this.counts.get(k) ?? 0) + 1);
    let h = this.latency.get(route);
    if (!h) this.latency.set(route, (h = Metrics.BUCKETS.map(() => 0)));
    Metrics.BUCKETS.forEach((b, i) => { if (ms <= b) h![i]!++; });
  }
  render(): string {
    const out = ["# TYPE orp_requests_total counter"];
    for (const [k, v] of this.counts) { const [r, s] = k.split("|"); out.push(`orp_requests_total{route="${r}",status="${s}"} ${v}`); }
    out.push("# TYPE orp_request_ms histogram");
    for (const [r, h] of this.latency) {
      Metrics.BUCKETS.forEach((b, i) => out.push(`orp_request_ms_bucket{route="${r}",le="${b === Infinity ? "+Inf" : b}"} ${h[i]}`));
    }
    return out.join("\n") + "\n";
  }
}

interface HttpSession { session: McpSession; principalKey: string; streams: Set<ServerResponse>; seen: number }

export class Gateway {
  readonly server: Server;
  private buckets = new Map<string, Bucket>();
  private failBuckets = new Map<string, Bucket>();
  private sessions = new Map<string, HttpSession>();
  readonly metrics = new Metrics();
  private inflight = 0;
  private closing = false;

  constructor(readonly svc: Service, readonly auth: PrincipalSource, readonly opt: HttpOptions, readonly log: Log) {
    this.server = createServer((req, res) => void this.handle(req, res));
    this.server.headersTimeout = 30_000;
    this.server.requestTimeout = 120_000;
    setInterval(() => this.reapSessions(), 60_000).unref();
  }

  listen(): Promise<void> {
    return new Promise((ok) => this.server.listen(this.opt.port, this.opt.host, () => ok()));
  }

  get url(): string {
    const a = this.server.address();
    const port = typeof a === "object" && a ? a.port : this.opt.port;
    return this.opt.publicUrl ?? `http://${this.opt.host === "0.0.0.0" ? "127.0.0.1" : this.opt.host}:${port}`;
  }

  async close(): Promise<void> {
    this.closing = true;
    for (const s of this.sessions.values()) for (const r of s.streams) r.end();
    await new Promise<void>((ok) => this.server.close(() => ok()));
    const t0 = Date.now();
    while (this.inflight > 0 && Date.now() - t0 < 10_000) await new Promise((r) => setTimeout(r, 50));
  }

  private reapSessions() {
    const ttl = this.opt.sessionTtlMs ?? 30 * 60_000;
    for (const [id, s] of this.sessions) if (Date.now() - s.seen > ttl && !s.streams.size) this.sessions.delete(id);
  }

  private send(res: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}) {
    const data = typeof body === "string" ? body : JSON.stringify(body);
    res.writeHead(status, { "content-type": typeof body === "string" ? "text/plain; charset=utf-8" : "application/json",
      "content-length": Buffer.byteLength(data), "cache-control": "no-store", "x-content-type-options": "nosniff", ...headers });
    res.end(data);
  }

  private cors(req: IncomingMessage, res: ServerResponse): boolean {
    const origin = req.headers.origin;
    if (!origin) return true;
    const allowed = this.opt.allowedOrigins ?? [];
    if (!allowed.includes(origin) && !allowed.includes("*")) return false; // also blocks DNS rebinding against MCP
    res.setHeader("access-control-allow-origin", origin);
    res.setHeader("vary", "origin");
    res.setHeader("access-control-allow-headers", "authorization, content-type, if-match, mcp-session-id, mcp-protocol-version");
    res.setHeader("access-control-expose-headers", "etag, mcp-session-id");
    res.setHeader("access-control-allow-methods", "GET, POST, PUT, DELETE, OPTIONS");
    return true;
  }

  private body(req: IncomingMessage, limit: number): Promise<Json> {
    return new Promise((ok, fail) => {
      const chunks: Buffer[] = [];
      let n = 0;
      req.on("data", (c: Buffer) => {
        n += c.length;
        if (n > limit) { req.pause(); req.removeAllListeners("data"); fail(new ORPError("payload_too_large", `body over ${limit} bytes`)); return; }
        chunks.push(c);
      });
      req.on("end", () => {
        if (!n) return ok({});
        try { ok(JSON.parse(Buffer.concat(chunks).toString("utf8"))); } catch { fail(new ORPError("invalid_parameter", "body is not valid JSON")); }
      });
      req.on("error", fail);
    });
  }

  private resourceMetadataUrl() {
    return `${this.url}/.well-known/oauth-protected-resource`;
  }

  private authFail(res: ServerResponse, e: AuthError) {
    this.send(res, e.status, { error: { code: e.status === 403 ? "forbidden" : "unauthorized", message: e.message, details: {} } },
      { "www-authenticate": `Bearer resource_metadata="${this.resourceMetadataUrl()}", error="${e.wwwError}"` });
  }

  private rateOk(pr: Principal): boolean {
    const r = this.opt.rate;
    if (!r) return true;
    const k = `${pr.tenant}\0${pr.id}`;
    let b = this.buckets.get(k);
    if (!b) this.buckets.set(k, (b = new Bucket(r.rps, r.burst)));
    return b.take();
  }

  /** Failed authentication is limited per client address, so a caller without a key cannot hammer token checks. */
  private authFailBucket(req: IncomingMessage): Bucket {
    const r = this.opt.authFailRate ?? { rps: 1, burst: 20 };
    const ip = req.socket.remoteAddress ?? "?";
    let b = this.failBuckets.get(ip);
    if (!b) {
      if (this.failBuckets.size > 50_000) this.failBuckets.clear(); // bounded; a reset only forgives
      this.failBuckets.set(ip, (b = new Bucket(r.rps, r.burst)));
    }
    return b;
  }

  private async handle(req: IncomingMessage, res: ServerResponse) {
    const t0 = performance.now();
    const rid = "req_" + randomBytes(8).toString("hex");
    let url: URL;
    let parts: string[];
    try {
      url = new URL(req.url ?? "/", "http://x");
      parts = url.pathname.split("/").filter(Boolean).map(decodeURIComponent);
    } catch {
      this.send(res, 400, { error: { code: "invalid_parameter", message: "malformed request path", details: {} } });
      this.metrics.observe(`${req.method} other`, 400, performance.now() - t0);
      return;
    }
    const route = routeTemplate(String(req.method), parts);
    let status = 500;
    let pr: Principal | null = null;
    this.inflight++;
    res.setHeader("x-request-id", rid);
    try {
      if (this.closing) { status = 503; return this.send(res, 503, { error: { code: "unavailable", message: "shutting down", details: {} } }); }
      if (!this.cors(req, res)) { status = 403; return this.send(res, 403, { error: { code: "forbidden", message: "origin not allowed", details: {} } }); }
      if (req.method === "OPTIONS") { status = 204; res.writeHead(204); return res.end(); }
      if (url.pathname === "/healthz") { status = 200; return this.send(res, 200, { ok: true }); }
      if (url.pathname === "/readyz") { status = 200; return this.send(res, 200, { ok: true, store: Boolean(this.svc.store) }); }
      if (url.pathname === "/metrics") {
        if (!this.opt.metricsToken || req.headers.authorization !== `Bearer ${this.opt.metricsToken}`) { status = 404; return this.send(res, 404, "not found"); }
        status = 200;
        return this.send(res, 200, this.metrics.render());
      }
      if (url.pathname === "/.well-known/oauth-protected-resource" || url.pathname === "/.well-known/oauth-protected-resource/mcp") {
        status = 200;
        return this.send(res, 200, { resource: `${this.url}/mcp`, authorization_servers: this.opt.authorizationServers ?? [],
          bearer_methods_supported: ["header"], scopes_supported: ["orp:read", "orp:write"], resource_name: "ORP memory" });
      }
      const write = req.method === "POST" || req.method === "DELETE" || req.method === "PUT";
      let authz = req.headers.authorization;
      if (parts[0] === "mem0" && authz && /^Token\s+/i.test(authz)) authz = authz.replace(/^Token\s+/i, "Bearer "); // mem0 clients send "Token <key>"
      const fb = this.authFailBucket(req);
      if (!fb.peek()) {
        status = 429;
        return this.send(res, 429, { error: { code: "rate_limited", message: "too many failed authentications", details: {} } }, { "retry-after": "5" });
      }
      try {
        pr = await this.auth.resolve(authz, write);
      } catch (e) {
        if (e instanceof AuthError) { fb.take(); status = e.status; return this.authFail(res, e); }
        throw e;
      }
      if (!this.rateOk(pr)) { status = 429; return this.send(res, 429, { error: { code: "rate_limited", message: "slow down", details: {} } }, { "retry-after": "1" }); }
      if (parts[0] === "mcp" && parts.length === 1) { status = await this.mcp(req, res, pr); return; }
      if (parts[0] === "v1") { const r = await this.v1(req, res, url, parts, pr); status = r; return; }
      if (parts[0] === "mem0" && (parts[1] === "v1" || parts[1] === "v2")) {
        status = await mem0Route(this.svc, pr, req, res, parts, url, () => this.body(req, this.opt.maxBody ?? 1 << 20), (st, b) => this.send(res, st, b));
        return;
      }
      status = 404;
      this.send(res, 404, { error: { code: "not_found", message: url.pathname, details: {} } });
    } catch (e) {
      if (e instanceof ORPError) {
        status = ERR_STATUS[e.code] ?? 400;
        if (e.code === "payload_too_large") { // answer, then drop the connection instead of reading the rest
          res.setHeader("connection", "close");
          res.on("finish", () => req.socket.destroy());
        }
        this.send(res, status, e.toJSON());
      } else {
        status = 500;
        this.log.error("unhandled", { rid, err: String((e as Error)?.stack ?? e) });
        if (!res.headersSent) this.send(res, 500, { error: { code: "internal", message: "internal error", details: { request_id: rid } } });
      }
    } finally {
      this.inflight--;
      const ms = performance.now() - t0;
      this.metrics.observe(route, status, ms);
      this.log.info("request", { rid, method: req.method, route, status, ms: Math.round(ms), tenant: pr?.tenant, principal: pr?.id });
    }
  }

  // ------------------------------------------------------------ ORP HTTP binding
  private async v1(req: IncomingMessage, res: ServerResponse, url: URL, parts: string[], pr: Principal): Promise<number> {
    if (req.method === "GET" && parts.length === 2 && parts[1] === "whoami") { // tenant-free: lets a client discover its tenant
      this.send(res, 200, { id: pr.id, tenant: pr.tenant, groups: [...pr.groups].sort(), tokens: [...pr.tokens()].sort(),
        write_scopes: pr.writeScopes });
      return 200;
    }
    if (parts.length < 3) throw new ORPError("not_found", url.pathname);
    if (parts[1] !== pr.tenant) throw new ORPError("forbidden", "tenant mismatch");
    const s = this.svc, verb = parts[2]!, m = req.method, q = Object.fromEntries(url.searchParams);
    const ok = (body: Json, headers: Record<string, string> = {}) => { this.send(res, 200, body, headers); return 200; };
    const intQ = (k: string, d: number) => {
      if (q[k] === undefined) return d;
      if (!/^-?\d+$/.test(q[k]!)) throw new ORPError("invalid_parameter", `${k} must be an integer`);
      return parseInt(q[k]!, 10);
    };
    if (m === "GET" && verb === "capabilities" && parts.length === 3) return ok(await s.capabilities(pr));
    if (m === "GET" && verb === "whoami" && parts.length === 3) { // extension x-whoami: who the token resolves to
      return ok({ id: pr.id, tenant: pr.tenant, groups: [...pr.groups].sort(), tokens: [...pr.tokens()].sort(), write_scopes: pr.writeScopes });
    }
    if (m === "POST" && parts.length === 3 && ["recall", "remember", "forget", "feedback", "reflect", "profile", "link", "import"].includes(verb)) {
      const body = await this.body(req, verb === "import" ? 64 << 20 : this.opt.maxBody ?? 1 << 20);
      if (req.headers["if-match"] !== undefined && verb === "remember" && body.if_version === undefined) {
        const v = String(req.headers["if-match"]).replace(/^W\//, "").replace(/"/g, "");
        if (!/^\d+$/.test(v)) throw new ORPError("invalid_parameter", "If-Match must carry a version number");
        body.if_version = parseInt(v, 10);
      }
      const out = verb === "import" ? await s.import(body, pr) : await (s as any)[verb](body, pr);
      return ok(out);
    }
    if (verb === "memories") {
      if (m === "GET" && parts.length === 3) {
        return ok(await s.list({ ...(q.kind ? { kinds: q.kind.split(",") } : {}), ...(q.thread ? { thread: q.thread } : {}),
          ...(q.scope ? { scope: q.scope } : {}), limit: intQ("limit", 100), cursor: intQ("cursor", 0) }, pr));
      }
      const id = parts[3];
      if (id === undefined) throw new ORPError("not_found", url.pathname);
      if (m === "GET" && parts.length === 4) {
        const r = await s.get([id], pr);
        if (!r.memories.length) throw new ORPError("not_found", id);
        return ok(r.memories[0], { etag: `"${r.memories[0].version}"` });
      }
      if (m === "GET" && parts[4] === "neighbours") return ok(await s.neighbours(id, intQ("before", 2), intQ("after", 2), pr));
      if (m === "GET" && parts[4] === "edges") return ok({ edges: await s.edges(id, pr) });
      if (m === "GET" && parts[4] === "history") {
        const r = await s.get([id], pr);
        if (!r.memories.length || !r.memories[0].claim) throw new ORPError("not_found", id);
        return ok(await s.history(r.memories[0].claim.subject, r.memories[0].claim.predicate, pr));
      }
      if (m === "POST" && parts[4] === "approve") return ok(await s.approve(id, pr));
      if (m === "DELETE" && parts.length === 4) { // REST-style forget: retract, with If-Match guarding the version
        let ifVersion: number | undefined;
        if (req.headers["if-match"] !== undefined) {
          const v = String(req.headers["if-match"]).replace(/^W\//, "").replace(/"/g, "");
          if (!/^\d+$/.test(v)) throw new ORPError("invalid_parameter", "If-Match must carry a version number");
          ifVersion = parseInt(v, 10);
        }
        // the version check and the forget run under one lock, so nothing can change the record in between
        return ok(await s.forget({ ids: [id], mode: q.mode ?? "retract" }, pr, ifVersion === undefined ? {} : { ifVersion }));
      }
    }
    if (m === "GET" && verb === "history" && parts.length === 3) {
      if (typeof q.subject !== "string" || typeof q.predicate !== "string") throw new ORPError("invalid_parameter", "subject and predicate are required");
      return ok(await s.history(q.subject, q.predicate, pr));
    }
    if (m === "GET" && verb === "threads" && parts.length === 5 && parts[4] === "neighbours") return ok(await s.thread(parts[3], pr));
    if (m === "GET" && verb === "changes" && parts.length === 3) return ok(await s.changes(intQ("cursor", 0), pr, intQ("limit", 500)));
    if (m === "GET" && verb === "audit" && parts.length === 3) return ok(await s.auditLog(pr));
    if (m === "GET" && verb === "jobs" && parts.length === 4) return ok(await s.job(parts[3], pr));
    if (m === "GET" && verb === "export" && parts.length === 3) return ok(await s.export(pr));
    if (m === "DELETE" && verb === "edges" && parts.length === 4) return ok(await s.unlink(parts[3], pr));
    throw new ORPError("not_found", url.pathname);
  }

  // ------------------------------------------------------------ MCP Streamable HTTP
  private async mcp(req: IncomingMessage, res: ServerResponse, pr: Principal): Promise<number> {
    const pv = req.headers["mcp-protocol-version"];
    if (pv !== undefined && !PROTOCOLS.includes(String(pv))) {
      this.send(res, 400, { jsonrpc: "2.0", id: null, error: { code: -32600, message: `unsupported MCP-Protocol-Version ${pv}` } });
      return 400;
    }
    const pkey = `${pr.tenant}\0${pr.id}\0${[...pr.groups].sort().join(",")}`;
    const sid = req.headers["mcp-session-id"] as string | undefined;
    const existing = sid ? this.sessions.get(sid) : undefined;
    if (sid && (!existing || existing.principalKey !== pkey)) { // unknown, expired, or someone else's session
      this.send(res, 404, { jsonrpc: "2.0", id: null, error: { code: -32001, message: "session not found" } });
      return 404;
    }
    if (req.method === "DELETE") {
      if (existing) { for (const r of existing.streams) r.end(); this.sessions.delete(sid!); }
      res.writeHead(204); res.end();
      return 204;
    }
    if (req.method === "GET") {
      if (!existing || !String(req.headers.accept ?? "").includes("text/event-stream")) { this.send(res, 405, "method not allowed"); return 405; }
      res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-store", connection: "keep-alive" });
      res.write(": ok\n\n");
      existing.streams.add(res);
      const ping = setInterval(() => res.write(": ping\n\n"), 25_000);
      req.on("close", () => { clearInterval(ping); existing.streams.delete(res); });
      return 200;
    }
    if (req.method !== "POST") { this.send(res, 405, "method not allowed"); return 405; }
    const msg = await this.body(req, this.opt.maxBody ?? 1 << 20);
    if (Array.isArray(msg)) {
      this.send(res, 400, { jsonrpc: "2.0", id: null, error: { code: -32600, message: "batching is not supported" } });
      return 400;
    }
    let hs = existing;
    if (msg.method === "initialize") {
      const newId = randomUUID();
      const holder: { s?: HttpSession } = {};
      const session = new McpSession(this.svc, pr, { ...this.opt.mcp,
        notify: (n) => { for (const r of holder.s?.streams ?? []) r.write(`event: message\ndata: ${JSON.stringify(n)}\n\n`); } });
      await session.primeCursor();
      hs = { session, principalKey: pkey, streams: new Set(), seen: Date.now() };
      holder.s = hs;
      this.sessions.set(newId, hs);
      res.setHeader("mcp-session-id", newId);
    } else if (!hs) {
      this.send(res, 400, { jsonrpc: "2.0", id: msg.id ?? null, error: { code: -32000, message: "missing Mcp-Session-Id; initialize first" } });
      return 400;
    }
    hs.seen = Date.now();
    const out = await hs.session.handle(msg);
    if (out === null) { res.writeHead(202); res.end(); return 202; }
    this.send(res, 200, out);
    return 200;
  }
}
