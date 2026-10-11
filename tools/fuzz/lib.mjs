// Shared helpers for the audit repros and fuzzers.
import { Service, SqliteStore, PgStore, Principal, HashingEncoder, rankerParams, chooseRanker } from "../../packages/core/dist/index.js";
import { randomBytes } from "node:crypto";

// ORP_FUZZ_PG=postgres://... runs the same checks against the Postgres store (one fresh schema per run)
const PG = process.env.ORP_FUZZ_PG;
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export { Service, SqliteStore, Principal, HashingEncoder };
export const enc = new HashingEncoder();
export const params = chooseRanker("auto", enc.name).params;

export function tmpdb(tag = "db") {
  if (PG) return `pg:fuzz_${tag.replace(/[^a-z0-9_]/gi, "").toLowerCase()}_${randomBytes(4).toString("hex")}`;
  const d = mkdtempSync(join(tmpdir(), "orp-fuzz-"));
  return join(d, tag + ".sqlite");
}

function storeFor(path) {
  if (!path) return null;
  if (path.startsWith("pg:")) {
    const u = new URL(PG);
    u.searchParams.set("schema", path.slice(3));
    return new PgStore(u.toString(), { leaseMs: 60_000, nodeId: "fuzz-node", exactNodeId: true }); // live and reopened act as one node
  }
  return new SqliteStore(path);
}

export function open(path, opts = {}) {
  return new Service(enc, { params, store: storeFor(path), secretScan: false, ...opts });
}

export const P = (id, groups = [], tenant = "t1") => new Principal(id, groups, tenant);
export const star = (tenant) => new Principal("root", ["*"], tenant);
export const auditor = (tenant) => new Principal("aud", ["auditor", "*"], tenant);

export async function attempt(fn) {
  try {
    const r = await fn();
    if (r && typeof r === "object") delete r.request_id;
    return { ok: r };
  } catch (e) {
    return { err: e.code ?? String(e) , msg: e.message };
  }
}

const TABLES = ["meta", "memories", "claims", "dedup", "idem", "blocked", "edges", "tombstones", "changes", "audit", "jobs", "feedback", "counters"];

/** Every table of a store, as sorted JSON lines (for "store unchanged" checks). */
export async function dumpStore(svc) {
  const out = {};
  if (svc.store.kind === "postgres") {
    for (const t of TABLES) out[t] = (await svc.store.pool.query(`SELECT * FROM ${svc.store.t(t)}`)).rows.map((r) => JSON.stringify(r)).sort();
    return out;
  }
  const db = svc.store.db;
  for (const t of TABLES) out[t] = db.prepare(`SELECT * FROM ${t === "meta" ? "orp_meta" : t}`).all().map((r) => JSON.stringify(r)).sort();
  return out;
}
