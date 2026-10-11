// Postgres store (the engine's Store port), schema orp-pg-1, for hosted and multi-node deployments. Several `orp
// serve` processes can share one database: each tenant is served by one process at a time, which holds the
// tenant's lease, and every write commits only while that lease is still held (a fencing check inside the same
// transaction). Everything stored is data (text, integers, bytea); nothing is evaluated.
import { randomBytes } from "node:crypto";
import { hostname } from "node:os";
import { parsePgUrl, PgPool, redactPgUrl, type PgQuery, type PgRow } from "./pg.js";
import type { Lease, Store, StoreDelta, TenantLeases, TenantRows } from "./store.js";

export const PG_SCHEMA = "orp-pg-1";

export interface PgStoreOptions {
  /** Postgres schema (namespace) for the tables; default "public". */
  schema?: string;
  /** This process's id for leases (default: hostname plus a random suffix) and the URL other nodes can reach it on. */
  nodeId?: string;
  nodeUrl?: string | null;
  /** Lease length; renewed every third of it. Default 30 s. */
  leaseMs?: number;
  /** Connections in the pool (default 4), and how long an idle one is kept (default 30 s). */
  poolSize?: number;
  poolIdleMs?: number;
}

function ddl(s: string): string {
  return `
SELECT pg_advisory_xact_lock(7158000001);
CREATE TABLE IF NOT EXISTS ${s}.orp_meta (key text PRIMARY KEY, value text NOT NULL);
CREATE TABLE IF NOT EXISTS ${s}.orp_memories (iid text PRIMARY KEY, tenant text NOT NULL, ord bigint NOT NULL, record text NOT NULL);
CREATE INDEX IF NOT EXISTS orp_memories_tenant_ord ON ${s}.orp_memories (tenant, ord);
CREATE TABLE IF NOT EXISTS ${s}.orp_claims (tenant text NOT NULL, key text NOT NULL, pos int NOT NULL, iid text NOT NULL, PRIMARY KEY (tenant, key, pos));
CREATE TABLE IF NOT EXISTS ${s}.orp_dedup (hkey text PRIMARY KEY, tenant text NOT NULL, iid text NOT NULL);
CREATE INDEX IF NOT EXISTS orp_dedup_tenant ON ${s}.orp_dedup (tenant);
CREATE TABLE IF NOT EXISTS ${s}.orp_idem (tenant text NOT NULL, principal text NOT NULL, key text NOT NULL, ids text NOT NULL, PRIMARY KEY (tenant, principal, key));
CREATE TABLE IF NOT EXISTS ${s}.orp_blocked (tenant text NOT NULL, chash text NOT NULL, pos int NOT NULL, iid text NOT NULL, PRIMARY KEY (tenant, chash, pos));
CREATE TABLE IF NOT EXISTS ${s}.orp_edges (id text PRIMARY KEY, tenant text NOT NULL, body text NOT NULL);
CREATE INDEX IF NOT EXISTS orp_edges_tenant ON ${s}.orp_edges (tenant);
CREATE TABLE IF NOT EXISTS ${s}.orp_tombstones (iid text PRIMARY KEY, tenant text NOT NULL, body text NOT NULL);
CREATE INDEX IF NOT EXISTS orp_tombstones_tenant ON ${s}.orp_tombstones (tenant);
CREATE TABLE IF NOT EXISTS ${s}.orp_changes (tenant text NOT NULL, cursor bigint NOT NULL, body text NOT NULL, PRIMARY KEY (tenant, cursor));
CREATE TABLE IF NOT EXISTS ${s}.orp_audit (seq bigserial PRIMARY KEY, tenant text NOT NULL, body text NOT NULL);
CREATE INDEX IF NOT EXISTS orp_audit_tenant ON ${s}.orp_audit (tenant, seq);
CREATE TABLE IF NOT EXISTS ${s}.orp_jobs (id text PRIMARY KEY, tenant text NOT NULL, body text NOT NULL);
CREATE INDEX IF NOT EXISTS orp_jobs_tenant ON ${s}.orp_jobs (tenant);
CREATE TABLE IF NOT EXISTS ${s}.orp_feedback (seq bigserial PRIMARY KEY, tenant text NOT NULL, body text NOT NULL);
CREATE TABLE IF NOT EXISTS ${s}.orp_counters (tenant text PRIMARY KEY, tmut bigint NOT NULL, tcount bigint NOT NULL, max_wseq bigint NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ${s}.orp_vectors (encoder text NOT NULL, text_sha text NOT NULL, vec bytea NOT NULL, PRIMARY KEY (encoder, text_sha));
CREATE TABLE IF NOT EXISTS ${s}.orp_leases (tenant text PRIMARY KEY, node text NOT NULL, url text, epoch bigint NOT NULL, expires_at timestamptz NOT NULL);
CREATE OR REPLACE FUNCTION ${s}.orp_fence(t text, n text, e bigint) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM ${s}.orp_leases WHERE tenant = t AND node = n AND epoch = e AND expires_at > now()) THEN
    RAISE EXCEPTION 'orp: lease for tenant % is no longer held by %', t, n USING ERRCODE = 'P0001', HINT = 'orp_lease_lost';
  END IF;
END $$;
INSERT INTO ${s}.orp_meta VALUES ('schema', '${PG_SCHEMA}') ON CONFLICT (key) DO NOTHING;
`;
}

