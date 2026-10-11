// orp doctor: proves a conformance level against any ORP HTTP endpoint, by MUST id, and checks an adopter's own
// integration (removal equivalence for their real principals). The report is the only badge: claiming a level
// with a known violation is non-conformant (M65).
//
//   orp doctor <url> --tenant <t> --token alice=ENV_A --token bob=ENV_B --token admin=ENV_ADMIN --token auditor=ENV_AUD
//              [--token other=ENV_OTHER@<other-tenant>] [--json report.json]
//   orp doctor --self                      spin up an in-process server on a scratch store and run every check
//   orp doctor <url> --principals p.json --admin admin=ENV --as-of <iso> --encoder <spec>   integration: M67 per principal
//
// Principals the suite expects (create them in a scratch tenant; "orp doctor --setup --keys k.json" prints commands):
//   alice: groups [sales]   bob: groups [eng]   admin: groups [admin]   auditor: groups [auditor]   other: another tenant
import { randomBytes } from "node:crypto";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

type Json = Record<string, any>;
type Who = "alice" | "bob" | "admin" | "auditor" | "other";

interface Ctx {
  url: string;
  tenant: string;
  otherTenant: string | null;
  tokens: Partial<Record<Who, string>>;
  run: string; // unique per run, so checks never collide with existing data
  call(who: Who, method: string, path: string, body?: unknown, headers?: Record<string, string>): Promise<{ status: number; body: any; headers: Headers }>;
  caps?: Json;
}

interface Check { id: string; level: "L1" | "L2" | "L3" | "L4"; musts: string[]; title: string; needs?: Who[]; fn: (c: Ctx) => Promise<void> }

class Fail extends Error {}
const must = (cond: unknown, msg: string) => { if (!cond) throw new Fail(msg); };
const clip = (x: unknown) => { const s = JSON.stringify(x) ?? "undefined"; return s.length > 160 ? s.slice(0, 160) + "…" : s; };
const eq = (a: unknown, b: unknown, msg: string) => must(JSON.stringify(a) === JSON.stringify(b), `${msg}: expected ${clip(b)}, got ${clip(a)}`);
const texts = (pack: Json) => (pack.items ?? []).map((i: Json) => i.text).join("\n");
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function v1(c: Ctx, verb: string, tenant = c.tenant) {
  return `/v1/${encodeURIComponent(tenant)}/${verb}`;
}
async function remember(c: Ctx, who: Who, memories: Json[], extra: Json = {}): Promise<string[]> {
  const r = await c.call(who, "POST", v1(c, "remember", who === "other" ? c.otherTenant! : c.tenant), { memories, ...extra });
  must(r.status === 200, `remember as ${who} failed: ${r.status} ${JSON.stringify(r.body)}`);
  return r.body.ids;
}
async function recall(c: Ctx, who: Who, query: string, extra: Json = {}): Promise<Json> {
  const r = await c.call(who, "POST", v1(c, "recall", who === "other" ? c.otherTenant! : c.tenant), { query, ...extra });
  must(r.status === 200, `recall as ${who} failed: ${r.status} ${JSON.stringify(r.body)}`);
  return r.body;
}
const errCode = (r: { body: any }) => r.body?.error?.code;
const packKey = (p: Json) => (p.items ?? []).map((i: Json) => [i.memory_ids, i.text, i.score]);

