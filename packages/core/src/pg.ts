// A small PostgreSQL client (protocol 3.0) with no dependencies: TLS, SCRAM-SHA-256 / MD5 / cleartext password
// authentication, parameterised queries, and pipelined batches that commit or roll back as one transaction.
// Values travel as text; results come back as strings, numbers (int2/4/8, float4/8, numeric when safe), booleans,
// Buffers (bytea) and null. It is what the Postgres store needs and no more.
import { createHash, createHmac, pbkdf2Sync, randomBytes, timingSafeEqual } from "node:crypto";
import { connect as netConnect, type Socket } from "node:net";
import { connect as tlsConnect, type ConnectionOptions, type TLSSocket } from "node:tls";
import { readFileSync } from "node:fs";

export type PgValue = string | number | boolean | null | Buffer | Uint8Array | bigint;
export type PgRow = Record<string, any>;
export interface PgResult { rows: PgRow[]; command: string; rowCount: number }
export interface PgQuery { text: string; params?: PgValue[] }

export interface PgConfig {
  host: string;
  port: number;
  user: string;
  password?: string;
  database: string;
  /** "disable" | "prefer" | "require" | "verify-ca" | "verify-full" (libpq's names). Default "prefer". */
  sslmode: string;
  sslrootcert?: string;
  applicationName: string;
  connectTimeoutMs: number;
}

const SSLMODES = ["disable", "prefer", "require", "verify-ca", "verify-full"];
const MAX_MESSAGE = 1 << 30;
const MAX_SCRAM_ITERATIONS = 1_000_000;

