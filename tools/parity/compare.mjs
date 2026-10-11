// Differential parity, product side: rebuild each haystack in the TypeScript engine from the oracle's own records,
// recall every question, and compare packs with the oracle's.
//   node tools/parity/compare.mjs <dump.jsonl.gz> <config: smartware-linear-1 | smartware-rrf-1 | fast> [--verbose]
import { createReadStream } from "node:fs";
import { createGunzip } from "node:zlib";
import { createInterface } from "node:readline";
import { Engine, SifEncoder, memoryFromJson, rankerParams, recallRequest, parseTime } from "../../packages/core/dist/index.js";

const [file, config, ...flags] = process.argv.slice(2);
const verbose = flags.includes("--verbose");
const enc = new SifEncoder(new URL("../../results/cache/sif-ppmi-256.swenc", import.meta.url).pathname);
let params = rankerParams(config === "fast" ? "smartware-rrf-1" : config);
if (config === "fast") params = { ...params, w_prf: 0, ranker: "rrf" };

const st = { q: 0, idsExact: 0, textExact: 0, top1: 0, setOverlap: 0, scoreMaxDiff: 0, covDiff: 0, firstDiv: [] };
const rl = createInterface({ input: createReadStream(file).pipe(createGunzip()), crlfDelay: Infinity });
const t0 = Date.now();
let recallMs = 0;
for await (const line of rl) {
  const task = JSON.parse(line);
  const eng = new Engine(enc, params);
  eng.remember(task.memories.map((d) => memoryFromJson(d)));
  for (const q of task.queries) {
    const t1 = performance.now();
    const pack = eng.recall(recallRequest({ query: q.query, as_of: q.as_of ? parseTime(q.as_of) : null, budget_tokens: null, max_items: task.k }));
    recallMs += performance.now() - t1;
    st.q++;
    const a = q.items.map((i) => i.ids.join("|")), b = pack.items.map((i) => i.memory_ids.join("|"));
    if (JSON.stringify(a) === JSON.stringify(b)) st.idsExact++;
    else {
      const d = a.findIndex((x, i) => x !== b[i]);
      st.firstDiv.push(d < 0 ? Math.min(a.length, b.length) : d);
      if (verbose && st.firstDiv.length <= 5) {
        console.log("DIFF", q.qid, q.query, "first divergence at", d);
        for (let i = Math.max(0, d - 1); i < Math.min(d + 3, Math.max(a.length, b.length)); i++) {
          console.log("  oracle ", i, a[i], q.items[i]?.score, JSON.stringify(q.items[i]?.why).slice(0, 300));
          console.log("  product", i, b[i], pack.items[i]?.score, JSON.stringify(pack.items[i]?.why).slice(0, 300));
        }
      }
    }
    if (a[0] === b[0]) st.top1++;
    const sa = new Set(a);
    st.setOverlap += b.filter((x) => sa.has(x)).length / Math.max(1, a.length);
    if (JSON.stringify(q.items.map((i) => i.text)) === JSON.stringify(pack.items.map((i) => i.text))) st.textExact++;
    for (let i = 0; i < Math.min(q.items.length, pack.items.length); i++) {
      if (a[i] === b[i]) st.scoreMaxDiff = Math.max(st.scoreMaxDiff, Math.abs(q.items[i].score - pack.items[i].score));
    }
    st.covDiff = Math.max(st.covDiff, Math.abs(q.coverage - pack.coverage));
  }
}
const pct = (x) => (100 * x / st.q).toFixed(2) + "%";
console.log(JSON.stringify({ file, config, questions: st.q, ids_exact: pct(st.idsExact), text_exact: pct(st.textExact),
  top1_same: pct(st.top1), mean_set_overlap: pct(st.setOverlap), max_score_diff_same_item: st.scoreMaxDiff,
  max_coverage_diff: st.covDiff, first_divergence_ranks: st.firstDiv.slice(0, 40),
  recall_ms_per_query: +(recallMs / st.q).toFixed(2), total_s: (Date.now() - t0) / 1000 }, null, 1));
