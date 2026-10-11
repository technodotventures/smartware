// End-to-end tests for @technodotventures/smartware-client against an in-process gateway. Run: node --test packages/client/test/
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { SmartwareClient, SmartwareError } from "../dist/index.js";
import { startServer, recordingFetch } from "./server.js";

let srv, alice, bob;
const quiet = () => {};

before(async () => {
  srv = await startServer();
  alice = new SmartwareClient({ url: srv.url, token: srv.tokens.alice, onDegraded: quiet });
  bob = new SmartwareClient({ url: srv.url, token: srv.tokens.bob, tenant: "acme", onDegraded: quiet });
});
after(async () => { await srv.close(); });

const texts = (pack) => pack.items.map((i) => i.text).join("\n");

test("tenant comes from whoami and identity from the token", async () => {
  assert.equal(await alice.tenant(), "acme");
  const me = await alice.advanced.whoami();
  assert.equal(me.id, "alice");
  assert.ok(me.tokens.includes("principal:alice"));
  // the client has no way to name a principal: an asserted actor does not change written_by
  const w = await alice.remember({ content: "Kickoff notes for the Osprey account", actor: "bob" });
  const rec = await alice.advanced.get(w.ids[0]);
  assert.equal(rec.written_by, "alice");
  // a token from another tenant discovers its own tenant and sees nothing of this one
  const olga = new SmartwareClient({ url: srv.url, token: srv.tokens.outsider, onDegraded: quiet });
  assert.equal(await olga.tenant(), "elsewhere");
  assert.equal((await olga.recall("Osprey kickoff notes")).items.length, 0);
  // a wrong explicit tenant is refused by the gateway, never silently corrected
  const olga2 = new SmartwareClient({ url: srv.url, token: srv.tokens.outsider, tenant: "acme", onDegraded: quiet });
  await assert.rejects(olga2.recall("Osprey kickoff notes"), (e) => e instanceof SmartwareError && e.code === "forbidden" && e.status === 403);
});

test("fail-closed default audience: others cannot recall a write until it is shared", async () => {
  const w = await alice.remember("The Heron project codename was chosen by the founders in March");
  const rec = await alice.advanced.get(w.ids[0]);
  assert.deepEqual(rec.readers, ["principal:alice"]);
  assert.match(texts(await alice.recall("Heron project codename")), /Heron/);
  assert.doesNotMatch(texts(await bob.recall("Heron project codename")), /Heron/);
  await assert.rejects(bob.advanced.get(w.ids[0]), (e) => e.code === "not_found" && e.status === 404);
  // bob cannot widen what he cannot see
  await assert.rejects(bob.share(w.ids[0], "principal:bob"), (e) => e.code === "not_found");

  const s = await alice.share(w.ids[0], "principal:bob");
  assert.equal(s.changed, true);
  assert.deepEqual(s.readers, ["principal:alice", "principal:bob"]);
  assert.match(texts(await bob.recall("Heron project codename")), /Heron/);
  const again = await alice.share(w.ids[0], ["principal:bob"]);
  assert.equal(again.changed, false);

  // explicit tenant-wide audience
  const t = await alice.remember("The office wifi network is called Kestrel Guest", { audience: "tenant" });
  assert.equal((await alice.advanced.get(t.ids[0])).readers, null);
  assert.match(texts(await bob.recall("office wifi network name")), /Kestrel Guest/);
  // readers: null is never a silent default
  await assert.rejects(alice.remember({ content: "x", readers: null }), (e) => e.code === "invalid_parameter");
  // explicit readers are kept as given
  const g = await alice.remember({ content: "Sales offsite is in Byron Bay", readers: ["group:sales"] });
  assert.deepEqual((await alice.advanced.get(g.ids[0])).readers, ["group:sales"]);
});

test("procedures default to drafts that need approval", async () => {
  const w = await alice.remember({ content: "Always cc legal on supplier contracts", kind: "procedure", load: "always" });
  const rec = await alice.advanced.get(w.ids[0]);
  assert.equal(rec.status, "draft");
  assert.equal((await alice.profile({ budgetTokens: 200 })).items.some((i) => i.id === w.ids[0]), false);
  await alice.advanced.approve(w.ids[0]);
  assert.ok((await alice.profile({ budgetTokens: 200 })).items.some((i) => i.id === w.ids[0]));
  await assert.rejects(alice.remember([{ content: "a", kind: "procedure" }, { content: "b" }]), (e) => e.code === "invalid_parameter");
  await assert.rejects(alice.profile({}), (e) => e.code === "invalid_parameter");
});