export const CHECKS: Check[] = [
  // ------------------------------------------------------------ L1 core
  { id: "capabilities", level: "L1", musts: ["M23", "M24", "M25", "M39", "M40", "M44", "M53"], title: "capabilities declare tokenizer, defaults, modes and a named ranker",
    fn: async (c) => {
      const r = await c.call("alice", "GET", v1(c, "capabilities"));
      must(r.status === 200, `capabilities: ${r.status}`);
      const k = r.body;
      c.caps = k;
      must(typeof k.tokenizer === "string" && k.tokenizer, "no tokenizer declared (M23)");
      must(k.defaults && k.defaults["budget.tokens"], "no default budget declared (M24)");
      must(k.modes && k.modes.fast && k.modes.deep, "modes not declared (M25)");
      must(k.ranker && typeof k.ranker.name === "string" && /\d/.test(k.ranker.name), "ranker not named and versioned (M53)");
      must(Array.isArray(k.kinds) && k.kinds.includes("event"), "kinds not declared");
      must(k.dedup && k.dedup.idempotency_key, "dedup window not declared (M70)");
    } },
  { id: "reject-unknown", level: "L1", musts: ["M18"], title: "unknown fields and enum values are rejected at every level, never ignored",
    fn: async (c) => {
      const a = await c.call("alice", "POST", v1(c, "recall"), { query: "x", bogus: 1 });
      eq(errCode(a), "unsupported_parameter", "unknown recall field");
      const b = await c.call("alice", "POST", v1(c, "remember"), { memories: [{ content: "x", bogus: 1 }] });
      eq(errCode(b), "unsupported_parameter", "unknown memory field");
      const d = await c.call("alice", "POST", v1(c, "remember"), { memories: [{ content: "x", kind: "fact", claim: { subject: "a", predicate: "b", object: "c", extra: 1 } }] });
      eq(errCode(d), "unsupported_parameter", "unknown nested claim field");
      const e = await c.call("alice", "POST", v1(c, "remember"), { memories: [{ content: "x" }], mode: "bogus" });
      eq(errCode(e), "unsupported_parameter", "unsupported enum value");
      const f = await c.call("alice", "POST", v1(c, "remember"), { memories: [{ content: 7 }] });
      eq(errCode(f), "invalid_parameter", "wrong type");
      const g = await c.call("alice", "POST", v1(c, "recall"), { query: "x", budget: { tokens: "many" } });
      eq(errCode(g), "invalid_parameter", "wrong budget type");
    } },
  { id: "empty-query", level: "L1", musts: ["M18"], title: "an empty query is an error, never 'everything'",
    fn: async (c) => {
      const r = await c.call("alice", "POST", v1(c, "recall"), { query: "   " });
      eq(errCode(r), "invalid_parameter", "empty query");
    } },
  { id: "idempotency", level: "L1", musts: ["M17"], title: "a replay with the same idempotency key returns the original ids",
    fn: async (c) => {
      const key = `doctor-${c.run}-idem`;
      const a = await remember(c, "alice", [{ content: `idempotent write ${c.run}`, thread: `doc-${c.run}` }], { idempotency_key: key });
      const r = await c.call("alice", "POST", v1(c, "remember"), { memories: [{ content: `something else ${c.run}` }], idempotency_key: key });
      must(r.status === 200, "replay failed");
      eq(r.body.ids, a, "replay ids");
      must(r.body.replayed === true, "replay not flagged");
    } },
  { id: "dedup-scoped", level: "L1", musts: ["M70"], title: "de-duplication never crosses writers: the same key and content from another writer is a new record",
    needs: ["bob"],
    fn: async (c) => {
      const key = `doctor-${c.run}-shared`;
      const a = await remember(c, "alice", [{ content: `shared phrase ${c.run}` }], { idempotency_key: key });
      const b = await remember(c, "bob", [{ content: `shared phrase ${c.run}` }], { idempotency_key: key });
      must(a[0] !== b[0], "bob's write resolved to alice's record");
    } },
  { id: "budget-hard-cap", level: "L1", musts: ["M23", "M43"], title: "budget.tokens is a hard cap; too small is an explicit error",
    fn: async (c) => {
      const many = Array.from({ length: 12 }, (_, i) => ({ content: `Budget probe ${c.run} item ${i}: ` + "the quarterly planning notes cover pricing, hiring and the roadmap. ".repeat(3), thread: `doc-${c.run}-b`, seq: i }));
      await remember(c, "alice", many);
      const p = await recall(c, "alice", `budget probe ${c.run} quarterly planning`, { budget: { tokens: 120 } });
      must(p.tokens <= 120, `pack used ${p.tokens} tokens over a 120 budget`);
      must((p.items as Json[]).reduce((a, i) => a + i.tokens, 0) <= 120, "item tokens exceed the budget");
      const small = await c.call("alice", "POST", v1(c, "recall"), { query: "x", budget: { tokens: 1 } });
      must(small.status >= 400, "budget of 1 token accepted");
    } },
  { id: "pack-shape", level: "L1", musts: ["M41", "M50"], title: "packs carry why, validity, citations, coverage and a snapshot",
    fn: async (c) => {
      await remember(c, "alice", [{ content: `The ${c.run} launch review is on Thursday`, thread: `doc-${c.run}-p` }]);
      const p = await recall(c, "alice", `when is the ${c.run} launch review`);
      must(p.items.length > 0, "no items");
      const it = p.items[0];
      for (const k of ["memory_ids", "text", "tokens", "score", "why", "validity", "citations"]) must(k in it, `item lacks ${k}`);
      must(typeof p.snapshot === "string" && p.snapshot, "no snapshot");
      must(typeof p.coverage === "number", "no coverage");
      must(Array.isArray(p.degraded), "no degraded[]");
      must(typeof p.request_id === "string", "no request_id");
    } },
  { id: "determinism", level: "L2", musts: ["M52", "M68"], title: "same request and snapshot give the same pack; any write changes the snapshot",
    fn: async (c) => {
      const asOf = new Date(Date.now() + 60_000).toISOString();
      await remember(c, "alice", [{ content: `Determinism probe ${c.run}: the vendor is Acme`, thread: `doc-${c.run}-d` }]);
      const a = await recall(c, "alice", `who is the vendor ${c.run}`, { as_of: asOf });
      const b = await recall(c, "alice", `who is the vendor ${c.run}`, { as_of: asOf });
      eq([a.snapshot, packKey(a)], [b.snapshot, packKey(b)], "repeat recall");
      await remember(c, "alice", [{ content: `Another note ${c.run}` }]);
      const d = await recall(c, "alice", `who is the vendor ${c.run}`, { as_of: asOf });
      must(d.snapshot !== a.snapshot, "snapshot unchanged after a write");
    } },
  { id: "client-ids", level: "L1", musts: ["M02", "M14"], title: "client ids are unique per tenant; meta round-trips",
    fn: async (c) => {
      const id = `doc-${c.run}-cid`;
      const got = await remember(c, "alice", [{ id, content: "client id record", meta: { "x-doctor": { n: 1, s: "é" } } }]);
      eq(got, [id], "client id echoed");
      const g = await c.call("alice", "GET", v1(c, `memories/${encodeURIComponent(id)}`));
      must(g.status === 200, `get by client id: ${g.status}`);
      eq(g.body.meta["x-doctor"], { n: 1, s: "é" }, "meta round trip");
      const dup = await c.call("alice", "POST", v1(c, "remember"), { memories: [{ id, content: "again" }] });
      eq(errCode(dup), "version_conflict", "append to an existing id");
    } },
  { id: "profile-budget", level: "L1", musts: ["M26"], title: "profile requires a budget and orders rules before preferences",
    fn: async (c) => {
      const r = await c.call("alice", "POST", v1(c, "profile"), {});
      eq(errCode(r), "invalid_parameter", "profile without budget");
      await remember(c, "alice", [{ kind: "preference", content: `Prefers short answers ${c.run}`, load: "always", readers: ["principal:alice"] },
        { kind: "procedure", content: `Always cite sources ${c.run}`, load: "always", readers: ["principal:alice"] }]);
      const p = await c.call("alice", "POST", v1(c, "profile"), { budget: { tokens: 2000 } });
      must(p.status === 200, "profile failed");
      const mine = (p.body.items as Json[]).filter((i) => i.text.includes(c.run));
      eq(mine.map((i) => i.kind), ["procedure", "preference"], "profile order");
    } },
  // ------------------------------------------------------------ L2 temporal
  { id: "as-of", level: "L2", musts: ["M20"], title: "as_of is the clock: records valid later are invisible earlier",
    fn: async (c) => {
      await remember(c, "alice", [{ kind: "fact", content: `${c.run} office moved to Lisbon`, valid_from: "2030-01-01T00:00:00Z" }]);
      const before = await recall(c, "alice", `where is the ${c.run} office`, { as_of: "2029-06-01T00:00:00Z" });
      must(!texts(before).includes("Lisbon"), "a record valid from 2030 was returned as of 2029");
      const after = await recall(c, "alice", `where is the ${c.run} office`, { as_of: "2030-06-01T00:00:00Z" });
      must(texts(after).includes("Lisbon"), "the record is missing after it became valid");
    } },
  { id: "supersession", level: "L2", musts: ["M10", "M11", "M34", "M48"], title: "a newer claim supersedes the older one, with links, history and versions",
    fn: async (c) => {
      const subj = `dana-${c.run}`;
      const [a] = await remember(c, "alice", [{ kind: "fact", content: `${subj} lives in Berlin`, valid_from: "2025-01-01T00:00:00Z", claim: { subject: subj, predicate: "lives_in", object: "Berlin" } }]);
      const [b] = await remember(c, "alice", [{ kind: "fact", content: `${subj} lives in Paris`, valid_from: "2025-06-01T00:00:00Z", claim: { subject: subj, predicate: "lives_in", object: "Paris" } }]);
      const now = await recall(c, "alice", `where does ${subj} live`, { as_of: "2026-01-01T00:00:00Z" });
      must(texts(now).includes("Paris") && !texts(now).includes("Berlin"), "current view should show Paris only");
      const then = await recall(c, "alice", `where does ${subj} live`, { as_of: "2025-03-01T00:00:00Z" });
      must(texts(then).includes("Berlin"), "as of March 2025 should show Berlin");
      const hist = await recall(c, "alice", `where does ${subj} live`, { as_of: "2026-01-01T00:00:00Z", include: { history: true } });
      must(texts(hist).includes("[superseded]") && texts(hist).includes("Berlin"), "history should label the old version");
      const g = await c.call("alice", "GET", v1(c, `memories/${a}`));
      eq(g.body.status, "superseded", "old status");
      eq(g.body.superseded_by, b, "superseded_by link");
      must(g.body.version >= 2, "version must increment on supersession (M11)");
      const h = await c.call("alice", "GET", v1(c, `history?subject=${encodeURIComponent(subj)}&predicate=lives_in`));
      eq((h.body.versions as Json[]).map((v) => v.object), ["Berlin", "Paris"], "history chain");
    } },
  { id: "known-at", level: "L2", musts: ["M76"], title: "known_at: what did we know then (a correction learned later does not hide it)",
    fn: async (c) => {
      const subj = `erin-${c.run}`;
      await remember(c, "alice", [{ kind: "fact", content: `${subj} drinks tea`, valid_from: "2025-01-01T00:00:00Z", claim: { subject: subj, predicate: "drinks", object: "tea" } }]);
      await sleep(30);
      const t = new Date().toISOString();
      await sleep(30);
      await remember(c, "alice", [{ kind: "fact", content: `${subj} drinks coffee`, valid_from: "2025-03-01T00:00:00Z", claim: { subject: subj, predicate: "drinks", object: "coffee" } }]);
      const then = await recall(c, "alice", `what does ${subj} drink`, { as_of: "2025-06-01T00:00:00Z", known_at: t });
      must(texts(then).includes("tea") && !texts(then).includes("coffee"), "known_at should show only what was known then");
      const now = await recall(c, "alice", `what does ${subj} drink`, { as_of: "2025-06-01T00:00:00Z" });
      must(texts(now).includes("coffee") && !texts(now).includes("tea"), "without known_at the correction applies");
    } },
  { id: "evidence-immutable", level: "L2", musts: ["M36"], title: "evidence (event/document) is never superseded",
    fn: async (c) => {
      const [e] = await remember(c, "alice", [{ content: `meeting notes ${c.run}`, thread: `doc-${c.run}-e` }]);
      const r = await c.call("alice", "POST", v1(c, "remember"), { mode: "supersede", memories: [{ kind: "fact", content: "replacement", supersedes: [e] }] });
      must(r.status >= 400, "superseding evidence was accepted");
    } },
  // ------------------------------------------------------------ L3 governed
  { id: "acl", level: "L3", musts: ["M06", "M58"], title: "readers are enforced on recall, get and neighbours; forbidden looks like absent",
    needs: ["bob"],
    fn: async (c) => {
      const [m] = await remember(c, "alice", [{ content: `Sales-only pricing floor ${c.run} is 40k`, readers: ["group:sales"], thread: `doc-${c.run}-acl` }]);
      const b = await recall(c, "bob", `pricing floor ${c.run}`);
      must(!texts(b).includes("40k"), "bob recalled a sales-only memory");
      const g = await c.call("bob", "GET", v1(c, `memories/${m}`));
      eq(g.status, 404, "bob get of a hidden record");
      const a = await recall(c, "alice", `pricing floor ${c.run}`);
      must(texts(a).includes("40k"), "alice cannot recall her own sales memory");
    } },
  { id: "removal-equivalence", level: "L3", musts: ["M32", "M33", "M54", "M66", "M67"], title: "records you cannot read never change your pack (scores, order, text)",
    needs: ["bob"],
    fn: async (c) => {
      const asOf = new Date(Date.now() + 3_600_000).toISOString();
      await remember(c, "alice", [
        { content: `Renewal plan ${c.run}: Acme renews in March at the same price`, thread: `doc-${c.run}-re`, seq: 1 },
        { content: `Renewal risk ${c.run}: Acme asked about discounts`, thread: `doc-${c.run}-re`, seq: 2 },
        { content: `Unrelated ${c.run}: lunch is at noon`, thread: `doc-${c.run}-re`, seq: 3 }]);
      const q = `Acme renewal discount ${c.run}`;
      const before = await recall(c, "alice", q, { as_of: asOf });
      // bob writes records alice cannot read, packed with the query's terms (a statistics side channel would show)
      await remember(c, "bob", Array.from({ length: 25 }, (_, i) => ({ content: `Acme renewal discount ${c.run} acme acme renewal ${i}`,
        readers: ["principal:bob"], thread: `doc-${c.run}-re`, seq: 100 + i })));
      const after = await recall(c, "alice", q, { as_of: asOf });
      eq(packKey(after), packKey(before), "alice's pack changed after bob wrote hidden records (M67)");
    } },
  { id: "tenant-isolation", level: "L3", musts: ["M03", "M67", "M73"], title: "another tenant's records never appear or change scores",
    needs: ["other"],
    fn: async (c) => {
      const asOf = new Date(Date.now() + 3_600_000).toISOString();
      await remember(c, "alice", [{ content: `Tenant probe ${c.run}: the migration finishes Friday` }]);
      const q = `tenant probe ${c.run} migration`;
      const before = await recall(c, "alice", q, { as_of: asOf });
      await remember(c, "other", [{ content: `Tenant probe ${c.run}: migration migration migration secret plan` }]);
      const after = await recall(c, "alice", q, { as_of: asOf });
      eq(packKey(after), packKey(before), "another tenant's write changed alice's pack");
      must(!texts(after).includes("secret plan"), "another tenant's record appeared");
      const x = await c.call("other", "POST", v1(c, "recall"), { query: q });
      eq(x.status, 403, "a token for another tenant used this tenant's path");
    } },
  { id: "identity-bound", level: "L1", musts: ["M56"], title: "identity is never assertable in a body; written_by is the authenticated writer",
    fn: async (c) => {
      const r = await c.call("alice", "POST", v1(c, "recall"), { query: "x", principal: "admin" });
      eq(errCode(r), "unsupported_parameter", "principal in a recall body");
      const [m] = await remember(c, "alice", [{ content: `asserted actor ${c.run}`, actor: "ceo" }]);
      const g = await c.call("alice", "GET", v1(c, `memories/${m}`));
      eq(g.body.written_by, "alice", "written_by");
      eq(g.body.actor, "ceo", "actor is kept as an assertion");
    } },
  { id: "closure", level: "L3", musts: ["M59"], title: "derived memories are readable only by readers of every source (no laundering)",
    needs: ["bob"],
    fn: async (c) => {
      const [src] = await remember(c, "alice", [{ content: `Board minutes ${c.run}: layoffs planned`, readers: ["group:sales"] }]);
      const [d] = await remember(c, "alice", [{ kind: "summary", origin: "extracted", content: `Summary ${c.run}: restructuring ahead`, derived_from: [src], readers: null }]);
      const g = await c.call("alice", "GET", v1(c, `memories/${d}`));
      eq(g.body.readers, ["group:sales"], "derived readers narrowed to the source's");
      eq((await c.call("bob", "GET", v1(c, `memories/${d}`))).status, 404, "bob reads a derived memory of a hidden source");
      const bad = await c.call("bob", "POST", v1(c, "remember"), { memories: [{ kind: "summary", origin: "extracted", content: "x", derived_from: [src] }] });
      must(bad.status === 403 || bad.status === 404, "bob derived from a record he cannot read");
    } },
  { id: "secret-label", level: "L3", musts: ["M60"], title: "secret-labelled memories need an explicit label request and the label token",
    fn: async (c) => {
      await remember(c, "alice", [{ content: `Vault combination ${c.run} is 1234`, labels: ["secret"] }]);
      const p = await recall(c, "alice", `vault combination ${c.run}`);
      must(!texts(p).includes("1234"), "secret returned without being requested");
      const q = await recall(c, "alice", `vault combination ${c.run}`, { include: { labels: ["secret"] } });
      must(!texts(q).includes("1234"), "secret returned to a principal without label:secret");
    } },
  { id: "forget", level: "L4", musts: ["M27", "M28"], title: "dry_run changes nothing; forget cascades to derived memories",
    fn: async (c) => {
      const [src] = await remember(c, "alice", [{ content: `Forget probe ${c.run}: customer phone number is on file` }]);
      const [d] = await remember(c, "alice", [{ kind: "observation", origin: "extracted", content: `Observation ${c.run}: customer shared contact details`, derived_from: [src] }]);
      const dry = await c.call("alice", "POST", v1(c, "forget"), { ids: [src], dry_run: true });
      eq([...dry.body.affected].sort(), [src, d].sort(), "dry_run affected set");
      eq((await c.call("alice", "GET", v1(c, `memories/${d}`))).body.status, "active", "dry_run changed state");
      await c.call("alice", "POST", v1(c, "forget"), { ids: [src] });
      eq((await c.call("alice", "GET", v1(c, `memories/${d}`))).body.status, "retracted", "derived memory not cascaded");
      const p = await recall(c, "alice", `customer contact details ${c.run}`);
      must(!texts(p).includes("contact details"), "a forgotten derived memory is still recalled");
      const again = await c.call("alice", "POST", v1(c, "remember"), { memories: [{ content: `Forget probe ${c.run}: customer phone number is on file` }] });
      eq(errCode(again), "forbidden", "re-ingest of retracted content");
    } },
  { id: "forget-needs-write", level: "L3", musts: ["§4.4"], title: "forgetting a record others can read needs write permission",
    needs: ["bob"],
    fn: async (c) => {
      const [m] = await remember(c, "alice", [{ content: `Team-wide note ${c.run}`, readers: null }]);
      const r = await c.call("bob", "POST", v1(c, "forget"), { ids: [m] });
      eq(r.status, 403, "bob forgot alice's record");
    } },
  { id: "supersede-needs-write", level: "L3", musts: ["M69"], title: "a claim cannot supersede a record its writer may not write",
    needs: ["bob"],
    fn: async (c) => {
      const subj = `fran-${c.run}`;
      await remember(c, "alice", [{ kind: "fact", content: `${subj} reports to Kim`, readers: null, valid_from: "2025-01-01T00:00:00Z", claim: { subject: subj, predicate: "reports_to", object: "Kim" } }]);
      await remember(c, "bob", [{ kind: "fact", content: `${subj} reports to Bob`, readers: null, valid_from: "2025-06-01T00:00:00Z", claim: { subject: subj, predicate: "reports_to", object: "Bob" } }]);
      const p = await recall(c, "alice", `who does ${subj} report to`, { as_of: "2026-01-01T00:00:00Z" });
      must(texts(p).includes("Kim"), "bob's claim hid alice's record from alice");
    } },
  { id: "changes-feed", level: "L3", musts: ["M74"], title: "the change feed never shows records the reader cannot read",
    needs: ["bob"],
    fn: async (c) => {
      const [m] = await remember(c, "alice", [{ content: `Feed probe ${c.run}`, readers: ["principal:alice"] }]);
      let cursor = 0, seen = false;
      for (;;) {
        const r = await c.call("bob", "GET", v1(c, `changes?cursor=${cursor}&limit=1000`));
        must(r.status === 200, "changes failed");
        if ((r.body.changes as Json[]).some((x) => x.id === m)) seen = true;
        if (r.body.cursor === cursor || !(r.body.changes as Json[]).length && r.body.cursor <= cursor) break;
        cursor = r.body.cursor;
        if (!(r.body.changes as Json[]).length) break;
      }
      must(!seen, "bob saw alice's private record in the change feed");
    } },
  { id: "audit-role", level: "L3", musts: ["§6.5"], title: "the audit log needs the auditor role",
    needs: ["auditor"],
    fn: async (c) => {
      const r = await c.call("alice", "GET", v1(c, "audit"));
      must(r.status === 403 || r.status === 404, "alice read the audit log");
      const a = await c.call("auditor", "GET", v1(c, "audit"));
      if (a.status === 404) return; // binding without an audit route (optional)
      must(a.status === 200 && Array.isArray(a.body.entries), "auditor cannot read the audit log");
    } },
  { id: "import-admin", level: "L4", musts: ["M71"], title: "import is restricted to tenant administrators",
    fn: async (c) => {
      const r = await c.call("alice", "POST", v1(c, "import"), { "memories.jsonl": "" });
      must(r.status === 403 || r.status === 404, "a non-admin imported a bundle");
    } },
  // ------------------------------------------------------------ L4 adaptive
  { id: "reflect", level: "L4", musts: ["M19", "M31"], title: "reflect creates summaries with lineage and closed ACLs",
    fn: async (c) => {
      const th = `doc-${c.run}-refl`;
      await remember(c, "alice", [
        { content: `We decided to move the ${c.run} launch to May because QA needs two more weeks.`, thread: th, seq: 1, readers: ["group:sales"] },
        { content: `Marketing will prepare the ${c.run} press release for the new May launch date.`, thread: th, seq: 2, readers: ["group:sales"] }]);
      const r = await c.call("alice", "POST", v1(c, "reflect"), { threads: [th] });
      must(r.status === 200 && r.body.created.length === 1, `reflect: ${JSON.stringify(r.body)}`);
      const g = await c.call("alice", "GET", v1(c, `memories/${r.body.created[0]}`));
      eq(g.body.kind, "summary", "summary kind");
      eq(g.body.origin, "extracted", "summary origin");
      eq(g.body.derived_from.length, 2, "summary lineage");
      eq(g.body.readers, ["group:sales"], "summary ACL closed over its sources");
    } },
  { id: "feedback-rules", level: "L4", musts: ["M29"], title: "draft→final feedback proposes a draft rule that needs approval",
    fn: async (c) => {
      const [m] = await remember(c, "alice", [{ content: `Email draft ${c.run}` }]);
      const f = await c.call("alice", "POST", v1(c, "feedback"), { target: m, signal: "draft_final", draft: "Dear Sir, kindly find attached the report as requested", final: "Hi Sam, report attached" });
      must(f.status === 200 && f.body.rule_candidate, "no rule candidate");
      const g = await c.call("alice", "GET", v1(c, `memories/${f.body.rule_candidate}`));
      eq(g.body.status, "draft", "rule candidate status");
      const a = await c.call("alice", "POST", v1(c, `memories/${f.body.rule_candidate}/approve`));
      if (a.status === 404) return; // binding without the approve route
      eq((await c.call("alice", "GET", v1(c, `memories/${f.body.rule_candidate}`))).body.status, "active", "approved rule");
    } },
  { id: "export-round-trip", level: "L4", musts: ["M62"], title: "export is lossless and hashed",
    needs: ["admin"],
    fn: async (c) => {
      const e = await c.call("admin", "GET", v1(c, "export"));
      must(e.status === 200, `export: ${e.status}`);
      const lines = String(e.body["memories.jsonl"]).split("\n").filter(Boolean);
      eq(e.body.manifest.count, lines.length, "manifest count");
      const { createHash } = await import("node:crypto");
      eq(e.body.manifest.sha256, createHash("sha256").update(lines.join("\n")).digest("hex"), "manifest hash");
    } },
];

