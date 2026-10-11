// End-to-end tests for smartware serve: HTTP binding, auth (hashed keys, OIDC JWTs), MCP over Streamable HTTP and stdio,
// limits. Run: node --test packages/server/test/
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync, sign } from "node:crypto";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawn } from "node:child_process";
import { HashingEncoder, Service, SqliteStore, chooseRanker } from "@technodotventures/smartware-core";
import { Authenticator, Gateway, JwtVerifier, KeyStore, Log } from "../dist/index.js";

const dir = mkdtempSync(join(tmpdir(), "smartware-srv-"));
const keysPath = join(dir, "keys.json");
let gw, base, svc, tokA, tokB, tokEve, tokAdmin, tokStar, jwk, privKey;
const ISS = "https://idp.example.com";

function jwt(claims, { kid = "k1", alg = "RS256", key = privKey } = {}) {
  const h = Buffer.from(JSON.stringify({ alg, kid, typ: "JWT" })).toString("base64url");
  const p = Buffer.from(JSON.stringify(claims)).toString("base64url");
  const s = sign("sha256", Buffer.from(h + "." + p), key).toString("base64url");
  return `${h}.${p}.${s}`;
}

before(async () => {
  const ks = new KeyStore(keysPath);
  tokA = ks.add({ id: "alice", tenant: "acme", groups: ["sales"] });
  tokB = ks.add({ id: "bob", tenant: "acme", groups: ["eng"] });
  tokEve = ks.add({ id: "eve", tenant: "other", groups: ["sales"] });
  tokAdmin = ks.add({ id: "root", tenant: "acme", groups: ["admin", "auditor"] });
  tokStar = ks.add({ id: "ops", tenant: "acme", groups: ["*"] });
  const { publicKey, privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  privKey = privateKey;
  jwk = { ...publicKey.export({ format: "jwk" }), kid: "k1", use: "sig", alg: "RS256" };
  writeFileSync(join(dir, "jwks.json"), JSON.stringify({ keys: [jwk] }));
  const enc = new HashingEncoder();
  const { params } = chooseRanker("auto", enc.name);
  svc = new Service(enc, { params, store: new SqliteStore(join(dir, "smartware.db")) });
  const jwtv = new JwtVerifier({ issuer: ISS, audience: "http://127.0.0.1/mcp", jwks: join(dir, "jwks.json"), tenantClaim: "org" });
  gw = new Gateway(svc, new Authenticator(new KeyStore(keysPath), jwtv), {
    host: "127.0.0.1", port: 0, rate: { rps: 1000, burst: 40 }, allowedOrigins: ["https://app.example.com"], maxBody: 200_000,
    mcp: { toolsets: ["safe", "mcp-memory"], defaultReaders: "self" },
  }, new Log("silent"));
  await gw.listen();
  base = gw.url;
});

after(async () => { await gw.close(); svc.close(); });

const call = async (method, path, token, body, headers = {}) => {
  const r = await fetch(base + path, { method, headers: { ...(token ? { authorization: `Bearer ${token}` } : {}), "content-type": "application/json", ...headers },
    body: body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body) });
  const text = await r.text();
  let json; try { json = JSON.parse(text); } catch { json = text; }
  return { status: r.status, body: json, headers: r.headers };
};

test("keys file stores only hashes", async () => {
  const raw = JSON.parse((await import("node:fs")).readFileSync(keysPath, "utf8"));
  assert.ok(raw.keys.every((k) => /^sha256:[0-9a-f]{64}$/.test(k.hash)));
  assert.ok(!JSON.stringify(raw).includes(tokA));
});

test("unauthenticated requests get 401 with resource metadata", async () => {
  const r = await call("POST", "/v1/acme/recall", null, { query: "x" });
  assert.equal(r.status, 401);
  assert.match(r.headers.get("www-authenticate"), /resource_metadata="http:\/\/127\.0\.0\.1:\d+\/\.well-known\/oauth-protected-resource"/);
  const md = await call("GET", "/.well-known/oauth-protected-resource");
  assert.equal(md.status, 200);
  assert.match(md.body.resource, /\/mcp$/);
});