/** A Postgres text[] literal (each element quoted and escaped). */
function textArray(xs: string[]): string {
  return "{" + xs.map((x) => '"' + x.replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"').join(",") + "}";
}

const json = (r: PgRow) => JSON.parse(r.body);

export class PgStore implements Store {
  readonly kind = "postgres";
  readonly location: string;
  readonly pool: PgPool;
  readonly leases: PgLeases;
  private readonly s: string;
  private ready: Promise<void> | null = null;

  constructor(readonly url: string, opts: PgStoreOptions = {}) {
    const schema = opts.schema ?? new URL(url).searchParams.get("schema") ?? "public";
    if (!/^[a-z_][a-z0-9_]{0,62}$/.test(schema)) throw new Error(`bad schema name ${JSON.stringify(schema)}`);
    this.s = `"${schema}"`;
    this.location = redactPgUrl(url);
    this.pool = new PgPool(parsePgUrl(url), opts.poolSize ?? 4, opts.poolIdleMs ?? 30_000);
    this.leases = new PgLeases(this, opts.nodeId ?? `${hostname()}-${randomBytes(4).toString("hex")}`, opts.nodeUrl ?? null, opts.leaseMs ?? 30_000);
  }

  /** Create the tables on first use (idempotent and safe to race: guarded by an advisory lock). */
  init(): Promise<void> {
    if (!this.ready) {
      this.ready = (async () => {
        await this.pool.with(async (c) => {
          if (this.s !== '"public"') await c.exec(`CREATE SCHEMA IF NOT EXISTS ${this.s}`);
          await c.exec(ddl(this.s));
          const r = await c.query(`SELECT value FROM ${this.s}.orp_meta WHERE key = 'schema'`);
          if (r.rows[0]?.value !== PG_SCHEMA) throw new Error(`store schema ${r.rows[0]?.value}, this engine reads ${PG_SCHEMA}`);
        });
      })();
      this.ready.catch(() => { this.ready = null; });
    }
    return this.ready;
  }

  /** @internal table name */
  t(name: string): string {
    return `${this.s}.orp_${name}`;
  }

  async meta(): Promise<Record<string, string>> {
    await this.init();
    // the snapshot secret is created once, by whichever process gets there first
    const [, m, mx] = await this.pool.batch([
      { text: `INSERT INTO ${this.t("meta")} VALUES ('secret', $1) ON CONFLICT (key) DO NOTHING`, params: [randomBytes(16).toString("hex")] },
      { text: `SELECT key, value FROM ${this.t("meta")}` },
      { text: `SELECT COALESCE(MAX(ord), 0) AS m FROM ${this.t("memories")}` },
    ]);
    const out: Record<string, string> = {};
    for (const r of m!.rows) out[r.key] = r.value;
    out.max_ord = String(mx!.rows[0]?.m ?? 0);
    return out;
  }

