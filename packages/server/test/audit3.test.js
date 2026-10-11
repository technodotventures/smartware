// Regression tests for the third audit (results/AUDIT3.md). Each test is one finding's reproduction, turned into an
// assertion of the fixed behaviour.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { createHash } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ApiEncoder, HashingEncoder, Principal, Service, SqliteStore, chooseRanker } from "@technodotventures/smartware-core";
import { Gateway, routeTemplate } from "../dist/http.js";
import { Log } from "../dist/log.js";
import { McpSession } from "../dist/mcp.js";
import { mem0Route } from "../dist/mem0.js";

const enc = new HashingEncoder();
const params = chooseRanker("auto", enc.name).params;
const tmpdb = () => join(mkdtempSync(join(tmpdir(), "smartware-audit3-")), "db.sqlite");
const open = (path) => new Service(enc, { params, store: path ? new SqliteStore(path) : null, secretScan: false });
const P = (id, groups = [], tenant = "t1") => new Principal(id, groups, tenant);
const star = (t) => new Principal("root", ["*"], t);
const auditor = (t) => new Principal("aud", ["auditor", "*"], t);
const code = async (p) => { try { await p; return "ok"; } catch (e) { return e.code ?? String(e); } };

test("a failed write keeps the in-memory audit log", async () => {
  const s = open(null), a = P("alice");
  await s.remember({ memories: [{ id: "x", content: "first fact" }, { id: "y", content: "second fact" }] }, a);
  assert.equal(await code(s.remember({ memories: [{ id: "x", content: "again" }] }, a)), "version_conflict");
  assert.equal((await s.auditLog(auditor("t1"))).entries.length, 2);
});

test("a failing batch is all-or-nothing, with or without a store", async () => {
  for (const db of [null, tmpdb()]) {
    const s = open(db), a = P("alice");
    await s.remember({ memories: [{ id: "dup", content: "exists already" }] }, a);
    assert.equal(await code(s.remember({ memories: [{ id: "new1", content: "first of batch" }, { id: "dup", content: "conflicts" }] }, a)), "version_conflict");
    assert.equal((await s.get(["new1"], a)).memories.length, 0);
    assert.deepEqual((await s.changes(0, a)).changes.map((c) => c.id), ["dup"]);
    s.close();
  }
});

test("a forget in one tenant never touches another tenant's records", async () => {
  const db = tmpdb();
  let s = open(db);
  const alice = P("alice", [], "A"), bob = P("bob", [], "B");
  await s.remember({ memories: [{ id: "src", content: "Alice moved to Paris" }] }, alice);
  await s.remember({ memories: [{ id: "sum", kind: "summary", content: "Alice lives in Paris", origin: "extracted", derived_from: ["src"] }] }, alice);
  await s.forget({ ids: ["src"], mode: "retract" }, alice);
  await s.remember({ memories: [{ id: "x", content: "unrelated note in tenant B" }] }, bob);
  const r = await s.forget({ ids: ["x"], mode: "purge" }, bob);
  assert.deepEqual(r.affected, ["x"]);
  assert.equal((await s.get(["sum"], star("A"))).memories[0].status, "retracted");
  assert.deepEqual((await s.export(star("B"))).manifest.tombstones.map((x) => x.id), ["x"]);
  const live = (await s.recall({ query: "paris" }, star("A"))).snapshot;
  s.close();
  s = open(db);
  assert.equal((await s.recall({ query: "paris" }, star("A"))).snapshot, live);
  s.close();
});

