// Shared helpers for the audit repros and fuzzers.
import { Service, SqliteStore, Principal, HashingEncoder, rankerParams, chooseRanker } from "../../packages/core/dist/index.js";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export { Service, SqliteStore, Principal, HashingEncoder };
export const enc = new HashingEncoder();
export const params = chooseRanker("auto", enc.name).params;

export function tmpdb(tag = "db") {
  const d = mkdtempSync(join(tmpdir(), "orp-fuzz-"));
  return join(d, tag + ".sqlite");
}

export function open(path, opts = {}) {
  return new Service(enc, { params, store: path ? new SqliteStore(path) : null, secretScan: false, ...opts });
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

/** Every table of a store, as sorted JSON lines (for "store unchanged" checks). */
export function dumpStore(svc) {
  const db = svc.store.db;
  const out = {};
  for (const t of ["orp_meta", "memories", "claims", "dedup", "idem", "blocked", "edges", "tombstones", "changes", "audit", "jobs", "feedback", "counters"]) {
    out[t] = db.prepare(`SELECT * FROM ${t}`).all().map((r) => JSON.stringify(r)).sort();
  }
  return out;
}