  async loadTenant(t: string): Promise<TenantRows> {
    await this.init();
    const q = (text: string): PgQuery => ({ text, params: [t] });
    const [mem, claims, dedup, idem, blocked, edges, tombs, jobs, counters, cc, mo] = await this.pool.batch([
      q(`SELECT iid, ord, record FROM ${this.t("memories")} WHERE tenant = $1 ORDER BY ord`),
      q(`SELECT key, pos, iid FROM ${this.t("claims")} WHERE tenant = $1 ORDER BY key, pos`),
      q(`SELECT hkey, iid FROM ${this.t("dedup")} WHERE tenant = $1`),
      q(`SELECT principal, key, ids FROM ${this.t("idem")} WHERE tenant = $1`),
      q(`SELECT chash, pos, iid FROM ${this.t("blocked")} WHERE tenant = $1 ORDER BY chash, pos`),
      q(`SELECT id, body FROM ${this.t("edges")} WHERE tenant = $1`),
      q(`SELECT iid, body FROM ${this.t("tombstones")} WHERE tenant = $1`),
      q(`SELECT id, body FROM ${this.t("jobs")} WHERE tenant = $1`),
      q(`SELECT tmut, tcount, max_wseq FROM ${this.t("counters")} WHERE tenant = $1`),
      q(`SELECT COALESCE(MAX(cursor), 0) AS m FROM ${this.t("changes")} WHERE tenant = $1`),
      q(`SELECT COALESCE(MAX(ord), 0) AS m FROM ${this.t("memories")} WHERE tenant = $1`),
    ]);
    const c = counters!.rows[0];
    return {
      memories: mem!.rows.map((r) => ({ iid: r.iid, ord: Number(r.ord), record: r.record })),
      claims: claims!.rows as any, dedup: dedup!.rows as any, idem: idem!.rows as any, blocked: blocked!.rows as any,
      edges: edges!.rows as any, tombstones: tombs!.rows as any, jobs: jobs!.rows as any,
      counters: c ? { tmut: Number(c.tmut), tcount: Number(c.tcount) } : null,
      changeCount: Number(cc!.rows[0]?.m ?? 0),
      maxOrd: Number(mo!.rows[0]?.m ?? 0),
      maxWseq: c ? Number(c.max_wseq) : 0,
    };
  }

  async flush(d: StoreDelta): Promise<void> {
    await this.init();
    const Q: PgQuery[] = [];
    const add = (text: string, params: any[]) => Q.push({ text, params });
    for (const f of d.fence ?? []) add(`SELECT ${this.s}.orp_fence($1, $2, $3)`, [f.tenant, this.leases.node, f.epoch]);
    if (d.meta.secret) add(`INSERT INTO ${this.t("meta")} VALUES ('secret', $1) ON CONFLICT (key) DO NOTHING`, [d.meta.secret]);
    // global counters only ever move up, whichever process writes them; the next owner of a tenant also gets that
    // tenant's own floor (max_wseq, MAX(ord)) when it loads it
    for (const k of ["epoch", "wseq", "mutations"]) {
      if (d.meta[k] === undefined) continue;
      add(`INSERT INTO ${this.t("meta")} VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = GREATEST(${this.t("meta")}.value::bigint, EXCLUDED.value::bigint)::text`,
        [k, d.meta[k]]);
    }
    for (const id of d.edgesDel) add(`DELETE FROM ${this.t("edges")} WHERE id = $1`, [id]);
    for (const v of d.vectorsDel) add(`DELETE FROM ${this.t("vectors")} WHERE encoder = $1 AND text_sha = $2`, [v.encoder, v.sha]);
    for (const i of d.deleted) add(`DELETE FROM ${this.t("memories")} WHERE iid = $1`, [i]);
    for (const m of d.memories) {
      add(`INSERT INTO ${this.t("memories")} VALUES ($1, $2, $3, $4) ON CONFLICT (iid) DO UPDATE SET tenant = EXCLUDED.tenant, ord = EXCLUDED.ord, record = EXCLUDED.record`,
        [m.iid, m.tenant, m.ord, m.record]);
    }
    for (const c of d.claims) {
      add(`DELETE FROM ${this.t("claims")} WHERE tenant = $1 AND key = $2`, [c.tenant, c.key]);
      c.iids.forEach((iid, pos) => add(`INSERT INTO ${this.t("claims")} VALUES ($1, $2, $3, $4)`, [c.tenant, c.key, pos, iid]));
    }
    for (const h of d.dedupDel) add(`DELETE FROM ${this.t("dedup")} WHERE hkey = $1`, [h]);
    for (const h of d.dedupAdd) add(`INSERT INTO ${this.t("dedup")} VALUES ($1, $2, $3) ON CONFLICT (hkey) DO UPDATE SET tenant = EXCLUDED.tenant, iid = EXCLUDED.iid`, [h.hkey, h.tenant, h.iid]);
    for (const x of d.idem) add(`INSERT INTO ${this.t("idem")} VALUES ($1, $2, $3, $4) ON CONFLICT (tenant, principal, key) DO UPDATE SET ids = EXCLUDED.ids`, [x.tenant, x.principal, x.key, x.ids]);
    for (const b of d.blocked) {
      add(`DELETE FROM ${this.t("blocked")} WHERE tenant = $1 AND chash = $2`, [b.tenant, b.chash]);
      b.iids.forEach((iid, pos) => add(`INSERT INTO ${this.t("blocked")} VALUES ($1, $2, $3, $4)`, [b.tenant, b.chash, pos, iid]));
    }
    for (const e of d.edges) add(`INSERT INTO ${this.t("edges")} VALUES ($1, $2, $3) ON CONFLICT (id) DO UPDATE SET tenant = EXCLUDED.tenant, body = EXCLUDED.body`, [e.id, e.tenant, e.body]);
    for (const x of d.tombstones) add(`INSERT INTO ${this.t("tombstones")} VALUES ($1, $2, $3) ON CONFLICT (iid) DO UPDATE SET tenant = EXCLUDED.tenant, body = EXCLUDED.body`, [x.iid, x.tenant, x.body]);
    for (const c of d.changes) add(`INSERT INTO ${this.t("changes")} VALUES ($1, $2, $3)`, [c.tenant, c.cursor, c.body]);
    for (const a of d.audit) add(`INSERT INTO ${this.t("audit")} (tenant, body) VALUES ($1, $2)`, [a.tenant, a.body]);
    for (const j of d.jobs) add(`INSERT INTO ${this.t("jobs")} VALUES ($1, $2, $3) ON CONFLICT (id) DO UPDATE SET tenant = EXCLUDED.tenant, body = EXCLUDED.body`, [j.id, j.tenant, j.body]);
    for (const f of d.feedback) add(`INSERT INTO ${this.t("feedback")} (tenant, body) VALUES ($1, $2)`, [f.tenant, f.body]);
    const wseq = d.wseqFloor ?? 0;
    for (const c of d.counters) {
      add(`INSERT INTO ${this.t("counters")} VALUES ($1, $2, $3, $4) ON CONFLICT (tenant) DO UPDATE SET tmut = EXCLUDED.tmut, tcount = EXCLUDED.tcount,
        max_wseq = GREATEST(${this.t("counters")}.max_wseq, EXCLUDED.max_wseq)`, [c.tenant, c.tmut, c.tcount, wseq]);
    }
    if (Q.length === (d.fence ?? []).length) return; // nothing to write
    await this.pool.batch(Q); // one round trip, one implicit transaction: all of it or none of it
  }

