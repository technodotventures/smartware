// Bundles on disk (export, import, zip) and importers from other projects' formats.
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, existsSync, symlinkSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { HashingEncoder, Principal, Service, SqliteStore } from "@technodotventures/smartware-core";
import { exportBundle, importBundle, readBundleFiles, readZip, writeBundleFiles, writeZip } from "../dist/bundle.js";
import { importLetta, importMcpMemory, importMem0 } from "../dist/importers.js";

const enc = new HashingEncoder();
const dir = () => mkdtempSync(join(tmpdir(), "smartware-bundle-"));
const open = (path) => new Service(enc, { store: path ? new SqliteStore(path) : null, secretScan: false });
const BIN = new URL("../bin/smartware.js", import.meta.url).pathname;
const FIXTURE = new URL("../../../clients/python/tests/fixtures/travel_planner.af", import.meta.url).pathname;

async function seed(s, t = "acme") {
  const a = new Principal("alice", [], t), root = new Principal("root", ["*"], t);
  await s.remember({ memories: [
    { id: "trip", content: "Alice is flying to Lisbon on 12 May", thread: "travel", seq: 1, event_time: "2026-05-01T09:00:00Z" },
    { id: "city", kind: "fact", content: "Alice lives in Paris", claim: { subject: "alice", predicate: "city", object: "paris" }, valid_from: "2024-01-01" },
    { id: "pin", content: "my PIN is 4921", labels: ["secret"] },
  ] }, a);
  await s.remember({ memories: [{ id: "city2", kind: "fact", content: "Alice lives in Rome", claim: { subject: "alice", predicate: "city", object: "rome" }, valid_from: "2025-01-01" }] }, a);
  await s.remember({ memories: [{ kind: "summary", content: "Alice travels a lot", origin: "extracted", derived_from: ["trip"] }] }, a);
  await s.link({ type: "relates", from: "trip", to: "city" }, root);
  await s.remember({ memories: [{ id: "gone", content: "to be purged" }] }, a);
  await s.forget({ ids: ["gone"], mode: "purge" }, a);
}

test("zip round trip; unsafe names, corruption and symbolic links are refused", () => {
  const files = new Map([["manifest.json", Buffer.from("{}")], ["render/threads/a.md", Buffer.from("x".repeat(5000))], ["empty.jsonl", Buffer.alloc(0)]]);
  const z = writeZip(files);
  assert.deepEqual([...readZip(z)].map(([k, v]) => [k, v.toString()]).sort(), [...files].map(([k, v]) => [k, v.toString()]).sort());
  assert.deepEqual(writeZip(files), z, "byte-stable");
  assert.throws(() => writeZip(new Map([["../evil", Buffer.from("x")]])), /unsafe path/);
  const bad = Buffer.from(z);
  const at = bad.indexOf(Buffer.from("{}"));
  bad[at] = 0x5b; // flip a stored byte: the CRC check catches it
  assert.throws(() => readZip(bad), /checksum|corrupt|invalid/i);
  const d = dir();
  mkdirSync(join(d, "b"));
  writeFileSync(join(d, "b", "manifest.json"), "{}");
  symlinkSync("/etc/passwd", join(d, "b", "memories.jsonl"));
  assert.throws(() => readBundleFiles(join(d, "b")), /symbolic link/);
});

