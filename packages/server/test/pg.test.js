// Postgres store and multi-node behaviour. Runs when ORP_TEST_PG names a database the tests may create schemas in,
// e.g. ORP_TEST_PG=postgres://postgres@127.0.0.1:5432/postgres?sslmode=disable (password in PGPASSWORD).
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomBytes } from "node:crypto";
import { HashingEncoder, PgConnection, PgStore, Principal, Service, SqliteStore, parsePgUrl } from "@orp/core";
import { Gateway } from "../dist/http.js";
import { Log } from "../dist/log.js";

const PG = process.env.ORP_TEST_PG;
const skip = PG ? false : "set ORP_TEST_PG to run the Postgres tests";
const enc = new HashingEncoder();
const BIN = new URL("../bin/orp.js", import.meta.url).pathname;

function schemaUrl(schema) {
  const u = new URL(PG);
  u.searchParams.set("schema", schema);
  return u.toString();
}
const fresh = () => "t_" + randomBytes(5).toString("hex");
const svc = (schema, opts = {}) => new Service(enc, { secretScan: false, store: new PgStore(schemaUrl(schema), { leaseMs: opts.leaseMs ?? 30_000, nodeId: opts.node, nodeUrl: opts.url ?? null, poolSize: 2 }), ...opts.svc });
const A = (t = "acme") => new Principal("alice", [], t);
const code = async (p) => { try { await p; return "ok"; } catch (e) { return e.code ?? String(e); } };

async function sql(text, params) {
  const c = await PgConnection.connect(parsePgUrl(PG));
  try { return (await c.query(text, params)).rows; } finally { await c.close(); }
}

test("records, claims, forgets and counters survive a restart; a write is all or nothing", { skip }, async () => {
  const sc = fresh();
  let s = svc(sc, { node: "n1" });
  await s.remember({ memories: [{ id: "x", content: "Alice moved to Lisbon in May" },
    { id: "c1", kind: "fact", content: "Alice lives in Paris", claim: { subject: "alice", predicate: "city", object: "paris" }, valid_from: "2024-01-01" }] }, A());
  await s.remember({ memories: [{ id: "c2", kind: "fact", content: "Alice lives in Rome", claim: { subject: "alice", predicate: "city", object: "rome" }, valid_from: "2025-01-01" }] }, A());
  await s.forget({ ids: ["x"], mode: "purge" }, A());
  assert.equal(await code(s.remember({ memories: [{ id: "new", content: "first" }, { id: "c1", content: "clash" }] }, A())), "version_conflict");
  const before = await s.export(new Principal("root", ["*"], "acme"));
  const snap = (await s.recall({ query: "where does alice live" }, A())).snapshot;
  await s.close();
  s = svc(sc, { node: "n1" });
  const after = await s.export(new Principal("root", ["*"], "acme"));
  assert.equal(after["memories.jsonl"], before["memories.jsonl"]);
  assert.deepEqual(after.manifest.tombstones, before.manifest.tombstones);
  assert.equal((await s.recall({ query: "where does alice live" }, A())).snapshot, snap);
  assert.deepEqual((await s.history("alice", "city", A())).versions.map((v) => [v.id, v.status]), [["c1", "superseded"], ["c2", "active"]]);
  assert.equal((await s.get(["new"], A())).memories.length, 0);
  await s.close();
});

test("one node per tenant: another node is told who holds it, and a write after losing the lease is refused", { skip }, async () => {
  const sc = fresh();
  const a = svc(sc, { node: "node-a", url: "http://a.internal:1" }), b = svc(sc, { node: "node-b", url: "http://b.internal:2" });
  await a.remember({ memories: [{ id: "k", content: "held by node a" }] }, A());
  assert.deepEqual(await b.route("acme"), { mine: false, owner: "node-a", url: "http://a.internal:1" });
  assert.equal(await code(b.recall({ query: "held" }, A())), "unavailable");
  assert.deepEqual(await b.route("other"), { mine: true }, "a free tenant is taken");
  // node a stalls past its lease; node b takes the tenant over
  await sql(`UPDATE "${sc}".orp_leases SET expires_at = now() - interval '1 second' WHERE tenant = 'acme'`);
  assert.deepEqual(await b.route("acme"), { mine: true });
  await b.remember({ memories: [{ id: "k2", content: "written by node b" }] }, A());
  // a still believes it holds the lease: its write is fenced off and it lets go
  const r = await code(a.remember({ memories: [{ id: "k3", content: "stale writer" }] }, A()));
  assert.equal(r, "unavailable");
  assert.equal((await b.get(["k3"], A())).memories.length, 0);
  assert.equal((await b.get(["k", "k2"], A())).memories.length, 2);
  assert.equal(await code(a.get(["k"], A())), "unavailable", "a now forwards rather than serving a stale copy");
  const ep = await sql(`SELECT epoch FROM "${sc}".orp_leases WHERE tenant = 'acme'`);
  assert.equal(Number(ep[0].epoch), 2);
  await a.close(); await b.close();
  // closing hands the leases back at once (without waiting for expiry) and the epoch keeps counting
  const c = svc(sc, { node: "node-c" });
  assert.deepEqual(await c.route("acme"), { mine: true });
  assert.equal(Number((await sql(`SELECT epoch FROM "${sc}".orp_leases WHERE tenant = 'acme'`))[0].epoch), 3);
  assert.equal((await c.get(["k", "k2"], A())).memories.length, 2);
  await c.close();
});