test("export is byte-stable across a restart (edges and tombstones sorted)", async () => {
  const db = tmpdb();
  let s = open(db);
  const r = star("t1");
  await s.remember({ memories: ["alpha", "beta", "gamma", "delta"].map((c, i) => ({ id: "abcd"[i], content: c })) }, r);
  await s.link({ type: "relates", from: "a", to: "b" }, r);
  await s.link({ type: "relates", from: "b", to: "d" }, r);
  await s.forget({ ids: ["c"], mode: "purge" }, r);
  const ex = await s.export(r);
  await s.import({ "memories.jsonl": "", "edges.jsonl": ex["edges.jsonl"].split("\n")[0], manifest: { tenant: "t1", tombstones: ex.manifest.tombstones } }, r);
  const { request_id: _a, ...live } = await s.export(r);
  s.close();
  s = open(db);
  const { request_id: _b, ...fresh } = await s.export(r);
  s.close();
  assert.equal(JSON.stringify(fresh), JSON.stringify(live));
  const ids = live["edges.jsonl"].split("\n").map((l) => JSON.parse(l).id);
  assert.deepEqual(ids, [...ids].sort());
});

test("meta nesting is limited", async () => {
  const s = open(null), a = P("alice");
  let deep = {};
  for (let i = 0; i < 100; i++) deep = { a: deep };
  assert.equal(await code(s.remember({ memories: [{ content: "x", meta: { d: deep } }] }, a)), "invalid_parameter");
  assert.equal(await code(s.remember({ memories: [{ content: "x", meta: { d: { e: [1, { f: 2 }] } } }] }, a)), "ok");
});

test("history survives a claim being removed or moved by upsert", async () => {
  const s = open(null), a = P("alice");
  await s.remember({ memories: [{ id: "f", kind: "fact", content: "Alice lives in Paris", claim: { subject: "alice", predicate: "city", object: "paris" } }] }, a);
  await s.remember({ memories: [{ id: "f", kind: "fact", content: "Alice likes tea" }], mode: "upsert", if_version: 1 }, a);
  assert.deepEqual((await s.history("alice", "city", a)).versions, []);
  await s.remember({ memories: [{ id: "g", kind: "fact", content: "Bob works at Acme", valid_from: "2024-01-01", claim: { subject: "bob", predicate: "job", object: "acme" } }] }, a);
  await s.remember({ memories: [{ id: "g", kind: "fact", content: "Bob lives in Rome", valid_from: "2024-01-01", claim: { subject: "bob", predicate: "city", object: "rome" } }], mode: "upsert", if_version: 1 }, a);
  assert.deepEqual((await s.history("bob", "job", a)).versions, []);
  // a later claim on the old key does not supersede the moved record
  await s.remember({ memories: [{ id: "h", kind: "fact", content: "Bob works at Globex", valid_from: "2025-01-01", claim: { subject: "bob", predicate: "job", object: "globex" } }] }, a);
  const g = (await s.get(["g"], a)).memories[0];
  assert.equal(g.status, "active");
  assert.equal(g.superseded_by, null);
});

test("purging the current version of a claim restores the one it superseded", async () => {
  for (const mode of ["retract", "purge"]) {
    const s = open(null), a = P("alice");
    await s.remember({ memories: [{ id: "old", kind: "fact", content: "Alice lives in Paris", valid_from: "2024-01-01", claim: { subject: "alice", predicate: "city", object: "paris" } }] }, a);
    await s.remember({ memories: [{ id: "new", kind: "fact", content: "Alice lives in Rome", valid_from: "2025-01-01", claim: { subject: "alice", predicate: "city", object: "rome" } }] }, a);
    await s.forget({ ids: ["new"], mode }, a);
    const old = (await s.get(["old"], star("t1"))).memories[0];
    assert.equal(old.status, "active", mode);
    assert.equal(old.superseded_by, null, mode);
  }
});

