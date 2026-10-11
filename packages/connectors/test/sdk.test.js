// Connector SDK: scope-prefix refusal, privileged-principal refusal, id and ACL mapping, state, idempotent retries.
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { clientId, defineConnector, mapAcl, OrpHttp, ScopedWriter, ScopeError, sync, underPrefix } from "../dist/index.js";
import { api, getRec, startServer } from "./helpers.js";

const item = (id, extra = {}) => ({ id, content: `note ${id} about the quarterly roadmap`, acl: ["alice"], uri: `test://${id}`, ...extra });

test("definitions are validated and ids are stable, safe client ids", () => {
  assert.throws(() => defineConnector({ name: "Bad Name", scopePrefix: "x", pull: () => ({ items: [], cursor: null }) }));
  assert.throws(() => defineConnector({ name: "mem", scopePrefix: "x", pull: () => ({ items: [], cursor: null }) }));
  assert.throws(() => defineConnector({ name: "ok", scopePrefix: "a/../b", pull: () => ({ items: [], cursor: null }) }), ScopeError);
  const id = clientId("vault", "notes/a~b.md#intro");
  assert.equal(id, clientId("vault", "notes/a~b.md#intro"));
  assert.ok(!id.includes("~") && !id.includes("/"));
  assert.notEqual(id, clientId("vault", "notes/a~b.md#intro2"));
  assert.ok(underPrefix("vault/team", "vault") && underPrefix("vault", "vault") && !underPrefix("vault-x", "vault"));
});

test("source ACLs map to reader tokens and fail closed", () => {
  const res = (s) => ({ U1: "alice", U2: "bob" })[s] ?? null;
  assert.deepEqual(mapAcl(["U1", "U2", "U3", "group:eng", "channel:C1", "evil:x", "public"], res),
    ["channel:C1", "group:eng", "principal:alice", "principal:bob", "public"]);
  assert.deepEqual(mapAcl(["U3"], res), [], "an unknown user maps to nobody, never to the whole tenant");
});

test("writes outside the scope prefix are refused and nothing is written", async (t) => {
  const srv = await startServer();
  t.after(() => srv.close());
  const statePath = join(srv.dir, "s.json");
  const bad = defineConnector({ name: "notes", scopePrefix: "vault", pull: () => ({ items: [item("a"), item("b", { scope: "hr/salaries" })], cursor: 1 }) });
  await assert.rejects(sync(bad, { url: srv.url, token: srv.tokens.connector, tenant: srv.tenant, statePath }), ScopeError);
  const sneaky = defineConnector({ name: "notes", scopePrefix: "vault", pull: () => ({ items: [item("c", { scope: "vault-other" })], cursor: 1 }) });
  await assert.rejects(sync(sneaky, { url: srv.url, token: srv.tokens.connector, tenant: srv.tenant, statePath }), ScopeError);
  const all = await api(srv, "admin", "GET", "/memories");
  assert.equal(all.body.memories.length, 0, "validation happens before the first write");
  assert.equal(existsSync(statePath), false);

  // the writer itself refuses too, for code that bypasses the runner
  const w = new ScopedWriter(new OrpHttp({ url: srv.url, token: srv.tokens.connector, tenant: srv.tenant }), "vault", () => false);
  await assert.rejects(w.remember({ memories: [{ content: "x", scope: "hr", readers: ["principal:alice"] }] }), ScopeError);
  await assert.rejects(w.remember({ memories: [{ content: "x", scope: "vault", readers: null }] }), /explicit readers/);
  await assert.rejects(w.forget({ ids: ["someone-elses-record"] }), ScopeError);

  // an item may not carry un-namespaced meta, and a group change may not name a principal or privileged group
  const meta = defineConnector({ name: "notes", scopePrefix: "vault", pull: () => ({ items: [item("d", { meta: { owner: "x" } })], cursor: 1 }) });
  await assert.rejects(sync(meta, { url: srv.url, token: srv.tokens.connector, tenant: srv.tenant, statePath }), /x-/);
  const grp = defineConnector({ name: "notes", scopePrefix: "vault", pull: () => ({ items: [], aclChanges: [{ group: "admin", members: ["bob"] }], cursor: 1 }) });
  await assert.rejects(sync(grp, { url: srv.url, token: srv.tokens.connector, tenant: srv.tenant, statePath }), ScopeError);
});

test("a connector may not run as a privileged principal", async (t) => {
  const srv = await startServer();
  t.after(() => srv.close());
  const c = defineConnector({ name: "notes", scopePrefix: "vault", pull: () => ({ items: [item("a")], cursor: 1 }) });
  await assert.rejects(sync(c, { url: srv.url, token: srv.tokens.admin, tenant: srv.tenant, statePath: join(srv.dir, "s.json") }), /privileged/);
});

test("state survives a lost write acknowledgement: replays are idempotent and versions stay right", async (t) => {
  const srv = await startServer();
  t.after(() => srv.close());
  let content = "first version of the roadmap note";
  const c = defineConnector({ name: "notes", scopePrefix: "vault", pull: () => ({ items: [item("a", { content })], cursor: 1 }) });
  const statePath = join(srv.dir, "s.json");
  const opts = { url: srv.url, token: srv.tokens.connector, tenant: srv.tenant, statePath };
  await sync(c, opts);
  const saved = readFileSync(statePath, "utf8");
  content = "second version of the roadmap note";
  await sync(c, opts);
  writeFileSync(statePath, saved); // simulate a crash after the write but before the state was saved
  const r = await sync(c, opts);
  assert.equal(r.updated, 1);
  const rec = await getRec(srv, "alice", clientId("notes", "a"));
  assert.equal(rec.content, content);
  assert.equal(rec.version, 2, "the replay did not write a third version");
  // and A -> B -> A is a real change, not mistaken for a replay
  content = "first version of the roadmap note";
  await sync(c, opts);
  assert.equal((await getRec(srv, "alice", clientId("notes", "a"))).content, content);
  // the state file belongs to one connector
  const other = defineConnector({ name: "other", scopePrefix: "vault", pull: () => ({ items: [], cursor: 1 }) });
  await assert.rejects(sync(other, opts), /belongs to connector/);
});

test("purge mode removes deleted items for good", async (t) => {
  const srv = await startServer();
  t.after(() => srv.close());
  let items = [item("a"), item("b")];
  const c = defineConnector({ name: "notes", scopePrefix: "vault", pull: (cur) => ({ items, deletions: (cur ?? []).filter((x) => !items.some((i) => i.id === x)), cursor: items.map((i) => i.id) }) });
  const opts = { url: srv.url, token: srv.tokens.connector, tenant: srv.tenant, statePath: join(srv.dir, "s.json"), deleteMode: "purge" };
  await sync(c, opts);
  items = [item("a")];
  const r = await sync(c, opts);
  assert.equal(r.deleted, 1);
  assert.equal(await getRec(srv, "admin", clientId("notes", "b")), null);
  assert.ok(await getRec(srv, "alice", clientId("notes", "a")));
});
