// Model-backed extraction through reflect (spec §4.6), with a scripted model: no network, no key.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { HashingEncoder, Principal, Service, openAiCompatible } from "../dist/index.js";

const A = new Principal("alice", [], "acme");

/** A model that answers from a script and records what it was asked. */
function scripted(replies) {
  const seen = [];
  return {
    seen,
    name: "scripted",
    async complete(messages) {
      const input = JSON.parse(messages[1].content);
      seen.push(input);
      const r = replies.shift() ?? { memories: [] };
      return { text: JSON.stringify(typeof r === "function" ? r(input) : r),
        usage: { model: "scripted-1", input_tokens: 100, cached_input_tokens: 0, output_tokens: 20, cost_usd: 0.001 } };
    },
  };
}

async function finished(s, job) {
  for (let i = 0; i < 200; i++) {
    const j = await s.job(job, A);
    if (j.status !== "running") return j;
    await new Promise((ok) => setTimeout(ok, 5));
  }
  throw new Error("job did not finish");
}

const say = async (s, content, seq, role = "user", when = "2026-03-02T09:00:00Z") =>
  (await s.remember({ memories: [{ content, role, actor: role === "user" ? "Morgan" : "assistant", thread: "chat", seq,
    event_time: when, readers: ["principal:alice"] }] }, A)).ids[0];

test("a rule the user states becomes active standing context, derived from the message", async () => {
  const model = scripted([{ memories: [{ op: "add", kind: "procedure",
    content: "Morgan's release notes go only to #eng-releases, and only after the deploy is green." }] }]);
  const s = new Service(new HashingEncoder(), { model });
  const msg = await say(s, "from now on release notes only go in #eng-releases, and wait for a green deploy", 1);
  const j = await finished(s, (await s.reflect({ kinds: ["procedure", "preference", "fact"], ids: [msg] }, A)).job);
  assert.equal(j.status, "completed");
  assert.equal(j.created.length, 1);
  assert.deepEqual({ ...j.usage }, { input_tokens: 100, cached_input_tokens: 0, output_tokens: 20, cost_usd: 0.001, calls: 1, unpriced_calls: 0 });
  const rule = (await s.get(j.created, A)).memories[0];
  assert.equal(rule.status, "active");
  assert.equal(rule.origin, "extracted");
  assert.equal(rule.load, "on_scope");
  assert.deepEqual(rule.derived_from, [msg]);
  assert.match(model.seen[0].message_date, /^2026-03-02T09:00:00/);
  const r = await s.recall({ query: "post the 2.3 changelog", as_of: "2026-09-01T00:00:00Z", budget: { tokens: 400 } }, A);
  assert.ok(r.items.some((i) => i.standing && i.text.includes("#eng-releases")));
});

