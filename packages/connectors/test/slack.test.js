// Slack connector against a fake Slack Web API (a local HTTP server; the real Slack is never contacted):
// history with cursor pagination, rate-limit Retry-After, thread replies, edits, deletions, and a member who leaves
// a channel losing access to its messages and to a summary derived from them.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { clientId, slackConnector, slackPrincipalResolver, sync } from "../dist/index.js";
import { api, getRec, recallIds, startServer } from "./helpers.js";

const BOT_TOKEN = "xoxb-test-0000-not-a-real-token";

function fakeSlack() {
  const now = Math.floor(Date.now() / 1000) - 600;
  const ts = (n) => `${now + n}.000${String(n).padStart(3, "0")}`;
  const s = {
    channels: [{ id: "C1", name: "general", is_member: true }, { id: "C2", name: "leads", is_member: true, is_private: true },
      { id: "C3", name: "random", is_member: false }],
    history: {
      C1: [
        { type: "message", user: "U1", text: "The quarterly roadmap puts the mobile app first", ts: ts(1) },
        { type: "message", subtype: "channel_join", user: "U2", text: "<@U2> has joined the channel", ts: ts(2) },
        { type: "message", user: "U2", text: "Agreed, the onboarding redesign ships in March", ts: ts(3), reply_count: 1, thread_ts: ts(3) },
        { type: "message", user: "U1", text: "Budget review for the Lisbon office is on Thursday", ts: ts(4) },
        { type: "message", user: "U9", text: "A guest posted the venue shortlist", ts: ts(5) },
      ],
      C2: [{ type: "message", user: "U1", text: "Confidential: the Acme acquisition term sheet arrives Monday", ts: ts(6) }],
      C3: [{ type: "message", user: "U3", text: "never synced: the bot is not a member", ts: ts(7) }],
    },
    replies: { [`C1/${ts(3)}`]: [{ type: "message", user: "U2", text: "Agreed, the onboarding redesign ships in March", ts: ts(3), thread_ts: ts(3) },
      { type: "message", user: "U1", text: "Then the pricing page refresh follows in April", ts: ts(8), thread_ts: ts(3) }] },
    members: { C1: ["U1", "U2", "U9"], C2: ["U1"], C3: ["U3"] },
    calls: [], rateLimited: 0, badAuth: 0, ts,
  };
  const page = (arr, q) => {
    const limit = Math.max(1, Number(q.get("limit") ?? 100));
    const start = Number(q.get("cursor") || 0);
    const slice = arr.slice(start, start + limit);
    const next = start + limit < arr.length ? String(start + limit) : "";
    return { slice, meta: { next_cursor: next } };
  };
  const server = createServer((req, res) => {
    const u = new URL(req.url, "http://x");
    const method = u.pathname.replace(/^\/api\//, "");
    s.calls.push({ method, cursor: u.searchParams.get("cursor"), oldest: u.searchParams.get("oldest") });
    const send = (status, body, headers = {}) => { res.writeHead(status, { "content-type": "application/json", ...headers }); res.end(JSON.stringify(body)); };
    if (req.headers.authorization !== `Bearer ${BOT_TOKEN}`) { s.badAuth++; return send(200, { ok: false, error: "invalid_auth" }); }
    if (method === "conversations.history" && s.rateLimited === 0) { s.rateLimited++; return send(429, { ok: false, error: "ratelimited" }, { "retry-after": "1" }); }
    const q = u.searchParams;
    if (method === "conversations.list") { const p = page(s.channels, q); return send(200, { ok: true, channels: p.slice, response_metadata: p.meta }); }
    if (method === "conversations.history") {
      const ch = q.get("channel");
      const oldest = q.get("oldest") ? parseFloat(q.get("oldest")) : -Infinity;
      if (!s.channels.find((c) => c.id === ch)?.is_member) return send(200, { ok: false, error: "not_in_channel" });
      const msgs = (s.history[ch] ?? []).filter((m) => parseFloat(m.ts) >= oldest).sort((a, b) => parseFloat(b.ts) - parseFloat(a.ts)); // newest first
      const p = page(msgs, q);
      return send(200, { ok: true, messages: p.slice, has_more: Boolean(p.meta.next_cursor), response_metadata: p.meta });
    }
    if (method === "conversations.replies") { const p = page(s.replies[`${q.get("channel")}/${q.get("ts")}`] ?? [], q); return send(200, { ok: true, messages: p.slice, response_metadata: p.meta }); }
    if (method === "conversations.members") { const p = page(s.members[q.get("channel")] ?? [], q); return send(200, { ok: true, members: p.slice, response_metadata: p.meta }); }
    send(200, { ok: false, error: "unknown_method" });
  });
  return new Promise((ok) => server.listen(0, "127.0.0.1", () => ok({ s, server, base: `http://127.0.0.1:${server.address().port}/api` })));
}

test("slack: history with pagination and rate limits; a member who leaves a channel loses access", async (t) => {
  const fake = await fakeSlack();
  t.after(() => fake.server.close());
  const srv = await startServer({ membership: "slack" });
  t.after(() => srv.close());
  process.env.SMARTWARE_TEST_SLACK_TOKEN = BOT_TOKEN;
  t.after(() => { delete process.env.SMARTWARE_TEST_SLACK_TOKEN; });
  const users = { U1: "alice", U2: "bob" }; // U9 is a guest with no Smartware identity
  const conn = slackConnector({ tokenEnv: "SMARTWARE_TEST_SLACK_TOKEN", apiBase: fake.base, pageSize: 2, principalResolver: (u) => users[u] ?? null });
  const statePath = join(srv.dir, "slack-state.json");
  const opts = { url: srv.url, token: srv.tokens.connector, tenant: srv.tenant, statePath, membershipPath: srv.membershipPath };
  const mid = (ch, n) => clientId("slack", `${ch}/${fake.s.ts(n)}`);

  const r1 = await sync(conn, opts);
  assert.equal(r1.created, 6, "5 C1 messages minus the join notice, one thread reply, one C2 message");
  assert.equal(fake.s.rateLimited, 1);
  assert.ok(fake.s.calls.filter((c) => c.method === "conversations.history").length >= 4, "the 429 was retried and history was paged");
  assert.ok(fake.s.calls.some((c) => c.method === "conversations.list" && c.cursor), "conversations.list was paged with a cursor");
  assert.ok(fake.s.calls.some((c) => c.method === "conversations.members" && c.cursor), "conversations.members was paged with a cursor");
  assert.equal(fake.s.badAuth, 0);
  assert.ok(!(await getRec(srv, "admin", clientId("slack", `C3/${fake.s.ts(7)}`))), "channels the bot is not in are never read");

  const m1 = await getRec(srv, "alice", mid("C1", 1));
  assert.equal(m1.kind, "event");
  assert.equal(m1.thread, "slack:C1");
  assert.equal(m1.actor, "alice");
  assert.equal(m1.scope, "slack/C1");
  assert.deepEqual(m1.readers, ["channel:C1", "principal:connector-bot"]);
  assert.match(m1.sources[0].uri, /^slack:\/\/C1\/p\d+$/);
  const reply = await getRec(srv, "alice", mid("C1", 8));
  assert.equal(reply.thread, `slack:C1/${fake.s.ts(3)}`);
  assert.equal((await getRec(srv, "alice", mid("C1", 5))).actor, "U9", "an unmapped user stays a raw attribution, never a principal");

  // membership drives tokens at the gateway: bob is in C1 only
  const who = await api(srv, "bob", "GET", "/whoami");
  assert.ok(who.body.tokens.includes("channel:C1") && !who.body.tokens.includes("channel:C2"));
  assert.ok((await recallIds(srv, "bob", "quarterly roadmap mobile app")).includes(mid("C1", 1)));
  assert.ok(!(await recallIds(srv, "bob", "Acme acquisition term sheet")).includes(mid("C2", 6)));
  assert.ok((await recallIds(srv, "alice", "Acme acquisition term sheet")).includes(mid("C2", 6)));
  assert.equal(await getRec(srv, "carol", mid("C1", 1)), null, "carol is in no channel");

  const refl = await api(srv, "admin", "POST", "/reflect", { threads: ["slack:C1"] });
  const summary = refl.body.created[0];
  assert.deepEqual((await getRec(srv, "admin", summary)).readers, ["channel:C1", "principal:connector-bot"]);
  assert.ok(await getRec(srv, "bob", summary));

  // bob leaves C1; a message is edited, one deleted, one added
  fake.s.members.C1 = ["U1", "U9"];
  const h = fake.s.history.C1;
  h.find((m) => m.ts === fake.s.ts(4)).text = "Budget review for the Lisbon office moved to Friday";
  h.find((m) => m.ts === fake.s.ts(4)).edited = { user: "U1", ts: `${Math.floor(Date.now() / 1000)}.000100` };
  fake.s.history.C1 = h.filter((m) => m.ts !== fake.s.ts(5));
  fake.s.history.C1.push({ type: "message", user: "U1", text: "New hire Dana starts on the platform team next week", ts: `${Math.floor(Date.now() / 1000)}.000200` });
  const r2 = await sync(conn, opts);
  assert.equal(r2.groupsChanged, 1);
  assert.equal(r2.updated, 1);
  assert.equal(r2.deleted, 1);
  assert.equal(r2.created, 1);
  assert.ok(r2.lag.count >= 2, "lag is measured for changes since the previous pull");
  assert.ok(r2.lag.p95Ms !== null && r2.lag.p95Ms < 60_000);
  assert.ok(fake.s.calls.some((c) => c.method === "conversations.history" && c.oldest), "later pulls re-scan a window, not everything");

  assert.ok(!(await api(srv, "bob", "GET", "/whoami")).body.tokens.includes("channel:C1"));
  assert.equal(await getRec(srv, "bob", mid("C1", 1)), null);
  assert.ok(!(await recallIds(srv, "bob", "quarterly roadmap mobile app")).includes(mid("C1", 1)));
  assert.equal(await getRec(srv, "bob", summary), null, "the derived summary is gone for bob too");
  assert.ok((await recallIds(srv, "alice", "quarterly roadmap mobile app")).includes(mid("C1", 1)));
  assert.match((await getRec(srv, "alice", mid("C1", 4))).content, /Friday/);
  assert.equal((await getRec(srv, "alice", mid("C1", 5))).status, "retracted");

  // credentials and content never reach the state or membership files
  for (const f of [statePath, srv.membershipPath]) {
    const text = readFileSync(f, "utf8");
    assert.ok(!text.includes(BOT_TOKEN) && !text.includes(srv.tokens.connector) && !text.includes("roadmap"));
  }
  const mem = JSON.parse(readFileSync(srv.membershipPath, "utf8"));
  assert.deepEqual(mem.groups["channel:C1"], ["alice"]);
});

test("slack: the token comes only from the named environment variable, and only over https", async () => {
  assert.throws(() => slackConnector({ tokenEnv: "X", apiBase: "http://slack.example.com/api" }), /https/);
  assert.throws(() => slackConnector({ tokenEnv: "not a var name" }), /environment variable/);
  delete process.env.SMARTWARE_TEST_MISSING_TOKEN;
  const c = slackConnector({ tokenEnv: "SMARTWARE_TEST_MISSING_TOKEN", apiBase: "http://127.0.0.1:9/api" });
  await assert.rejects(c.pull(null), /SMARTWARE_TEST_MISSING_TOKEN is not set/);
});

test("slack resolver grants only channel tokens, only from the slack connector's file, only in its tenant", async (t) => {
  const srv = await startServer();
  t.after(() => srv.close());
  const p = join(srv.dir, "m.json");
  const { writeFileSync } = await import("node:fs");
  writeFileSync(p, JSON.stringify({ format: 1, connector: "slack", tenant: "acme", groups: { "channel:C1": ["bob"], "group:admin": ["bob"], "role:auditor": ["bob"] } }));
  const r = slackPrincipalResolver({ membershipPath: p });
  assert.deepEqual(r("bob", "acme"), ["channel:C1"]);
  assert.deepEqual(r("bob", "other"), []);
  writeFileSync(p, JSON.stringify({ format: 1, connector: "vault", tenant: "acme", groups: { "channel:C1": ["bob"] } }));
  assert.deepEqual(r("bob", "acme"), [], "a file from another connector is ignored");
});
