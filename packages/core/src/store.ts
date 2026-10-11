// SQLite store (the engine's Store port), schema orp-store-1, shared with the Python oracle (orp/store.py), so
// either engine opens a file the other wrote (one writer at a time). Uses Node's built-in node:sqlite: no native
// module to compile, nothing runs at install. Everything stored is data (JSON text, integers); nothing is evaluated.
//
// Writes are incremental: each verb collects a journal of what it changed and flush() writes exactly that, in one
// transaction. Tenants load lazily, so a hosted instance holds only the tenants it is serving.
import { createRequire } from "node:module";
import { chmodSync, existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";

export const SCHEMA = "orp-store-1";

const DDL = `
CREATE TABLE IF NOT EXISTS orp_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memories (iid TEXT PRIMARY KEY, tenant TEXT NOT NULL, ord INTEGER NOT NULL, record TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS memories_tenant_ord ON memories (tenant, ord);
CREATE TABLE IF NOT EXISTS claims (tenant TEXT NOT NULL, key TEXT NOT NULL, pos INTEGER NOT NULL, iid TEXT NOT NULL,
                                   PRIMARY KEY (tenant, key, pos));
CREATE TABLE IF NOT EXISTS dedup (hkey TEXT PRIMARY KEY, tenant TEXT NOT NULL, iid TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS dedup_tenant ON dedup (tenant);
CREATE TABLE IF NOT EXISTS idem (tenant TEXT NOT NULL, principal TEXT NOT NULL, key TEXT NOT NULL, ids TEXT NOT NULL,
                                 PRIMARY KEY (tenant, principal, key));
CREATE TABLE IF NOT EXISTS blocked (tenant TEXT NOT NULL, chash TEXT NOT NULL, pos INTEGER NOT NULL, iid TEXT NOT NULL,
                                    PRIMARY KEY (tenant, chash, pos));
CREATE TABLE IF NOT EXISTS edges (id TEXT PRIMARY KEY, tenant TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tombstones (iid TEXT PRIMARY KEY, tenant TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS changes (tenant TEXT NOT NULL, cursor INTEGER NOT NULL, body TEXT NOT NULL,
                                    PRIMARY KEY (tenant, cursor));
CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY, tenant TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS audit_tenant ON audit (tenant, seq);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, tenant TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feedback (seq INTEGER PRIMARY KEY, tenant TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters (tenant TEXT PRIMARY KEY, tmut INTEGER NOT NULL, tcount INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS vectors (encoder TEXT NOT NULL, text_sha TEXT NOT NULL, vec BLOB NOT NULL,
                                    PRIMARY KEY (encoder, text_sha));
`;

type Row = Record<string, any>;

let sqliteMod: any;
function sqlite(): any {
  if (!sqliteMod) {
    // node:sqlite prints an ExperimentalWarning on load (Node 22-24); keep the server's stderr clean.
    const orig = process.emitWarning;
    process.emitWarning = ((w: any, ...rest: any[]) => {
      const type = typeof rest[0] === "string" ? rest[0] : rest[0]?.type;
      if (type === "ExperimentalWarning" && String(w).includes("SQLite")) return;
      return (orig as any).call(process, w, ...rest);
    }) as typeof process.emitWarning;
    try {
      sqliteMod = createRequire(import.meta.url)("node:sqlite");
    } finally {
      process.emitWarning = orig;
    }
  }
  return sqliteMod;
}

export interface TenantRows {
  memories: Array<{ iid: string; ord: number; record: string }>;
  claims: Array<{ key: string; pos: number; iid: string }>;
  dedup: Array<{ hkey: string; iid: string }>;
  idem: Array<{ principal: string; key: string; ids: string }>;
  blocked: Array<{ chash: string; pos: number; iid: string }>;
  edges: Array<{ id: string; body: string }>;
  tombstones: Array<{ iid: string; body: string }>;
  jobs: Array<{ id: string; body: string }>;
  counters: { tmut: number; tcount: number } | null;
  changeCount: number;
  /** Shared stores: the tenant's highest order and write sequence. */
  maxOrd?: number;
  maxWseq?: number;
}

export interface StoreDelta {
  meta: Record<string, string>;
  /** Shared stores: commit only while these leases are still held (fencing). */
  fence?: Array<{ tenant: string; epoch: number }>;
  /** Shared stores: this node's write sequence, kept per tenant as a floor for the next owner. */
  wseqFloor?: number;
  edgesDel: string[];
  vectorsDel: Array<{ encoder: string; sha: string }>;
  memories: Array<{ iid: string; tenant: string; ord: number; record: string }>;
  deleted: string[];
  claims: Array<{ tenant: string; key: string; iids: string[] }>;
  dedupAdd: Array<{ hkey: string; tenant: string; iid: string }>;
  dedupDel: string[];
  idem: Array<{ tenant: string; principal: string; key: string; ids: string }>;
  blocked: Array<{ tenant: string; chash: string; iids: string[] }>;
  edges: Array<{ id: string; tenant: string; body: string }>;
  tombstones: Array<{ iid: string; tenant: string; body: string }>;
  changes: Array<{ tenant: string; cursor: number; body: string }>;
  audit: Array<{ tenant: string; body: string }>;
  jobs: Array<{ id: string; tenant: string; body: string }>;
  feedback: Array<{ tenant: string; body: string }>;
  counters: Array<{ tenant: string; tmut: number; tcount: number }>;
}

export function emptyDelta(): StoreDelta {
  return { meta: {}, edgesDel: [], vectorsDel: [], memories: [], deleted: [], claims: [], dedupAdd: [], dedupDel: [], idem: [],
    blocked: [], edges: [], tombstones: [], changes: [], audit: [], jobs: [], feedback: [], counters: [] };
}

/** The engine's Store port. Every method is async so a networked store (Postgres) fits the same service code; the
 * SQLite store answers synchronously underneath. A store that can be shared by several server processes also
 * implements tenant leases (one writer per tenant at a time, fenced on every write). */
export interface Store {
  readonly kind: string;
  /** Where it is, for logs (never includes a password). */
  readonly location: string;
  /** Global metadata: schema, the snapshot secret, counters (max_ord included). */
  meta(): Promise<Record<string, string>>;
  loadTenant(t: string): Promise<TenantRows>;
  /** Write one verb's changes atomically. */
  flush(d: StoreDelta): Promise<void>;
  changes(t: string, cursor: number, limit: number): Promise<Row[]>;
  audit(t: string): Promise<Row[]>;
  /** Tenants, most recently written first (for background warm-up). */
  tenantsByRecency(): Promise<string[]>;
  /** Whether a row with this key exists under another tenant. */
  otherTenant(table: "memories" | "edges" | "tombstones", key: string, t: string): Promise<boolean>;
  getVector(encoder: string, sha: string): Promise<Float32Array | null>;
  putVector(encoder: string, sha: string, v: Float32Array): Promise<void>;
  /** A consistent copy of the whole store into a new SQLite file. */
  backupTo?(path: string): Promise<void>;
  /** Tenant leases, for stores shared by several processes. */
  leases?: TenantLeases;
  close(): Promise<void> | void;
}

export interface Lease { tenant: string; epoch: number }
export interface TenantLeases {
  /** This process's node id and the URL other nodes can reach it on. */
  readonly node: string;
  url: string | null;
  /** Lease length in milliseconds, and how often the holder renews. */
  readonly ttlMs: number;
  readonly renewEveryMs: number;
  /** Take the tenant's lease if it is free, expired or already ours; otherwise say who holds it. */
  acquire(t: string): Promise<{ lease: Lease } | { owner: string; url: string | null }>;
  /** Extend our leases; returns the tenants whose lease was lost. */
  renew(tenants: string[]): Promise<string[]>;
  release(tenants: string[]): Promise<void>;
}

const COL: Record<string, string> = { memories: "iid", edges: "id", tombstones: "iid" };

export class SqliteStore implements Store {
  readonly kind = "sqlite";
  readonly db: any;
  get location(): string { return this.path; }
  private st = new Map<string, any>();

  constructor(readonly path: string) {
    const { DatabaseSync } = sqlite();
    const isNew = path !== ":memory:" && !existsSync(path);
    if (path !== ":memory:") mkdirSync(dirname(resolve(path)), { recursive: true, mode: 0o700 });
    this.db = new DatabaseSync(path);
    // the file holds every tenant's memories and the snapshot secret: owner-only from the start (SQLite gives its
    // -wal and -shm files the same permissions)
    if (isNew) chmodSync(path, 0o600);
    this.db.exec("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;");
    this.db.exec(DDL);
    const row = this.q("SELECT value FROM orp_meta WHERE key='schema'").get() as Row | undefined;
    if (!row) this.q("INSERT INTO orp_meta VALUES ('schema', ?)").run(SCHEMA);
    else if (row.value !== SCHEMA) throw new Error(`${path}: store schema ${row.value}, this engine reads ${SCHEMA}`);
  }

  private q(sql: string): any {
    let s = this.st.get(sql);
    if (!s) this.st.set(sql, (s = this.db.prepare(sql)));
    return s;
  }

  async meta(): Promise<Record<string, string>> {
    return this.metaSync();
  }

  metaSync(): Record<string, string> {
    const out: Record<string, string> = {};
    for (const r of this.q("SELECT key, value FROM orp_meta").all() as Row[]) out[r.key] = r.value;
    const mx = this.q("SELECT MAX(ord) AS m FROM memories").get() as Row;
    out.max_ord = String(mx?.m ?? 0);
    return out;
  }

  tenants(): string[] {
    return (this.q("SELECT DISTINCT tenant FROM memories UNION SELECT tenant FROM counters").all() as Row[]).map((r) => r.tenant);
  }

  async loadTenant(t: string): Promise<TenantRows> {
    return this.loadTenantSync(t);
  }

  async tenantsByRecency(): Promise<string[]> {
    return (this.q("SELECT tenant, MAX(ord) AS o FROM memories GROUP BY tenant ORDER BY o DESC").all() as Row[]).map((r) => r.tenant);
  }

  async otherTenant(table: "memories" | "edges" | "tombstones", key: string, t: string): Promise<boolean> {
    const r = this.q(`SELECT tenant FROM ${table} WHERE ${COL[table]} = ?`).get(key) as Row | undefined;
    return Boolean(r && r.tenant !== t);
  }

  async backupTo(path: string): Promise<void> {
    const { backup } = sqlite();
    await backup(this.db, path);
    chmodSync(path, 0o600);
  }

  loadTenantSync(t: string): TenantRows {
    const all = (sql: string) => this.q(sql).all(t) as Row[];
    const c = this.q("SELECT tmut, tcount FROM counters WHERE tenant = ?").get(t) as Row | undefined;
    const cc = this.q("SELECT MAX(cursor) AS m FROM changes WHERE tenant = ?").get(t) as Row;
    return {
      memories: all("SELECT iid, ord, record FROM memories WHERE tenant = ? ORDER BY ord") as any,
      claims: all("SELECT key, pos, iid FROM claims WHERE tenant = ? ORDER BY key, pos") as any,
      dedup: all("SELECT hkey, iid FROM dedup WHERE tenant = ?") as any,
      idem: all("SELECT principal, key, ids FROM idem WHERE tenant = ?") as any,
      blocked: all("SELECT chash, pos, iid FROM blocked WHERE tenant = ? ORDER BY chash, pos") as any,
      edges: all("SELECT id, body FROM edges WHERE tenant = ?") as any,
      tombstones: all("SELECT iid, body FROM tombstones WHERE tenant = ?") as any,
      jobs: all("SELECT id, body FROM jobs WHERE tenant = ?") as any,
      counters: c ? { tmut: Number(c.tmut), tcount: Number(c.tcount) } : null,
      changeCount: Number(cc?.m ?? 0),
    };
  }

  async changes(t: string, cursor: number, limit: number): Promise<Row[]> {
    return (this.q("SELECT body FROM changes WHERE tenant = ? AND cursor > ? ORDER BY cursor LIMIT ?")
      .all(t, cursor, limit) as Row[]).map((r) => JSON.parse(r.body));
  }

  async audit(t: string): Promise<Row[]> {
    return (this.q("SELECT body FROM audit WHERE tenant = ? ORDER BY seq").all(t) as Row[]).map((r) => JSON.parse(r.body));
  }

  async getVector(encoder: string, sha: string): Promise<Float32Array | null> {
    const r = this.q("SELECT vec FROM vectors WHERE encoder = ? AND text_sha = ?").get(encoder, sha) as Row | undefined;
    if (!r) return null;
    const b = r.vec as Uint8Array;
    return new Float32Array(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength));
  }

  async putVector(encoder: string, sha: string, v: Float32Array): Promise<void> {
    this.q("INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)").run(encoder, sha, new Uint8Array(v.buffer, v.byteOffset, v.byteLength));
  }

  async flush(d: StoreDelta): Promise<void> {
    this.flushSync(d);
  }

  flushSync(d: StoreDelta) {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      for (const [k, v] of Object.entries(d.meta)) this.q("INSERT OR REPLACE INTO orp_meta VALUES (?, ?)").run(k, v);
      for (const id of d.edgesDel ?? []) this.q("DELETE FROM edges WHERE id = ?").run(id);
      for (const v of d.vectorsDel ?? []) this.q("DELETE FROM vectors WHERE encoder = ? AND text_sha = ?").run(v.encoder, v.sha);
      // deletes before inserts, so an id purged and then re-used inside one verb ends up present
      for (const i of d.deleted) this.q("DELETE FROM memories WHERE iid = ?").run(i);
      for (const m of d.memories) this.q("INSERT OR REPLACE INTO memories VALUES (?, ?, ?, ?)").run(m.iid, m.tenant, m.ord, m.record);
      for (const c of d.claims) {
        this.q("DELETE FROM claims WHERE tenant = ? AND key = ?").run(c.tenant, c.key);
        c.iids.forEach((iid, pos) => this.q("INSERT INTO claims VALUES (?, ?, ?, ?)").run(c.tenant, c.key, pos, iid));
      }
      for (const h of d.dedupDel) this.q("DELETE FROM dedup WHERE hkey = ?").run(h);
      for (const h of d.dedupAdd) this.q("INSERT OR REPLACE INTO dedup VALUES (?, ?, ?)").run(h.hkey, h.tenant, h.iid);
      for (const x of d.idem) this.q("INSERT OR REPLACE INTO idem VALUES (?, ?, ?, ?)").run(x.tenant, x.principal, x.key, x.ids);
      for (const b of d.blocked) {
        this.q("DELETE FROM blocked WHERE tenant = ? AND chash = ?").run(b.tenant, b.chash);
        b.iids.forEach((iid, pos) => this.q("INSERT INTO blocked VALUES (?, ?, ?, ?)").run(b.tenant, b.chash, pos, iid));
      }
      for (const e of d.edges) this.q("INSERT OR REPLACE INTO edges VALUES (?, ?, ?)").run(e.id, e.tenant, e.body);
      for (const x of d.tombstones) this.q("INSERT OR REPLACE INTO tombstones VALUES (?, ?, ?)").run(x.iid, x.tenant, x.body);
      for (const c of d.changes) this.q("INSERT INTO changes VALUES (?, ?, ?)").run(c.tenant, c.cursor, c.body);
      for (const a of d.audit) this.q("INSERT INTO audit (tenant, body) VALUES (?, ?)").run(a.tenant, a.body);
      for (const j of d.jobs) this.q("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?)").run(j.id, j.tenant, j.body);
      for (const f of d.feedback) this.q("INSERT INTO feedback (tenant, body) VALUES (?, ?)").run(f.tenant, f.body);
      for (const c of d.counters) this.q("INSERT OR REPLACE INTO counters VALUES (?, ?, ?)").run(c.tenant, c.tmut, c.tcount);
      this.db.exec("COMMIT");
    } catch (e) {
      this.db.exec("ROLLBACK");
      throw e;
    }
  }

  close() {
    this.db.close();
  }
}