/** Parse a postgres:// URL (and the usual PG* environment variables as defaults). */
export function parsePgUrl(url: string, env: NodeJS.ProcessEnv = process.env): PgConfig {
  const u = new URL(url);
  if (u.protocol !== "postgres:" && u.protocol !== "postgresql:") throw new Error("expected a postgres:// URL");
  const q = u.searchParams;
  const sslmode = q.get("sslmode") || env.PGSSLMODE || "prefer";
  if (!SSLMODES.includes(sslmode)) throw new Error(`sslmode must be one of ${SSLMODES.join(", ")} (got ${JSON.stringify(sslmode)})`);
  return {
    host: decodeURIComponent(u.hostname) || q.get("host") || env.PGHOST || "localhost",
    port: Number(u.port || q.get("port") || env.PGPORT || 5432),
    user: decodeURIComponent(u.username) || env.PGUSER || "postgres",
    password: u.password ? decodeURIComponent(u.password) : env.PGPASSWORD,
    database: decodeURIComponent(u.pathname.replace(/^\//, "")) || env.PGDATABASE || decodeURIComponent(u.username) || "postgres",
    sslmode,
    sslrootcert: q.get("sslrootcert") || env.PGSSLROOTCERT || undefined,
    applicationName: q.get("application_name") || "smartware",
    connectTimeoutMs: Number(q.get("connect_timeout") ?? 10) * 1000,
  };
}

/** The URL with any password removed, for logs. */
export function redactPgUrl(url: string): string {
  try {
    const u = new URL(url);
    if (u.password) u.password = "***";
    return u.toString();
  } catch {
    return "postgres://(unparseable)";
  }
}

export class PgError extends Error {
  constructor(readonly fields: Record<string, string>) {
    super(fields.M ?? "postgres error");
  }
  get code(): string { return this.fields.C ?? ""; }
}

const OID = { bool: 16, bytea: 17, int8: 20, int2: 21, int4: 23, float4: 700, float8: 701, numeric: 1700 };

function parseValue(oid: number, s: string): any {
  switch (oid) {
    case OID.bool: return s === "t";
    case OID.int2: case OID.int4: case OID.float4: case OID.float8: return Number(s);
    case OID.int8: case OID.numeric: { const n = Number(s); return Number.isSafeInteger(n) || !Number.isInteger(n) ? n : s; }
    case OID.bytea: return s.startsWith("\\x") ? Buffer.from(s.slice(2), "hex") : Buffer.from(s, "binary");
    default: return s;
  }
}

function encodeParam(v: PgValue): Buffer | null {
  if (v === null || v === undefined) return null;
  if (Buffer.isBuffer(v) || v instanceof Uint8Array) return Buffer.from("\\x" + Buffer.from(v).toString("hex"), "utf8");
  if (typeof v === "boolean") return Buffer.from(v ? "t" : "f");
  return Buffer.from(String(v), "utf8");
}

class Writer {
  private parts: Buffer[] = [];
  private len = 0;
  push(b: Buffer) { this.parts.push(b); this.len += b.length; return this; }
  str(s: string) { return this.push(Buffer.concat([Buffer.from(s, "utf8"), Buffer.from([0])])); }
  i16(n: number) { const b = Buffer.alloc(2); b.writeInt16BE(n); return this.push(b); }
  i32(n: number) { const b = Buffer.alloc(4); b.writeInt32BE(n); return this.push(b); }
  body(): Buffer { return Buffer.concat(this.parts, this.len); }
  msg(type: string): Buffer {
    const body = this.body();
    const h = Buffer.alloc(5);
    h.write(type, 0, "latin1");
    h.writeInt32BE(body.length + 4, 1);
    return Buffer.concat([h, body]);
  }
}

interface Msg { type: string; body: Buffer }

/** One connection. Queries run one batch at a time, in order. */
export class PgConnection {
  private sock!: Socket | TLSSocket;
  private buf: Buffer = Buffer.alloc(0);
  private chunks: Buffer[] = [];
  private pending = 0;
  private tls = false;
  private waiters: Array<(m: Msg | Error) => void> = [];
  private queue: Msg[] = [];
  private chain: Promise<unknown> = Promise.resolve();
  private dead: Error | null = null;
  readonly params = new Map<string, string>();

  constructor(readonly cfg: PgConfig) {}

  static async connect(cfg: PgConfig): Promise<PgConnection> {
    const c = new PgConnection(cfg);
    await c.open();
    return c;
  }

  get closed(): boolean { return this.dead !== null; }

  /** An idle pooled connection does not keep the process alive; one in use does. */
  hold(on: boolean) {
    if (this.dead) return;
    if (on) this.sock.ref(); else this.sock.unref();
  }

  private async open() {
    const cfg = this.cfg;
    let sock: Socket | TLSSocket = await new Promise<Socket>((ok, fail) => {
      const s = netConnect({ host: cfg.host, port: cfg.port });
      const t = setTimeout(() => { s.destroy(); fail(new Error(`connect to ${cfg.host}:${cfg.port} timed out`)); }, cfg.connectTimeoutMs);
      s.once("connect", () => { clearTimeout(t); ok(s); });
      s.once("error", (e) => { clearTimeout(t); fail(e); });
    });
    sock.setNoDelay(true);
    if (cfg.sslmode !== "disable") {
      const req = Buffer.alloc(8);
      req.writeInt32BE(8, 0); req.writeInt32BE(80877103, 4);
      sock.write(req);
      const answer = await new Promise<string>((ok, fail) => {
        sock.once("data", (d) => ok(d.subarray(0, 1).toString("latin1")));
        sock.once("error", fail);
      });
      if (answer === "S") {
        // as libpq: "require" with a root certificate given verifies the chain (verify-ca)
        const mode = cfg.sslmode === "require" && cfg.sslrootcert ? "verify-ca" : cfg.sslmode;
        const verify = mode === "verify-ca" || mode === "verify-full";
        const opts: ConnectionOptions = { socket: sock, servername: /^[\d.:]+$/.test(cfg.host) ? undefined : cfg.host, rejectUnauthorized: verify };
        if (cfg.sslrootcert) opts.ca = readFileSync(cfg.sslrootcert);
        if (mode === "verify-ca") opts.checkServerIdentity = () => undefined;
        sock = await new Promise<TLSSocket>((ok, fail) => {
          const t = tlsConnect(opts, () => ok(t));
          t.once("error", fail);
        });
        this.tls = true;
      } else if (cfg.sslmode === "require" || cfg.sslmode === "verify-ca" || cfg.sslmode === "verify-full") {
        sock.destroy();
        throw new Error(`the server at ${cfg.host}:${cfg.port} does not offer TLS (sslmode=${cfg.sslmode})`);
      }
    }
    this.sock = sock;
    sock.on("data", (d: Buffer) => this.onData(d));
    sock.on("error", (e) => this.fail(e));
    sock.on("close", () => this.fail(new Error("postgres connection closed")));
    const startup = new Writer().i32(196608).str("user").str(cfg.user).str("database").str(cfg.database)
      .str("application_name").str(cfg.applicationName).str("client_encoding").str("UTF8").push(Buffer.from([0])).body();
    const len = Buffer.alloc(4); len.writeInt32BE(startup.length + 4);
    sock.write(Buffer.concat([len, startup]));
    await this.authenticate();
  }

  private onData(d: Buffer) {
    // gather chunks and join them only when a whole message is there (no quadratic re-copying of big rows)
    this.chunks.push(d);
    this.pending += d.length;
    for (;;) {
      const have = this.buf.length + this.pending;
      if (have < 5) return;
      if (this.buf.length < 5) this.join();
      const len = this.buf.readInt32BE(1);
      if (len < 4 || len > MAX_MESSAGE) { this.fail(new Error("postgres sent a malformed message")); this.sock.destroy(); return; }
      if (have < len + 1) return;
      if (this.buf.length < len + 1) this.join();
      const m = { type: String.fromCharCode(this.buf[0]!), body: this.buf.subarray(5, len + 1) };
      this.buf = this.buf.subarray(len + 1);
      if (m.type === "N" || m.type === "A") continue; // notices and notifications are not used
      if (m.type === "S") { const [k, v] = cstrings(m.body); this.params.set(k!, v!); continue; }
      const w = this.waiters.shift();
      if (w) w(m); else this.queue.push(m);
    }
  }

  private join() {
    if (!this.chunks.length) return;
    this.buf = Buffer.concat([this.buf, ...this.chunks]);
    this.chunks = [];
    this.pending = 0;
  }

  private fail(e: Error) {
    if (this.dead) return;
    this.dead = e;
    for (const w of this.waiters.splice(0)) w(e);
  }

  private next(): Promise<Msg> {
    const m = this.queue.shift();
    if (m) return Promise.resolve(m);
    if (this.dead) return Promise.reject(this.dead);
    return new Promise((ok, fail) => this.waiters.push((x) => (x instanceof Error ? fail(x) : ok(x))));
  }

  private send(b: Buffer) {
    if (this.dead) throw this.dead;
    this.sock.write(b);
  }

  private async authenticate() {
    const cfg = this.cfg;
    let scram: { nonce: string; bare: string; salted?: Buffer; auth?: string; verified?: boolean } | null = null;
    for (;;) {
      const m = await this.next();
      if (m.type === "E") throw new PgError(errorFields(m.body));
      if (m.type === "R") {
        const code = m.body.readInt32BE(0);
        if (code === 0) { // AuthenticationOk; wait for ReadyForQuery
          if (scram && !scram.verified) throw new Error("the server did not prove it knows the password (SCRAM final message missing)");
          continue;
        }
        if (code === 3) {
          if (!this.tls) throw new Error("the server asks for the password in clear text over an unencrypted connection; use sslmode=require or better");
          this.send(new Writer().str(this.password()).msg("p"));
          continue;
        }
        if (code === 5) {
          const salt = m.body.subarray(4, 8);
          const inner = createHash("md5").update(this.password() + cfg.user).digest("hex");
          const outer = "md5" + createHash("md5").update(Buffer.concat([Buffer.from(inner), salt])).digest("hex");
          this.send(new Writer().str(outer).msg("p"));
          continue;
        }
        if (code === 10) {
          const mechs = cstrings(m.body.subarray(4));
          if (!mechs.includes("SCRAM-SHA-256")) throw new Error(`no supported SASL mechanism in ${mechs.join(", ")}`);
          const nonce = randomBytes(18).toString("base64");
          const bare = `n=,r=${nonce}`;
          const first = Buffer.from("n,," + bare, "utf8");
          this.send(new Writer().str("SCRAM-SHA-256").i32(first.length).push(first).msg("p"));
          scram = { nonce, bare };
          continue;
        }
        if (code === 11 && scram) {
          const server = m.body.subarray(4).toString("utf8");
          const f = Object.fromEntries(server.split(",").map((kv) => [kv[0], kv.slice(2)]));
          if (!f.r?.startsWith(scram.nonce) || !f.s || !f.i) throw new Error("bad SCRAM server-first message");
          const iters = Number(f.i);
          if (!Number.isInteger(iters) || iters < 1 || iters > MAX_SCRAM_ITERATIONS) throw new Error("bad SCRAM iteration count");
          const salted = pbkdf2Sync(this.password().normalize("NFKC"), Buffer.from(f.s, "base64"), iters, 32, "sha256");
          const clientKey = createHmac("sha256", salted).update("Client Key").digest();
          const storedKey = createHash("sha256").update(clientKey).digest();
          const without = `c=biws,r=${f.r}`;
          const auth = `${scram.bare},${server},${without}`;
          const sig = createHmac("sha256", storedKey).update(auth).digest();
          const proof = Buffer.from(clientKey.map((b, i) => b ^ sig[i]!));
          this.send(new Writer().push(Buffer.from(`${without},p=${proof.toString("base64")}`, "utf8")).msg("p"));
          scram.salted = salted; scram.auth = auth;
          continue;
        }
        if (code === 12 && scram?.salted) {
          const v = m.body.subarray(4).toString("utf8").split(",").find((x) => x.startsWith("v="))?.slice(2) ?? "";
          const serverKey = createHmac("sha256", scram.salted).update("Server Key").digest();
          const want = createHmac("sha256", serverKey).update(scram.auth!).digest();
          const got = Buffer.from(v, "base64");
          if (got.length !== want.length || !timingSafeEqual(got, want)) throw new Error("the server's SCRAM signature does not verify");
          scram.verified = true;
          continue;
        }
        throw new Error(`unsupported postgres authentication request ${code}`);
      }
      if (m.type === "K") continue;
      if (m.type === "Z") return;
    }
  }

  private password(): string {
    if (this.cfg.password === undefined) throw new Error("the server asks for a password; put it in the URL or PGPASSWORD");
    return this.cfg.password;
  }

  /** Run one statement (no parameters may also mean several statements, run as a simple query). */
  query(text: string, params?: PgValue[]): Promise<PgResult> {
    return this.batch([{ text, params }]).then((r) => r[0]!);
  }

  /** Run statements as one pipeline ending in a single Sync: they commit together or not at all (an implicit
   * transaction), with one network round trip. */
  batch(qs: PgQuery[]): Promise<PgResult[]> {
    const run = async () => {
      if (!qs.length) return [];
      const out: Buffer[] = [];
      for (const q of qs) {
        const ps = q.params ?? [];
        out.push(new Writer().str("").str(q.text).i16(0).msg("P"));
        const b = new Writer().str("").str("").i16(0).i16(ps.length);
        for (const p of ps) { const v = encodeParam(p); if (v === null) b.i32(-1); else b.i32(v.length).push(v); }
        out.push(b.i16(0).msg("B"));
        out.push(new Writer().push(Buffer.from("P", "latin1")).str("").msg("D"));
        out.push(new Writer().str("").i32(0).msg("E"));
      }
      out.push(new Writer().msg("S"));
      this.send(Buffer.concat(out));
      const results: PgResult[] = [];
      let cols: Array<{ name: string; oid: number }> = [];
      let rows: PgRow[] = [];
      let err: PgError | null = null;
      for (;;) {
        const m = await this.next();
        switch (m.type) {
          case "1": case "2": case "n": break;
          case "T": cols = rowDescription(m.body); rows = []; break;
          case "D": rows.push(dataRow(m.body, cols)); break;
          case "C": {
            const tag = cstrings(m.body)[0] ?? "";
            const n = Number(tag.split(" ").pop());
            results.push({ rows, command: tag.split(" ")[0] ?? "", rowCount: Number.isFinite(n) ? n : rows.length });
            rows = []; cols = [];
            break;
          }
          case "I": results.push({ rows: [], command: "", rowCount: 0 }); break;
          case "E": err = new PgError(errorFields(m.body)); break;
          case "Z": if (err) throw err; return results;
          default: break;
        }
      }
    };
    const p = this.chain.then(run, run);
    this.chain = p.catch(() => undefined);
    return p;
  }

  /** A simple-protocol query (several statements allowed, no parameters), used for DDL. */
  exec(sql: string): Promise<void> {
    const run = async () => {
      this.send(new Writer().str(sql).msg("Q"));
      let err: PgError | null = null;
      for (;;) {
        const m = await this.next();
        if (m.type === "E") err = new PgError(errorFields(m.body));
        if (m.type === "Z") { if (err) throw err; return; }
      }
    };
    const p = this.chain.then(run, run);
    this.chain = p.catch(() => undefined);
    return p;
  }

  async close() {
    if (this.dead) return;
    try { this.sock.write(new Writer().msg("X")); } catch { /* already gone */ }
    this.sock.end();
    this.fail(new Error("closed"));
  }
}

function cstrings(b: Buffer): string[] {
  const out: string[] = [];
  let s = 0;
  for (let i = 0; i < b.length; i++) if (b[i] === 0) { out.push(b.subarray(s, i).toString("utf8")); s = i + 1; }
  return out;
}

function errorFields(b: Buffer): Record<string, string> {
  const f: Record<string, string> = {};
  let i = 0;
  while (i < b.length && b[i] !== 0) {
    const k = String.fromCharCode(b[i]!);
    const end = b.indexOf(0, i + 1);
    f[k] = b.subarray(i + 1, end).toString("utf8");
    i = end + 1;
  }
  return f;
}

function rowDescription(b: Buffer): Array<{ name: string; oid: number }> {
  const n = b.readInt16BE(0);
  const out: Array<{ name: string; oid: number }> = [];
  let p = 2;
  for (let k = 0; k < n; k++) {
    const end = b.indexOf(0, p);
    const name = b.subarray(p, end).toString("utf8");
    p = end + 1;
    const oid = b.readInt32BE(p + 6);
    p += 18;
    out.push({ name, oid });
  }
  return out;
}

function dataRow(b: Buffer, cols: Array<{ name: string; oid: number }>): PgRow {
  const n = b.readInt16BE(0);
  const row: PgRow = {};
  let p = 2;
  for (let k = 0; k < n; k++) {
    const len = b.readInt32BE(p);
    p += 4;
    const col = cols[k]!;
    if (len < 0) { row[col.name] = null; continue; }
    const raw = b.subarray(p, p + len);
    p += len;
    row[col.name] = parseValue(col.oid, col.oid === OID.bytea ? raw.toString("latin1") : raw.toString("utf8"));
  }
  return row;
}

/** Marks an error as the store being unreachable (retryable), as opposed to a statement the server refused. */
export function markUnavailable(e: unknown): Error {
  const err = e instanceof Error ? e : new Error(String(e));
  (err as any).unavailable = true;
  return err;
}

/** A few connections, handed out one caller at a time. */
export class PgPool {
  private idle: PgConnection[] = [];
  private busy = 0;
  private waiting: Array<(c: PgConnection | Error) => void> = [];
  private reaper: ReturnType<typeof setTimeout> | null = null;
  private lastUsed = new WeakMap<PgConnection, number>();
  /** Idle connections are closed after `idleMs` (default 30 s), so a quiet process holds none. */
  constructor(readonly cfg: PgConfig, readonly max = 4, readonly idleMs = 30_000) {}

  async with<T>(fn: (c: PgConnection) => Promise<T>): Promise<T> {
    let c: PgConnection;
    try {
      c = await this.take();
    } catch (e) {
      throw markUnavailable(e);
    }
    try {
      return await fn(c);
    } catch (e) {
      throw e instanceof PgError ? e : markUnavailable(e); // a statement error is the caller's; anything else is the link
    } finally {
      this.give(c);
    }
  }

  query(text: string, params?: PgValue[]): Promise<PgResult> {
    return this.with((c) => c.query(text, params));
  }

  batch(qs: PgQuery[]): Promise<PgResult[]> {
    return this.with((c) => c.batch(qs));
  }

  private async take(): Promise<PgConnection> {
    for (;;) {
      const c = this.idle.pop();
      if (!c) break;
      if (!c.closed) { this.busy++; c.hold(true); return c; }
    }
    if (this.busy < this.max) {
      this.busy++;
      try {
        return await PgConnection.connect(this.cfg);
      } catch (e) {
        this.busy--;
        throw e;
      }
    }
    return new Promise((ok, fail) => this.waiting.push((c) => {
      if (c instanceof Error) return fail(c);
      this.busy++;
      ok(c);
    }));
  }

  private give(c: PgConnection) {
    this.busy--;
    if (c.closed) {
      const w = this.waiting.shift();
      if (w) { this.busy++; PgConnection.connect(this.cfg).then((n) => { this.busy--; w(n); }, (e) => { this.busy--; w(e); }); }
      return;
    }
    const w = this.waiting.shift();
    if (w) { w(c); return; }
    c.hold(false);
    this.idle.push(c);
    this.lastUsed.set(c, Date.now());
    this.scheduleReap();
  }

  private scheduleReap() {
    if (this.reaper) return;
    this.reaper = setTimeout(() => {
      this.reaper = null;
      const now = Date.now();
      const keep: PgConnection[] = [];
      for (const c of this.idle) {
        if (now - (this.lastUsed.get(c) ?? 0) >= this.idleMs) void c.close();
        else keep.push(c);
      }
      this.idle = keep;
      if (keep.length) this.scheduleReap();
    }, Math.max(50, Math.min(this.idleMs, 5_000)));
    this.reaper.unref?.();
  }

  async close() {
    if (this.reaper) { clearTimeout(this.reaper); this.reaper = null; }
    for (const c of this.idle.splice(0)) await c.close();
  }
}
