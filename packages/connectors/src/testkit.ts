// Connector test kit. replayFixture replays a scripted source (add, edit, ACL change, delete, membership change)
// through a connector into a live Smartware server, and after every step checks:
//   1. source truth: each fixture principal can read exactly the items the source ACL allows; an item a principal
//      lost access to is gone from their get and recall;
//   2. derived memories re-narrow (M59): a summary made by reflect is never visible to a principal who cannot read
//      every item it was derived from;
//   3. removal equivalence (M67): recall through the server, for each principal and query, equals recall over a
//      clean in-process Engine built from only the records that principal can read.
// It also reports the sync lag the runner measured.
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chooseRanker, Engine, HashingEncoder, memoryFromJson, parseTime, Principal, recallRequest, type Encoder } from "@technodotventures/smartware-core";
import { SmartwareHttp, type Json } from "./http.js";
import { clientId, defineConnector, lagReport, mapAcl, sync, type Connector, type LagReport, type SourceItem, type SyncReport } from "./sdk.js";

// ------------------------------------------------------------ a scripted source

interface FixtureEntry { content: string; acl: string[]; thread: string; changedAt: number }

/** An in-memory source whose state a fixture scripts. */
export class FixtureSource {
  readonly items = new Map<string, FixtureEntry>();
  readonly groups = new Map<string, string[]>();
  add(id: string, content: string, acl: string[], thread = "main") {
    if (this.items.has(id)) throw new Error(`fixture item ${id} already exists`);
    this.items.set(id, { content, acl: [...acl], thread, changedAt: Date.now() });
  }
  edit(id: string, content: string) { const e = this.must(id); e.content = content; e.changedAt = Date.now(); }
  setAcl(id: string, acl: string[]) { const e = this.must(id); e.acl = [...acl]; e.changedAt = Date.now(); }
  delete(id: string) { this.must(id); this.items.delete(id); }
  setMembers(group: string, members: string[]) { this.groups.set(group, [...members]); }
  private must(id: string): FixtureEntry {
    const e = this.items.get(id);
    if (!e) throw new Error(`fixture item ${id} does not exist`);
    return e;
  }
}

export interface FixtureConnectorOptions {
  name?: string; // default "fixture"
  scopePrefix?: string; // default "fixture"
  principalResolver?: (s: string) => string | null;
  /** Test hook to simulate a buggy connector: rewrites each item's ACL before it reaches the runner. */
  aclMapper?: (acl: string[], sourceId: string) => string[];
}

export type FixtureConnector = Connector & { readonly source: FixtureSource };

export function fixtureConnector(source: FixtureSource, opt: FixtureConnectorOptions = {}): FixtureConnector {
  const prefix = opt.scopePrefix ?? "fixture";
  const c = defineConnector({
    name: opt.name ?? "fixture",
    scopePrefix: prefix,
    principalResolver: opt.principalResolver,
    pull(cursor) {
      const prev = new Set<string>(Array.isArray((cursor as any)?.ids) ? (cursor as any).ids : []);
      const items: SourceItem[] = [...source.items].map(([id, e], i) => ({
        id, content: e.content, acl: opt.aclMapper ? opt.aclMapper(e.acl, id) : e.acl, kind: "document",
        scope: prefix, thread: `fixture:${e.thread}`, seq: i, uri: `fixture://${encodeURIComponent(id)}`, changedAt: e.changedAt,
      }));
      const now = new Set(source.items.keys());
      return {
        items, deletions: [...prev].filter((id) => !now.has(id)),
        aclChanges: [...source.groups].map(([group, members]) => ({ group, members })),
        cursor: { ids: [...now] },
      };
    },
  });
  return Object.freeze({ ...c, source });
}

// ------------------------------------------------------------ fixtures

