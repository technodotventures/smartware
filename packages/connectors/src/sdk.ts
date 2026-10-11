// Connector SDK. A connector is a broker, not trusted code: it runs as its own principal in its own process, holds
// the source credentials itself (the engine never sees them), and may write only inside its declared scope prefix.
//
// The runner (sync) turns what a connector pulls from its source into ORP writes:
//   new item        -> remember (append), client id derived from the source id
//   changed item    -> remember (upsert) with if_version, after reading the current version
//   ACL-only change -> remember (upsert) that changes only readers (spec M57); the engine re-narrows derived
//                      memories (M59)
//   deleted item    -> forget (retract by default, purge if configured), cascading to derived memories
// It keeps its cursor and an id -> version map in a JSON state file written atomically, and measures sync lag
// (source change time to acknowledged write) per item.
import { createHash } from "node:crypto";
import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, renameSync, writeSync } from "node:fs";
import { dirname } from "node:path";
import { OrpHttp, OrpHttpError, type Json } from "./http.js";

// ------------------------------------------------------------ types

/** One unit from a source: a file section, a message, a page. */
export interface SourceItem {
  /** Stable id in the source system (a path, path#section, channel/ts). Any string. */
  id: string;
  content: string;
  /** Source ACL. Each entry is a source principal (mapped through principalResolver to principal:<id>), an ORP token
   * with a namespace (group:, channel:, role:, team:, org:, principal:), or "public" / "*" for everyone in the tenant. */
  acl: string[];
  /** Where the evidence lives, e.g. file://notes/a.md#intro or slack://C07/p1712345000041. */
  uri: string;
  kind?: string; // default "document"
  /** Full scope path. Must be the connector's scopePrefix or below it. Default: the scopePrefix. */
  scope?: string;
  thread?: string;
  seq?: number;
  /** Source user who wrote it; mapped through principalResolver. */
  actor?: string;
  role?: "user" | "assistant" | "system" | "tool" | "human" | "connector";
  /** ISO time the described thing happened. */
  eventTime?: string;
  /** When the source last changed this item, in ms since the epoch. Used to measure sync lag. */
  changedAt?: number;
  /** Previous source id when the source reports a move or rename: the record keeps its id and is updated in place. */
  movedFrom?: string;
  labels?: string[];
  /** Extra metadata. Keys must start with "x-" (namespaced, spec §3.1). */
  meta?: Json;
}

/** An ACL change: either an item whose ACL changed (content unchanged), or a group whose membership changed (for
 * the gateway's principal resolver, e.g. a Slack channel's members). */
export type AclChange = { item: SourceItem } | { group: string; members: string[] };

export interface PullResult {
  /** New or current items. Items the runner has already written unchanged are skipped, so a connector may simply
   * return its whole current snapshot. */
  items: SourceItem[];
  aclChanges?: AclChange[];
  /** Source ids that no longer exist in the source. */
  deletions?: string[];
  /** Opaque, JSON-serialisable cursor handed back to the next pull. Never put credentials in it. */
  cursor: unknown;
}

export interface Connector {
  readonly name: string;
  readonly scopePrefix: string;
  /** Maps a source principal (e.g. a Slack user id or a vault user name) to an ORP principal id, or null when the
   * user has no ORP identity (the entry is then dropped: fail closed). Default: the identity. */
  readonly principalResolver: (sourcePrincipal: string) => string | null;
  pull(cursor: unknown | null): Promise<PullResult> | PullResult;
}

export interface ConnectorDefinition {
  name: string;
  scopePrefix: string;
  principalResolver?: (sourcePrincipal: string) => string | null;
  pull(cursor: unknown | null): Promise<PullResult> | PullResult;
}

export class ScopeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ScopeError";
  }
}

const NAME = /^[a-z][a-z0-9-]{0,31}$/;
const SEGMENT = /^[^/~\s][^/~]*$/;
export const TOKEN_NAMESPACES = ["principal", "group", "channel", "role", "team", "org"];

