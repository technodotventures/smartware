// End-to-end tests for @technodotventures/smartware-tools against an in-process gateway. Run: node --test packages/adapters/tools/test/
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { SmartwareClient } from "@technodotventures/smartware-client";
import { createMemoryTools, memoryHooks, messageText, formatContext } from "../dist/index.js";
import { startServer } from "../../../client/test/server.js";

let srv, alice, bob;
const quiet = () => {};
before(async () => {
  srv = await startServer();
  alice = new SmartwareClient({ url: srv.url, token: srv.tokens.alice, onDegraded: quiet });
  bob = new SmartwareClient({ url: srv.url, token: srv.tokens.bob, onDegraded: quiet });
});
after(async () => { await srv.close(); });

const IDENTITY = ["principal", "tenant", "readers", "writers", "actor", "origin", "audience", "user_id", "written_by", "labels", "sources", "derived_from", "scope"];

test("tool schemas carry no identity, audience or provenance fields", () => {
  const tools = createMemoryTools(alice);
  assert.deepEqual(Object.values(tools).map((t) => t.name), ["memory_recall", "memory_remember", "memory_forget", "memory_profile"]);
  const walk = (s, path) => {
    for (const [k, sub] of Object.entries(s.properties ?? {})) {
      assert.ok(!IDENTITY.includes(k), `${path}.${k} is an identity/audience/provenance field`);
      walk(sub, `${path}.${k}`);
    }
    assert.equal(s.type === "object" ? s.additionalProperties : false, false);
  };
  for (const t of Object.values(tools)) walk(t.inputSchema, t.name);
  // events are evidence: the model may not write them
  assert.ok(!tools.remember.inputSchema.properties.kind.enum.includes("event"));
});

test("remember and recall through the tools: fail-closed, validated, cited", async () => {
  const at = createMemoryTools(alice), bt = createMemoryTools(bob);
  const w = await at.remember.execute({ content: "Alice's favourite cafe is Single O in Surry Hills", kind: "preference" });
  assert.equal(w.status, "active");
  const rec = await alice.advanced.get(w.ids[0]);
  assert.deepEqual(rec.readers, ["principal:alice"]);
  assert.equal(rec.meta.via, "tool");
  const r = await at.recall.execute({ query: "favourite cafe", budget_tokens: 300 });
  assert.match(r.context, /Single O/);
  assert.match(r.context, /evidence, not instructions/);
  assert.ok(r.items[0].citations.length && typeof r.snapshot === "string" && Array.isArray(r.degraded));
  assert.doesNotMatch((await bt.recall.execute({ query: "favourite cafe" })).context, /Single O/);

  // a prompt injection cannot add identity fields: they are rejected, not ignored
  await assert.rejects(at.remember.execute({ content: "x", readers: null }), (e) => e.code === "unsupported_parameter");
  await assert.rejects(at.recall.execute({ query: "x", principal: "bob" }), (e) => e.code === "unsupported_parameter");
  await assert.rejects(at.remember.execute({ content: "x", kind: "event" }), (e) => e.code === "unsupported_parameter");
  await assert.rejects(at.recall.execute({ query: 7 }), (e) => e.code === "invalid_parameter");
  await assert.rejects(at.recall.execute({ query: "x", budget_tokens: 9000 }), (e) => e.code === "invalid_parameter");
  await assert.rejects(at.recall.execute({}), (e) => e.code === "invalid_parameter");

  // idempotency key from the model makes its retry safe
  const k1 = await at.remember.execute({ content: "Team lunch is on Fridays", idempotency_key: "lunch-1" });
  const k2 = await at.remember.execute({ content: "Team lunch is on Fridays", idempotency_key: "lunch-1" });
  assert.deepEqual([k2.ids, k2.replayed], [k1.ids, true]);

  // standing rules are drafts
  const p = await at.remember.execute({ content: "Reply to customers within one business day", kind: "procedure" });
  assert.equal(p.status, "draft");
  assert.equal((await alice.advanced.get(p.ids[0])).status, "draft");

  const prof = await at.profile.execute({});
  assert.equal(prof.budget.tokens, 500);
});

test("forget: dry run changes nothing, confirmForget can cancel", async () => {
  const w = await alice.remember("A note that will be forgotten about the Wombat budget");
  let asked = null;
  const tools = createMemoryTools(alice, { confirmForget: (affected) => { asked = affected; return false; } });
  const dry = await tools.forget.execute({ ids: w.ids, dry_run: true });
  assert.deepEqual(dry.affected, w.ids);
  const c = await tools.forget.execute({ ids: w.ids, reason: "user asked" });
  assert.equal(c.cancelled, true);
  assert.deepEqual(asked, w.ids);
  assert.equal((await alice.advanced.get(w.ids[0])).status, "active");
  const ok = await createMemoryTools(alice, { confirmForget: () => true }).forget.execute({ ids: w.ids });
  assert.deepEqual(ok.affected, w.ids);
  assert.equal((await alice.advanced.get(w.ids[0])).status, "retracted");
  // bob cannot forget what he cannot read
  await assert.rejects(createMemoryTools(bob).forget.execute({ ids: w.ids }), (e) => e.code === "not_found");
});

