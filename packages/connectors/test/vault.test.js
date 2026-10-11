// Markdown vault connector end to end: ingest, edit, ACL change via .smartware-acl.json, move, delete, render back.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, writeFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { clientId, parseNote, formatFrontmatter, renderVault, splitSections, sync, vaultConnector } from "../dist/index.js";
import { getRec, recallIds, startServer, api } from "./helpers.js";

test("frontmatter: simple key/value and lists, safe on odd input", () => {
  const n = parseNote('---\ntitle: "Plan: Q3"\nreaders: [alice, "group:eng", \'bob\']\ntags:\n  - a\n  - b\ncount: 3\n__proto__: x\nnested:\n  key: v\n---\n# Body\n');
  assert.equal(n.frontmatter.title, "Plan: Q3");
  assert.deepEqual(n.frontmatter.readers, ["alice", "group:eng", "bob"]);
  assert.deepEqual(n.frontmatter.tags, ["a", "b"]);
  assert.equal(n.frontmatter.count, 3);
  assert.equal(Object.getPrototypeOf(n.frontmatter), Object.prototype);
  assert.equal(n.body, "# Body\n");
  const fm = { title: "Plan: Q3", readers: ["principal:alice", "a, b"], n: 2, flag: true, empty: "" };
  assert.deepEqual(parseNote(formatFrontmatter(fm) + "x").frontmatter, fm);
  assert.equal(parseNote("no frontmatter\n---\n").hadFrontmatter, false);
});

test("sections split on ## outside code fences, with stable keys", () => {
  const s = splitSections("# T\nintro\n\n## A\none\n```\n## not a heading\n```\n## A\ntwo\n");
  assert.deepEqual(s.map((x) => x.key), ["_", "a", "a-2"]);
  assert.ok(s[1].content.includes("## not a heading"));
});