function checkScopePath(p: string, what: string) {
  if (typeof p !== "string" || !p) throw new ScopeError(`${what} must be a non-empty path`);
  for (const seg of p.split("/")) {
    if (!SEGMENT.test(seg) || seg === "." || seg === "..") throw new ScopeError(`${what} ${JSON.stringify(p)} has an invalid segment`);
  }
}

/** True when scope is the prefix itself or a path below it (by whole segment: vault covers vault/x, not vault-x). */
export function underPrefix(scope: string, prefix: string): boolean {
  return scope === prefix || scope.startsWith(prefix + "/");
}

export function defineConnector(def: ConnectorDefinition): Connector {
  if (!def || typeof def !== "object") throw new Error("defineConnector needs a definition object");
  if (!NAME.test(def.name ?? "") || def.name === "mem") {
    throw new Error("connector name must match [a-z][a-z0-9-]{0,31} (and not be 'mem')");
  }
  checkScopePath(def.scopePrefix, "scopePrefix");
  if (typeof def.pull !== "function") throw new Error("connector needs a pull(cursor) function");
  return Object.freeze({
    name: def.name,
    scopePrefix: def.scopePrefix.replace(/\/+$/, ""),
    principalResolver: def.principalResolver ?? ((s: string) => s),
    pull: def.pull,
  });
}

// ------------------------------------------------------------ ids, readers, hashing

const sha256 = (s: string) => createHash("sha256").update(s).digest("hex");

/** Stable client id for a source id: the connector name and a hash of the source id (never contains "~" or "/"). */
export function clientId(connector: string, sourceId: string): string {
  return `${connector}-${sha256(sourceId).slice(0, 32)}`;
}

/** Map a source ACL to ORP reader tokens. Unknown users map to nothing (fail closed); never to null (tenant-wide). */
export function mapAcl(acl: readonly string[], resolve: (s: string) => string | null): string[] {
  const out = new Set<string>();
  for (const raw of acl) {
    if (typeof raw !== "string" || !raw) continue;
    if (raw === "*" || raw === "public") { out.add("public"); continue; }
    const i = raw.indexOf(":");
    if (i > 0 && TOKEN_NAMESPACES.includes(raw.slice(0, i))) { if (raw.length > i + 1) out.add(raw); continue; }
    const id = resolve(raw);
    if (typeof id === "string" && id) out.add(`principal:${id}`);
  }
  return [...out].sort();
}

// ------------------------------------------------------------ state

export interface ItemState {
  /** Client id of the record. */
  id: string;
  /** Last version this runner wrote or saw. */
  version: number;
  /** Hash of the written record without its readers. */
  hash: string;
  /** Hash of the written readers. */
  acl: string;
  /** Writes so far; part of the idempotency key, so A -> B -> A content is never mistaken for a replay. */
  n: number;
  deleted?: boolean;
  /** The engine de-duplicated this item into an identical record (same writer, thread, actor, content, readers)
   * instead of creating it. The other record is not this item's to update or forget. */
  aliasOf?: string;
}

export interface SyncState {
  format: 1;
  connector: string;
  scopePrefix: string;
  cursor: unknown;
  items: Record<string, ItemState>;
  /** Group memberships (e.g. channel:C07 -> ORP principal ids), for the gateway's principal resolver. */
  groups: Record<string, string[]>;
  /** Recent sync lag samples in ms (newest last, at most 1000). */
  lag: number[];
  updatedAt: string | null;
}

export function emptyState(c: Connector): SyncState {
  return { format: 1, connector: c.name, scopePrefix: c.scopePrefix, cursor: null, items: {}, groups: {}, lag: [], updatedAt: null };
}

export function loadState(path: string, c: Connector): SyncState {
  if (!existsSync(path)) return emptyState(c);
  const raw = JSON.parse(readFileSync(path, "utf8"));
  if (!raw || raw.format !== 1 || typeof raw.items !== "object") throw new Error(`${path} is not a connector state file`);
  if (raw.connector !== c.name) throw new Error(`${path} belongs to connector ${JSON.stringify(raw.connector)}, not ${JSON.stringify(c.name)}`);
  return { ...emptyState(c), ...raw };
}

