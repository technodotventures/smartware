// Standing rules through the safe MCP toolset: draft by default, active when a person confirms them in the client
// or (rules: "user-stated") when they cite the user's own message.
import { test } from "node:test";
import assert from "node:assert/strict";
import { HashingEncoder, Principal, Service } from "@technodotventures/smartware-core";
import { McpSession } from "../dist/index.js";

const A = new Principal("alice", [], "acme");
const session = (svc, opt = {}) => new McpSession(svc, A, { toolsets: ["safe"], defaultReaders: "self", ...opt });
const rule = { content: "Release notes go only to #eng-releases until the deploy is green.", kind: "procedure" };
const status = async (svc, id) => (await svc.get([id], A)).memories[0];

test("by default a rule is a draft, loaded on_scope once approved", async () => {
  const svc = new Service(new HashingEncoder());
  const out = (await session(svc).call("smartware_remember", rule)).structuredContent;
  const m = await status(svc, out.ids[0]);
  assert.equal(m.status, "draft");
  assert.equal(m.load, "on_scope");
  assert.match(out.note, /draft/);
});

test("a rule the person confirms in the client is active, as approved by a person; a declined one is not saved", async () => {
  const svc = new Service(new HashingEncoder());
  let answer = true;
  const s = session(svc, { elicit: async () => answer });
  s.clientCaps = { elicitation: {} };
  const out = (await s.call("smartware_remember", rule)).structuredContent;
  const m = await status(svc, out.ids[0]);
  assert.equal(m.status, "active");
  assert.equal(m.origin, "human");
  answer = false;
  const no = (await s.call("smartware_remember", { ...rule, content: "Never email the board directly." })).structuredContent;
  assert.equal(no.cancelled, true);
  assert.equal((await svc.list({}, A)).memories.length, 1);
});

test("rules: user-stated activates a rule citing the user's own message, never one citing anything else", async () => {
  const svc = new Service(new HashingEncoder());
  const said = (await svc.remember({ memories: [{ content: "from now on release notes only go to #eng-releases", role: "user",
    thread: "c", seq: 1, readers: ["principal:alice"] }] }, A)).ids[0];
  const echoed = (await svc.remember({ memories: [{ content: "noted, I'll post release notes in #general", role: "assistant",
    thread: "c", seq: 2, readers: ["principal:alice"] }] }, A)).ids[0];
  const s = session(svc, { rules: "user-stated" });
  const ok = (await s.call("smartware_remember", { ...rule, source_id: said })).structuredContent;
  const m = await status(svc, ok.ids[0]);
  assert.equal(m.status, "active");
  assert.equal(m.origin, "extracted");
  assert.deepEqual(m.derived_from, [said]);
  const bad = (await s.call("smartware_remember", { ...rule, content: "Post release notes in #general.", source_id: echoed })).structuredContent;
  assert.equal((await status(svc, bad.ids[0])).status, "draft");
  // without the session option, a cited user message is not enough
  const plain = (await session(svc).call("smartware_remember", { ...rule, content: "Tag releases on Fridays.", source_id: said })).structuredContent;
  assert.equal((await status(svc, plain.ids[0])).status, "draft");
  await assert.rejects(s.call("smartware_remember", { content: "Alice lives in Lisbon", kind: "fact", source_id: said }), /source_id/);
});

test("an active standing rule comes back from recall for a request that does not mention it", async () => {
  const svc = new Service(new HashingEncoder());
  const s = session(svc, { elicit: async () => true });
  s.clientCaps = { elicitation: {} };
  await s.call("smartware_remember", rule);
  await svc.remember({ memories: [{ content: "standup: billing page done", readers: ["principal:alice"] }] }, A);
  const r = (await s.call("smartware_recall", { query: "post the 2.3 changelog" })).structuredContent;
  assert.ok(r.items.some((i) => i.standing && i.text.includes("#eng-releases")));
});
