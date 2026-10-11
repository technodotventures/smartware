// Tests for @orp/mastra with stand-ins for Mastra's createTool and Zod (neither can be installed here).
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { OrpClient } from "@orp/client";
import { mastraTools, memoryProcessors, toolInput } from "../dist/index.js";
import { startServer } from "../../../client/test/server.js";

let srv, alice, bob;
const quiet = () => {};
before(async () => {
  srv = await startServer();
  alice = new OrpClient({ url: srv.url, token: srv.tokens.alice, onDegraded: quiet });
  bob = new OrpClient({ url: srv.url, token: srv.tokens.bob, onDegraded: quiet });
});
after(async () => { await srv.close(); });

// A recording stand-in for the Zod calls toZod makes.
function fakeZ() {
  const node = (t, extra = {}) => {
    const n = { t, ...extra, mods: [] };
    for (const m of ["optional", "describe", "max", "min", "int", "strict"]) n[m] = (...a) => { n.mods.push([m, ...a]); return n; };
    return n;
  };
  return { object: (shape) => node("object", { shape }), string: () => node("string"), number: () => node("number"),
    boolean: () => node("boolean"), array: (item) => node("array", { item }), enum: (values) => node("enum", { values }) };
}

test("mastraTools builds Mastra tools with your createTool, Zod or JSON Schema, both execute shapes", async () => {
  const created = [];
  const createTool = (cfg) => { created.push(cfg); return { ...cfg, kind: "mastra-tool" }; };
  const tools = mastraTools(alice, createTool, { z: fakeZ() });
  assert.deepEqual(Object.keys(tools), ["memoryRecall", "memoryRemember", "memoryForget", "memoryProfile"]);
  const rs = tools.memoryRemember.inputSchema;
  assert.equal(rs.t, "object");
  assert.ok(rs.mods.some((m) => m[0] === "strict"));
  assert.equal(rs.shape.content.t, "string");
  assert.ok(rs.shape.kind.mods.some((m) => m[0] === "optional"));
  assert.deepEqual(rs.shape.kind.values, ["fact", "preference", "procedure"]);
  assert.ok(!("readers" in rs.shape) && !("actor" in rs.shape));

  // Mastra 0.x: execute({ context, runtimeContext, ... })
  const w = await tools.memoryRemember.execute({ context: { content: "Coffee's design review is every Tuesday" }, runtimeContext: { userId: "bob" } });
  assert.deepEqual((await alice.advanced.get(w.ids[0])).readers, ["principal:alice"], "a host user id never becomes the principal");
  // later releases: execute(inputData, context)
  const r = await tools.memoryRecall.execute({ query: "design review day" }, { runtimeContext: {} });
  assert.match(r.context, /Tuesday/);
  assert.deepEqual(toolInput({ context: { a: 1 } }), { a: 1 });
  assert.deepEqual(toolInput({ query: "q" }), { query: "q" });

  const plain = mastraTools(alice, (c) => c);
  assert.equal(plain.memoryRecall.inputSchema.type, "object");
  await assert.rejects(plain.memoryRecall.execute({ context: { query: "x", tenant: "other" } }), (e) => e.code === "unsupported_parameter");
});

test("processors: recall into a system message before, record the turn after", async () => {
  await alice.remember("The Studio OS deploy window is Thursday 10pm Sydney time");
  const { input, output } = memoryProcessors(alice, { resourceScope: "users" });
  const user = { id: "m1", role: "user", threadId: "t-mastra", resourceId: "u_77", createdAt: new Date(),
    content: { format: 2, parts: [{ type: "text", text: "When is the deploy window?" }] } };
  // the resource scope narrows recall: nothing was written under users/u_77 yet
  const first = await input.processInput({ messages: [user], abort: () => { throw new Error("abort"); } });
  assert.equal(first.length, 1);
  const unscoped = memoryProcessors(alice, { thread: "t-mastra-2" });
  const msgs = await unscoped.input.processInput({ messages: [{ role: "system", content: "sys" }, user] });
  assert.equal(msgs.length, 3);
  assert.equal(msgs[1].role, "system");
  assert.match(msgs[1].content.content, /Thursday 10pm/);
  assert.equal(msgs[2], user);

  const reply = { id: "m2", role: "assistant", threadId: "t-mastra", resourceId: "u_77", content: { format: 2, parts: [{ type: "text", text: "Thursday at 10pm." }] } };
  const outMsgs = await output.processOutputResult({ messages: [reply] });
  assert.deepEqual(outMsgs, [reply]);
  const units = await alice.advanced.thread("t-mastra");
  assert.deepEqual(units.memories.map((m) => [m.actor, m.content]), [["user", "When is the deploy window?"], ["assistant", "Thursday at 10pm."]]);
  const rec = await alice.advanced.get(units.memories[0].id);
  assert.equal(rec.scope, "users/u_77");
  assert.deepEqual(rec.readers, ["principal:alice"]);
  assert.equal(rec.written_by, "alice");
  // bob's agent does not see alice's recorded turns
  const bobMsgs = await memoryProcessors(bob).input.processInput({ messages: [user] });
  assert.equal(bobMsgs.length, 1);
});