test("client meta cannot act as the engine's idempotency key", async () => {
  const db = tmpdb();
  const s = open(db), a = P("alice");
  await s.remember({ memories: [{ id: "other", content: "first record", meta: { idempotency_key: "K" } }] }, a);
  await s.remember({ memories: [{ id: "doc", content: "important document v1" }] }, a);
  const r = await s.remember({ memories: [{ id: "doc", content: "important document v2", meta: { idempotency_key: "K" } }], mode: "upsert", if_version: 1 }, a);
  assert.deepEqual(r.ids, ["doc"]);
  assert.deepEqual((await s.get(["doc", "other"], a)).memories.map((m) => m.id), ["doc", "other"]);
  const r2 = await s.remember({ memories: [{ content: "a completely different note", meta: { idempotency_key: "K" } }] }, a);
  assert.notEqual(r2.ids[0], "other");
  assert.equal(await code(s.remember({ memories: [{ content: "x", meta: { _idempotency_key: "K" } }] }, a)), "invalid_parameter");
  assert.equal(await code(s.remember({ memories: [{ content: "x", meta: { entities: ["Forged"] } }] }, a)), "invalid_parameter");
  s.close();
});

test("an idempotent retry replays before re-validating", async () => {
  const s = open(null), a = P("alice");
  await s.remember({ memories: [{ id: "src", content: "meeting notes" }] }, a);
  const body = { memories: [{ kind: "summary", content: "summary of the notes", origin: "extracted", derived_from: ["src"] }], idempotency_key: "req-42" };
  const first = await s.remember(body, a);
  await s.forget({ ids: ["src"], mode: "purge", cascade: false }, a);
  const again = await s.remember(body, a);
  assert.deepEqual(again.ids, first.ids);
  assert.equal(again.replayed, true);
});

test("timestamps: ISO 8601 strings only", async () => {
  const s = open(null), a = P("alice");
  for (const v of [5, 1700000000, true, "20240101", "2024-01-01T10:00:00+05:30:15", "2024-W01-1", "2024-001", "2024-01-01T24:00:00"]) {
    assert.equal(await code(s.remember({ memories: [{ content: "probe", event_time: v }] }, a)), "invalid_parameter", JSON.stringify(v));
  }
  for (const v of ["2024-01-01T10", "2024-01-01T10:00:00.1234567", "2024-01-01 10:00Z", "2024-01-01T10:00:00+05:30"]) {
    assert.equal(await code(s.remember({ memories: [{ content: "probe " + v, event_time: v }] }, a)), "ok", v);
  }
});

test("import: edges and tombstones never overwrite another tenant's, even one not loaded", async () => {
  const db = tmpdb();
  let s = open(db);
  const a = P("alice", [], "A");
  await s.remember({ memories: [{ id: "x", content: "alpha one" }, { id: "y", content: "alpha two" }] }, a);
  const { id: eid } = await s.link({ type: "relates", from: "x", to: "y" }, a);
  const r2 = await s.remember({ memories: [{ content: "engine id record purged" }] }, a);
  await s.forget({ ids: [r2.ids[0]], mode: "purge" }, a);
  const before = await s.export(star("A"));
  s.close();
  s = open(db);
  const b = star("B");
  await s.remember({ memories: [{ id: "p", content: "beta" }, { id: "q", content: "gamma" }] }, b);
  await s.import({ "memories.jsonl": "", "edges.jsonl": JSON.stringify({ id: eid, type: "relates", from: "p", to: "q" }),
    manifest: { tenant: "B", tombstones: [{ id: r2.ids[0], at: "2026-01-01T00:00:00" }] } }, b);
  s.close();
  s = open(db);
  const after = await s.export(star("A"));
  assert.equal(after["edges.jsonl"], before["edges.jsonl"]);
  assert.deepEqual(after.manifest.tombstones, before.manifest.tombstones);
  s.close();
});

test("import: a bad edge changes nothing (checked before any write), and malformed lines are invalid_parameter", async () => {
  const s = open(tmpdb()), b = star("B");
  await s.remember({ memories: [{ id: "p", content: "beta" }, { id: "q", content: "gamma" }] }, b);
  const bundle = { "memories.jsonl": JSON.stringify({ id: "n1", content: "new", kind: "event", tenant: "B" }),
    "edges.jsonl": [JSON.stringify({ id: "edge_ok", type: "relates", from: "p", to: "q" }), JSON.stringify({ id: "edge_bad", type: "relates", from: "p", to: "nope" })].join("\n"),
    manifest: { tenant: "B", tombstones: [] } };
  assert.equal(await code(s.import(bundle, b)), "invalid_parameter");
  assert.deepEqual(await s.edges("p", b), []);
  assert.equal((await s.get(["n1"], b)).memories.length, 0);
  assert.equal(await code(s.import({ "memories.jsonl": "", "edges.jsonl": "{not json" }, b)), "invalid_parameter");
  assert.equal(await code(s.import({ "memories.jsonl": "[1,2]" }, b)), "invalid_parameter");
  s.close();
});