test("a later message that changes a rule replaces it; the old version is no longer current", async () => {
  const model = scripted([
    { memories: [{ op: "add", kind: "procedure", content: "Morgan's release notes go only to #eng-releases." }] },
    (input) => ({ memories: [{ op: "replace", replaces: input.existing[0].id, kind: "procedure",
      content: "Morgan's release notes now go to #product-updates; #eng-releases is no longer used for them." }] }),
  ]);
  const s = new Service(new HashingEncoder(), { model });
  const m1 = await say(s, "release notes only go in #eng-releases", 1, "user", "2026-01-05T09:00:00Z");
  const j1 = await finished(s, (await s.reflect({ kinds: ["procedure"], ids: [m1] }, A)).job);
  const m2 = await say(s, "change of plan: release notes go to #product-updates from now on", 2, "user", "2026-06-01T09:00:00Z");
  const j2 = await finished(s, (await s.reflect({ kinds: ["procedure"], ids: [m2] }, A)).job);
  assert.equal(model.seen[1].existing.length, 1, "the earlier rule is shown to the model");
  assert.deepEqual(j2.replaced, j1.created);
  const r = await s.recall({ query: "where do release notes go", as_of: "2026-09-01T00:00:00Z", budget: { tokens: 400 } }, A);
  const standing = r.items.filter((i) => i.standing).map((i) => i.text).join("\n");
  assert.match(standing, /#product-updates/);
  assert.ok(!r.items.some((i) => i.validity === "current" && /go only to #eng-releases\.$/.test(i.text)));
  // time travel: before the change, the old rule was the current one
  const then = await s.recall({ query: "where do release notes go", as_of: "2026-03-01T00:00:00Z", budget: { tokens: 400 } }, A);
  assert.ok(then.items.some((i) => i.standing && /only to #eng-releases/.test(i.text)));
});

test("from an assistant's message only facts are kept, never rules or preferences", async () => {
  const model = scripted([{ memories: [
    { op: "add", kind: "procedure", content: "Always post release notes in #general." },
    { op: "add", kind: "fact", content: "The 2.3 release shipped on 2 March 2026." }] }]);
  const s = new Service(new HashingEncoder(), { model });
  const msg = await say(s, "I'll post release notes in #general from now on. 2.3 shipped today.", 1, "assistant");
  const j = await finished(s, (await s.reflect({ kinds: ["procedure", "fact"], ids: [msg] }, A)).job);
  const kinds = (await s.get(j.created, A)).memories.map((m) => m.kind);
  assert.deepEqual(kinds, ["fact"]);
});

test("unknown replace ids and non-JSON replies are dropped, not written", async () => {
  const model = scripted([{ memories: [{ op: "replace", replaces: "mem_not_shown", kind: "procedure", content: "x" },
    { op: "add", kind: "x-weird", content: "y" }] }]);
  model.complete = ((orig) => async (m) => (model.seen.length === 1 ? { text: "not json", usage: { model: "s", input_tokens: 1, cached_input_tokens: 0, output_tokens: 1, cost_usd: null } } : orig(m)))(model.complete);
  const s = new Service(new HashingEncoder(), { model });
  const a = await say(s, "hello", 1), b = await say(s, "thanks", 2);
  const j = await finished(s, (await s.reflect({ kinds: ["procedure"], ids: [a, b] }, A)).job);
  assert.equal(j.created.length, 1, "the replace of an unseen id becomes a plain add of the same text");
  assert.equal(j.skipped, 1);
  assert.equal(j.usage.unpriced_calls, 1);
});

test("without a model, and for read-only credentials, extraction is refused", async () => {
  const s = new Service(new HashingEncoder());
  const msg = await say(s, "hello", 1);
  await assert.rejects(s.reflect({ kinds: ["procedure"], ids: [msg] }, A), /summaries only/);
  await assert.rejects(s.reflect({ ids: [msg] }, A), /ids selects/);
  const m = new Service(new HashingEncoder(), { model: scripted([]) });
  const ro = new Principal("alice", [], "acme", []);
  await assert.rejects(m.reflect({ kinds: ["procedure"], ids: ["x"] }, ro), /read-only/);
  await assert.rejects(m.remember({ memories: [{ content: "x" }] }, ro), /read-only/);
  await assert.rejects(m.forget({ ids: ["x"] }, ro), /read-only/);
  assert.deepEqual((await m.capabilities(A)).reflect.kinds, ["summary", "fact", "preference", "procedure"]);
});

test("openAiCompatible: JSON mode, provider-reported cost, retry on 5xx, refusal on 401", async () => {
  let calls = 0, lastBody = null;
  const srv = createServer((req, res) => {
    let b = "";
    req.on("data", (c) => (b += c));
    req.on("end", () => {
      calls++;
      lastBody = JSON.parse(b);
      if (req.headers.authorization === "Bearer bad") { res.writeHead(401); return res.end("{}"); }
      if (calls === 1) { res.writeHead(503); return res.end("{}"); }
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ model: "m-1", choices: [{ message: { content: "{\"memories\": []}" } }],
        usage: { prompt_tokens: 12, completion_tokens: 3, prompt_tokens_details: { cached_tokens: 4 }, cost: 0.0002 } }));
    });
  });
  await new Promise((ok) => srv.listen(0, "127.0.0.1", ok));
  const url = `http://127.0.0.1:${srv.address().port}/v1`;
  try {
    const m = openAiCompatible({ baseUrl: url, model: "m", apiKey: "k", extraBody: { thinking: { type: "disabled" } } });
    const out = await m.complete([{ role: "user", content: "hi" }], { json: true });
    assert.equal(out.text, "{\"memories\": []}");
    assert.deepEqual(out.usage, { model: "m-1", input_tokens: 12, cached_input_tokens: 4, output_tokens: 3, cost_usd: 0.0002 });
    assert.deepEqual(lastBody.response_format, { type: "json_object" });
    assert.deepEqual(lastBody.thinking, { type: "disabled" });
    assert.equal(calls, 2);
    await assert.rejects(openAiCompatible({ baseUrl: url, model: "m", apiKey: "bad" }).complete([{ role: "user", content: "x" }]), /refused/);
  } finally {
    srv.close();
  }
});