test("remember, recall, get with ETag, If-Match conflicts, list, delete", async () => {
  const w = await call("POST", "/v1/acme/remember", tokA, { memories: [{ id: "pref-1", content: "Alice prefers oat milk flat whites", kind: "preference", readers: ["group:sales"] }] });
  assert.equal(w.status, 200);
  assert.deepEqual(w.body.ids, ["pref-1"]);
  const r = await call("POST", "/v1/acme/recall", tokA, { query: "what coffee does alice like" });
  assert.equal(r.status, 200);
  assert.ok(r.body.items[0].text.includes("oat milk"));
  const g = await call("GET", "/v1/acme/memories/pref-1", tokA);
  assert.equal(g.headers.get("etag"), '"1"');
  const c = await call("POST", "/v1/acme/remember", tokA, { memories: [{ id: "pref-1", content: "Alice prefers long blacks", kind: "preference", readers: ["group:sales"] }], mode: "upsert" }, { "if-match": '"7"' });
  assert.equal(c.status, 409);
  assert.equal(c.body.error.code, "version_conflict");
  const ok = await call("POST", "/v1/acme/remember", tokA, { memories: [{ id: "pref-1", content: "Alice prefers long blacks", kind: "preference", readers: ["group:sales"] }], mode: "upsert" }, { "if-match": '"1"' });
  assert.equal(ok.status, 200);
  const l = await call("GET", "/v1/acme/memories?kind=preference", tokA);
  assert.equal(l.body.memories.length, 1);
  assert.equal(l.body.memories[0].content, "Alice prefers long blacks");
  const lb = await call("GET", "/v1/acme/memories?kind=preference", tokB); // bob is not in sales
  assert.equal(lb.body.memories.length, 0);
  const d = await call("DELETE", "/v1/acme/memories/pref-1", tokA);
  assert.equal(d.status, 200);
  const after = await call("GET", "/v1/acme/memories/pref-1", tokA);
  assert.equal(after.body.status, "retracted"); // get shows the record and its status; recall no longer returns it
  assert.ok(!(await call("POST", "/v1/acme/recall", tokA, { query: "long blacks" })).body.items.some((i) => i.text.includes("long blacks")));
});

test("tenants are isolated by the token, never the path", async () => {
  const r = await call("POST", "/v1/acme/recall", tokEve, { query: "coffee" });
  assert.equal(r.status, 403);
  const own = await call("POST", "/v1/other/recall", tokEve, { query: "coffee" });
  assert.equal(own.status, 200);
  assert.equal(own.body.items.length, 0);
});

test("unknown parameters are rejected, never ignored", async () => {
  const r = await call("POST", "/v1/acme/recall", tokA, { query: "x", principal: "root" });
  assert.equal(r.status, 400);
  assert.equal(r.body.error.code, "unsupported_parameter");
});

test("body limit and malformed JSON", async () => {
  const big = await call("POST", "/v1/acme/remember", tokA, { memories: [{ content: "x".repeat(300_000) }] });
  assert.equal(big.status, 413);
  const bad = await call("POST", "/v1/acme/recall", tokA, "{not json");
  assert.equal(bad.status, 400);
});

test("browser origins outside the allowlist are refused", async () => {
  const r = await call("POST", "/v1/acme/recall", tokA, { query: "x" }, { origin: "https://evil.example" });
  assert.equal(r.status, 403);
  const ok = await call("POST", "/v1/acme/recall", tokA, { query: "x" }, { origin: "https://app.example.com" });
  assert.equal(ok.status, 200);
  assert.equal(ok.headers.get("access-control-allow-origin"), "https://app.example.com");
});

