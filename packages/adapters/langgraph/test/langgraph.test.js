// Tests for @technodotventures/smartware-langgraph's BaseStore-compatible store against an in-process gateway.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { SmartwareClient } from "@technodotventures/smartware-client";
import { SmartwareStore } from "../dist/index.js";
import { startServer } from "../../../client/test/server.js";

let srv, alice, bob, degraded;
before(async () => {
  srv = await startServer();
  degraded = [];
  alice = new SmartwareClient({ url: srv.url, token: srv.tokens.alice, onDegraded: (v, i) => degraded.push(...v.map((x) => `${i.source}:${x}`)) });
  bob = new SmartwareClient({ url: srv.url, token: srv.tokens.bob, onDegraded: () => {} });
});
after(async () => { await srv.close(); });

test("put, get, search, delete round-trip; values are lossless", async () => {
  const store = new SmartwareStore(alice);
  const value = { food: "laksa", spice: 3, tags: ["noodles", "coconut"], nested: { ok: true, none: null, pi: 3.25 } };
  await store.put(["users", "u1", "prefs"], "lunch", value);
  const got = await store.get(["users", "u1", "prefs"], "lunch");
  assert.deepEqual(got.value, value);
  assert.equal(got.key, "lunch");
  assert.deepEqual(got.namespace, ["users", "u1", "prefs"]);
  assert.ok(got.createdAt instanceof Date && got.updatedAt instanceof Date && got.createdAt.getTime() > 0);

  // update in place: same key, new value, version moves
  await store.put(["users", "u1", "prefs"], "lunch", { ...value, food: "pho" });
  assert.equal((await store.get(["users", "u1", "prefs"], "lunch")).value.food, "pho");

  await store.put(["users", "u1", "prefs"], "drink", { drink: "flat white with oat milk" });
  const hits = await store.search(["users", "u1"], { query: "what coffee does the user drink" });
  assert.equal(hits[0].key, "drink");
  assert.equal(typeof hits[0].score, "number");
  const listed = await store.search(["users", "u1", "prefs"]);
  assert.deepEqual(listed.map((i) => i.key).sort(), ["drink", "lunch"]);
  const filtered = await store.search(["users", "u1", "prefs"], { filter: { food: "pho" } });
  assert.deepEqual(filtered.map((i) => i.key), ["lunch"]);
  assert.deepEqual((await store.search(["users", "u1", "prefs"], { filter: { "nested.pi": { $gt: 3 } } })).map((i) => i.key), ["lunch"]);

  await store.delete(["users", "u1", "prefs"], "lunch");
  assert.equal(await store.get(["users", "u1", "prefs"], "lunch"), null);
  assert.deepEqual((await store.search(["users", "u1", "prefs"])).map((i) => i.key), ["drink"]);
  // put after delete works (a store put is not a re-ingest of forgotten evidence)
  await store.put(["users", "u1", "prefs"], "lunch", { food: "ramen" });
  assert.equal((await store.get(["users", "u1", "prefs"], "lunch")).value.food, "ramen");
  // put(null) deletes
  await store.put(["users", "u1", "prefs"], "lunch", null);
  assert.equal(await store.get(["users", "u1", "prefs"], "lunch"), null);
  // the interface cannot carry as_of or budgets: the store says so instead of pretending
  assert.ok(degraded.includes("langgraph:langgraph:as_of-from-config"));
  assert.ok(degraded.includes("langgraph:langgraph:filter-after-recall") === false);
});