test("run() pins as_of once and replays writes idempotently", async () => {
  const rec = recordingFetch();
  const c = new SmartwareClient({ url: srv.url, token: srv.tokens.alice, fetch: rec.fetch, onDegraded: quiet });
  const run = c.run({ runId: "r-42" });
  await new Promise((r) => setTimeout(r, 30));
  // valid from after the run started: a fresh view sees it, the run's pinned view does not
  await c.remember({ content: "The Bluebird launch code word was set today", kind: "fact", valid_from: new Date().toISOString() });
  assert.match(texts(await c.recall("Bluebird launch code word")), /Bluebird/);
  assert.doesNotMatch(texts(await run.recall("Bluebird launch code word")), /Bluebird/);
  await run.profile({ budgetTokens: 100 });
  const pinned = rec.calls.filter((x) => (x.verb === "recall" || x.verb === "profile") && x.body.as_of !== undefined);
  assert.ok(pinned.length >= 2);
  assert.ok(pinned.every((x) => x.body.as_of === run.asOf));
  assert.throws(() => run.recall("x", { asOf: "2020-01-01T00:00:00Z" }), (e) => e.code === "invalid_parameter");

  const a = await run.remember("step one of the run");
  const b = await run.remember("step two of the run");
  assert.equal(a.replayed, false);
  // the process restarts and the same run replays: same keys, same ids, nothing new written
  const replay = c.run({ runId: "r-42" });
  const a2 = await replay.remember("step one of the run (regenerated wording)");
  const b2 = await replay.remember("step two of the run (regenerated wording)");
  assert.deepEqual([a2.ids, a2.replayed, b2.ids, b2.replayed], [a.ids, true, b.ids, true]);
  const keys = rec.calls.filter((x) => x.verb === "remember" && x.body.idempotency_key).map((x) => x.body.idempotency_key);
  assert.deepEqual(keys, ["run:r-42:1", "run:r-42:2", "run:r-42:1", "run:r-42:2"]);
  const e = await replay.remember("explicit step", { step: 10 });
  const n = await replay.remember("after explicit");
  assert.notDeepEqual(e.ids, n.ids);
  assert.equal(rec.calls.at(-1).body.idempotency_key, "run:r-42:11");
});

test("retries never repeat a non-idempotent write, and do retry reads and keyed writes", async () => {
  const content = `Unkeyed write ${Date.now()}`;
  let failNext = true;
  const rec = recordingFetch((call) => {
    if (call.verb === "remember" && failNext) { failNext = false; return "503-after"; }
    return undefined;
  });
  const c = new SmartwareClient({ url: srv.url, token: srv.tokens.alice, fetch: rec.fetch, retries: 3, retryDelayMs: 1, onDegraded: quiet });
  await assert.rejects(c.remember(content), (e) => e instanceof SmartwareError && e.status === 503);
  assert.equal(rec.calls.filter((x) => x.verb === "remember").length, 1, "an unkeyed write must not be sent twice");
  const page = await c.advanced.list({ limit: 1000 });
  assert.equal(page.memories.filter((m) => m.content === content).length, 1);

  // a network failure after the gateway committed: also not retried without a key
  let throwNext = true;
  const rec2 = recordingFetch((call) => (call.verb === "remember" && throwNext ? ((throwNext = false), "throw-after") : undefined));
  const c2 = new SmartwareClient({ url: srv.url, token: srv.tokens.alice, fetch: rec2.fetch, retries: 3, retryDelayMs: 1, onDegraded: quiet });
  await assert.rejects(c2.remember(content + " net"), (e) => e.code === "network" && e.status === 0);
  assert.equal(rec2.calls.filter((x) => x.verb === "remember").length, 1);

  // keyed: the lost response is retried and the replay returns the committed ids
  failNext = true;
  const k = await c.remember(content + " keyed", { idempotencyKey: `k-${Date.now()}` });
  assert.equal(k.replayed, true);
  assert.equal(rec.calls.filter((x) => x.verb === "remember" && x.body.idempotency_key).length, 2);
  const after2 = await c.advanced.list({ limit: 1000 });
  assert.equal(after2.memories.filter((m) => m.content === content + " keyed").length, 1);

  // reads retry on 429
  let limited = 2;
  const rec3 = recordingFetch((call) => (call.verb === "recall" && limited-- > 0 ? "429" : undefined));
  const c3 = new SmartwareClient({ url: srv.url, token: srv.tokens.alice, fetch: rec3.fetch, retries: 2, retryDelayMs: 1, onDegraded: quiet });
  const p = await c3.recall("unkeyed write");
  assert.ok(Array.isArray(p.items));
  assert.equal(rec3.calls.filter((x) => x.verb === "recall").length, 3);
  // and give up after the configured attempts
  limited = 5;
  await assert.rejects(c3.recall("unkeyed write"), (e) => e.code === "rate_limited" && e.status === 429);
});