test("list validates parameter values", async () => {
  const s = open(null), a = P("alice");
  await s.remember({ memories: [{ content: "hello", scope: "a" }] }, a);
  assert.equal(await code(s.list({ kinds: ["bogus"] }, a)), "unsupported_parameter");
  for (const q of [{ scope: 42 }, { thread: ["x"] }, { include_history: "no" }, { kinds: "event" }]) {
    assert.equal(await code(s.list(q, a)), "invalid_parameter", JSON.stringify(q));
  }
  assert.equal(await code(s.list({ kinds: ["x-custom"] }, a)), "ok");
});

test("reflect honours scope and rejects kinds it cannot produce", async () => {
  const s = open(null), a = P("alice");
  await s.remember({ memories: [{ content: "standup notes one", scope: "team/a", thread: "t-a" }, { content: "planning notes", scope: "team/b", thread: "t-b" }] }, a);
  assert.equal(await code(s.reflect({ kinds: ["procedure"] }, a)), "unsupported_parameter");
  const r = await s.reflect({ scope: "team/a/" }, a);
  assert.equal(r.created.length, 1);
  assert.equal((await s.get(r.created, a)).memories[0].thread, "t-a");
});

test("MCP tool arguments are type-checked", async () => {
  const s = open(null), pr = P("alice");
  const m = new McpSession(s, pr, { toolsets: ["safe"], defaultReaders: "self" });
  const call = async (name, args) => (await m.handle({ jsonrpc: "2.0", id: 2, method: "tools/call", params: { name, arguments: args } })).result;
  for (const args of [{ query: "paris", budget_tokens: 120.9 }, { query: "paris", include_history: "false" }, { query: "paris", budget_tokens: "300" }]) {
    const r = await call("smartware_recall", args);
    assert.equal(r.isError, true, JSON.stringify(args));
    assert.equal(r.structuredContent.error.code, "invalid_parameter");
  }
  assert.equal((await call("smartware_recall", { query: "paris", budget_tokens: 300 })).isError, false);
});

test("mem0 PUT keeps the record's other fields and gets fresh derived meta", async () => {
  const s = open(null), pr = P("alice");
  const call = async (method, path, body) => {
    let out;
    const url = new URL("http://x" + path);
    await mem0Route(s, pr, { method }, {}, url.pathname.split("/").filter(Boolean), url, async () => body ?? {}, (st, b) => { out = { st, b }; });
    return out;
  };
  await s.remember({ memories: [{ id: "k", content: "I met Gandalf in Rivendell yesterday", event_time: "2026-10-10T09:00:00Z", valid_from: "2026-10-10", meta: { tag: "keep" } }] }, pr);
  const put = await call("PUT", "/mem0/v1/memories/k", { text: "Lunch with Bob in Paris next Friday" });
  assert.equal(put.st, 200);
  const after = (await s.get(["k"], pr)).memories[0];
  assert.equal(after.content, "Lunch with Bob in Paris next Friday");
  assert.equal(after.event_time, "2026-10-10T09:00:00");
  assert.equal(after.valid_from, "2026-10-10T00:00:00");
  assert.equal(after.meta.tag, "keep");
  assert.ok(!(after.meta.entities ?? []).includes("Gandalf"));
});

