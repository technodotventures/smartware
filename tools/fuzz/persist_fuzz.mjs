// Persistence fuzz: random verbs on a store-backed Service; after each step a fresh Service opened on the same file
// must be indistinguishable from the live one (exports, recall packs incl. snapshot, change feeds, audit, internal
// idempotency / re-ingest / counter state). A failing write must leave every table of the store unchanged.
//   node persist_fuzz.mjs [seeds=20] [steps=150] [checkEvery=1]
import { open, tmpdb, star, dumpStore, Principal } from "./lib.mjs";
import { makeGen, TENANTS, QUERIES, AS_OF } from "./gen.mjs";

globalThis.SORT_TOMBS = process.env.SORT_TOMBS === "1";
const [nSeeds = 20, nSteps = 150, every = 1, seed0 = 1] = process.argv.slice(2).map(Number);
const pr = (p) => new Principal(p.id, p.groups, p.tenant);
const canon = (v) => Array.isArray(v) ? v.map(canon) : v && typeof v === "object" ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, canon(v[k])])) : v;
const safe = async (f) => { try { return await f(); } catch (e) { return { ERROR: e.code ?? String(e).slice(0, 200) }; } };
const strip = (o) => { if (o && typeof o === "object" && !Array.isArray(o)) { const c = { ...o }; delete c.request_id; return c; } return o; };

import { call, retenant } from "./persist_fuzz_ops.mjs";
const WRITES = new Set(["remember", "forget", "feedback", "approve", "reflect", "link", "unlink", "import"]);

async function observe(s) {
  const out = {};
  for (const t of TENANTS) {
    const root = star(t), aud = new Principal("aud", ["auditor"], t), alice = new Principal("alice", [], t);
    const ex = strip(await s.export(root));
    ex["edges.jsonl"] = ex["edges.jsonl"].split("\n").sort().join("\n"); // order checked separately
    if (globalThis.SORT_TOMBS) ex.manifest.tombstones = ex.manifest.tombstones.map((x) => JSON.stringify(canon(x))).sort(); // order checked separately
    out[t + ":export"] = ex;
    for (const q of QUERIES.slice(0, 3)) for (const asOf of AS_OF.slice(0, 2)) for (const who of [root, alice]) {
      const r = strip(await s.recall({ query: q, as_of: asOf, include: { history: true } }, who));
      out[`${t}:recall:${who.id}:${q}:${asOf}`] = r;
    }
    out[t + ":changes"] = strip(await s.changes(0, aud, 100000));
    out[t + ":audit"] = strip(await s.auditLog(aud));
    out[t + ":list"] = strip(await s.list({ limit: 1000 }, root));
    out[t + ":thread"] = strip(await s.thread("th1", root));
    for (const subj of ["alice", "bob"]) out[`${t}:history:${subj}`] = await safe(async () => strip(await s.history(subj, "city", root)));
  }
  // internal state the API exposes only through later writes (replays, re-ingest blocks, dedup, counters, jobs)
  const E = s.engine;
  const sortMap = (m) => [...m].map(([k, v]) => [k, JSON.stringify(canon(v))]).sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  out.internal = {
    keys: sortMap(s.keys), blocked: sortMap(s.blocked), jobs: sortMap(s.jobs), tombs: sortMap(s.tombstones),
    edges: sortMap(new Map([...s.edgesMap].map(([k, v]) => [k, v]))),
    claims: sortMap(new Map([...E.claims].map(([k, v]) => [k, v.filter((i) => E.mem.has(i))]))),
    hash: sortMap(E.hash),
    tmut: sortMap(new Map([...E.tmut].filter(([, v]) => v))), tcount: sortMap(new Map([...E.tcount].filter(([, v]) => v))), epoch: E.epoch, // wseq/mutations may run ahead after a failed write (gaps only)
    changeCount: sortMap(s.changeCount),
    order: TENANTS.map((t) => (E.order.get(t) ?? []).join(",")),
  };
  return out;
}