/** Write JSON atomically: a temporary file in the same directory, fsync, then rename over the target. Owner-only. */
export function writeJsonAtomic(path: string, value: unknown) {
  mkdirSync(dirname(path), { recursive: true });
  const tmp = `${path}.${process.pid}.${Date.now()}.tmp`;
  const fd = openSync(tmp, "w", 0o600);
  try {
    writeSync(fd, JSON.stringify(value, null, 1) + "\n");
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(tmp, path);
}

// ------------------------------------------------------------ the scoped writer

/** The only way the runner writes. It refuses any record outside the connector's scope prefix, any record without
 * explicit readers (null would mean everyone in the tenant), and any forget of a record this connector does not own. */
export class ScopedWriter {
  constructor(readonly http: OrpHttp, readonly scopePrefix: string, private owned: (id: string) => boolean) {}

  check(memories: Json[]) {
    for (const m of memories) {
      if (typeof m.scope !== "string") throw new ScopeError("every connector record needs a scope");
      checkScopePath(m.scope, "scope");
      if (!underPrefix(m.scope, this.scopePrefix)) {
        throw new ScopeError(`scope ${JSON.stringify(m.scope)} is outside this connector's prefix ${JSON.stringify(this.scopePrefix)}`);
      }
      if (!Array.isArray(m.readers)) throw new ScopeError("connector records need explicit readers (null would mean the whole tenant)");
    }
  }

  async remember(body: Json): Promise<Json> {
    this.check(body.memories ?? []);
    return this.http.remember(body);
  }

  async forget(body: Json): Promise<Json> {
    for (const id of body.ids ?? []) if (!this.owned(id)) throw new ScopeError(`refusing to forget ${id}: not written by this connector`);
    return this.http.forget(body);
  }
}

// ------------------------------------------------------------ the runner

export interface SyncOptions {
  url: string;
  token: string;
  statePath: string;
  tenant?: string;
  /** How deletions are applied (default retract: hidden, auditable, blocks re-ingest by others). */
  deleteMode?: "retract" | "purge";
  /** Also write group memberships here, for the gateway's resolver (see slackPrincipalResolver). */
  membershipPath?: string;
  /** Refuse to run when the connector's own principal holds "*" or "admin" (default true). */
  refusePrivileged?: boolean;
  /** Structured log hook. Receives counts and record ids only, never content or credentials. */
  log?: (event: string, fields: Record<string, string | number>) => void;
}

export interface LagReport { count: number; p50Ms: number | null; p95Ms: number | null; maxMs: number | null }

export interface SyncReport {
  created: number;
  updated: number;
  aclUpdated: number;
  moved: number;
  deleted: number;
  unchanged: number;
  conflicts: number;
  /** Items the engine folded into an identical existing record instead of creating (see ItemState.aliasOf). */
  deduplicated: number;
  groupsChanged: number;
  /** Lag for items written in this run. */
  lag: LagReport;
  /** Lag over the last (up to) 1000 writes, across runs. */
  lagRolling: LagReport;
  /** Source id -> client id for every item written or deleted in this run. */
  ids: Record<string, string>;
}

export function percentile(xs: number[], p: number): number | null {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const i = Math.min(s.length - 1, Math.max(0, Math.ceil((p / 100) * s.length) - 1));
  return s[i]!;
}

export function lagReport(xs: number[]): LagReport {
  return { count: xs.length, p50Ms: percentile(xs, 50), p95Ms: percentile(xs, 95), maxMs: xs.length ? Math.max(...xs) : null };
}

interface Prepared { item: SourceItem; body: Json; hash: string; acl: string }

function prepare(c: Connector, it: SourceItem, self: string): Prepared {
  if (!it || typeof it !== "object") throw new Error("pull returned an item that is not an object");
  if (typeof it.id !== "string" || !it.id) throw new Error("every item needs a non-empty source id");
  if (typeof it.content !== "string" || !it.content.trim()) throw new Error(`item ${clientId(c.name, it.id)} has empty content`);
  if (!Array.isArray(it.acl)) throw new Error(`item ${clientId(c.name, it.id)} needs an acl list`);
  if (typeof it.uri !== "string" || !it.uri) throw new Error(`item ${clientId(c.name, it.id)} needs a source uri`);
  const scope = it.scope ?? c.scopePrefix;
  checkScopePath(scope, "scope");
  if (!underPrefix(scope, c.scopePrefix)) {
    throw new ScopeError(`item scope ${JSON.stringify(scope)} is outside this connector's prefix ${JSON.stringify(c.scopePrefix)}`);
  }
  for (const k of Object.keys(it.meta ?? {})) if (!k.startsWith("x-")) throw new Error(`meta key ${JSON.stringify(k)} must start with "x-"`);
  // the connector itself is always a reader of its own records: it must read a record's version to update it and
  // read access to forget it. It never gains read access to anything it did not write.
  const readers = [...new Set([...mapAcl(it.acl, c.principalResolver), self])].sort();
  const actor = it.actor !== undefined ? c.principalResolver(it.actor) ?? it.actor : undefined;
  const body: Json = {
    id: clientId(c.name, it.id), kind: it.kind ?? "document", content: it.content, scope, readers, origin: "observed",
    role: it.role ?? "connector",
    sources: [{ uri: it.uri, hash: "sha256:" + sha256(it.content) }],
    meta: { ...(it.meta ?? {}), "x-connector": { name: c.name } },
  };
  if (actor !== undefined) body.actor = actor;
  if (it.thread !== undefined) body.thread = it.thread;
  if (it.seq !== undefined) body.seq = it.seq;
  if (it.eventTime !== undefined) body.event_time = it.eventTime;
  if (it.labels?.length) body.labels = [...it.labels].sort();
  const { readers: _r, ...rest } = body;
  return { item: it, body, hash: sha256(stableJson(rest)), acl: sha256(JSON.stringify(readers)) };
}

function stableJson(v: unknown): string {
  if (Array.isArray(v)) return "[" + v.map(stableJson).join(",") + "]";
  if (v && typeof v === "object") {
    return "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + stableJson((v as Json)[k])).join(",") + "}";
  }
  return JSON.stringify(v);
}

