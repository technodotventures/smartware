// Slack connector and the gateway-side principal resolver for a Company Brain-style assistant.
//
// The connector pulls channel history through the Slack Web API with a bot token read from an environment variable
// the operator names (it is never written to disk, logged or sent anywhere but the Slack API). Each message becomes
// an `event` record:
//   thread  = slack:<channel>            (top-level messages)
//             slack:<channel>/<thread_ts> (thread replies)
//   actor   = the Slack user, mapped through principalResolver
//   readers = ["channel:<channel id>"]
// Channel membership is not written into records: it is published as a group membership (channel:<id> -> members),
// and the gateway gives each user the token channel:<id> for every channel they are in. A member who leaves a
// channel loses the token on the next sync, and with it every message and every summary derived from the channel.
import { existsSync, readFileSync, statSync } from "node:fs";
import { Principal } from "@orp/core";
import { defineConnector, type AclChange, type Connector, type SourceItem } from "./sdk.js";

export interface SlackOptions {
  /** Name of the environment variable holding the bot token (xoxb-...). The token itself is never an option. */
  tokenEnv: string;
  name?: string; // default "slack"
  scopePrefix?: string; // default "slack"; each channel is scoped <prefix>/<channel id>
  /** Slack Web API base (default https://slack.com/api). Plain http is accepted only for localhost (tests). */
  apiBase?: string;
  /** Only these channel ids (default: every channel the bot is a member of). */
  channels?: string[];
  /** conversations.list types (default "public_channel,private_channel"). */
  types?: string;
  /** Re-read this much history on every pull to catch edits and deletions (default 7 days). */
  rescanSeconds?: number;
  /** Also pull thread replies with conversations.replies (default true). */
  includeThreads?: boolean;
  /** Maps a Slack user id (U...) to an ORP principal id; null drops the user (fail closed). Default: the Slack id. */
  principalResolver?: (slackUserId: string) => string | null;
  pageSize?: number; // default 200
  maxRetries?: number; // on HTTP 429, default 5
  /** Upper bound for one Retry-After wait, in seconds (default 120). */
  maxRetryAfterSeconds?: number;
}

export class SlackApiError extends Error {
  constructor(readonly method: string, readonly error: string, readonly status: number) {
    super(`Slack ${method} failed: ${error}`);
    this.name = "SlackApiError";
  }
}

const SKIP_SUBTYPES = new Set(["channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name",
  "channel_archive", "channel_unarchive", "group_join", "group_leave", "pinned_item", "unpinned_item"]);
const CHANNEL_ID = /^[A-Z0-9]{2,32}$/;
const TS = /^\d{1,12}\.\d{1,6}$/;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function checkApiBase(raw: string): string {
  const u = new URL(raw);
  const local = ["localhost", "127.0.0.1", "[::1]", "::1"].includes(u.hostname);
  if (u.protocol !== "https:" && !(u.protocol === "http:" && local)) {
    throw new Error("apiBase must be https (plain http only for localhost), so the bot token never crosses the network in clear");
  }
  return u.toString().replace(/\/+$/, "");
}

interface ChannelCursor { latest: string | null; seen: string[] }
interface SlackCursor { pulledAt: number | null; channels: Record<string, ChannelCursor> }

const tsMs = (ts: string) => Math.round(parseFloat(ts) * 1000);
const tsSeq = (ts: string) => { const [a, b = ""] = ts.split("."); return Number(a) * 1e6 + Number(b.padEnd(6, "0")); };