test("OIDC JWT: valid token maps claims to a principal; wildcard groups are ignored", async () => {
  const now = Math.floor(Date.now() / 1000);
  const t = jwt({ iss: ISS, aud: "http://127.0.0.1/mcp", sub: "carol", groups: ["sales", "*"], org: "acme", exp: now + 300 });
  const w = await call("POST", "/v1/acme/remember", t, { memories: [{ content: "Carol owns the Q3 renewal", readers: ["group:sales"] }] });
  assert.equal(w.status, 200);
  const g = await call("GET", "/v1/acme/memories?kind=event", t);
  assert.ok(g.body.memories.some((m) => m.written_by === "carol"));
  const exp = jwt({ iss: ISS, aud: "http://127.0.0.1/mcp", sub: "carol", org: "acme", exp: now - 3600 });
  assert.equal((await call("POST", "/v1/acme/recall", exp, { query: "x" })).status, 401);
  const aud = jwt({ iss: ISS, aud: "https://other/mcp", sub: "carol", org: "acme", exp: now + 300 });
  assert.equal((await call("POST", "/v1/acme/recall", aud, { query: "x" })).status, 401);
  const forged = t.slice(0, -4) + (t.endsWith("AAAA") ? "BBBB" : "AAAA");
  assert.equal((await call("POST", "/v1/acme/recall", forged, { query: "x" })).status, 401);
  const none = Buffer.from(JSON.stringify({ alg: "none" })).toString("base64url") + "." + t.split(".")[1] + ".";
  assert.equal((await call("POST", "/v1/acme/recall", none, { query: "x" })).status, 401);
  // "*" from the IdP must not grant cross-reader access
  const rr = await call("POST", "/v1/acme/remember", tokB, { memories: [{ content: "Bob's private salary note", readers: ["principal:bob"] }] });
  assert.equal(rr.status, 200);
  const look = await call("POST", "/v1/acme/recall", t, { query: "salary note" });
  assert.ok(!look.body.items.some((i) => i.text.includes("salary")));
});

async function mcpSession(token) {
  const init = await call("POST", "/mcp", token, { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {} } },
    { accept: "application/json, text/event-stream" });
  assert.equal(init.status, 200);
  const sid = init.headers.get("mcp-session-id");
  assert.ok(sid);
  const rpc = async (id, method, params) => (await call("POST", "/mcp", token, { jsonrpc: "2.0", id, method, params },
    { "mcp-session-id": sid, "mcp-protocol-version": "2025-06-18", accept: "application/json, text/event-stream" })).body;
  return { sid, rpc };
}

test("MCP over Streamable HTTP: sessions bound to the token's principal", async () => {
  const { sid, rpc } = await mcpSession(tokA);
  const tools = await rpc(2, "tools/list", {});
  const names = tools.result.tools.map((t) => t.name);
  assert.ok(names.includes("smartware_recall") && names.includes("create_entities"));
  assert.ok(!JSON.stringify(tools.result.tools.find((t) => t.name === "smartware_remember").inputSchema).includes("readers"));
  const w = await rpc(3, "tools/call", { name: "smartware_remember", arguments: { content: "The Lisbon offsite is on 14 November", kind: "event" } });
  assert.equal(w.result.isError, false);
  const r = await rpc(4, "tools/call", { name: "smartware_recall", arguments: { query: "when is the offsite?" } });
  assert.match(r.result.content[0].text, /Lisbon/);
  // safe toolset writes default to the writer only: bob cannot see alice's MCP write
  const rb = await call("POST", "/v1/acme/recall", tokB, { query: "when is the offsite?" });
  assert.ok(!rb.body.items.some((i) => i.text.includes("Lisbon")));
  // another principal cannot use alice's session id
  const hijack = await call("POST", "/mcp", tokB, { jsonrpc: "2.0", id: 9, method: "tools/list" }, { "mcp-session-id": sid });
  assert.equal(hijack.status, 404);
  const nosess = await call("POST", "/mcp", tokA, { jsonrpc: "2.0", id: 9, method: "tools/list" });
  assert.equal(nosess.status, 400);
});