test("typed errors carry code, status and details", async () => {
  await assert.rejects(alice.remember({ content: "x", bogus: 1 }), (e) => e instanceof SmartwareError && e.code === "unsupported_parameter" && e.status === 400);
  await assert.rejects(alice.recall("x", { budgetTokens: 10 }), (e) => e.code === "budget_too_small");
  const w = await alice.remember({ id: "doc-cfg", content: "Config v1", kind: "fact" });
  await assert.rejects(alice.remember({ id: "doc-cfg", content: "Config v2", kind: "fact" }, { mode: "upsert", ifVersion: 7 }),
    (e) => e.code === "version_conflict" && e.status === 409 && e.details.current === 1);
  assert.deepEqual(w.ids, ["doc-cfg"]);
  const bad = new SmartwareClient({ url: srv.url, token: "smartware_not-a-real-key", tenant: "acme", onDegraded: quiet });
  await assert.rejects(bad.recall("x"), (e) => e.code === "unauthorized" && e.status === 401);
});

test("degraded[] reaches onDegraded; the default warns once per value without content", async () => {
  const seen = [];
  const fake = async () => new Response(JSON.stringify({ items: [{ text: "SECRET CONTENT", memory_ids: ["m"], tokens: 1, score: 1, why: {}, validity: "current", citations: [] }],
    tokens: 1, budget: { tokens: 2000 }, coverage: 1, snapshot: "s", degraded: ["encoder:fallback-hashing"], request_id: "r" }), { status: 200 });
  const c = new SmartwareClient({ url: "http://x.invalid", token: "t", tenant: "acme", fetch: fake, onDegraded: (v, info) => seen.push([v, info.source]) });
  await c.recall("q");
  assert.deepEqual(seen, [[["encoder:fallback-hashing"], "recall"]]);
  const warned = [];
  const orig = console.warn;
  console.warn = (...a) => warned.push(a.join(" "));
  try {
    const d = new SmartwareClient({ url: "http://x.invalid", token: "t", tenant: "acme", fetch: fake });
    await d.recall("q"); await d.recall("q");
  } finally { console.warn = orig; }
  assert.equal(warned.length, 1);
  assert.match(warned[0], /encoder:fallback-hashing/);
  assert.doesNotMatch(warned[0], /SECRET|Bearer|\bt\b/);
});

test("forUser acts as the user the gateway verifies, never as the service", async () => {
  const platform = new SmartwareClient({ url: srv.url, token: srv.tokens.platform, onDegraded: quiet });
  const asCarol = platform.forUser(srv.jwt({ sub: "carol", groups: ["sales"] }));
  assert.equal((await asCarol.advanced.whoami()).id, "carol");
  const w = await asCarol.remember("Carol prefers morning standups at 9");
  assert.deepEqual((await asCarol.advanced.get(w.ids[0])).readers, ["principal:carol"]);
  assert.equal((await asCarol.advanced.get(w.ids[0])).written_by, "carol");
  assert.doesNotMatch(texts(await platform.recall("Carol standup preference")), /Carol prefers/);
  const asDan = platform.forUser(srv.jwt({ sub: "dan" }));
  assert.doesNotMatch(texts(await asDan.recall("Carol standup preference")), /Carol prefers/);
  await assert.rejects(platform.forUser("not.a.jwt").recall("x"), (e) => e.code === "unauthorized");
});