export function slackConnector(opt: SlackOptions): Connector {
  if (!opt.tokenEnv || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(opt.tokenEnv)) throw new Error("tokenEnv must name an environment variable");
  const api = checkApiBase(opt.apiBase ?? "https://slack.com/api");
  const prefix = (opt.scopePrefix ?? "slack").replace(/\/+$/, "");
  const pageSize = Math.min(Math.max(opt.pageSize ?? 200, 1), 1000);
  const rescan = opt.rescanSeconds ?? 7 * 86400;
  const maxRetries = opt.maxRetries ?? 5;
  const maxWait = opt.maxRetryAfterSeconds ?? 120;
  const allow = opt.channels ? new Set(opt.channels) : null;

  async function call(method: string, params: Record<string, string | number | undefined>): Promise<any> {
    const token = process.env[opt.tokenEnv];
    if (!token) throw new Error(`environment variable ${opt.tokenEnv} is not set`);
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") qs.set(k, String(v));
    for (let attempt = 0; ; attempt++) {
      const r = await fetch(`${api}/${method}?${qs}`, { headers: { authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(30_000) });
      if (r.status === 429 && attempt < maxRetries) {
        const after = Number(r.headers.get("retry-after"));
        await r.arrayBuffer().catch(() => undefined);
        await sleep(Math.min(Number.isFinite(after) && after >= 0 ? after : 1, maxWait) * 1000);
        continue;
      }
      let body: any = null;
      try { body = await r.json(); } catch { /* not JSON */ }
      if (!r.ok || !body || body.ok !== true) throw new SlackApiError(method, String(body?.error ?? `http_${r.status}`), r.status);
      return body;
    }
  }

  /** Every page of a cursor-paginated method, concatenating `key`. */
  async function paged(method: string, key: string, params: Record<string, string | number | undefined>): Promise<any[]> {
    const out: any[] = [];
    let cursor: string | undefined;
    for (let guard = 0; guard < 100_000; guard++) {
      const body = await call(method, { ...params, limit: pageSize, cursor });
      out.push(...(Array.isArray(body[key]) ? body[key] : []));
      cursor = body.response_metadata?.next_cursor || undefined;
      if (!cursor) break;
    }
    return out;
  }

  const toItem = (ch: string, m: any, thread: string, prevPull: number | null): SourceItem | null => {
    if (!m || typeof m.ts !== "string" || !TS.test(m.ts)) return null;
    if (m.subtype && SKIP_SUBTYPES.has(m.subtype)) return null;
    const text = typeof m.text === "string" ? m.text : "";
    if (!text.trim()) return null;
    const changed = typeof m.edited?.ts === "string" && TS.test(m.edited.ts) ? m.edited.ts : m.ts;
    const user = typeof m.user === "string" ? m.user : typeof m.bot_id === "string" ? m.bot_id : undefined;
    return {
      id: `${ch}/${m.ts}`, content: text, acl: [`channel:${ch}`], kind: "event", scope: `${prefix}/${ch}`, thread,
      seq: tsSeq(m.ts), uri: `slack://${ch}/p${m.ts.replace(".", "")}`, eventTime: new Date(tsMs(m.ts)).toISOString(),
      role: m.bot_id && !m.user ? "assistant" : "user", ...(user ? { actor: user } : {}),
      // lag is measured only for changes made since the previous pull (a first backfill is not "lag")
      ...(prevPull !== null && tsMs(changed) >= prevPull - 1000 ? { changedAt: tsMs(changed) } : {}),
      meta: { "x-slack": { channel: ch, ts: m.ts, ...(m.thread_ts ? { thread_ts: m.thread_ts } : {}), ...(m.edited ? { edited: true } : {}) } },
    };
  };

  return defineConnector({
    name: opt.name ?? "slack",
    scopePrefix: prefix,
    principalResolver: opt.principalResolver,
    async pull(cursor) {
      const prev: SlackCursor = cursor && typeof cursor === "object" && (cursor as any).channels ? cursor as SlackCursor : { pulledAt: null, channels: {} };
      const startedAt = Date.now();
      const listed = await paged("conversations.list", "channels", { types: opt.types ?? "public_channel,private_channel", exclude_archived: "true" });
      const chans = listed.filter((c) => c && typeof c.id === "string" && CHANNEL_ID.test(c.id) && c.is_member !== false && (!allow || allow.has(c.id)))
        .map((c) => c.id as string).sort();
      const items: SourceItem[] = [];
      const deletions: string[] = [];
      const aclChanges: AclChange[] = [];
      const next: SlackCursor = { pulledAt: startedAt, channels: {} };
      for (const ch of chans) {
        const pc = prev.channels[ch] ?? { latest: null, seen: [] };
        const oldest = pc.latest ? Math.max(0, parseFloat(pc.latest) - rescan).toFixed(6) : undefined;
        const msgs = await paged("conversations.history", "messages", { channel: ch, oldest, inclusive: "true" });
        const seen: string[] = [];
        let latest = pc.latest;
        for (const m of msgs) {
          const it = toItem(ch, m, `slack:${ch}`, prev.pulledAt);
          if (typeof m?.ts === "string" && TS.test(m.ts) && (!latest || parseFloat(m.ts) > parseFloat(latest))) latest = m.ts;
          if (it) { items.push(it); seen.push(m.ts); }
          if (opt.includeThreads !== false && typeof m?.ts === "string" && TS.test(m.ts) && (m.reply_count ?? 0) > 0) {
            const replies = await paged("conversations.replies", "messages", { channel: ch, ts: m.ts });
            for (const r of replies) {
              if (r?.ts === m.ts) continue; // the parent is already in the channel thread
              const ri = toItem(ch, r, `slack:${ch}/${m.ts}`, prev.pulledAt);
              if (ri) { items.push(ri); seen.push(r.ts); }
            }
          }
        }
        // a message seen inside the rescan window before and missing now was deleted
        const now = new Set(seen);
        const floor = oldest ? parseFloat(oldest) : -Infinity;
        for (const ts of pc.seen) if (!now.has(ts) && parseFloat(ts) >= floor) deletions.push(`${ch}/${ts}`);
        const keepFloor = latest ? parseFloat(latest) - rescan : -Infinity;
        next.channels[ch] = { latest, seen: seen.filter((ts) => parseFloat(ts) >= keepFloor).sort() };
        const members = await paged("conversations.members", "members", { channel: ch });
        aclChanges.push({ group: `channel:${ch}`, members: members.filter((u) => typeof u === "string") });
      }
      // channels the bot can no longer see: their members lose the channel token (fail closed)
      for (const ch of Object.keys(prev.channels)) if (!chans.includes(ch)) aclChanges.push({ group: `channel:${ch}`, members: [] });
      return { items, deletions, aclChanges, cursor: next };
    },
  });
}

// ------------------------------------------------------------ gateway side: channel membership -> tokens

export interface SlackResolverOptions {
  /** The membership file the Slack connector writes (sync option membershipPath). */
  membershipPath: string;
  /** Token namespaces this file may grant (default ["channel"]). Anything else in the file is ignored, so a
   * compromised connector cannot grant admin, "*", or another connector's groups. */
  namespaces?: string[];
  /** Only principals of this tenant get the groups (default: the tenant recorded by the connector, else "default"). */
  tenant?: string;
  /** Only accept a file written by this connector name (default "slack"). */
  connector?: string;
}

/** Gateway-side resolver: returns the extra groups (channel:<id>) a principal holds according to the Slack
 * connector's membership file. The file is re-read whenever it changes. */
export function slackPrincipalResolver(opt: SlackResolverOptions): (principalId: string, tenant: string) => string[] {
  const ns = new Set(opt.namespaces ?? ["channel"]);
  let stamp = "", byPrincipal = new Map<string, string[]>(), fileTenant = "default";
  const load = () => {
    if (!existsSync(opt.membershipPath)) { byPrincipal = new Map(); stamp = ""; return; }
    const st = statSync(opt.membershipPath);
    const s = `${st.ino}:${st.mtimeMs}:${st.size}`;
    if (s === stamp) return;
    const raw = JSON.parse(readFileSync(opt.membershipPath, "utf8"));
    const m = new Map<string, string[]>();
    if (raw && raw.format === 1 && raw.connector === (opt.connector ?? "slack") && raw.groups && typeof raw.groups === "object") {
      for (const [g, members] of Object.entries(raw.groups as Record<string, unknown>)) {
        const i = g.indexOf(":");
        if (i <= 0 || !ns.has(g.slice(0, i)) || !Array.isArray(members)) continue;
        for (const p of members) {
          if (typeof p !== "string") continue;
          let a = m.get(p);
          if (!a) m.set(p, (a = []));
          a.push(g);
        }
      }
      fileTenant = typeof raw.tenant === "string" ? raw.tenant : "default";
    }
    byPrincipal = m;
    stamp = s;
  };
  return (principalId, tenant) => {
    load();
    if (tenant !== (opt.tenant ?? fileTenant)) return [];
    return [...(byPrincipal.get(principalId) ?? [])].sort();
  };
}

export interface PrincipalAuthenticator { resolve(authorization: string | undefined, write: boolean): Promise<Principal> }

/** Wrap the gateway's authenticator so each resolved principal also holds the groups a resolver grants (for Slack,
 * channel:<id> per channel). Pass the result to the Gateway in place of the plain Authenticator. */
export function withMembership(auth: PrincipalAuthenticator, resolver: (principalId: string, tenant: string) => string[]): PrincipalAuthenticator {
  return {
    async resolve(authorization, write) {
      const pr = await auth.resolve(authorization, write);
      const extra = resolver(pr.id, pr.tenant).filter((g) => g !== "*" && g !== "admin");
      if (!extra.length) return pr;
      return new Principal(pr.id, [...pr.groups, ...extra], pr.tenant, pr.writeScopes);
    },
  };
}