// ------------------------------------------------------------ runner
export interface DoctorResult { url: string; level: string | null; checks: Array<{ id: string; level: string; musts: string[]; title: string; status: "pass" | "fail" | "skip"; error?: string; ms: number }> }

export async function runChecks(url: string, tenant: string, tokens: Partial<Record<Who, string>>, otherTenant: string | null, only?: string[]): Promise<DoctorResult> {
  const c: Ctx = {
    url, tenant, otherTenant, tokens, run: randomBytes(4).toString("hex"),
    async call(who, method, path, body, headers = {}) {
      const tok = tokens[who];
      const r = await fetch(url.replace(/\/$/, "") + path, { method, headers: { ...(tok ? { authorization: `Bearer ${tok}` } : {}),
        ...(body !== undefined ? { "content-type": "application/json" } : {}), ...headers }, body: body === undefined ? undefined : JSON.stringify(body) });
      const text = await r.text();
      let json: any; try { json = JSON.parse(text); } catch { json = text; }
      return { status: r.status, body: json, headers: r.headers };
    },
  };
  const out: DoctorResult = { url, level: null, checks: [] };
  for (const ch of CHECKS) {
    if (only && !only.includes(ch.id) && !only.includes(ch.level)) continue;
    const t0 = performance.now();
    if ((ch.needs ?? []).some((w) => !tokens[w])) { out.checks.push({ id: ch.id, level: ch.level, musts: ch.musts, title: ch.title, status: "skip", error: `needs ${ch.needs!.join(", ")}`, ms: 0 }); continue; }
    try {
      await ch.fn(c);
      out.checks.push({ id: ch.id, level: ch.level, musts: ch.musts, title: ch.title, status: "pass", ms: Math.round(performance.now() - t0) });
    } catch (e) {
      out.checks.push({ id: ch.id, level: ch.level, musts: ch.musts, title: ch.title, status: "fail", error: (e as Error).message, ms: Math.round(performance.now() - t0) });
    }
  }
  let level: string | null = null;
  for (const L of ["L1", "L2", "L3", "L4"]) {
    const at = out.checks.filter((x) => x.level === L);
    if (!at.length || at.some((x) => x.status !== "pass")) break;
    level = L;
  }
  out.level = level;
  return out;
}