test("remote embeddings: purge deletes stored vectors; an outage degrades recall and fails writes with 503", async () => {
  let down = false;
  const srv = createServer((req, res) => {
    let b = "";
    req.on("data", (c) => (b += c));
    req.on("end", () => {
      if (down) { res.writeHead(503); return res.end("down"); }
      const { input } = JSON.parse(b);
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ data: input.map((t, index) => ({ index, embedding: Array.from({ length: 8 }, (_, j) => createHash("md5").update(t + j).digest()[0] - 128) })) }));
    });
  });
  await new Promise((r) => srv.listen(0, "127.0.0.1", r));
  try {
    const api = new ApiEncoder(`http://127.0.0.1:${srv.address().port}/v1/embeddings`, "fake", 8);
    const s = new Service(api, { params: chooseRanker("auto", api.name).params, store: new SqliteStore(tmpdb()), secretScan: false });
    const a = P("alice");
    await s.remember({ memories: [{ id: "x", content: "my bank PIN is 4921" }, { id: "y", content: "lunch on friday" }] }, a);
    const sha = createHash("sha256").update("alice: my bank PIN is 4921").digest("hex");
    const has = () => Boolean(s.store.db.prepare("SELECT 1 FROM vectors WHERE text_sha = ?").get(sha));
    assert.equal(has(), true);
    await s.forget({ ids: ["x"], mode: "purge" }, a);
    assert.equal(has(), false);
    down = true;
    const r = await s.recall({ query: "what do I do on friday" }, a);
    assert.deepEqual(r.degraded, ["dense_unavailable"]);
    assert.ok(r.items.some((i) => i.memory_ids.includes("y")));
    assert.equal(await code(s.remember({ memories: [{ content: "a new note" }] }, a)), "unavailable");
    s.close();
  } finally {
    srv.close();
  }
});

test("a tenant that fails to load is not marked loaded, so it is never served half-loaded", async () => {
  const db = tmpdb();
  let s = open(db);
  const a = P("alice");
  await s.remember({ memories: [{ id: "keep", content: "an important record" }, { id: "bad", content: "row to corrupt" }] }, a);
  s.store.db.prepare("UPDATE memories SET record = json_set(record, '$.future_field', 1) WHERE iid = 'bad~t1'").run();
  s.close();
  s = open(db);
  assert.notEqual(await code(s.get(["keep"], a)), "ok");
  assert.notEqual(await code(s.get(["keep"], a)), "ok"); // still refuses, rather than answering from a partial load
  assert.notEqual(await code(s.remember({ memories: [{ id: "new", content: "x" }] }, a)), "ok");
  s.close();
});

test("a failed batch in one tenant does not unload other tenants", async () => {
  const s = open(tmpdb());
  const big = P("u", [], "big"), other = P("mallory", [], "small");
  await s.remember({ memories: Array.from({ length: 300 }, (_, k) => ({ content: `note ${k} about project ${k % 37}` })) }, big);
  await s.recall({ query: "project" }, big);
  const part = s.engine.parts?.get?.("big");
  await s.remember({ memories: [{ id: "a", content: "x" }] }, other);
  assert.equal(await code(s.remember({ memories: [{ content: "fresh" }, { id: "a", content: "dup" }] }, other)), "version_conflict");
  if (part) assert.equal(s.engine.parts.get("big"), part);
  assert.equal((await s.list({}, big)).memories.length, 100);
  s.close();
});

