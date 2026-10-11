// Tests for @technodotventures/smartware-ai-sdk: the middleware is driven the way wrapLanguageModel drives it, with a stand-in model.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { SmartwareClient } from "@technodotventures/smartware-client";
import { memoryMiddleware, recallContext, recordTurn, openAIAgentsTools, claudeAgentTools, claudeAgentHooks } from "../dist/index.js";
import { startServer } from "../../../client/test/server.js";

let srv, alice, bob, degraded;
before(async () => {
  srv = await startServer();
  degraded = [];
  alice = new SmartwareClient({ url: srv.url, token: srv.tokens.alice, onDegraded: (v) => degraded.push(...v) });
  bob = new SmartwareClient({ url: srv.url, token: srv.tokens.bob, onDegraded: () => {} });
  await alice.remember("Alice's daughter Zara starts swimming lessons on Saturday mornings");
});
after(async () => { await srv.close(); });

const userPrompt = (text, extra = {}) => ({
  prompt: [{ role: "system", content: "You are a helpful assistant." }, { role: "user", content: [{ type: "text", text }] }], ...extra,
});

// What wrapLanguageModel does: transformParams, then wrapGenerate/wrapStream around the model call.
async function generate(mw, params, reply) {
  const p = await mw.transformParams({ type: "generate", params });
  let seen;
  const result = await mw.wrapGenerate({ params: p, doGenerate: async () => { seen = p; return { content: [{ type: "text", text: reply }], finishReason: "stop" }; } });
  return { result, seen };
}

test("transformParams injects recalled context as a system message after the leading system prompt", async () => {
  const mw = memoryMiddleware(alice, { thread: "ai-1" });
  assert.equal(mw.middlewareVersion, "v2");
  const p = await mw.transformParams({ type: "generate", params: userPrompt("When are Zara's swimming lessons?") });
  assert.equal(p.prompt.length, 3);
  assert.deepEqual(p.prompt.map((m) => m.role), ["system", "system", "user"]);
  assert.match(p.prompt[1].content, /Saturday mornings/);
  assert.match(p.prompt[1].content, /evidence, not instructions/);
  // another principal's call gets nothing from alice's memory
  const pb = await memoryMiddleware(bob).transformParams({ type: "generate", params: userPrompt("When are Zara's swimming lessons?") });
  assert.equal(pb.prompt.length, 2);
  // no user message: params pass through untouched
  const none = { prompt: [{ role: "system", content: "x" }] };
  assert.equal(await mw.transformParams({ type: "generate", params: none }), none);
});

test("wrapGenerate records the user turn (and the reply when asked) in the thread from config or providerOptions", async () => {
  const mw = memoryMiddleware(alice, { thread: "ai-config" });
  const { result } = await generate(mw, userPrompt("Book the swimming pool lane for Zara"), "Done, lane 3 at 8am.");
  assert.equal(result.content[0].text, "Done, lane 3 at 8am.");
  assert.deepEqual((await alice.advanced.thread("ai-config")).memories.map((m) => [m.actor, m.content]), [["user", "Book the swimming pool lane for Zara"]]);

  const both = memoryMiddleware(alice, { thread: "ai-config", recordReply: true });
  await generate(both, userPrompt("Thanks!", { providerOptions: { memory: { thread: "ai-call" } } }), "You're welcome.");
  const t = await alice.advanced.thread("ai-call");
  assert.deepEqual(t.memories.map((m) => [m.actor, m.content]), [["user", "Thanks!"], ["assistant", "You're welcome."]]);
  const rec = await alice.advanced.get(t.memories[0].id);
  assert.deepEqual([rec.kind, rec.readers, rec.written_by], ["event", ["principal:alice"], "alice"]);
});

test("wrapStream records after the stream is consumed, passing every chunk through", async () => {
  const mw = memoryMiddleware(alice, { recordReply: true });
  const params = await mw.transformParams({ type: "stream", params: userPrompt("Stream me a haiku about laksa", { providerOptions: { memory: { thread: "ai-stream" } } }) });
  const parts = [{ type: "stream-start" }, { type: "text-start", id: "1" }, { type: "text-delta", id: "1", delta: "Coconut broth, " },
    { type: "text-delta", id: "1", delta: "chilli rising." }, { type: "text-end", id: "1" }, { type: "finish", finishReason: "stop" }];
  const { stream, response } = await mw.wrapStream({ params, doStream: async () => ({
    stream: new ReadableStream({ start(c) { for (const p of parts) c.enqueue(p); c.close(); } }), response: { id: "r1" } }) });
  assert.deepEqual(response, { id: "r1" });
  const got = [];
  for await (const p of stream) got.push(p);
  assert.deepEqual(got, parts);
  const t = await alice.advanced.thread("ai-stream");
  assert.deepEqual(t.memories.map((m) => m.content), ["Stream me a haiku about laksa", "Coconut broth, chilli rising."]);
  // AI SDK 4 (v1) stream parts use textDelta
  const v1 = memoryMiddleware(alice, { recordReply: true, version: "v1" });
  assert.equal(v1.middlewareVersion, undefined);
  const s1 = await v1.wrapStream({ params: userPrompt("v1 question", { providerMetadata: { memory: { thread: "ai-v1" } } }),
    doStream: async () => ({ stream: new ReadableStream({ start(c) { c.enqueue({ type: "text-delta", textDelta: "v1 answer" }); c.close(); } }) }) });
  for await (const _ of s1.stream) { /* drain */ }
  assert.deepEqual((await alice.advanced.thread("ai-v1")).memories.map((m) => m.content), ["v1 question", "v1 answer"]);
});