function print(r: DoctorResult) {
  const w = (s: string) => process.stdout.write(s + "\n");
  w(`orp doctor: ${r.url}`);
  for (const x of r.checks) w(`  ${x.status === "pass" ? "PASS" : x.status === "skip" ? "SKIP" : "FAIL"}  ${x.level}  ${x.id.padEnd(22)} ${x.musts.join(",").padEnd(18)} ${x.status === "fail" || x.status === "skip" ? x.error : ""}`);
  const failed = r.checks.filter((x) => x.status === "fail");
  w(`level reached: ${r.level ?? "none"}${failed.length ? `; failing MUSTs: ${[...new Set(failed.flatMap((x) => x.musts))].join(", ")}` : ""}`);
}

export async function selfTest(f: Record<string, any>): Promise<DoctorResult> {
  const { HashingEncoder, Service, SqliteStore, chooseRanker } = await import("@orp/core");
  const { Gateway } = await import("./http.js");
  const { Authenticator, KeyStore } = await import("./auth.js");
  const { Log } = await import("./log.js");
  const dir = mkdtempSync(join(tmpdir(), "orp-doctor-"));
  const ks = new KeyStore(join(dir, "keys.json"));
  const tokens: Partial<Record<Who, string>> = {
    alice: ks.add({ id: "alice", tenant: "doctor", groups: ["sales"] }), bob: ks.add({ id: "bob", tenant: "doctor", groups: ["eng"] }),
    admin: ks.add({ id: "admin", tenant: "doctor", groups: ["admin"] }), auditor: ks.add({ id: "aud", tenant: "doctor", groups: ["auditor"] }),
    other: ks.add({ id: "olga", tenant: "elsewhere", groups: ["sales"] }),
  };
  const enc = new HashingEncoder();
  const svc = new Service(enc, { params: chooseRanker("auto", enc.name).params, store: f.memory ? null : new SqliteStore(join(dir, "orp.db")) });
  const gw = new Gateway(svc, new Authenticator(ks, null), { host: "127.0.0.1", port: 0, mcp: { toolsets: ["safe"], defaultReaders: "self" } }, new Log("silent"));
  await gw.listen();
  try {
    return await runChecks(gw.url, "doctor", tokens, "elsewhere");
  } finally {
    await gw.close();
    svc.close();
  }
}