  async changes(t: string, cursor: number, limit: number): Promise<PgRow[]> {
    await this.init();
    const r = await this.pool.query(`SELECT body FROM ${this.t("changes")} WHERE tenant = $1 AND cursor > $2 ORDER BY cursor LIMIT $3`, [t, cursor, limit]);
    return r.rows.map(json);
  }

  async audit(t: string): Promise<PgRow[]> {
    await this.init();
    return (await this.pool.query(`SELECT body FROM ${this.t("audit")} WHERE tenant = $1 ORDER BY seq`, [t])).rows.map(json);
  }

  async tenantsByRecency(): Promise<string[]> {
    await this.init();
    return (await this.pool.query(`SELECT tenant, MAX(ord) AS o FROM ${this.t("memories")} GROUP BY tenant ORDER BY o DESC`)).rows.map((r) => r.tenant);
  }

  async otherTenant(table: "memories" | "edges" | "tombstones", key: string, t: string): Promise<boolean> {
    await this.init();
    const col = table === "edges" ? "id" : "iid";
    const r = await this.pool.query(`SELECT tenant FROM ${this.t(table)} WHERE ${col} = $1`, [key]);
    return Boolean(r.rows[0] && r.rows[0].tenant !== t);
  }

  async getVector(encoder: string, sha: string): Promise<Float32Array | null> {
    await this.init();
    const r = await this.pool.query(`SELECT vec FROM ${this.t("vectors")} WHERE encoder = $1 AND text_sha = $2`, [encoder, sha]);
    const b = r.rows[0]?.vec as Buffer | undefined;
    if (!b) return null;
    return new Float32Array(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength));
  }

  async putVector(encoder: string, sha: string, v: Float32Array): Promise<void> {
    await this.init();
    await this.pool.query(`INSERT INTO ${this.t("vectors")} VALUES ($1, $2, $3) ON CONFLICT (encoder, text_sha) DO UPDATE SET vec = EXCLUDED.vec`,
      [encoder, sha, Buffer.from(v.buffer, v.byteOffset, v.byteLength)]);
  }

  /** A consistent copy of every table into a new SQLite file (schema orp-store-1), which `orp serve --store` or the
   * Python oracle can open: a portable backup, and the way from Postgres back to a single file. */
  async backupTo(path: string): Promise<void> {
    await this.init();
    const { SqliteStore } = await import("./store.js");
    const out = new SqliteStore(path);
    try {
      await this.pool.with(async (c) => {
        await c.exec("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY");
        try {
          const cols: Record<string, string> = {
            memories: "iid, tenant, ord, record", claims: "tenant, key, pos, iid", dedup: "hkey, tenant, iid",
            idem: "tenant, principal, key, ids", blocked: "tenant, chash, pos, iid", edges: "id, tenant, body",
            tombstones: "iid, tenant, body", changes: "tenant, cursor, body", audit: "seq, tenant, body", jobs: "id, tenant, body",
            feedback: "seq, tenant, body", counters: "tenant, tmut, tcount", vectors: "encoder, text_sha, vec",
          };
          out.db.exec("BEGIN");
          for (const r of (await c.query(`SELECT key, value FROM ${this.t("meta")} WHERE key <> 'schema'`)).rows) {
            out.db.prepare("INSERT OR REPLACE INTO orp_meta VALUES (?, ?)").run(r.key, r.value);
          }
          for (const [table, list] of Object.entries(cols)) {
            const names = list.split(", ");
            const ins = out.db.prepare(`INSERT INTO ${table} (${list}) VALUES (${names.map(() => "?").join(", ")})`);
            for (const r of (await c.query(`SELECT ${list} FROM ${this.t(table)}`)).rows) ins.run(...names.map((n) => (Buffer.isBuffer(r[n]) ? new Uint8Array(r[n]) : r[n])));
          }
          out.db.exec("COMMIT");
        } finally {
          await c.exec("COMMIT");
        }
      });
    } finally {
      out.close();
    }
  }

  async close(): Promise<void> {
    await this.pool.close();
  }
}