test("HTTP: If-Match on DELETE is checked atomically; bad paths are 400; metrics use route templates", async () => {
  const s = open(null), alice = P("alice");
  const gw = new Gateway(s, { resolve: async () => alice }, { host: "127.0.0.1", port: 0, metricsToken: "m", mcp: { toolsets: ["safe"], defaultReaders: "self" } }, new Log("silent"));
  await gw.listen();
  try {
    await s.remember({ memories: [{ id: "doc", content: "v1 text" }] }, alice);
    await s.remember({ memories: [{ id: "doc", content: "v2 text" }], mode: "upsert", if_version: 1 }, alice);
    let res = await fetch(gw.url + "/v1/t1/memories/doc", { method: "DELETE", headers: { "if-match": '"1"', authorization: "Bearer x" } });
    assert.equal(res.status, 409);
    assert.equal((await s.get(["doc"], alice)).memories[0].status, "active");
    res = await fetch(gw.url + "/v1/t1/memories/doc", { method: "DELETE", headers: { "if-match": '"2"', authorization: "Bearer x" } });
    assert.equal(res.status, 200);
    res = await fetch(gw.url + "/v1/t1/memories/%E0%A4%A", { headers: { authorization: "Bearer x" } });
    assert.equal(res.status, 400);
    await fetch(gw.url + "/v1/t1/memories/some-secret-id-123", { headers: { authorization: "Bearer x" } });
    const metrics = await (await fetch(gw.url + "/metrics", { headers: { authorization: "Bearer m" } })).text();
    assert.ok(!metrics.includes("some-secret-id-123"));
    assert.ok(metrics.includes('route="GET /v1/:tenant/memories/:id"'));
  } finally {
    await gw.close();
  }
  assert.equal(routeTemplate("GET", ["v1", "t", "jobs", "job_1"]), "GET /v1/:tenant/jobs/:id");
  assert.equal(routeTemplate("GET", ["anything", "else"]), "GET other");
});

test("HTTP: repeated failed authentication from one address is rate limited", async () => {
  const s = open(null);
  const { AuthError } = await import("../dist/auth.js");
  const auth = { resolve: async () => { throw new AuthError("bad token", 401, "invalid_token"); } };
  const gw = new Gateway(s, auth, { host: "127.0.0.1", port: 0, authFailRate: { rps: 0.1, burst: 3 }, mcp: { toolsets: ["safe"], defaultReaders: "self" } }, new Log("silent"));
  await gw.listen();
  try {
    const st = [];
    for (let i = 0; i < 5; i++) st.push((await fetch(gw.url + "/v1/t1/capabilities", { headers: { authorization: "Bearer nope" } })).status);
    assert.deepEqual(st, [401, 401, 401, 429, 429]);
  } finally {
    await gw.close();
  }
});

// ------------------------------------------------------------ fourth audit (results/AUDIT4.md)
test("NUL is refused in any string of a request", async () => {
  const s = open(tmpdb()), a = P("alice");
  for (const body of [{ memories: [{ id: "a\u0000b", content: "x" }] }, { memories: [{ content: "x\u0000y" }] }, { memories: [{ content: "x" }], idempotency_key: "k\u0000" }]) {
    assert.equal(await code(s.remember(body, a)), "invalid_parameter");
  }
  assert.equal(await code(s.recall({ query: "a\u0000" }, a)), "invalid_parameter");
  s.close();
});

test("a purged record takes its edges with it, so a re-used id does not inherit them", async () => {
  const s = open(tmpdb()), r = star("t1");
  await s.remember({ memories: [{ id: "ann", content: "Ann" }, { id: "bob", content: "Bob" }] }, r);
  await s.link({ type: "relates", from: "ann", to: "bob" }, r);
  await s.forget({ ids: ["bob"], mode: "purge" }, r);
  await s.remember({ memories: [{ id: "bob", content: "a different Bob" }], allow_reingest: true }, r);
  assert.deepEqual(await s.edges("ann", r), []);
  assert.equal((await s.export(r))["edges.jsonl"], "");
  s.close();
});