export async function doctor(pos: string[], f: Record<string, any>): Promise<number> {
  if (f.setup) {
    const k = typeof f.keys === "string" ? f.keys : "keys.json";
    const t = typeof f.tenant === "string" ? f.tenant : "doctor";
    process.stdout.write([`orp keys add --keys ${k} --id alice --tenant ${t} --groups sales`, `orp keys add --keys ${k} --id bob --tenant ${t} --groups eng`,
      `orp keys add --keys ${k} --id admin --tenant ${t} --groups admin`, `orp keys add --keys ${k} --id aud --tenant ${t} --groups auditor`,
      `orp keys add --keys ${k} --id olga --tenant ${t}-other --groups sales`].join("\n") + "\n");
    return 0;
  }
  let r: DoctorResult;
  if (f.self) r = await selfTest(f);
  else {
    const url = pos[0];
    if (!url) { process.stderr.write("usage: orp doctor <url> --tenant <t> --token alice=ENV ... | orp doctor --self\n"); return 2; }
    if (f.principals) return integration(url, f);
    const tokens: Partial<Record<Who, string>> = {};
    let otherTenant: string | null = null;
    for (const spec of ([] as string[]).concat(f.token ?? [])) {
      const [who, rest] = String(spec).split("=", 2) as [Who, string];
      const [env, ot] = rest.split("@");
      const tok = process.env[env!];
      if (!tok) { process.stderr.write(`environment variable ${env} is not set\n`); return 2; }
      tokens[who] = tok;
      if (ot) otherTenant = ot;
    }
    r = await runChecks(url, typeof f.tenant === "string" ? f.tenant : "default", tokens, otherTenant, f.only ? String(f.only).split(",") : undefined);
  }
  print(r);
  if (typeof f.json === "string") writeFileSync(f.json, JSON.stringify(r, null, 1));
  return r.checks.some((x) => x.status === "fail") ? 1 : 0;
}