test("export to .smartware and to a directory, import into another store, export again: identical records", async () => {
  const d = dir();
  const s1 = open(join(d, "a.db"));
  await seed(s1);
  for (const out of [join(d, "acme.smartware"), join(d, "acme-dir")]) {
    const files = await exportBundle(s1, "acme", { render: true });
    writeBundleFiles(out, files);
    const back = readBundleFiles(out);
    assert.deepEqual([...back.keys()].sort(), [...files.keys()].sort());
    const man = JSON.parse(back.get("manifest.json").toString());
    assert.equal(man.format, "smartware-bundle-1");
    assert.equal(man.counts.memories, 5);
    assert.equal(man.counts.tombstones, 1);
    assert.ok(man.files["memories.jsonl"].sha256);
    assert.ok([...back.keys()].some((k) => k.startsWith("render/threads/")));
    assert.ok(back.get("memories.jsonl").toString().includes("4921"), "the operator export includes secret-labelled records");
    const s2 = open(join(d, `b-${out.endsWith(".smartware") ? 1 : 2}.db`));
    const r = await importBundle(s2, "acme", back);
    assert.equal(r.imported, 5);
    const again = await exportBundle(s2, "acme");
    assert.equal(again.get("memories.jsonl").toString(), files.get("memories.jsonl").toString());
    assert.equal(again.get("edges.jsonl").toString(), files.get("edges.jsonl").toString());
    assert.deepEqual(JSON.parse(again.get("manifest.json")).tombstones, man.tombstones);
    s2.close();
  }
  s1.close();
});

test("a tampered bundle is refused before anything is written", async () => {
  const d = dir();
  const s1 = open(null);
  await seed(s1);
  const files = await exportBundle(s1, "acme");
  files.set("memories.jsonl", Buffer.from(files.get("memories.jsonl").toString().replace("Lisbon", "Berlin")));
  const s2 = open(join(d, "x.db"));
  await assert.rejects(importBundle(s2, "acme", files), /does not match its manifest hash/);
  assert.equal((await s2.export(new Principal("root", ["*"], "acme"))).manifest.count, 0);
  s2.close();
});

test("the CLI exports and imports bundles", () => {
  const d = dir();
  const s1 = open(join(d, "src.db"));
  return seed(s1).then(() => {
    s1.close();
    const out = execFileSync(process.execPath, [BIN, "export", "--store", join(d, "src.db"), "--tenant", "acme", "--out", join(d, "x.smartware"), "--render"], { encoding: "utf8" });
    assert.match(out, /exported 5 memories, 1 edges, 1 tombstones/);
    const imp = JSON.parse(execFileSync(process.execPath, [BIN, "import", join(d, "x.smartware"), "--store", join(d, "dst.db"), "--tenant", "acme"], { encoding: "utf8" }));
    assert.equal(imp.imported, 5);
    assert.ok(existsSync(join(d, "dst.db")));
  });
});

test("import-from mcp-memory: entities, observations and relations as the toolset writes them; re-import adds only what is new", async () => {
  const s = open(null);
  const alice = new Principal("alice", [], "acme");
  const lines = [
    { type: "entity", name: "Priya", entityType: "person", observations: ["Lives in Fitzroy", "Vegetarian"] },
    { type: "entity", name: "Acme", entityType: "organisation", observations: ["Makes anvils"] },
    { type: "relation", from: "Priya", to: "Acme", relationType: "works_at" },
    { type: "relation", from: "Priya", to: "Nobody", relationType: "knows" },
  ];
  const text = lines.map((l) => JSON.stringify(l)).join("\n");
  const r1 = await importMcpMemory(s, alice, text);
  assert.equal(r1.created_entities, 2);
  assert.equal(r1.created_relations, 1);
  assert.equal(r1.skipped.length, 1);
  const got = (await s.get(["entity:Priya"], alice)).memories[0];
  assert.equal(got.kind, "entity");
  assert.deepEqual(got.readers, ["principal:alice"]);
  assert.equal((await s.get(["entity:Priya"], new Principal("bob", [], "acme"))).memories.length, 0, "private to the importer by default");
  const r2 = await importMcpMemory(s, alice, text);
  assert.equal(r2.created_entities, 0);
  assert.equal(r2.created_relations, 0);
  assert.equal(r2.added_observations, 0);
  // a grown file: new observation merged; one forgotten on purpose stays forgotten
  const obs = (await s.list({ kinds: ["observation"], thread: "entity:Priya" }, alice)).memories;
  await s.forget({ ids: [obs.find((o) => o.content === "Vegetarian").id], mode: "retract" }, alice);
  lines[0].observations.push("Prefers trains");
  const r3 = await importMcpMemory(s, alice, lines.map((l) => JSON.stringify(l)).join("\n"));
  assert.equal(r3.added_observations, 1);
  assert.deepEqual(r3.skipped.filter((x) => x.observation).map((x) => x.observation), ["Vegetarian"]);
  // the older single-object layout
  const r4 = await importMcpMemory(open(null), alice, JSON.stringify({ entities: [lines[0], lines[1]], relations: [lines[2]] }));
  assert.equal(r4.created_entities, 2);
  assert.equal(r4.created_relations, 1);
});

