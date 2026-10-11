// Scale benchmark for the product engine: one tenant with N memories (real LongMemEval-S turns), SQLite store.
// Measures write throughput, cold start (process restart: load from the store, first recall builds the index),
// warm recall latency (p50/p95) and resident memory.
//   node tools/bench/scale.mjs <set.swbench.jsonl> <N> [encoder spec]
import { createReadStream, mkdtempSync, rmSync } from "node:fs";
import { createInterface } from "node:readline";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Service, SqliteStore, Principal, loadEncoder, chooseRanker } from "../../packages/core/dist/index.js";

const [file, nStr, encSpec = "hashing"] = process.argv.slice(2);
const N = Number(nStr);
const turns = [], queries = [];
const rl = createInterface({ input: createReadStream(file) });
for await (const line of rl) {
  const h = JSON.parse(line);
  for (const q of h.questions) queries.push(q.question);
  for (const m of h.memories) { if (turns.length < N && String(m.content).trim()) turns.push(m); }
  if (turns.length >= N && queries.length >= 60) break;
}
const dir = mkdtempSync(join(tmpdir(), "smartware-scale-"));
const path = join(dir, "smartware.db");
const pr = new Principal("u1", ["team"], "t1");
const enc = loadEncoder(encSpec);
const { params, name } = chooseRanker("auto", enc.name);
const mb = () => Math.round(process.memoryUsage().rss / 1e6);
let svc = new Service(enc, { params, store: new SqliteStore(path), rankerName: name, secretScan: true });
let t0 = performance.now();
for (let i = 0; i < turns.length; i += 500) {
  const batch = turns.slice(i, i + 500).map((m, j) => ({ content: m.content, actor: m.actor, role: m.role, thread: `${m.thread}:${i}`,
    seq: m.seq, readers: (i / 500 + j) % 10 === 0 ? ["group:other"] : null }));
  await svc.remember({ memories: batch, idempotency_key: `b${i}` }, pr);
}
const writeS = (performance.now() - t0) / 1000;
// first recall after the writes (index built incrementally during writes)
t0 = performance.now();
await svc.recall({ query: queries[0] }, pr);
const firstWarm = performance.now() - t0;
svc.close();
// restart: a new process would do exactly this
svc = new Service(enc, { params, store: new SqliteStore(path), rankerName: name });
t0 = performance.now();
await svc.recall({ query: queries[1] }, pr);
const cold = performance.now() - t0;
const lat = [];
for (const q of queries.slice(2, 52)) { const a = performance.now(); await svc.recall({ query: q, budget: { tokens: 2000 } }, pr); lat.push(performance.now() - a); }
lat.sort((a, b) => a - b);
const fast = [];
for (const q of queries.slice(2, 32)) { const a = performance.now(); await svc.recall({ query: q, mode: "fast" }, pr); fast.push(performance.now() - a); }
fast.sort((a, b) => a - b);
const p = (xs, q) => Math.round(xs[Math.min(xs.length - 1, Math.floor(q * xs.length))]);
console.log(JSON.stringify({ N: turns.length, encoder: enc.name, ranker: name, writes_per_s: Math.round(turns.length / writeS),
  first_recall_after_writes_ms: Math.round(firstWarm), cold_start_first_recall_ms: Math.round(cold),
  recall_p50_ms: p(lat, 0.5), recall_p95_ms: p(lat, 0.95), fast_p50_ms: p(fast, 0.5), fast_p95_ms: p(fast, 0.95), rss_mb: mb() }));
svc.close();
rmSync(dir, { recursive: true, force: true });