/** Run one sync pass: pull from the source and apply the changes to the ORP server. */
export async function sync(connector: Connector, opt: SyncOptions): Promise<SyncReport> {
  const c = connector;
  const log = opt.log ?? (() => {});
  const http = new OrpHttp({ url: opt.url, token: opt.token, tenant: opt.tenant });
  const state = loadState(opt.statePath, c);
  if (state.scopePrefix !== c.scopePrefix) {
    throw new ScopeError(`state file was written for scope prefix ${JSON.stringify(state.scopePrefix)}; refusing to reuse it for ${JSON.stringify(c.scopePrefix)}`);
  }
  const me = await http.whoami();
  if (opt.refusePrivileged !== false && (me.groups ?? []).some((g: string) => g === "*" || g === "admin")) {
    throw new ScopeError(`connector principal ${me.id} holds a privileged group; give each connector its own unprivileged key`);
  }
  const owned = new Set(Object.values(state.items).map((s) => s.id));
  const w = new ScopedWriter(http, c.scopePrefix, (id) => owned.has(id));
  const report: SyncReport = { created: 0, updated: 0, aclUpdated: 0, moved: 0, deleted: 0, unchanged: 0, conflicts: 0,
    deduplicated: 0, groupsChanged: 0, lag: lagReport([]), lagRolling: lagReport([]), ids: {} };
  const runLag: number[] = [];

  const pulled = await c.pull(state.cursor ?? null);
  if (!pulled || !Array.isArray(pulled.items)) throw new Error("pull must return { items, aclChanges?, deletions?, cursor }");
  const itemChanges = (pulled.aclChanges ?? []).filter((a): a is { item: SourceItem } => "item" in a).map((a) => a.item);
  const groupChanges = (pulled.aclChanges ?? []).filter((a): a is { group: string; members: string[] } => "group" in a);
  // validate everything before the first write, so a scope violation writes nothing
  const self = `principal:${me.id}`;
  const prepared = [...pulled.items, ...itemChanges].map((it) => prepare(c, it, self));
  const seenIds = new Set<string>();
  for (const p of prepared) {
    if (seenIds.has(p.item.id)) throw new Error(`pull returned source id twice: ${clientId(c.name, p.item.id)}`);
    seenIds.add(p.item.id);
  }
  for (const g of groupChanges) {
    const i = g.group.indexOf(":");
    if (i <= 0 || !TOKEN_NAMESPACES.includes(g.group.slice(0, i)) || g.group.startsWith("principal:")) {
      throw new ScopeError(`group ${JSON.stringify(g.group)} must be a namespaced group token such as channel:C07`);
    }
  }
  const deletions = (pulled.deletions ?? []).filter((d) => !seenIds.has(d));

  let sinceSave = 0;
  const save = (withCursor: boolean) => {
    if (withCursor) state.cursor = pulled.cursor ?? null;
    state.updatedAt = new Date().toISOString();
    writeJsonAtomic(opt.statePath, state);
    sinceSave = 0;
  };
  const wrote = (p: Prepared) => {
    if (typeof p.item.changedAt === "number" && Number.isFinite(p.item.changedAt)) {
      const ms = Math.max(0, Date.now() - p.item.changedAt);
      runLag.push(ms);
      state.lag.push(ms);
      if (state.lag.length > 1000) state.lag.splice(0, state.lag.length - 1000);
    }
    if (++sinceSave >= 50) save(false);
  };

  // the current version: read it when this principal can (it may not be among the readers), else the state's
  const currentVersion = async (id: string, fallback: number) => (await http.get(id))?.version ?? fallback;

  const upsert = async (p: Prepared, st: ItemState, extra: Json = {}) => {
    const key = `${c.name}:${st.id}:${st.n + 1}:${p.hash.slice(0, 12)}${p.acl.slice(0, 8)}`;
    let v = await currentVersion(st.id, st.version);
    for (let attempt = 0; ; attempt++) {
      try {
        const r = await w.remember({ memories: [p.body], mode: "upsert", if_version: v, idempotency_key: key, ...extra });
        st.version = r.replayed ? Math.max(st.version, v + 1) : v + 1;
        break;
      } catch (e) {
        // the source is the truth for records this connector owns: one retry against the server's current version
        if (e instanceof OrpHttpError && e.code === "version_conflict" && typeof e.details.current === "number" && attempt === 0) {
          report.conflicts++;
          v = e.details.current;
          continue;
        }
        throw e;
      }
    }
    st.n++; st.hash = p.hash; st.acl = p.acl; delete st.deleted;
  };

  const create = async (p: Prepared): Promise<ItemState> => {
    const st: ItemState = { id: p.body.id, version: 0, hash: p.hash, acl: p.acl, n: 0 };
    owned.add(st.id);
    const key = `${c.name}:${st.id}:1:${p.hash.slice(0, 12)}${p.acl.slice(0, 8)}`;
    try {
      const r = await w.remember({ memories: [p.body], mode: "append", idempotency_key: key });
      if (r.ids?.[0] !== st.id) { // content de-duplication (M70) resolved the write to another record
        owned.delete(st.id);
        st.aliasOf = r.ids?.[0];
        report.deduplicated++;
        return st;
      }
      st.version = 1;
    } catch (e) {
      if (!(e instanceof OrpHttpError)) throw e;
      if (e.code === "version_conflict") { // the record exists (e.g. the state file was lost): fall back to upsert
        const cur = await http.get(st.id);
        if (!cur) throw new Error(`record ${st.id} exists but this connector cannot read its version; restore the state file`);
        st.version = cur.version;
        await upsert(p, st, cur.status === "retracted" ? { allow_reingest: true } : {});
        return st;
      }
      if (e.code === "forbidden" && /re-ingest/.test(e.message)) { // the source has the content again
        await w.remember({ memories: [p.body], mode: "append", idempotency_key: key, allow_reingest: true });
        st.version = 1;
      } else throw e;
    }
    st.n = 1;
    return st;
  };

  for (const p of prepared) {
    const sid = p.item.id;
    let st = state.items[sid];
    if (!st && p.item.movedFrom && state.items[p.item.movedFrom] && !state.items[p.item.movedFrom]!.deleted && !seenIds.has(p.item.movedFrom)) {
      st = state.items[sid] = state.items[p.item.movedFrom]!; // keep the record id: derived memories stay attached
      delete state.items[p.item.movedFrom];
      p.body.id = st.id;
      report.moved++;
    }
    if (st) p.body.id = st.id;
    if (st?.aliasOf && st.hash === p.hash && st.acl === p.acl) { report.unchanged++; continue; }
    if (!st || st.aliasOf) {
      const made = state.items[sid] = await create(p);
      if (made.aliasOf) { log("deduplicated", { id: p.body.id }); continue; }
      report.created++;
      log("created", { id: p.body.id });
    } else if (st.deleted) {
      await upsert(p, st, { allow_reingest: true });
      report.created++;
      log("restored", { id: st.id });
    } else if (st.hash === p.hash && st.acl === p.acl) {
      report.unchanged++;
      continue;
    } else if (st.hash === p.hash) {
      await upsert(p, st); // readers only (M57): the engine re-closes derived memories (M59)
      report.aclUpdated++;
      log("acl", { id: st.id, readers: p.body.readers.length });
    } else {
      await upsert(p, st);
      report.updated++;
      log("updated", { id: st.id, version: st.version });
    }
    report.ids[sid] = st?.id ?? state.items[sid]!.id;
    wrote(p);
  }

  for (const sid of deletions) {
    const st = state.items[sid];
    if (!st || st.deleted) continue;
    if (st.aliasOf) { delete state.items[sid]; continue; } // never forget a record this item does not own
    try {
      await w.forget({ ids: [st.id], mode: opt.deleteMode ?? "retract", cascade: true, reason: `source deleted (${c.name})` });
      st.version++;
    } catch (e) {
      if (!(e instanceof OrpHttpError && e.status === 404)) throw e;
      if (await http.get(st.id)) throw e; // readable but not forgettable: report, never pretend it is gone
    }
    st.deleted = true;
    report.deleted++;
    report.ids[sid] = st.id;
    log("deleted", { id: st.id });
    if (++sinceSave >= 50) save(false);
  }

  for (const g of groupChanges) {
    const members = [...new Set(g.members.map((m) => c.principalResolver(m)).filter((x): x is string => typeof x === "string" && !!x))].sort();
    if (JSON.stringify(state.groups[g.group] ?? null) !== JSON.stringify(members)) {
      state.groups[g.group] = members;
      report.groupsChanged++;
      log("group", { group: g.group, members: members.length });
    }
  }
  if (opt.membershipPath && (report.groupsChanged || !existsSync(opt.membershipPath))) {
    writeJsonAtomic(opt.membershipPath, { format: 1, connector: c.name, tenant: opt.tenant ?? "default", updated_at: new Date().toISOString(), groups: state.groups });
  }

  save(true);
  report.lag = lagReport(runLag);
  report.lagRolling = lagReport(state.lag);
  log("sync", { created: report.created, updated: report.updated, acl: report.aclUpdated, deleted: report.deleted,
    unchanged: report.unchanged, p50_ms: report.lag.p50Ms ?? -1, p95_ms: report.lag.p95Ms ?? -1 });
  return report;
}