export type FixtureStep =
  | { name?: string; op: "add"; id: string; content: string; acl: string[]; thread?: string; reflect?: boolean | string[] }
  | { name?: string; op: "edit"; id: string; content: string; reflect?: boolean | string[] }
  | { name?: string; op: "acl"; id: string; acl: string[]; reflect?: boolean | string[] }
  | { name?: string; op: "delete"; id: string; reflect?: boolean | string[] }
  | { name?: string; op: "members"; group: string; members: string[]; reflect?: boolean | string[] }
  | {
      name?: string; apply: () => unknown | Promise<unknown>; reflect?: boolean | string[];
      /** For custom steps the kit cannot see the source: name, per principal, the source ids they must and must
       * not be able to read after this step. */
      expect?: Record<string, { sees?: string[]; hidden?: string[] }>;
    };

export interface Fixture {
  steps: FixtureStep[];
  /** Queries for the removal-equivalence check (default: a few words from each item). */
  queries?: string[];
  /** How the source ACL names principals, used as ground truth (default: the identity). Deliberately separate from
   * the connector's own mapping, so a connector bug cannot hide itself. */
  resolve?: (sourcePrincipal: string) => string | null;
}

export interface ReplayServer {
  url: string;
  tenant?: string;
  /** The connector's own (unprivileged) key. */
  connectorToken: string;
  /** A key in the "*" group: reads every record for the clean-engine rebuild and runs reflect. */
  adminToken: string;
  /** Fixture principal name -> that principal's key. */
  principals: Record<string, string>;
  /** The server's encoder (default HashingEncoder, the server default). */
  encoder?: Encoder;
}

export interface ReplayOptions {
  connector?: Connector;
  fixture: Fixture;
  server: ReplayServer;
  statePath?: string;
  membershipPath?: string;
  deleteMode?: "retract" | "purge";
}

export interface StepResult { name: string; sync: SyncReport; checks: number; failures: string[] }
export interface ReplayResult { ok: boolean; steps: StepResult[]; failures: string[]; checks: number; lag: LagReport }