test("re-importing a bundle whose engine ids collide with another tenant's is idempotent", async () => {
  const { exportBundle, importBundle } = await import("../dist/bundle.js");
  const s = open(tmpdb());
  const r = await s.remember({ memories: [{ content: "Alice moved to Lisbon in May" }, { content: "Bob likes tea" }, { content: "gone soon" }] }, P("alice", [], "src"));
  await s.forget({ ids: [r.ids[2]], mode: "purge" }, P("alice", [], "src"));
  const b = await exportBundle(s, "src");
  const sw = (buf) => Buffer.from(buf.toString().split("\n").map((l) => (l.trim() ? JSON.stringify({ ...JSON.parse(l), tenant: "dst" }) : l)).join("\n"));
  const mem = sw(b.get("memories.jsonl")), edges = sw(b.get("edges.jsonl"));
  const man = JSON.parse(b.get("manifest.json"));
  man.tenant = "dst";
  const { createHash } = await import("node:crypto");
  man.files = { "memories.jsonl": { sha256: createHash("sha256").update(mem).digest("hex") }, "edges.jsonl": { sha256: createHash("sha256").update(edges).digest("hex") } };
  const files = new Map([["manifest.json", Buffer.from(JSON.stringify(man))], ["memories.jsonl", mem], ["edges.jsonl", edges]]);
  const counts = [];
  for (let i = 0; i < 3; i++) {
    await importBundle(s, "dst", files);
    const e = await exportBundle(s, "dst");
    counts.push([e.get("memories.jsonl").toString().trim().split("\n").length, JSON.parse(e.get("manifest.json")).tombstones.length]);
  }
  assert.deepEqual(counts, [[2, 1], [2, 1], [2, 1]]);
  s.close();
});

test("export, import, export is byte-equal for redacted records too", async () => {
  const { exportBundle, importBundle } = await import("../dist/bundle.js");
  const s = open(tmpdb()), a = P("alice", [], "src");
  await s.remember({ memories: [{ content: "Alice moved to Lisbon in May", thread: "th" }, { id: "c1", kind: "fact", content: "Alice lives in Paris", claim: { subject: "alice", predicate: "city", object: "paris" } }] }, a);
  await s.forget({ ids: ["c1"], mode: "redact" }, a);
  const b1 = await exportBundle(s, "src");
  const s2 = open(tmpdb());
  await importBundle(s2, "src", b1);
  const b2 = await exportBundle(s2, "src");
  assert.equal(b2.get("memories.jsonl").toString(), b1.get("memories.jsonl").toString());
  s.close(); s2.close();
});

test("HTTP: only path request targets are served (no absolute-form or //authority, which a forward could turn into SSRF)", async () => {
  const s = open(null), alice = P("alice");
  const gw = new Gateway(s, { resolve: async () => alice }, { host: "127.0.0.1", port: 0, mcp: { toolsets: ["safe"], defaultReaders: "self" } }, new Log("silent"));
  await gw.listen();
  const { connect } = await import("node:net");
  const raw = (target) => new Promise((ok) => {
    const c = connect(gw.server.address().port, "127.0.0.1", () => c.write(`GET ${target} HTTP/1.1\r\nhost: x\r\nauthorization: Bearer x\r\nconnection: close\r\n\r\n`));
    let d = "";
    c.on("data", (x) => (d += x));
    c.on("end", () => ok(d.split("\r\n")[0]));
  });
  try {
    assert.match(await raw("http://169.254.169.254/v1/t1/capabilities"), / 400 /);
    assert.match(await raw("//169.254.169.254/v1/t1/capabilities"), / 400 /);
    assert.match(await raw("/v1/t1/capabilities"), / 200 /);
  } finally {
    await gw.close();
  }
});

test("bundles: entries may not overlap or run into the central directory, and the manifest must hash both data files", async () => {
  const { readZip, writeZip, verifyBundle } = await import("../dist/bundle.js");
  const z = writeZip(new Map([["a.txt", Buffer.from("hello hello hello")], ["b.txt", Buffer.from("world")]]));
  // point b's central directory entry at a's local header: two names, one body
  const cd = z.indexOf(Buffer.from([0x50, 0x4b, 0x01, 0x02]));
  const second = z.indexOf(Buffer.from([0x50, 0x4b, 0x01, 0x02]), cd + 4);
  const bad = Buffer.from(z);
  bad.writeUInt32LE(0, second + 42);
  assert.throws(() => readZip(bad), /overlap|corrupt|checksum/);
  assert.throws(() => verifyBundle(new Map([["manifest.json", Buffer.from("{}")], ["memories.jsonl", Buffer.from("")]])), /must list memories.jsonl/);
});
