// smartware eval: metrics, baselines, intervals, and the reader/judge path against a fake OpenAI-compatible API.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pack, scoreQuery, bootstrap, readSet, runEval, modelFrom } from "../dist/eval.js";

const dir = mkdtempSync(join(tmpdir(), "smartware-eval-"));
const set = join(dir, "tiny.swbench.jsonl");
const mems = [
  { id: "u1", content: "I adopted a dog called Biscuit last spring", actor: "user", role: "user", thread: "s1", seq: 0, observed_at: "2025-04-02T10:00:00" },
  { id: "u2", content: "That's lovely! What breed is Biscuit?", actor: "assistant", role: "assistant", thread: "s1", seq: 1, observed_at: "2025-04-02T10:00:30" },
  { id: "u3", content: "A beagle. He loves the beach.", actor: "user", role: "user", thread: "s1", seq: 2, observed_at: "2025-04-02T10:01:00" },
  { id: "u4", content: "My sister lives in Perth and works as a nurse", actor: "user", role: "user", thread: "s2", seq: 0, observed_at: "2025-06-10T09:00:00" },
  { id: "u5", content: "We should plan the Perth trip for December", actor: "user", role: "user", thread: "s2", seq: 1, observed_at: "2025-06-10T09:01:00" },
];
writeFileSync(set, JSON.stringify({ format: "smartware-bench-1", id: "h1", memories: mems, questions: [
  { id: "q1", question: "What breed is my dog?", as_of: "2025-07-01T00:00:00", answer: "beagle", evidence: ["u3"], category: "single-hop" },
  { id: "q2", question: "Where does my sister live?", as_of: "2025-07-01T00:00:00", answer: "Perth", evidence: ["u4"], category: "single-hop" }] }) + "\n");

test("metrics: greedy skip packing, evidence recall and MRR", () => {
  const items = [{ ids: ["a"], text: "x".repeat(4000) }, { ids: ["b"], text: "short answer" }];
  const p = pack(items, 500);
  assert.deepEqual([...p.covered], ["b"]); // the long item is skipped, the next one still fits
  const m = scoreQuery(items, new Set(["b"]), "short answer", new Map([["b", "short answer"]]), new Map([["b", "t"]]));
  assert.equal(m["recall@500"], 1);
  assert.equal(m.mrr, 0.5);
  const ci = bootstrap([1, 1, 0, 1], [0, 1, 0, 0]);
  assert.ok(ci.lo <= ci.mean && ci.mean <= ci.hi);
});

test("runEval: Smartware and baselines on a tiny set", async () => {
  const sets = await readSet(set);
  const { summary, rows } = await runEval(sets, { systems: ["smartware", "bm25", "dense"], encoder: "hashing", ranker: "auto", k: 50, qaBudget: 2000, concurrency: 2 });
  assert.equal(summary.questions, 2);
  assert.equal(summary.systems.smartware["recall@1000"].mean, 1);
  assert.ok(summary.systems.bm25["recall@1000_vs_smartware"]);
  assert.equal(rows.filter((r) => r.system === "smartware").length, 2);
});

test("reader and judge through an OpenAI-compatible API, with the gold-evidence ceiling", async () => {
  let seenAuth = null;
  const srv = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      seenAuth = req.headers.authorization;
      const msg = JSON.parse(body).messages;
      const sys = msg[0].content, user = msg[1].content;
      let out;
      if (sys.startsWith("Grade")) out = /Candidate answer: .*(beagle|Perth)/i.test(user) ? "CORRECT" : "WRONG";
      else out = /beagle/i.test(user) && /breed/i.test(user) ? "A beagle" : /Perth/.test(user) && /sister/i.test(user) ? "Perth" : "I don't know";
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ choices: [{ message: { content: out } }] }));
    });
  });
  await new Promise((ok) => srv.listen(0, "127.0.0.1", ok));
  process.env.SMARTWARE_EVAL_TEST_KEY = "test-key-not-real";
  try {
    const reader = modelFrom(`openai:fake-model@http://127.0.0.1:${srv.address().port}/v1`, "SMARTWARE_EVAL_TEST_KEY");
    const { summary, rows } = await runEval(await readSet(set), { systems: ["smartware", "bm25"], encoder: "hashing", ranker: "auto", k: 50, reader, qaBudget: 2000, concurrency: 2 });
    assert.equal(seenAuth, "Bearer test-key-not-real");
    assert.equal(summary.systems.ceiling.qa.mean, 1);
    assert.equal(summary.systems.smartware.qa.mean, 1);
    assert.ok(rows.every((r) => !JSON.stringify(r).includes("test-key-not-real")));
  } finally { srv.close(); }
});
