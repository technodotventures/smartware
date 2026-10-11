// Core engine tests that need no Python: schema drift, persistence round trips, lazy tenant loading, secret scan.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { HashingEncoder, Principal, Service, SqliteStore, schema, MEM_KEYS, REMEMBER_KEYS, RECALL_KEYS, FORGET_KEYS,
  scanSecrets, chooseRanker } from "../dist/index.js";

const A = new Principal("alice", ["sales"], "acme");
const ROOT = new Principal("root", ["*"], "acme");
const strip = (o) => { const { request_id, ...r } = o; return r; };

test("validators match the schema pack", () => {
  const keys = (n) => new Set(Object.keys(schema.SCHEMAS[n].properties));
  assert.deepEqual(keys("MemoryInput"), MEM_KEYS);
  assert.deepEqual(keys("RememberRequest"), REMEMBER_KEYS);
  assert.deepEqual(keys("RecallRequest"), RECALL_KEYS);
  assert.deepEqual(keys("ForgetRequest"), FORGET_KEYS);
});

test("store round trip: a fresh process sees the same state, and tenants load lazily", async () => {
  const path = join(mkdtempSync(join(tmpdir(), "orp-core-")), "orp.db");
  const enc = new HashingEncoder();
  const p = chooseRanker("auto", enc.name).params;
  const s = new Service(enc, { params: p, store: new SqliteStore(path) });
  const ids = (await s.remember({ memories: [{ content: "Alice moved to Berlin", kind: "fact", readers: ["group:sales"],
    claim: { subject: "alice", predicate: "lives_in", object: "Berlin" }, valid_from: "2025-01-01" }], idempotency_key: "k1" }, A)).ids;
  await s.remember({ memories: [{ content: "Alice moved to Paris", kind: "fact", readers: ["group:sales"],
    claim: { subject: "alice", predicate: "lives_in", object: "Paris" }, valid_from: "2025-06-01" }] }, A);
  await s.remember({ memories: [{ content: "Other tenant note" }] }, new Principal("eve", [], "other"));
  await s.link({ type: "relates", from: ids[0], to: ids[0] }, A);
  const gone = (await s.remember({ memories: [{ content: "temporary" }] }, A)).ids;
  await s.forget({ ids: gone, mode: "retract" }, A);
  await s.reflect({}, A);
  const before = strip(await s.export(ROOT));
  const rec = strip(await s.recall({ query: "where does alice live", as_of: "2026-01-01" }, A));
  s.close();
  const t = new Service(enc, { params: p, store: new SqliteStore(path) });
  assert.equal(t.engine.mem.size, 0); // nothing loaded until a tenant is used
  assert.deepEqual(strip(await t.export(ROOT)), before);
  assert.equal([...t.engine.mem.values()].filter((m) => m.tenant === "other").length, 0); // only acme loaded
  assert.deepEqual(strip(await t.recall({ query: "where does alice live", as_of: "2026-01-01" }, A)), rec);
  assert.equal((await t.remember({ memories: [{ content: "x" }], idempotency_key: "k1" }, A)).replayed, true);
  await assert.rejects(t.remember({ memories: [{ content: "temporary" }] }, A), /re-ingest blocked/);
  t.close();
});

test("a failed write leaves the store unchanged (batches are atomic)", async () => {
  const path = join(mkdtempSync(join(tmpdir(), "orp-core-")), "orp.db");
  const s = new Service(new HashingEncoder(), { store: new SqliteStore(path) });
  await s.remember({ memories: [{ id: "a", content: "first" }] }, A);
  await assert.rejects(s.remember({ memories: [{ id: "b", content: "second" }, { id: "a", content: "dup" }] }, A), /exists/);
  const g = await s.get(["b"], A);
  assert.deepEqual(g.not_found, ["b"]); // the first item of the failed batch was rolled back
  s.close();
});

test("secret scan labels credentials", async () => {
  assert.deepEqual(scanSecrets("plain note about lunch"), []);
  assert.ok(scanSecrets("my key is sk-ant-api03-abcdefghijklmnopqrstuvwx").includes("anthropic-key"));
  assert.ok(scanSecrets("export AWS=AKIAABCDEFGHIJKLMNOP").includes("aws-access-key"));
  assert.ok(scanSecrets("password: hunter2hunter2").includes("password-assignment"));
  const s = new Service(new HashingEncoder());
  const id = (await s.remember({ memories: [{ content: "deploy token ghp_abcdefghijklmnopqrstuvwxyz0123456789AB" }] }, A)).ids[0];
  const m = s.engine.mem.get(id);
  assert.ok(m.labels.includes("secret"));
  const r = await s.recall({ query: "deploy token" }, A);
  assert.ok(!r.items.some((i) => i.text.includes("ghp_")));
});