test("import-from mem0: memories land in the shim's scopes, private by default, idempotent", async () => {
  const s = open(null);
  const alice = new Principal("alice", [], "acme");
  const exp = { results: [
    { id: "m1", memory: "Likes window seats", user_id: "u1", hash: "h1", metadata: { src: "chat" }, created_at: "2025-03-01T10:00:00Z", categories: ["travel"] },
    { id: "m2", memory: "Allergic to peanuts", user_id: "u1", agent_id: "travel" },
    { id: "m3", memory: "", user_id: "u1" },
    { id: "m4", memory: "orphan" },
  ] };
  const r = await importMem0(s, alice, JSON.stringify(exp));
  assert.equal(r.written, 2);
  assert.equal(r.skipped.length, 2);
  const list = (await s.list({ scope: "mem0/user/u1" }, alice)).memories;
  assert.deepEqual(list.map((m) => m.content).sort(), ["Allergic to peanuts", "Likes window seats"]);
  const m1 = list.find((m) => m.content === "Likes window seats");
  assert.equal(m1.kind, "fact");
  assert.equal(m1.origin, "imported");
  assert.equal(m1.event_time, "2025-03-01T10:00:00");
  assert.deepEqual(m1.meta.mem0, { src: "chat" });
  const r2 = await importMem0(s, alice, JSON.stringify(exp));
  assert.equal((await s.list({ scope: "mem0" }, alice)).memories.length, 2);
  assert.deepEqual(r2.written, 2); // replayed: the same ids, nothing new
});

test("import-from letta: the same mapping as the Python importer", async () => {
  const s = open(null);
  const alice = new Principal("alice", [], "acme");
  const out = await importLetta(s, alice, readFileSync(FIXTURE, "utf8"));
  assert.equal(out.layout, "agent");
  assert.equal(out.version, "0.6.45");
  const a = out.agents[0];
  assert.equal(a.agent, "travel_planner");
  assert.equal(a.profile_ids.length, 2);
  assert.deepEqual(a.skipped, { empty_blocks: 1, system_messages: 1, tool_messages: 1, heartbeats: 2, tool_calls_without_reply: 1 });
  assert.equal(a.event_ids.length, 5);
  assert.equal(a.document_ids.length, 2);
  assert.ok(a.not_imported.includes("tools"));
  const p = await s.profile({ budget: { tokens: 2000 } }, alice);
  assert.ok(JSON.stringify(p).includes("Priya"));
  const again = await importLetta(s, alice, readFileSync(FIXTURE, "utf8"));
  assert.deepEqual(again.agents[0].event_ids, a.event_ids);
  await assert.rejects(importLetta(s, alice, JSON.stringify({ hello: "world" })), /not a Letta agent file/);
});

test("the CLI imports from other formats", () => {
  const d = dir();
  writeFileSync(join(d, "memory.json"), JSON.stringify({ type: "entity", name: "Priya", entityType: "person", observations: ["Lives in Fitzroy"] }) + "\n");
  const out = JSON.parse(execFileSync(process.execPath, [BIN, "import-from", "mcp-memory", join(d, "memory.json"), "--store", join(d, "s.db"), "--tenant", "acme", "--as", "alice"], { encoding: "utf8" }));
  assert.equal(out.created_entities, 1);
  const out2 = JSON.parse(execFileSync(process.execPath, [BIN, "import-from", "letta", FIXTURE, "--store", join(d, "s.db"), "--tenant", "acme", "--as", "alice", "--readers", "tenant"], { encoding: "utf8" }));
  assert.equal(out2.agents[0].event_ids.length, 5);
});