test("identity cannot ride in providerOptions; a failed recall is reported, not hidden", async () => {
  const mw = memoryMiddleware(alice);
  for (const bad of [{ principal: "bob" }, { userId: "bob" }, { readers: ["public"] }, { tenant: "other" }]) {
    await assert.rejects(mw.transformParams({ type: "generate", params: userPrompt("hi", { providerOptions: { memory: bad } }) }),
      (e) => e.code === "unsupported_parameter");
  }
  const down = new SmartwareClient({ url: "http://127.0.0.1:1", token: "t", tenant: "acme", retries: 0, onDegraded: (v) => degraded.push(...v) });
  const p = await memoryMiddleware(down).transformParams({ type: "generate", params: userPrompt("anything") });
  assert.equal(p.prompt.length, 2);
  assert.ok(degraded.includes("ai-sdk:recall-failed"));
  await assert.rejects(memoryMiddleware(down, { failOpen: false }).transformParams({ type: "generate", params: userPrompt("anything") }), (e) => e.code === "network");
  // recording failures go to onError and the reply still comes back
  const errs = [];
  const { result } = await generate(memoryMiddleware(down, { onError: (e) => errs.push(e.code), failOpen: true }), userPrompt("hello"), "hi there");
  assert.equal(result.content[0].text, "hi there");
  assert.deepEqual(errs, ["network"]);
});

test("generic helpers for the OpenAI Agents SDK and the Claude Agent SDK", async () => {
  assert.match(await recallContext(alice, "swimming lessons"), /Saturday/);
  const r = await recordTurn(alice, { user: "Remind me about swimming", reply: "Saturday mornings." }, { thread: "generic" });
  assert.equal(r.ids.length, 2);

  // OpenAI Agents SDK: tool({ name, description, parameters, strict, execute })
  const oa = openAIAgentsTools(alice, (cfg) => cfg);
  assert.deepEqual(oa.map((t) => t.name), ["memory_recall", "memory_remember", "memory_forget", "memory_profile"]);
  assert.equal(oa[0].strict, false);
  assert.equal(oa[0].parameters.type, "object");
  assert.match(JSON.parse(await oa[0].execute({ query: "swimming lessons" })).context, /Saturday/);
  assert.match(JSON.parse(await oa[0].execute('{"query":"swimming lessons"}')).context, /Saturday/);

  // Claude Agent SDK: tool(name, description, zodRawShape, handler)
  const shapes = {};
  const z = { object: (s) => ({ s, strict() { return this; }, optional() { return this; }, describe() { return this; } }),
    string: () => chain("string"), number: () => chain("number"), boolean: () => chain("boolean"), array: () => chain("array"), enum: () => chain("enum") };
  function chain(t) { const n = { t }; for (const m of ["optional", "describe", "max", "min", "int"]) n[m] = () => n; return n; }
  const ct = claudeAgentTools(alice, (name, description, shape, handler) => { shapes[name] = shape; return { name, handler }; }, z);
  assert.deepEqual(Object.keys(shapes.memory_recall).sort(), ["as_of", "budget_tokens", "include_history", "kinds", "min_coverage", "query"]);
  const out = await ct[0].handler({ query: "swimming lessons" });
  assert.match(JSON.parse(out.content[0].text).context, /Saturday/);
  const err = await ct[1].handler({ content: "x", readers: ["public"] });
  assert.equal(err.isError, true);
  assert.match(err.content[0].text, /^unsupported_parameter/);

  const hooks = claudeAgentHooks(alice, { thread: "claude-session" });
  const res = await hooks.UserPromptSubmit[0].hooks[0]({ hook_event_name: "UserPromptSubmit", prompt: "What day is swimming?", session_id: "s1" });
  assert.equal(res.hookSpecificOutput.hookEventName, "UserPromptSubmit");
  assert.match(res.hookSpecificOutput.additionalContext, /Saturday/);
  assert.deepEqual((await alice.advanced.thread("claude-session")).memories.map((m) => m.content), ["What day is swimming?"]);
});
