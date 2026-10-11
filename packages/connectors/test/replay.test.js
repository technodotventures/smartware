// The connector test kit: removal equivalence (M67) and re-narrowing (M59) hold through a scripted fixture, and the
// kit catches a connector that maps ACLs wrongly.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { FixtureSource, fixtureConnector, replayFixture, vaultConnector } from "../dist/index.js";
import { startServer } from "./helpers.js";

const FIXTURE = {
  steps: [
    { op: "add", id: "offsite", content: "The Lisbon offsite budget is 40k euros for the whole team", acl: ["alice", "bob"], thread: "plans" },
    { op: "add", id: "agenda", content: "The Lisbon offsite agenda opens with a product roadmap session", acl: ["alice", "bob"], thread: "plans", reflect: ["fixture:plans"] },
    { op: "add", id: "review", content: "Bob's performance review is booked for next Tuesday", acl: ["bob"], thread: "hr" },
    { op: "edit", id: "offsite", content: "The Lisbon offsite budget is now 55k euros for the whole team" },
    { name: "revoke bob from the offsite budget", op: "acl", id: "offsite", acl: ["alice"] },
    { op: "members", group: "channel:C9", members: ["alice", "bob"] },
    { op: "add", id: "standup", content: "Standup note: the deploy pipeline is green again", acl: ["channel:C9"], thread: "eng", reflect: ["fixture:eng"] },
    { name: "bob leaves channel C9", op: "members", group: "channel:C9", members: ["alice"] },
    { op: "delete", id: "review" },
  ],
  queries: ["Lisbon offsite budget", "offsite agenda roadmap", "performance review", "deploy pipeline standup", "euros"],
};

async function replay(t, connector) {
  const srv = await startServer({ membership: "fixture" });
  t.after(() => srv.close());
  return replayFixture({
    connector, fixture: FIXTURE, membershipPath: srv.membershipPath,
    server: { url: srv.url, tenant: srv.tenant, connectorToken: srv.tokens.connector, adminToken: srv.tokens.admin,
      principals: { alice: srv.tokens.alice, bob: srv.tokens.bob, carol: srv.tokens.carol } },
  });
}

test("replay kit: removal equivalence and re-narrowing hold for a correct connector", async (t) => {
  const res = await replay(t, fixtureConnector(new FixtureSource()));
  assert.deepEqual(res.failures, []);
  assert.ok(res.ok);
  assert.equal(res.steps.length, FIXTURE.steps.length);
  assert.ok(res.checks > 100, `ran ${res.checks} checks`);
  assert.equal(res.steps[4].sync.aclUpdated, 1, "the revoke was a readers-only upsert");
  assert.ok(res.lag.count > 0 && res.lag.p50Ms !== null && res.lag.p95Ms >= res.lag.p50Ms);
});

test("replay kit detects a connector that never syncs ACL changes (a leak)", async (t) => {
  const first = new Map();
  const stale = fixtureConnector(new FixtureSource(), { aclMapper: (acl, id) => { if (!first.has(id)) first.set(id, acl); return first.get(id); } });
  const res = await replay(t, stale);
  assert.equal(res.ok, false);
  const revoke = res.steps.find((s) => s.name === "revoke bob from the offsite budget");
  assert.ok(revoke.failures.some((f) => /bob can read offsite .*LEAK/.test(f)), revoke.failures.join("\n"));
  assert.ok(revoke.failures.some((f) => /bob can read derived summary .*M59/.test(f)), "the summary leak is reported too");
});

test("replay kit detects a connector that maps ACLs to the whole tenant", async (t) => {
  const res = await replay(t, fixtureConnector(new FixtureSource(), { aclMapper: () => ["public"] }));
  assert.equal(res.ok, false);
  assert.ok(res.failures.some((f) => /alice can read review .*LEAK/.test(f)));
  assert.ok(res.failures.some((f) => /carol can read/.test(f)));
});

test("replay kit drives a real connector with custom steps (vault)", async (t) => {
  const srv = await startServer();
  t.after(() => srv.close());
  const dir = mkdtempSync(join(tmpdir(), "vault-replay-"));
  mkdirSync(join(dir, "team"));
  const conn = vaultConnector({ dir, defaultReaders: ["principal:alice"] });
  const res = await replayFixture({
    connector: conn,
    server: { url: srv.url, tenant: srv.tenant, connectorToken: srv.tokens.connector, adminToken: srv.tokens.admin,
      principals: { alice: srv.tokens.alice, bob: srv.tokens.bob } },
    fixture: {
      queries: ["incident retro database", "hiring plan"],
      steps: [
        { name: "add team notes", reflect: ["team/retro.md"], apply: () => {
          writeFileSync(join(dir, "team", ".orp-acl.json"), JSON.stringify({ readers: ["alice", "bob"] }));
          writeFileSync(join(dir, "team", "retro.md"), "## What happened\nThe incident retro found a database failover gap.\n");
          writeFileSync(join(dir, "hiring.md"), "The hiring plan adds two engineers in Q3.\n");
        }, expect: { bob: { sees: ["team/retro.md#what-happened"], hidden: ["hiring.md#_"] } } },
        { name: "remove bob from team/", apply: () => writeFileSync(join(dir, "team", ".orp-acl.json"), JSON.stringify({ readers: ["alice"] })),
          expect: { bob: { hidden: ["team/retro.md#what-happened"] }, alice: { sees: ["team/retro.md#what-happened", "hiring.md#_"] } } },
      ],
    },
  });
  assert.deepEqual(res.failures, []);
  assert.equal(res.steps[1].sync.aclUpdated, 1);
});