test("MCP memory-server compatibility tools", async () => {
  const { rpc } = await mcpSession(tokAdmin);
  await rpc(2, "tools/call", { name: "create_entities", arguments: { entities: [
    { name: "Acme Corp", entityType: "company", observations: ["Renewal due in March", "Uses SSO"] },
    { name: "Dana", entityType: "person", observations: ["Head of procurement at Acme"] }] } });
  await rpc(3, "tools/call", { name: "create_relations", arguments: { relations: [{ from: "Dana", to: "Acme Corp", relationType: "works_at" }] } });
  await rpc(4, "tools/call", { name: "add_observations", arguments: { observations: [{ entityName: "Acme Corp", contents: ["Prefers annual billing", "Uses SSO"] }] } });
  const g = (await rpc(5, "tools/call", { name: "read_graph", arguments: {} })).result.structuredContent;
  const acme = g.entities.find((e) => e.name === "Acme Corp");
  assert.deepEqual(acme.observations.sort(), ["Prefers annual billing", "Renewal due in March", "Uses SSO"]);
  assert.deepEqual(g.relations, [{ from: "Dana", to: "Acme Corp", relationType: "works_at" }]);
  const s = (await rpc(6, "tools/call", { name: "search_nodes", arguments: { query: "who handles procurement?" } })).result.structuredContent;
  assert.ok(s.entities.some((e) => e.name === "Dana"));
  await rpc(7, "tools/call", { name: "delete_relations", arguments: { relations: [{ from: "Dana", to: "Acme Corp", relationType: "works_at" }] } });
  await rpc(8, "tools/call", { name: "delete_entities", arguments: { entityNames: ["Dana"] } });
  const g2 = (await rpc(9, "tools/call", { name: "read_graph", arguments: {} })).result.structuredContent;
  assert.deepEqual(g2.entities.map((e) => e.name), ["Acme Corp"]);
  assert.deepEqual(g2.relations, []);
});

test("MCP stdio: initialize, tools, and elicitation-confirmed forget", async () => {
  const p = spawn(process.execPath, [new URL("../bin/smartware.js", import.meta.url).pathname, "serve", "--memory"],
    { env: { ...process.env, SMARTWARE_PRINCIPAL: "zed", SMARTWARE_TENANT: "t1" }, stdio: ["pipe", "pipe", "pipe"] });
  const lines = [];
  let buf = "";
  const waiters = [];
  p.stdout.on("data", (d) => {
    buf += d;
    let i;
    while ((i = buf.indexOf("\n")) >= 0) { const l = JSON.parse(buf.slice(0, i)); buf = buf.slice(i + 1); lines.push(l); waiters.splice(0).forEach((w) => w()); }
  });
  const send = (m) => p.stdin.write(JSON.stringify(m) + "\n");
  const wait = async (pred) => { for (;;) { const hit = lines.find(pred); if (hit) return hit; await new Promise((r) => waiters.push(r)); } };
  send({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: { elicitation: {} } } });
  await wait((l) => l.id === 1);
  send({ jsonrpc: "2.0", id: 2, method: "tools/call", params: { name: "smartware_remember", arguments: { content: "door code is in the vault" } } });
  const id = JSON.parse((await wait((l) => l.id === 2)).result.content[0].text).ids[0];
  send({ jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "smartware_forget", arguments: { ids: [id] } } });
  const ask = await wait((l) => l.method === "elicitation/create");
  send({ jsonrpc: "2.0", id: ask.id, result: { action: "accept", content: { confirm: true } } });
  const done = await wait((l) => l.id === 3);
  assert.deepEqual(done.result.structuredContent.affected, [id]);
  p.kill();
});

test("smartware doctor --self reaches L4", async () => {
  const { selfTest } = await import("../dist/doctor.js");
  const r = await selfTest({ memory: true });
  assert.deepEqual(r.checks.filter((c) => c.status !== "pass").map((c) => c.id), []);
  assert.equal(r.level, "L4");
});

test("smartware doctor --principals: removal equivalence on real principals", async () => {
  await call("POST", "/v1/acme/remember", tokA, { memories: [
    { content: "Q4 pipeline review: Acme deal at risk over pricing", readers: ["group:sales"], thread: "int-1", seq: 1 },
    { content: "Engineering capacity is tight for the Acme integration", readers: ["group:eng"], thread: "int-1", seq: 2 },
    { content: "All-hands: everyone should read the Acme account plan", readers: null, thread: "int-2", seq: 1 }] });
  const pfile = join(dir, "principals.json");
  writeFileSync(pfile, JSON.stringify({ tenant: "acme", queries: ["what is happening with Acme?", "pricing risk", "integration capacity"],
    principals: [{ name: "alice", token_env: "T_ALICE", id: "alice", groups: ["sales"] }, { name: "bob", token_env: "T_BOB", id: "bob", groups: ["eng"] }] }));
  process.env.T_ALICE = tokA; process.env.T_BOB = tokB; process.env.T_ADMIN = tokStar;
  const { doctor } = await import("../dist/doctor.js");
  const lines = [];
  const orig = process.stdout.write;
  process.stdout.write = function (s) { if (String(s).includes("PASS") || String(s).includes("FAIL") || String(s).includes("equivalence")) { lines.push(String(s)); return true; } return orig.apply(this, arguments); };
  let code;
  try { code = await doctor([base], { principals: pfile, admin: "T_ADMIN", "as-of": "2030-01-01T00:00:00Z" }); } finally { process.stdout.write = orig; }
  assert.equal(code, 0, lines.join(""));
  assert.equal(lines.filter((l) => l.includes("PASS")).length, 6);
});