const words = (s: string, n = 6) => s.replace(/[#*_`>\[\]()]/g, " ").split(/\s+/).filter((w) => w.length > 2).slice(0, n).join(" ");
const ENGINE_ID = /^mem_[0-9a-f]{24}$/;

/** Replay a fixture through a connector into a live server and check source truth, re-narrowing and M67. */
export async function replayFixture(opt: ReplayOptions): Promise<ReplayResult> {
  const { fixture, server } = opt;
  const tenant = server.tenant ?? "default";
  const connector = opt.connector ?? fixtureConnector(new FixtureSource());
  const source = (connector as Partial<FixtureConnector>).source;
  const statePath = opt.statePath ?? join(mkdtempSync(join(tmpdir(), "smartware-replay-")), "state.json");
  const admin = new SmartwareHttp({ url: server.url, token: server.adminToken, tenant });
  const who = new Map<string, { http: SmartwareHttp; pr: Principal }>();
  const truthResolve = fixture.resolve ?? ((s: string) => s);
  const enc = server.encoder ?? new HashingEncoder();
  const caps = await admin.capabilities();
  if (caps.encoder && caps.encoder !== enc.name) throw new Error(`server encoder is ${caps.encoder}; pass server.encoder to match (have ${enc.name})`);
  const { params } = chooseRanker(caps.ranker?.name ?? "auto", enc.name);
  const summaries = new Set<string>(); // ids of derived memories made by reflect during the replay
  const steps: StepResult[] = [];
  const failures: string[] = [];
  let checks = 0;

  const refreshPrincipals = async () => {
    for (const [name, token] of Object.entries(server.principals)) {
      const http = new SmartwareHttp({ url: server.url, token, tenant });
      const w = await http.whoami(); // groups as the gateway resolves them now (memberships included)
      who.set(name, { http, pr: new Principal(w.id, w.groups ?? [], w.tenant ?? tenant) });
    }
  };

  for (let si = 0; si < fixture.steps.length; si++) {
    const step = fixture.steps[si]!;
    const name = step.name ?? ("op" in step ? `${step.op} ${"id" in step ? step.id : step.group}` : `step ${si + 1}`);
    if ("op" in step) {
      if (!source) throw new Error(`step "${name}": declarative steps need a fixtureConnector (or omit connector)`);
      if (step.op === "add") source.add(step.id, step.content, step.acl, step.thread);
      else if (step.op === "edit") source.edit(step.id, step.content);
      else if (step.op === "acl") source.setAcl(step.id, step.acl);
      else if (step.op === "delete") source.delete(step.id);
      else if (step.op === "members") source.setMembers(step.group, step.members);
    } else {
      await step.apply();
    }
    const rep = await sync(connector, { url: server.url, token: server.connectorToken, tenant, statePath,
      membershipPath: opt.membershipPath, deleteMode: opt.deleteMode });
    if (step.reflect) {
      const r = await admin.reflect(Array.isArray(step.reflect) ? { threads: step.reflect } : {});
      for (const id of r.created ?? []) summaries.add(id);
    }
    await refreshPrincipals();
    const fails: string[] = [];
    let n = 0;

    // ---- 1. source truth
    const expectations: Array<{ who: string; sid: string; visible: boolean }> = [];
    if (source) {
      const known = new Set<string>([...source.items.keys(), ...Object.keys(readState(statePath).items ?? {})]);
      for (const [pname, { pr }] of who) {
        // the tokens the source says this principal holds: its own, plus the source-managed groups it is a member of
        const tok = new Set([...pr.tokens()].filter((t) => !source.groups.has(t)));
        for (const [g, members] of source.groups) if (members.some((m) => truthResolve(m) === pr.id)) tok.add(g);
        for (const sid of known) {
          const e = source.items.get(sid);
          const readers = e ? mapAcl(e.acl, truthResolve) : [];
          expectations.push({ who: pname, sid, visible: readers.some((r) => tok.has(r)) });
        }
      }
    } else if ("expect" in step && step.expect) {
      for (const [pname, ex] of Object.entries(step.expect)) {
        for (const sid of ex.sees ?? []) expectations.push({ who: pname, sid, visible: true });
        for (const sid of ex.hidden ?? []) expectations.push({ who: pname, sid, visible: false });
      }
    }
    const contentOf = new Map<string, string>();
    for (const ex of expectations) {
      const p = who.get(ex.who);
      if (!p) { fails.push(`${name}: unknown principal ${ex.who}`); continue; }
      const cid = clientId(connector.name, ex.sid);
      const rec = await p.http.get(cid);
      const sees = Boolean(rec && rec.status === "active");
      n++;
      if (sees !== ex.visible) {
        fails.push(`${name}: ${ex.who} ${sees ? "can read" : "cannot read"} ${ex.sid} (${cid}) but the source says it ${ex.visible ? "should" : "should not"}${sees ? " [LEAK]" : ""}`);
      }
      if (!ex.visible) {
        let text = contentOf.get(cid);
        if (text === undefined) { text = String((await admin.get(cid))?.content ?? ""); contentOf.set(cid, text); }
        if (text && text !== "[redacted]") {
          const pack = await p.http.recall({ query: words(text) || text.slice(0, 60) });
          n++;
          if ((pack.items ?? []).some((it: Json) => (it.memory_ids ?? []).includes(cid))) fails.push(`${name}: ${ex.who} recalled ${ex.sid} after losing access [LEAK]`);
        }
      }
    }

    // ---- 2. derived memories re-narrow (M59)
    const hiddenFor = new Map<string, Set<string>>(); // principal -> client ids it must not read
    for (const ex of expectations) if (!ex.visible) {
      let s = hiddenFor.get(ex.who);
      if (!s) hiddenFor.set(ex.who, (s = new Set()));
      s.add(clientId(connector.name, ex.sid));
    }
    for (const sumId of summaries) {
      const d = await admin.get(sumId);
      if (!d) continue;
      for (const [pname, hidden] of hiddenFor) {
        if (!(d.derived_from ?? []).some((x: string) => hidden.has(x))) continue;
        const p = who.get(pname)!;
        const rec = await p.http.get(sumId);
        n++;
        if (rec && rec.status === "active") fails.push(`${name}: ${pname} can read derived ${d.kind} ${sumId} built from an item they lost access to [LEAK, M59]`);
        const pack = await p.http.recall({ query: words(String(d.content)) || "summary" });
        n++;
        if ((pack.items ?? []).some((it: Json) => (it.memory_ids ?? []).includes(sumId))) fails.push(`${name}: ${pname} recalled derived ${sumId} [LEAK, M59]`);
      }
    }

    // ---- 3. removal equivalence (M67)
    const eq = await removalEquivalence(admin, who, tenant, enc, params, fixture.queries ?? defaultQueries(source, contentOf));
    n += eq.checks;
    fails.push(...eq.failures.map((f) => `${name}: ${f}`));

    steps.push({ name, sync: rep, checks: n, failures: fails });
    failures.push(...fails);
    checks += n;
  }
  const lag = readState(statePath).lag;
  return { ok: failures.length === 0, steps, failures, checks, lag: lagReport(Array.isArray(lag) ? lag : []) };
}

function readState(statePath: string): Json {
  try { return JSON.parse(readFileSync(statePath, "utf8")); } catch { return {}; }
}

function defaultQueries(source: FixtureSource | undefined, extra: Map<string, string>): string[] {
  const texts = [...(source ? [...source.items.values()].map((e) => e.content) : []), ...extra.values()];
  const qs = [...new Set(texts.map((t) => words(t, 4)).filter(Boolean))];
  return qs.length ? qs.slice(0, 12) : ["notes"];
}

/** M67 for every principal and query: server recall == recall over a clean engine of only that principal's records. */
export async function removalEquivalence(admin: SmartwareHttp, who: Map<string, { http: SmartwareHttp; pr: Principal }>, tenant: string,
  enc: Encoder, params: any, queries: string[]): Promise<{ checks: number; failures: string[] }> {
  const exp = await admin.export();
  const feed = await admin.changes(0, 100_000);
  const pos = new Map<string, number>(); // the server's index order: last write of each record
  for (const row of feed.changes ?? []) if (row.op === "remember" && typeof row.id === "string") pos.set(row.id, row.cursor);
  const conv = (x: string) => (ENGINE_ID.test(x) || x.includes("~") ? x : `${x}~${tenant}`);
  const all: Json[] = String(exp["memories.jsonl"] ?? "").split("\n").filter(Boolean).map((l) => JSON.parse(l))
    .filter((d) => d.status !== "retracted")
    .sort((a, b) => (pos.get(a.id) ?? Infinity) - (pos.get(b.id) ?? Infinity) || (a.meta?._wseq ?? 0) - (b.meta?._wseq ?? 0));
  const asOf = new Date().toISOString();
  const failures: string[] = [];
  let checks = 0;
  for (const [pname, { http, pr }] of who) {
    const clean = new Engine(enc, params);
    const mine = all.filter((d) => clean.readable(memoryFromJson(d), pr, ["secret"]));
    clean.remember(mine.map((d) => {
      const m = memoryFromJson({ ...d, derived_from: (d.derived_from ?? []).map(conv), supersedes: (d.supersedes ?? []).map(conv),
        superseded_by: d.superseded_by ? conv(d.superseded_by) : null });
      m.id = conv(d.id);
      return m;
    }), pr, true);
    for (const q of queries) {
      const server = await http.recall({ query: q, as_of: asOf, budget: { tokens: 2000 } });
      const local = clean.recall(recallRequest({ query: q, principal: pr, tenant: pr.tenant, as_of: parseTime(asOf), budget_tokens: 2000,
        max_items: 50, filters: { labels: [], drafts: false, granularity: "auto", neighbours: 0, quotas: {}, prefer: [] } }));
      let used = 0;
      const b: string[] = [];
      for (const it of local.items) {
        if (used + it.tokens > 2000) continue;
        used += it.tokens;
        b.push(it.memory_ids.map((i) => i.replace(`~${tenant}`, "")).join(",") + "|" + it.text);
      }
      const a = (server.items ?? []).map((it: Json) => it.memory_ids.join(",") + "|" + it.text);
      checks++;
      if (JSON.stringify(a) !== JSON.stringify(b)) {
        failures.push(`M67: ${pname} query ${JSON.stringify(q)}: server returned ${a.length} item(s), the clean engine ${b.length}`);
      }
    }
  }
  return { checks, failures };
}