test("advanced verbs: list pages, thread, links, history, forget dry run, changes, export", async () => {
  const th = `thread-${Date.now()}`;
  const w = await alice.remember([{ content: "We agreed to ship on Friday", seq: 1 }, { content: "Marketing needs the copy by Wednesday", seq: 2 }], { thread: th });
  const units = await alice.advanced.thread(th);
  assert.deepEqual(units.memories.map((m) => m.id), w.ids);
  const nb = await alice.advanced.neighbours(w.ids[0], { after: 1 });
  assert.equal(nb.memories.length, 2);
  const pages = [];
  for await (const p of alice.advanced.pages({ limit: 2 })) pages.push(p);
  assert.ok(pages.length > 1 && pages.at(-1).cursor === null);
  const all = [];
  for await (const m of alice.advanced.all({ thread: th })) all.push(m.id);
  assert.deepEqual(all.sort(), [...w.ids].sort());

  const l = await alice.advanced.link({ type: "follows", from: w.ids[1], to: w.ids[0] });
  assert.equal((await alice.advanced.edges(w.ids[0]))[0].id, l.id);
  assert.equal((await alice.advanced.unlink(l.id)).removed, true);
  assert.equal((await alice.advanced.edges(w.ids[0])).length, 0);

  await alice.remember({ content: "Alice lives in Sydney", claim: { subject: "alice", predicate: "lives_in", object: "Sydney" } });
  await alice.remember({ content: "Alice lives in Dubai", claim: { subject: "alice", predicate: "lives_in", object: "Dubai" } });
  const h = await alice.history("alice", "lives_in");
  assert.deepEqual(h.versions.map((v) => v.object).sort(), ["Dubai", "Sydney"]);

  const dry = await alice.forget(w.ids[0], { dryRun: true });
  assert.equal(dry.dry_run, true);
  assert.ok((await alice.advanced.get(w.ids[0])).status === "active");
  await alice.forget(w.ids[0], { reason: "test" });
  assert.equal((await alice.advanced.get(w.ids[0])).status, "retracted");

  const seen = [];
  for await (const c of alice.advanced.changes({ limit: 3 })) seen.push(c);
  assert.ok(seen.some((c) => c.op.startsWith("forget") && c.id === w.ids[0]));
  assert.ok(seen.every((c, i) => i === 0 || c.cursor > seen[i - 1].cursor));
  const ac = new AbortController();
  const it = alice.advanced.changes({ cursor: seen.at(-1).cursor, follow: true, pollMs: 10, signal: ac.signal });
  const next = it.next();
  await alice.remember("A change that arrives while following");
  const got = await next;
  assert.equal(got.value.op, "remember");
  ac.abort();
  assert.equal((await it.next()).done, true);

  const b = await alice.advanced.export();
  assert.ok(b.manifest.count > 0 && b["memories.jsonl"].length > 0);
  const caps = await alice.advanced.capabilities();
  assert.equal(typeof caps.tokenizer, "string");
  const r = await alice.advanced.reflect({ threads: [th] });
  assert.equal((await alice.advanced.job(r.job)).status, "completed");
});