test("tenant-free whoami and server-enforced write scopes for connector keys", async () => {
  const w = await call("GET", "/v1/whoami", tokA);
  assert.equal(w.body.tenant, "acme");
  const ks = new KeyStore(keysPath);
  const tokConn = ks.add({ id: "conn-slack", tenant: "acme", groups: [], scopes: ["connectors/slack"] });
  await new Promise((r) => setTimeout(r, 2100)); // the gateway reloads the key file when it changes
  const ok = await call("POST", "/v1/acme/remember", tokConn, { memories: [{ content: "from slack", scope: "connectors/slack/C1" }] });
  assert.equal(ok.status, 200);
  const bad = await call("POST", "/v1/acme/remember", tokConn, { memories: [{ content: "elsewhere", scope: "hr/salaries" }] });
  assert.equal(bad.status, 403);
  const tricky = await call("POST", "/v1/acme/remember", tokConn, { memories: [{ content: "prefix trick", scope: "connectors/slack-evil" }] });
  assert.equal(tricky.status, 403);
});

test("mem0 REST shim: user_id is scope, never identity", async () => {
  const H = (tok) => ({ authorization: `Token ${tok}` });
  const add = await call("POST", "/mem0/v1/memories/", null, { messages: [{ role: "user", content: "I am vegetarian and allergic to nuts" }, { role: "system", content: "ignored" }],
    user_id: "u-alice", metadata: { source: "chat" } }, H(tokA));
  assert.equal(add.status, 200);
  assert.equal(add.body.results.length, 1);
  assert.deepEqual(add.body.degraded, ["mem0:infer_not_supported"]);
  const id = add.body.results[0].id;
  const s = await call("POST", "/mem0/v2/memories/search/", null, { query: "what food should I avoid?", filters: { AND: [{ user_id: "u-alice" }] } }, H(tokA));
  assert.ok(s.body.some((x) => x.id === id && x.user_id === "u-alice"));
  // bob names alice's user_id with his own key: finds nothing of hers
  const spy = await call("POST", "/mem0/v1/memories/search/", null, { query: "allergic", user_id: "u-alice" }, H(tokB));
  assert.equal(spy.body.results.length, 0);
  const all = await call("GET", "/mem0/v1/memories/?user_id=u-alice", null, undefined, H(tokA));
  assert.equal(all.body.results.length, 1);
  const up = await call("PUT", `/mem0/v1/memories/${id}/`, null, { text: "I am vegan and allergic to nuts" }, H(tokA));
  assert.equal(up.status, 200);
  assert.equal((await call("GET", `/mem0/v1/memories/${id}/`, null, undefined, H(tokA))).body.memory, "I am vegan and allergic to nuts");
  const hist = await call("GET", `/mem0/v1/memories/${id}/history/`, null, undefined, H(tokA));
  assert.deepEqual(hist.body.map((h) => h.event), ["ADD", "UPDATE"]);
  await call("DELETE", "/mem0/v1/memories/?user_id=u-alice", null, undefined, H(tokA));
  assert.equal((await call("GET", "/mem0/v1/memories/?user_id=u-alice", null, undefined, H(tokA))).body.results.length, 0);
});

test("the shipped memory policy matches the policy pack and is served as an MCP prompt", async () => {
  const { readFileSync } = await import("node:fs");
  const pack = readFileSync(new URL("../../../policy/memory-policy.md", import.meta.url), "utf8");
  assert.equal(readFileSync(new URL("../policy/memory-policy.md", import.meta.url), "utf8"), pack);
  const { rpc } = await mcpSession(tokA);
  const list = await rpc(2, "prompts/list", {});
  assert.equal(list.result.prompts[0].name, "memory_policy");
  const got = await rpc(3, "prompts/get", { name: "memory_policy" });
  assert.equal(got.result.messages[0].content.text, pack);
});