/** Tenant leases in the orp_leases table. A lease is taken when free, expired or already ours; the epoch increases
 * whenever it changes hands, and writes are fenced on (tenant, node, epoch, not expired). */
export class PgLeases implements TenantLeases {
  readonly renewEveryMs: number;
  constructor(private readonly store: PgStore, readonly node: string, readonly url: string | null, readonly ttlMs: number) {
    this.renewEveryMs = Math.max(1000, Math.floor(ttlMs / 3));
  }

  async acquire(t: string): Promise<{ lease: Lease } | { owner: string; url: string | null }> {
    await this.store.init();
    const L = this.store.t("leases");
    const r = await this.store.pool.query(
      `INSERT INTO ${L} (tenant, node, url, epoch, expires_at) VALUES ($1, $2, $3, 1, now() + $4 * interval '1 millisecond')
       ON CONFLICT (tenant) DO UPDATE SET node = EXCLUDED.node, url = EXCLUDED.url, expires_at = EXCLUDED.expires_at,
         epoch = CASE WHEN ${L}.node = EXCLUDED.node AND ${L}.expires_at > now() THEN ${L}.epoch ELSE ${L}.epoch + 1 END
       WHERE ${L}.node = EXCLUDED.node OR ${L}.expires_at <= now()
       RETURNING epoch`, [t, this.node, this.url, this.ttlMs]);
    if (r.rows.length) return { lease: { tenant: t, epoch: Number(r.rows[0]!.epoch) } };
    const o = await this.store.pool.query(`SELECT node, url FROM ${L} WHERE tenant = $1`, [t]);
    if (!o.rows.length) return this.acquire(t); // removed in between: try again
    return { owner: o.rows[0]!.node, url: o.rows[0]!.url ?? null };
  }

  async renew(tenants: string[]): Promise<string[]> {
    if (!tenants.length) return [];
    await this.store.init();
    const r = await this.store.pool.query(
      `UPDATE ${this.store.t("leases")} SET expires_at = now() + $3 * interval '1 millisecond'
       WHERE node = $1 AND tenant = ANY($2::text[]) AND expires_at > now() RETURNING tenant`, [this.node, textArray(tenants), this.ttlMs]);
    const ok = new Set(r.rows.map((x) => x.tenant));
    return tenants.filter((t) => !ok.has(t));
  }

  async release(tenants: string[]): Promise<void> {
    if (!tenants.length) return;
    await this.store.init();
    // expire rather than delete, so the epoch keeps counting up and is never handed out twice
    await this.store.pool.query(`UPDATE ${this.store.t("leases")} SET expires_at = now() - interval '1 millisecond'
      WHERE node = $1 AND tenant = ANY($2::text[])`, [this.node, textArray(tenants)]);
  }
}