test("vault: ingest, edit, ACL change, move, delete, render back", async (t) => {
  const srv = await startServer();
  t.after(() => srv.close());
  const vault = mkdtempSync(join(tmpdir(), "vault-"));
  const state = join(srv.dir, "vault-state.json");
  mkdirSync(join(vault, "team"));
  mkdirSync(join(vault, ".obsidian"));
  writeFileSync(join(vault, ".obsidian", "secret.md"), "app config, never ingested");
  writeFileSync(join(vault, "plan.md"), "---\ntitle: Plan\nreaders: [alice]\n---\n# Plan\nIntro to the launch.\n\n## Budget\nThe offsite budget is 40k euros.\n\n## Risks\nThe venue may cancel.\n");
  writeFileSync(join(vault, "team", "standup.md"), "## Monday\nBob fixed the flaky deploy pipeline.\n\n## Tuesday\nAlice reviewed the pricing page copy.\n");
  writeFileSync(join(vault, "team", ".smartware-acl.json"), JSON.stringify({ readers: ["alice", "bob"] }));
  writeFileSync(join(vault, "private.md"), "Carol has a dentist appointment on Friday.\n");
  const conn = vaultConnector({ dir: vault, defaultReaders: ["principal:carol"] });
  const opts = { url: srv.url, token: srv.tokens.connector, tenant: srv.tenant, statePath: state };
  const id = (sid) => clientId("vault", sid);

  // ingest
  const r1 = await sync(conn, opts);
  assert.equal(r1.created, 6);
  assert.ok(!Object.keys(r1.ids).some((k) => k.includes(".obsidian")));
  const standupMon = id("team/standup.md#monday");
  assert.ok((await recallIds(srv, "bob", "flaky deploy pipeline")).includes(standupMon));
  assert.equal(await getRec(srv, "bob", id("plan.md#budget")), null, "bob is not in plan.md's readers");
  const budget = await getRec(srv, "alice", id("plan.md#budget"));
  assert.deepEqual(budget.readers, ["principal:alice", "principal:connector-bot"]);
  assert.equal(budget.thread, "plan.md");
  assert.equal(budget.kind, "document");
  assert.equal(budget.scope, "vault");
  assert.equal(budget.written_by, "connector-bot");
  assert.match(budget.sources[0].uri, /^file:\/\/plan\.md#budget$/);
  assert.match(budget.sources[0].hash, /^sha256:[0-9a-f]{64}$/);
  assert.equal((await getRec(srv, "alice", standupMon)).scope, "vault/team");
  assert.equal(await getRec(srv, "alice", id("private.md#_")), null, "default readers are carol only");
  assert.ok(await getRec(srv, "carol", id("private.md#_")));

  // the state file: cursor, id -> version map, no credentials
  const st = JSON.parse(readFileSync(state, "utf8"));
  assert.equal(st.items["plan.md#budget"].id, id("plan.md#budget"));
  assert.equal(st.items["plan.md#budget"].version, 1);
  assert.ok(!readFileSync(state, "utf8").includes(srv.tokens.connector));
  assert.equal(statSync(state).mode & 0o777, 0o600);

  // a second pass with no source change writes nothing
  const r2 = await sync(conn, opts);
  assert.equal(r2.unchanged, 6);
  assert.equal(r2.created + r2.updated + r2.aclUpdated + r2.deleted, 0);

  // edit one section: an upsert with if_version, the record id is unchanged
  writeFileSync(join(vault, "plan.md"), readFileSync(join(vault, "plan.md"), "utf8").replace("40k", "55k"));
  const r3 = await sync(conn, opts);
  assert.equal(r3.updated, 1);
  assert.equal(r3.unchanged, 5);
  assert.equal(r3.lag.count, 1);
  assert.ok(r3.lag.p50Ms >= 0 && r3.lag.p95Ms >= r3.lag.p50Ms);
  const b2 = await getRec(srv, "alice", id("plan.md#budget"));
  assert.equal(b2.version, 2);
  assert.match(b2.content, /55k/);

  // a summary derived from the standup thread
  const refl = await api(srv, "admin", "POST", "/reflect", { threads: ["team/standup.md"] });
  const summary = refl.body.created[0];
  assert.ok(summary);
  assert.ok(await getRec(srv, "bob", summary), "bob can read the summary while he can read its sources");

  // ACL change through the folder file: readers-only upserts, then bob loses the items and the summary (M57, M59)
  writeFileSync(join(vault, "team", ".smartware-acl.json"), JSON.stringify({ readers: ["alice"] }));
  const r4 = await sync(conn, opts);
  assert.equal(r4.aclUpdated, 2);
  assert.equal(r4.updated, 0);
  assert.equal(await getRec(srv, "bob", standupMon), null);
  assert.ok(!(await recallIds(srv, "bob", "flaky deploy pipeline")).includes(standupMon));
  assert.equal(await getRec(srv, "bob", summary), null, "the derived summary re-narrowed");
  assert.ok(!(await recallIds(srv, "bob", "Bob fixed the flaky deploy pipeline")).includes(summary));
  const sumA = await getRec(srv, "alice", summary);
  assert.deepEqual(sumA.readers, ["principal:alice", "principal:connector-bot"]);
  const mon = await getRec(srv, "alice", standupMon);
  assert.equal(mon.content, "## Monday\nBob fixed the flaky deploy pipeline.", "content unchanged by the ACL update");

  // move: same content at a new path keeps the record id and updates thread, scope and source
  mkdirSync(join(vault, "archive"));
  renameSync(join(vault, "private.md"), join(vault, "archive", "private.md"));
  const r5 = await sync(conn, opts);
  assert.equal(r5.moved, 1);
  assert.equal(r5.deleted, 0);
  const moved = await getRec(srv, "carol", id("private.md#_"));
  assert.equal(moved.thread, "archive/private.md");
  assert.equal(moved.scope, "vault/archive");
  assert.equal(moved.status, "active");

  // delete: retracted (forget with cascade), so it is gone from recall
  rmSync(join(vault, "plan.md"));
  const r6 = await sync(conn, opts);
  assert.equal(r6.deleted, 3);
  assert.equal((await getRec(srv, "alice", id("plan.md#budget"))).status, "retracted");
  assert.ok(!(await recallIds(srv, "alice", "offsite budget euros")).includes(id("plan.md#budget")));

  // re-creating a deleted file brings it back (the source is the truth for connector records)
  writeFileSync(join(vault, "plan.md"), "---\nreaders: [alice]\n---\n## Budget\nThe offsite budget is 60k euros.\n");
  const r7 = await sync(conn, opts);
  assert.equal(r7.created, 1);
  assert.equal((await getRec(srv, "alice", id("plan.md#budget"))).status, "active");

  // render back: one file per thread, frontmatter carries the record fields, and the sections round-trip
  const out = mkdtempSync(join(tmpdir(), "render-"));
  const rr = await renderVault(srv.url, srv.tokens.alice, out, { tenant: srv.tenant });
  assert.ok(rr.files.includes("team/standup.md"));
  assert.ok(rr.files.includes("plan.md"));
  assert.ok(!rr.files.includes("archive/private.md"), "alice cannot read carol's note, so it is not rendered");
  const rendered = parseNote(readFileSync(join(out, "team", "standup.md"), "utf8"));
  assert.deepEqual(rendered.frontmatter.readers, ["principal:alice", "principal:connector-bot"]);
  assert.equal(rendered.frontmatter.smartware_thread, "team/standup.md");
  assert.deepEqual(rendered.frontmatter.smartware_ids, [standupMon, id("team/standup.md#tuesday")]);
  const src = splitSections(parseNote(readFileSync(join(vault, "team", "standup.md"), "utf8")).body);
  assert.deepEqual(splitSections(rendered.body), src, "sections survive the round trip");

  assert.ok(rr.files.some((f) => f.startsWith(".smartware/derived/")), "derived records render apart from the evidence");
  assert.ok(!readFileSync(join(out, "team", "standup.md"), "utf8").includes(sumA.content));

  // two-way: ingesting the rendered vault into a fresh server reproduces the same records
  const srv2 = await startServer();
  t.after(() => srv2.close());
  const conn2 = vaultConnector({ dir: out, defaultReaders: ["principal:nobody"] });
  const r8 = await sync(conn2, { url: srv2.url, token: srv2.tokens.connector, tenant: srv2.tenant, statePath: join(srv2.dir, "copy-state.json") });
  assert.equal(r8.created, 3);
  const copy = await getRec(srv2, "alice", standupMon);
  assert.equal(copy.content, mon.content);
  assert.deepEqual(copy.readers, ["principal:alice", "principal:connector-bot"]);
});