test("every key the client sends is a property of the Smartware 0.1 schema", async () => {
  const schemaDir = fileURLToPath(new URL("../../../schema/smartware-0.1/", import.meta.url));
  const load = (n) => JSON.parse(readFileSync(schemaDir + n + ".schema.json", "utf8"));
  const memIn = load("MemoryInput"), recallReq = load("RecallRequest"), forgetReq = load("ForgetRequest"),
    rememberReq = load("RememberRequest"), profileReq = load("ProfileRequest");
  const rec = recordingFetch();
  const c = new SmartwareClient({ url: srv.url, token: srv.tokens.alice, fetch: rec.fetch, onDegraded: quiet });
  await c.remember([{ content: "Schema probe one", kind: "fact", claim: { subject: "probe", predicate: "is", object: "one" }, scope: "s/a",
    actor: "alice", role: "user", thread: "probe", seq: 1, event_time: "2026-01-01T00:00:00Z", valid_from: "2026-01-01T00:00:00Z",
    labels: ["x"], load: "on_query", confidence: 0.9, support: 1, meta: { a: 1 }, sources: [{ uri: "https://example.com" }] },
  "Schema probe two"], { mode: "append", idempotencyKey: `schema-${Date.now()}`, wait: "indexed", allowReingest: false, thread: "probe" });
  await c.remember("Schema probe three", { audience: "tenant" });
  await c.recall("schema probe", { budgetTokens: 300, maxItems: 5, asOf: new Date(), knownAt: new Date(), scope: "s", prefer: ["s/a"],
    kinds: ["fact", "event"], granularity: "unit", mode: "balanced", minCoverage: 0, include: { history: true, drafts: false, neighbours: 1, explain: true, labels: [] } });
  await c.recall("schema probe", { scope: "s", kinds: { fact: 2 } });
  const target = (await c.recall("schema probe")).items[0].memory_ids[0];
  await c.forget([target], { mode: "retract", cascade: true, dryRun: true, reason: "probe", allowReingest: false });
  await c.profile({ budgetTokens: 100, subject: "probe", asOf: new Date() });

  const check = (obj, schema, where) => {
    for (const k of Object.keys(obj)) assert.ok(k in schema.properties, `${where}.${k} is not in the schema`);
  };
  const objOf = (s) => s.anyOf ? s.anyOf.find((x) => x.type === "object") : s;
  for (const call of rec.calls) {
    if (call.verb === "remember") {
      check(call.body, rememberReq, "RememberRequest");
      for (const m of call.body.memories) {
        check(m, memIn, "MemoryInput");
        if (m.claim) check(m.claim, objOf(memIn.properties.claim), "MemoryInput.claim");
        for (const s of m.sources ?? []) check(s, memIn.properties.sources.items, "MemoryInput.sources[]");
      }
    }
    if (call.verb === "recall") {
      check(call.body, recallReq, "RecallRequest");
      check(call.body.budget, recallReq.properties.budget, "RecallRequest.budget");
      if (call.body.include) check(call.body.include, recallReq.properties.include, "RecallRequest.include");
      if (call.body.scope && typeof call.body.scope === "object") check(call.body.scope, objOf(recallReq.properties.scope), "RecallRequest.scope");
    }
    if (call.verb === "forget") check(call.body, forgetReq, "ForgetRequest");
    if (call.verb === "profile") { check(call.body, profileReq, "ProfileRequest"); check(call.body.budget, profileReq.properties.budget, "ProfileRequest.budget"); }
  }
  const verbs = new Set(rec.calls.map((x) => x.verb));
  for (const v of ["remember", "recall", "forget", "profile"]) assert.ok(verbs.has(v), `${v} was not exercised`);
});

test("getToken is called per request and tokens never appear in errors", async () => {
  let n = 0;
  const c = new SmartwareClient({ url: srv.url, tenant: "acme", getToken: async () => { n++; return srv.tokens.bob; }, onDegraded: quiet });
  await c.recall("anything at all");
  await c.recall("anything at all");
  assert.equal(n, 2);
  const leaky = new SmartwareClient({ url: "http://127.0.0.1:1", tenant: "acme", token: "smartware_supersecret", retries: 0, onDegraded: quiet });
  await assert.rejects(leaky.recall("x"), (e) => e.code === "network" && !String(e.message).includes("supersecret") && !JSON.stringify(e).includes("supersecret"));
  assert.throws(() => new SmartwareClient({ url: srv.url }), (e) => e.code === "invalid_parameter");
});

test("coarse as_of means the end of the stated period", async () => {
  const { asOfTime } = await import("../dist/index.js");
  assert.equal(asOfTime("2024"), "2024-12-31T23:59:59.999999Z");
  assert.equal(asOfTime("2024-02"), "2024-02-29T23:59:59.999999Z");
  assert.equal(asOfTime("2023-02"), "2023-02-28T23:59:59.999999Z");
  assert.equal(asOfTime("2024-05-12"), "2024-05-12T23:59:59.999999Z");
  assert.equal(asOfTime("2024-05-12T08:00:00Z"), "2024-05-12T08:00:00Z");
  assert.equal(asOfTime(new Date("2024-05-12T08:00:00Z")), "2024-05-12T08:00:00.000Z");
  assert.throws(() => asOfTime("2024-13"), SmartwareError);
});