/** Integration check on the adopter's own data: for each real principal and query, recall through the server must
 * equal recall over a clean engine holding only the records that principal can read (M67), and every returned
 * record must be readable by that principal. Needs an admin token (for the full records) and the server's encoder. */
async function integration(url: string, f: Record<string, any>): Promise<number> {
  const { Engine, Principal, loadEncoder, chooseRanker, memoryFromJson, recallRequest, parseTime } = await import("@orp/core");
  const spec = JSON.parse(readFileSync(String(f.principals), "utf8")) as { tenant: string; principals: Array<{ name: string; token_env: string; id: string; groups: string[] }>; queries: string[] };
  const adminTok = process.env[String(f.admin).split("=").pop()!];
  if (!adminTok) { process.stderr.write("--admin <ENV> with an admin token is required\n"); return 2; }
  const asOf = typeof f["as-of"] === "string" ? f["as-of"] : new Date().toISOString();
  const get = async (tok: string, path: string, body?: unknown) => {
    const r = await fetch(url.replace(/\/$/, "") + path, { method: body ? "POST" : "GET", headers: { authorization: `Bearer ${tok}`, "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
    return r.json() as Promise<any>;
  };
  const caps = await get(adminTok, `/v1/${spec.tenant}/capabilities`);
  const enc = loadEncoder(typeof f.encoder === "string" ? f.encoder : "hashing");
  if (caps.encoder && caps.encoder !== enc.name) { process.stderr.write(`server encoder is ${caps.encoder}; pass --encoder for it (got ${enc.name})\n`); return 2; }
  const { params } = chooseRanker(caps.ranker?.name ?? "auto", enc.name);
  const exp = await get(adminTok, `/v1/${spec.tenant}/export`);
  // rebuild in write order (ties between equal scores break by position, so order matters)
  const all = String(exp["memories.jsonl"]).split("\n").filter(Boolean).map((l) => JSON.parse(l))
    .sort((a, b) => (a.meta?._wseq ?? 0) - (b.meta?._wseq ?? 0) || String(a.observed_at).localeCompare(String(b.observed_at)));
  let fails = 0;
  for (const p of spec.principals) {
    const tok = process.env[p.token_env];
    if (!tok) { process.stderr.write(`${p.token_env} not set\n`); return 2; }
    const pr = new Principal(p.id, p.groups, spec.tenant);
    const clean = new Engine(enc, params);
    const visible = all.filter((d) => clean.readable(memoryFromJson(d), pr, ["secret"]));
    // the export must hold everything this principal can see, or the comparison is meaningless
    let seen = 0, cursor: number | null = 0;
    while (cursor !== null) {
      const page: any = await get(tok, `/v1/${spec.tenant}/memories?limit=1000&cursor=${cursor}`);
      if (!Array.isArray(page.memories)) break; // binding without x-list: skip the coverage check
      seen += page.memories.length;
      cursor = page.cursor;
    }
    const visibleNow = visible.filter((d) => d.status === "active" || d.status === "superseded").length;
    if (seen > visibleNow) {
      process.stderr.write(`the admin export holds fewer records than ${p.name} can read (${visibleNow} < ${seen}): use a token in the "*" group\n`);
      return 2;
    }
    clean.remember(visible.map((d) => { const m = memoryFromJson(d); m.id = d.id.includes("~") || d.id.startsWith("mem_") ? d.id : `${d.id}~${spec.tenant}`; return m; }), pr, true);
    for (const q of spec.queries) {
      const server = await get(tok, `/v1/${spec.tenant}/recall`, { query: q, as_of: asOf, budget: { tokens: 2000 } });
      const local = clean.recall(recallRequest({ query: q, principal: pr, tenant: spec.tenant, as_of: parseTime(asOf), budget_tokens: 2000 }));
      const a = (server.items as any[]).map((i) => i.text), b = local.items.map((i) => i.text);
      const ok = JSON.stringify(a) === JSON.stringify(b);
      if (!ok) fails++;
      process.stdout.write(`  ${ok ? "PASS" : "FAIL"}  ${p.name.padEnd(12)} ${q.slice(0, 60)}${ok ? "" : `  (server ${a.length} items vs clean ${b.length})`}\n`);
    }
  }
  process.stdout.write(fails ? `removal equivalence violated in ${fails} case(s): check how principals and readers are mapped\n` : "removal equivalence holds for every principal and query\n");
  return fails ? 1 : 0;
}