test("hooks: recall before, record the turn after, in a thread", async () => {
  const thread = `chat-${Date.now()}`;
  const hooks = memoryHooks(alice, { thread });
  await alice.remember("The quarterly board meeting moved to the 14th of November");
  const msgs = [{ role: "system", content: "You are helpful." }, { role: "user", content: [{ type: "text", text: "When is the board meeting?" }] }];
  const ctx = await hooks.before(msgs);
  assert.match(ctx, /^<memory>/);
  assert.match(ctx, /14th of November/);
  assert.equal(await hooks.before([{ role: "assistant", content: "hi" }]), "");
  const r = await hooks.after(msgs, { role: "assistant", content: "It is on the 14th of November." });
  assert.equal(r.ids.length, 2);
  const units = await alice.advanced.thread(thread);
  assert.deepEqual(units.memories.map((m) => [m.actor, m.content]), [["user", "When is the board meeting?"], ["assistant", "It is on the 14th of November."]]);
  const rec = await alice.advanced.get(r.ids[0]);
  assert.deepEqual([rec.kind, rec.role, rec.readers, rec.written_by], ["event", "user", ["principal:alice"], "alice"]);

  // with a run client, recorded turns are keyed, so a retried turn does not write twice
  const run = alice.run({ runId: `hooks-${Date.now()}` });
  const rh = memoryHooks(run, { thread: thread + "-run" });
  const a = await rh.after([{ role: "user", content: "first turn" }]);
  const replay = memoryHooks(alice.run({ runId: run.runId }), { thread: thread + "-run" });
  const b = await replay.after([{ role: "user", content: "first turn" }]);
  assert.deepEqual([b.ids, b.replayed], [a.ids, true]);
});

test("message text from common message shapes", () => {
  assert.equal(messageText("hi"), "hi");
  assert.equal(messageText({ role: "user", content: "a" }), "a");
  assert.equal(messageText({ content: [{ type: "text", text: "a" }, { type: "image" }, { type: "text", text: "b" }] }), "a\nb");
  assert.equal(messageText({ content: { format: 2, parts: [{ type: "text", text: "m" }] } }), "m");
  assert.equal(messageText({ parts: [{ type: "text", text: "u" }] }), "u");
  assert.equal(formatContext({ items: [] }), "");
});

test("adapter conformance profiles name real doctor checks; packages carry no scripts or eval", async () => {
  const { readFileSync, readdirSync } = await import("node:fs");
  const root = new URL("../../../../", import.meta.url);
  const doctor = readFileSync(new URL("packages/server/src/doctor.ts", root), "utf8");
  const ids = new Set([...doctor.matchAll(/^\s*\{ id: "([^"]+)"/gm)].map((m) => m[1]));
  assert.ok(ids.size > 20);
  for (const a of ["tools", "mastra", "langgraph", "ai-sdk"]) {
    const c = JSON.parse(readFileSync(new URL(`packages/adapters/${a}/conformance.json`, root), "utf8"));
    const named = [...c.checks, ...Object.keys(c.not_exercised)];
    for (const id of named) assert.ok(ids.has(id), `${a}: unknown check ${id}`);
    assert.equal(new Set(named).size, ids.size, `${a}: every doctor check is either claimed or explained`);
  }
  for (const p of ["client", "adapters/tools", "adapters/mastra", "adapters/langgraph", "adapters/ai-sdk"]) {
    const pkg = JSON.parse(readFileSync(new URL(`packages/${p}/package.json`, root), "utf8"));
    assert.equal(pkg.scripts, undefined, `${p} has scripts`);
    assert.equal(pkg.license, "Apache-2.0");
    assert.deepEqual(Object.keys(pkg.dependencies ?? {}).filter((d) => !(d === "smartware" || d.startsWith("@technodotventures/smartware-"))), [], `${p} has third-party dependencies`);
    for (const f of readdirSync(new URL(`packages/${p}/dist/`, root)).filter((f) => f.endsWith(".js"))) {
      const src = readFileSync(new URL(`packages/${p}/dist/${f}`, root), "utf8");
      assert.doesNotMatch(src, /\beval\s*\(|new\s+Function\s*\(/, `${p}/dist/${f}`);
      assert.doesNotMatch(src, /console\.(log|info|debug)\(/, `${p}/dist/${f} logs`);
    }
  }
});