function diff(a, b, path = "") {
  if (JSON.stringify(canon(a)) === JSON.stringify(canon(b))) return [];
  if (Array.isArray(a) && Array.isArray(b) && a.length === b.length) {
    const out = [];
    a.forEach((x, i) => out.push(...diff(x, b[i], path + "[" + i + "]")));
    return out;
  }
  if (a && b && typeof a === "object" && typeof b === "object" && !Array.isArray(a)) {
    const out = [];
    for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) out.push(...diff(a[k], b[k], path + "." + k));
    return out;
  }
  return [`${path}: live=${JSON.stringify(a)?.slice(0, 300)} fresh=${JSON.stringify(b)?.slice(0, 300)}`];
}

let failures = 0;
const seen = new Map();
for (let seed = seed0; seed < seed0 + nSeeds; seed++) {
  const db = tmpdb("p" + seed);
  let live = open(db);
  const g = makeGen(seed);
  const state = { edges: [], rand: g.r.i };
  const log = [];
  const versions = (t, id) => { const m = live.engine.mem.get(`${id}~${t}`); return m ? m.version : null; };
  for (let step = 0; step < nSteps; step++) {
    const op = g.next(versions);
    for (const t of TENANTS) live.ensureTenant(t); // so internal maps are comparable
    const before = WRITES.has(op.verb) ? dumpStore(live) : null;
    let res;
    try { res = { ok: await call(live, op, state) }; } catch (e) { res = { err: e.code ?? "CRASH:" + String(e?.stack ?? e).slice(0, 400) }; }
    log.push({ step, op, res: res.err ?? "ok" });
    globalThis.STATS ??= {}; const sk = op.verb + ":" + (res.err ?? "ok"); globalThis.STATS[sk] = (globalThis.STATS[sk] ?? 0) + 1;
    if (res.err && String(res.err).startsWith("CRASH")) {
      const k = "crash:" + op.verb + ":" + String(res.err).split("\n")[0];
      if (!seen.has(k)) { seen.set(k, { seed, step, op, err: res.err }); console.log("CRASH", seed, step, JSON.stringify(op), res.err); }
    }
    if (res.err && before) {
      const after = dumpStore(live);
      const d = diff(before, after);
      if (d.length) { failures++; const k = "errstore:" + op.verb + ":" + res.err; if (!seen.has(k)) { seen.set(k, 1); console.log(`STORE CHANGED BY FAILED ${op.verb} (${res.err}) seed=${seed} step=${step}`, JSON.stringify(op), d.slice(0, 5)); } }
    }
    if (step % every !== 0 && step !== nSteps - 1) continue;
    const fresh = open(db);
    for (const t of TENANTS) fresh.ensureTenant(t);
    const A = await observe(live), B = await observe(fresh);
    fresh.close();
    const d = diff(A, B);
    if (d.length) {
      failures++;
      const k = "div:" + op.verb + ":" + d.map((x) => x.split(":")[0]).slice(0, 3).join("|");
      if (!seen.has(k)) {
        seen.set(k, { seed, step, op, d });
        console.log(`\nDIVERGENCE live vs reopened after seed=${seed} step=${step} verb=${op.verb} res=${res.err ?? "ok"}`);
        console.log("op:", JSON.stringify(op));
        console.log("recent:", JSON.stringify(log.slice(-6).map((l) => [l.step, l.op.verb, l.res])));
        for (const x of [...d.filter((y) => !y.includes(".snapshot:")).slice(0, 8), ...d.filter((y) => y.includes(".snapshot:")).slice(0, 1)]) console.log("  ", x);
      }
      // resync: continue from the reopened state so later steps test fresh ground
      live.close();
      live = open(db);
    }
  }
  live.close();
}
const stats = {};
for (const [k, v] of Object.entries(globalThis.STATS ?? {})) stats[k] = v;
console.log(JSON.stringify(stats));
console.log(`\ndone: seeds=${nSeeds} steps=${nSteps} failing checks=${failures} distinct=${seen.size}`);
process.exitCode = failures ? 1 : 0;