test("idle tenants are released and unloaded", { skip }, async () => {
  const sc = fresh();
  const a = svc(sc, { node: "node-a", leaseMs: 3000, svc: { idleTenantMs: 200 } }), b = svc(sc, { node: "node-b" });
  await a.remember({ memories: [{ content: "soon idle" }] }, A());
  assert.equal((await b.route("acme")).mine, false);
  await new Promise((r) => setTimeout(r, 1600)); // one renewal tick (lease/3) after going idle
  assert.equal((await b.route("acme")).mine, true);
  await a.close(); await b.close();
});

test("two gateways on one store: a request to the wrong node is forwarded to the right one", { skip }, async () => {
  const sc = fresh();
  const alice = new Principal("alice", [], "acme");
  const auth = { resolve: async () => alice };
  const mk = async (node) => {
    const s = svc(sc, { node });
    const gw = new Gateway(s, auth, { host: "127.0.0.1", port: 0, mcp: { toolsets: ["safe"], defaultReaders: "self" } }, new Log("silent"));
    await gw.listen();
    s.store.leases.url = gw.url;
    return { s, gw };
  };
  const A1 = await mk("gw-a"), B1 = await mk("gw-b");
  try {
    const h = { authorization: "Bearer x", "content-type": "application/json" };
    let r = await fetch(A1.gw.url + "/v1/acme/remember", { method: "POST", headers: h, body: JSON.stringify({ memories: [{ id: "doc", content: "the launch is on Friday" }] }) });
    assert.equal(r.status, 200);
    r = await fetch(B1.gw.url + "/v1/acme/recall", { method: "POST", headers: h, body: JSON.stringify({ query: "when is the launch" }) });
    assert.equal(r.status, 200);
    assert.deepEqual((await r.json()).items[0].memory_ids, ["doc"]);
    r = await fetch(B1.gw.url + "/v1/acme/remember", { method: "POST", headers: h, body: JSON.stringify({ memories: [{ id: "doc2", content: "via node b" }] }) });
    assert.equal(r.status, 200);
    assert.equal((await A1.s.get(["doc2"], alice)).memories.length, 1, "written on the node that holds the tenant");
    r = await fetch(B1.gw.url + "/v1/acme/recall", { method: "POST", headers: { ...h, "x-orp-forwarded": "someone" }, body: JSON.stringify({ query: "x" }) });
    assert.equal(r.status, 503, "never a second hop");
  } finally {
    await A1.gw.close(); await B1.gw.close(); await A1.s.close(); await B1.s.close();
  }
});

test("orp backup copies a Postgres store into a SQLite file any orp can open", { skip }, async () => {
  const sc = fresh();
  const s = svc(sc, { node: "n1" });
  await s.remember({ memories: [{ id: "a", content: "alpha" }, { id: "b", content: "beta" }] }, A());
  await s.link({ type: "relates", from: "a", to: "b" }, new Principal("root", ["*"], "acme"));
  const ex = await s.export(new Principal("root", ["*"], "acme"));
  await s.close();
  const out = join(mkdtempSync(join(tmpdir(), "orp-pgb-")), "copy.db");
  execFileSync(process.execPath, [BIN, "backup", "--store", schemaUrl(sc), "--out", out], { encoding: "utf8" });
  const l = new Service(enc, { store: new SqliteStore(out), secretScan: false });
  const ex2 = await l.export(new Principal("root", ["*"], "acme"));
  assert.equal(ex2["memories.jsonl"], ex["memories.jsonl"]);
  assert.equal(ex2["edges.jsonl"], ex["edges.jsonl"]);
  await l.close();
});

// ORP_TEST_PG_CA: the CA file of a server with TLS on, to check certificate verification both ways
test("TLS: verify-full accepts the server's certificate with its CA and refuses it without", { skip: PG && process.env.ORP_TEST_PG_CA ? false : "set ORP_TEST_PG and ORP_TEST_PG_CA" }, async () => {
  const u = new URL(PG);
  u.searchParams.set("sslmode", "verify-full");
  const bad = await PgConnection.connect(parsePgUrl(u.toString())).then((c) => c.close().then(() => "connected"), (e) => e.message);
  assert.match(bad, /certificate/);
  u.searchParams.set("sslrootcert", process.env.ORP_TEST_PG_CA);
  const c = await PgConnection.connect(parsePgUrl(u.toString()));
  assert.deepEqual((await c.query("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")).rows, [{ ssl: true }]);
  await c.close();
});