test("namespaces are isolated, and so are principals", async () => {
  const store = new SmartwareStore(alice);
  await store.put(["team", "x"], "k", { note: "the marsupial roadmap lives in project x" });
  await store.put(["team", "y"], "k", { note: "the marsupial roadmap is not in project y" });
  await store.put(["team", "x.y"], "k", { note: "dotted namespace segment" });
  assert.equal((await store.get(["team", "x"], "k")).value.note, "the marsupial roadmap lives in project x");
  assert.equal((await store.get(["team", "y"], "k")).value.note, "the marsupial roadmap is not in project y");
  const sx = await store.search(["team", "x"], { query: "marsupial roadmap" });
  assert.deepEqual(sx.map((i) => i.namespace), [["team", "x"]]);
  const all = await store.search(["team"], { query: "marsupial roadmap", limit: 2 });
  assert.deepEqual(all.map((i) => i.namespace.join("/")).sort(), ["team/x", "team/y"]);
  // "x" is a scope segment, not a string prefix: ["team", "x.y"] is not under ["team", "x"]
  assert.ok((await store.search(["team", "x"])).every((i) => i.namespace[1] === "x"));
  assert.deepEqual(await store.listNamespaces({ prefix: ["team"] }), [["team", "x"], ["team", "x.y"], ["team", "y"]]);
  assert.deepEqual(await store.listNamespaces({ prefix: ["team"], maxDepth: 1 }), [["team"]]);
  assert.deepEqual(await store.listNamespaces({ suffix: ["y"] }), [["team", "y"]]);

  // another principal with the same namespace and key sees none of it, and writes its own record
  const bs = new SmartwareStore(bob);
  assert.equal(await bs.get(["team", "x"], "k"), null);
  assert.deepEqual(await bs.search(["team"], { query: "marsupial roadmap" }), []);
  await bs.put(["team", "x"], "k", { note: "bob's own" });
  assert.equal((await bs.get(["team", "x"], "k")).value.note, "bob's own");
  assert.equal((await store.get(["team", "x"], "k")).value.note, "the marsupial roadmap lives in project x");
  // a different prefix is a different store
  assert.equal(await new SmartwareStore(alice, { prefix: "other" }).get(["team", "x"], "k"), null);
});

test("batch runs mixed operations in order", async () => {
  const store = new SmartwareStore(alice, { prefix: "batch" });
  const res = await store.batch([
    { namespace: ["a"], key: "1", value: { n: 1 } },
    { namespace: ["a"], key: "2", value: { n: 2 }, index: false },
    { namespace: ["a"], key: "1" },
    { namespacePrefix: ["a"], limit: 10 },
    { matchConditions: [{ matchType: "prefix", path: ["a"] }], limit: 10, offset: 0 },
    { namespace: ["a"], key: "1", value: null },
    { namespace: ["a"], key: "1" },
  ]);
  assert.equal(res[0], undefined);
  assert.deepEqual(res[2].value, { n: 1 });
  assert.deepEqual(res[3].map((i) => i.key).sort(), ["1", "2"]);
  assert.deepEqual(res[4], [["a"]]);
  assert.equal(res[6], null);
  // index: false keeps an item out of query search but not out of listing
  assert.deepEqual((await store.search(["a"], { query: "n 2" })).map((i) => i.key), []);
  assert.deepEqual((await store.search(["a"])).map((i) => i.key), ["2"]);
});

test("audience tenant shares the namespace across principals; config as_of pins search", async () => {
  const shared = new SmartwareStore(alice, { prefix: "shared", audience: "tenant" });
  await shared.put(["kb"], "wifi", { text: "The guest wifi password rotates monthly" });
  const bobShared = new SmartwareStore(bob, { prefix: "shared", audience: "tenant" });
  assert.equal((await bobShared.get(["kb"], "wifi")).value.text, "The guest wifi password rotates monthly");
  // as_of comes from config: a store pinned before the write cannot see it, one pinned after can
  const past = new SmartwareStore(alice, { prefix: "shared", audience: "tenant", asOf: "2000-01-01T00:00:00Z" });
  const later = new SmartwareStore(alice, { prefix: "shared", audience: "tenant", asOf: new Date(Date.now() + 3600_000) });
  assert.equal((await past.search(["kb"], { query: "guest wifi password" })).length, 0);
  assert.equal((await later.search(["kb"], { query: "guest wifi password" }))[0].key, "wifi");
  assert.ok(SmartwareStore.capabilities.loses.length > 0 && SmartwareStore.capabilities.level === "L2");
});
