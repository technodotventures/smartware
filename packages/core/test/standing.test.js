// Standing context (load: on_scope, spec §5.12) and positioned de-duplication.
import { test } from "node:test";
import assert from "node:assert/strict";
import { HashingEncoder, Principal, Service } from "../dist/index.js";

const A = new Principal("alice", ["team"], "acme");
const B = new Principal("bob", ["team"], "acme");

const rule = "Release notes go only to #eng-releases, and only after the deploy is green.";
const seed = async (s) => {
  await s.remember({ memories: [{ content: rule, kind: "procedure", load: "on_scope", readers: ["principal:alice"] }] }, A);
  await s.remember({ memories: Array.from({ length: 30 }, (_, i) => ({
    content: `standup ${i}: worked on the billing page and the onboarding emails`, thread: "chat", seq: i + 1,
    readers: ["group:team"] })) }, A);
};

test("a standing rule comes back for a request that does not mention it, marked standing", async () => {
  const s = new Service(new HashingEncoder());
  await seed(s);
  const r = await s.recall({ query: "post the v2.3 changelog", budget: { tokens: 400 } }, A);
  const st = r.items.filter((i) => i.standing);
  assert.equal(st.length, 1);
  assert.ok(st[0].text.endsWith(rule));
  assert.equal(r.items[0].standing, true, "standing context comes first");
  assert.ok(r.tokens <= 400);
});

test("standing context is admissibility-first: a principal who cannot read the rule never sees it", async () => {
  const s = new Service(new HashingEncoder());
  await seed(s);
  const r = await s.recall({ query: "post the v2.3 changelog", budget: { tokens: 400 } }, B);
  assert.ok(!r.items.some((i) => i.standing || i.text.includes("eng-releases")));
});

test("budget.standing: 0 turns standing context off; a matching rule still comes back as a normal result", async () => {
  const s = new Service(new HashingEncoder());
  await seed(s);
  const off = await s.recall({ query: "post the v2.3 changelog", budget: { tokens: 400, standing: 0 } }, A);
  assert.ok(!off.items.some((i) => i.standing));
  const hit = await s.recall({ query: "where do release notes go", budget: { tokens: 400, standing: 0 } }, A);
  assert.ok(hit.items.some((i) => i.text.endsWith(rule) && !i.standing));
  await assert.rejects(s.recall({ query: "x", budget: { tokens: 100, standing: 101 } }, A), /budget.standing/);
});

test("a superseded standing preference is not returned as current", async () => {
  const s = new Service(new HashingEncoder());
  const pref = (object, valid_from) => ({ content: `Alice prefers ${object} for status updates`, kind: "preference",
    load: "on_scope", valid_from, claim: { subject: "alice", predicate: "status_channel", object } });
  await s.remember({ memories: [pref("email", "2024-01-01")] }, A);
  await s.remember({ memories: [pref("slack", "2025-01-01")] }, A);
  const r = await s.recall({ query: "send the weekly update", as_of: "2025-06-01", budget: { tokens: 400 } }, A);
  const st = r.items.filter((i) => i.standing).map((i) => i.text);
  assert.equal(st.length, 1);
  assert.ok(st[0].endsWith("Alice prefers slack for status updates"));
});

test("the same words at two positions in a thread stay two events; a replay at the same position does not", async () => {
  const s = new Service(new HashingEncoder());
  const one = await s.remember({ memories: [{ content: "ok", thread: "t", seq: 3 }] }, A);
  const two = await s.remember({ memories: [{ content: "ok", thread: "t", seq: 7 }] }, A);
  const again = await s.remember({ memories: [{ content: "ok", thread: "t", seq: 3 }] }, A);
  assert.notEqual(one.ids[0], two.ids[0]);
  assert.equal(again.ids[0], one.ids[0]);
});
