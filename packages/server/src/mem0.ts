// mem0-compatible REST shim (adapter level L2), mounted at /mem0 so a mem0 platform client can point its base URL at
// an ORP server. It speaks mem0's routes and shapes; underneath every call is an ORP verb with ORP's guarantees.
//
// Identity: mem0 takes user_id / agent_id / run_id from the caller. Here they are scope segments
// (mem0/user/<u>/agent/<a>/run/<r>) and filters, never the principal, which comes from the API key. mem0's own REST
// checks the key but not the user, so any key holder can name any user; this shim does not: naming another user's
// id finds nothing of theirs unless they shared it. New memories are readable by the writing principal only.
// What it cannot do is reported in degraded[]: no LLM fact extraction (infer), and filters that are not a scope prefix
// are applied after recall.
import type { IncomingMessage, ServerResponse } from "node:http";
import { ORPError, type Principal, type Service } from "@orp/core";

type Json = Record<string, any>;
const ROOT = "mem0";
const seg = (s: unknown) => encodeURIComponent(String(s)).replace(/~/g, "%7E");

function ids(src: Json): { user?: string; agent?: string; run?: string } {
  const f = (src.filters && typeof src.filters === "object" ? flatten(src.filters) : {}) as Json;
  const pick = (k: string) => src[k] ?? f[k];
  return { user: pick("user_id") ?? undefined, agent: pick("agent_id") ?? undefined, run: pick("run_id") ?? undefined };
}

/** mem0 v2 filters may nest AND/OR; equality on the three ids is what maps to scope. */
function flatten(f: Json): Json {
  const out: Json = {};
  const walk = (x: any) => {
    if (!x || typeof x !== "object") return;
    if (Array.isArray(x)) return x.forEach(walk);
    for (const [k, v] of Object.entries(x)) {
      if (k === "AND" || k === "OR") walk(v);
      else if (["user_id", "agent_id", "run_id"].includes(k) && (typeof v === "string" || typeof v === "number")) out[k] = String(v);
    }
  };
  walk(f);
  return out;
}

function scopeOf(i: { user?: string; agent?: string; run?: string }): { full: string; prefix: string; rest: boolean } {
  const parts: string[] = [ROOT];
  const order: Array<["user" | "agent" | "run", string | undefined]> = [["user", i.user], ["agent", i.agent], ["run", i.run]];
  for (const [k, v] of order) if (v !== undefined) parts.push(k, seg(v));
  const prefix: string[] = [ROOT];
  let rest = false;
  for (const [k, v] of order) {
    if (v === undefined) { if (order.slice(order.findIndex((o) => o[0] === k) + 1).some((o) => o[1] !== undefined)) rest = true; break; }
    prefix.push(k, seg(v));
  }
  return { full: parts.join("/"), prefix: prefix.join("/"), rest };
}

function fromScope(scope: string): Json {
  const p = scope.split("/");
  const out: Json = { user_id: null, agent_id: null, run_id: null };
  for (let i = 1; i + 1 < p.length; i += 2) out[`${p[i]}_id`] = decodeURIComponent(p[i + 1]!);
  return out;
}

function matches(scope: string, i: { user?: string; agent?: string; run?: string }): boolean {
  const s = fromScope(scope);
  return (i.user === undefined || s.user_id === i.user) && (i.agent === undefined || s.agent_id === i.agent) && (i.run === undefined || s.run_id === i.run);
}

function item(m: Json, score?: number): Json {
  return { id: m.id, memory: m.content, hash: m.meta?.mem0_hash ?? null, metadata: m.meta?.mem0 ?? null, ...(score !== undefined ? { score } : {}),
    created_at: m.observed_at ? m.observed_at + "Z" : null, updated_at: m.observed_at ? m.observed_at + "Z" : null, ...fromScope(m.scope ?? ""),
    role: m.role || null };
}

export async function mem0Route(svc: Service, pr: Principal, req: IncomingMessage, res: ServerResponse, parts: string[], url: URL,
  readBody: () => Promise<Json>, send: (status: number, body: unknown) => void): Promise<number> {
  const m = req.method, ver = parts[1], route = parts.slice(2).join("/");
  const q = Object.fromEntries(url.searchParams) as Json;
  const ok = (body: unknown) => { send(200, body); return 200; };
  const degraded: string[] = [];
  const listScope = async (i: Json) => {
    const sc = scopeOf(i);
    const out: Json[] = [];
    let cursor: number | null = 0;
    while (cursor !== null) {
      const page: Json = await svc.list({ scope: sc.prefix, limit: 1000, cursor }, pr);
      out.push(...(page.memories as Json[]).filter((x) => matches(x.scope, i)));
      cursor = page.cursor;
    }
    return out;
  };
  if (m === "POST" && route === "memories") { // add
    const b = await readBody();
    const i = ids(b);
    if (!i.user && !i.agent && !i.run) throw new ORPError("invalid_parameter", "one of user_id, agent_id or run_id is required");
    if (b.infer !== false) degraded.push("mem0:infer_not_supported");
    const msgs: Json[] = typeof b.messages === "string" ? [{ role: "user", content: b.messages }] : Array.isArray(b.messages) ? b.messages : [];
    const mems = msgs.filter((x) => x && x.role !== "system" && typeof x.content === "string" && x.content.trim()).map((x) => ({
      kind: "event", content: x.content, role: ["user", "assistant", "tool"].includes(x.role) ? x.role : "user", actor: x.name ?? i.user ?? x.role,
      scope: scopeOf(i).full, readers: [`principal:${pr.id}`], meta: { via: "mem0", ...(b.metadata ? { mem0: b.metadata } : {}) },
    }));
    if (!mems.length) return ok({ results: [], degraded });
    const r = await svc.remember({ memories: mems }, pr);
    return ok({ results: r.ids.map((id: string, k: number) => ({ id, event: "ADD", memory: mems[k]!.content, data: { memory: mems[k]!.content } })), degraded });
  }
  if (m === "POST" && route === "memories/search") {
    const b = await readBody();
    const i = ids(b);
    const sc = scopeOf(i);
    if (sc.rest) degraded.push("mem0:filter_applied_after_recall");
    const limit = Number(b.top_k ?? b.limit ?? 10);
    const r = await svc.recall({ query: String(b.query ?? ""), scope: { within: sc.prefix }, granularity: "unit", budget: { tokens: 8000, items: Math.min(200, Math.max(limit * 3, limit)) } }, pr);
    const want = new Set<string>();
    const out: Json[] = [];
    for (const it of r.items as Json[]) {
      const id = it.memory_ids[0];
      if (want.has(id)) continue;
      want.add(id);
      const g = await svc.get([id], pr);
      const rec = g.memories[0];
      if (!rec || !matches(rec.scope, i)) continue;
      out.push(item(rec, it.score));
      if (out.length >= limit) break;
    }
    return ok(ver === "v2" ? out : { results: out, degraded: [...degraded, ...r.degraded] });
  }
  if (m === "GET" && route === "memories") {
    const i = ids(q);
    const all = (await listScope(i)).map((x) => item(x));
    return ok(ver === "v2" ? all : { results: all });
  }
  if (m === "DELETE" && route === "memories") {
    const i = ids(q);
    if (!i.user && !i.agent && !i.run) throw new ORPError("invalid_parameter", "delete_all needs user_id, agent_id or run_id");
    const all = await listScope(i);
    if (all.length) await svc.forget({ ids: all.map((x) => x.id), mode: "retract" }, pr);
    return ok({ message: "Memories deleted successfully!" });
  }
  const id = parts[3];
  if (parts[2] === "memories" && id) {
    if (m === "GET" && parts.length === 4) {
      const g = await svc.get([id], pr);
      if (!g.memories.length) throw new ORPError("not_found", id);
      return ok(item(g.memories[0]));
    }
    if (m === "PUT" && parts.length === 4) {
      const b = await readBody();
      const g = await svc.get([id], pr);
      if (!g.memories.length) throw new ORPError("not_found", id);
      const cur = g.memories[0];
      const text = b.text ?? b.memory ?? b.data;
      if (typeof text !== "string" || !text) throw new ORPError("invalid_parameter", "text is required");
      // carry every client-settable field over, so a mem0 update changes the text (and metadata) and nothing else
      const keep: Json = {};
      for (const k of ["kind", "scope", "readers", "writers", "actor", "role", "origin", "thread", "seq", "labels", "load",
        "event_time", "valid_from", "valid_to", "expires_at", "sources", "derived_from", "confidence", "support"]) {
        if (cur[k] !== undefined && cur[k] !== "" && cur[k] !== null) keep[k] = cur[k];
      }
      if (cur.readers === null) keep.readers = null;
      if (cur.writers === null) keep.writers = null;
      if (text === cur.content) {
        if (cur.claim) keep.claim = cur.claim; // a metadata-only update keeps the claim
      } else if (keep.origin === "extracted" || keep.origin === "inferred") {
        keep.origin = "human"; // new text written by the caller; lineage stays in derived_from
      }
      const meta: Json = {};
      for (const [k, v] of Object.entries(cur.meta ?? {})) if (!k.startsWith("_") && k !== "entities" && k !== "time_refs") meta[k] = v;
      if (b.metadata) meta.mem0 = b.metadata;
      await svc.remember({ memories: [{ ...keep, id, content: text, meta }], mode: "upsert", if_version: cur.version }, pr);
      return ok({ message: "Memory updated successfully!" });
    }
    if (m === "DELETE" && parts.length === 4) {
      await svc.forget({ ids: [id], mode: "retract" }, pr);
      return ok({ message: "Memory deleted successfully!" });
    }
    if (m === "GET" && parts[4] === "history") {
      const g = await svc.get([id], pr);
      if (!g.memories.length) throw new ORPError("not_found", id);
      const out: Json[] = [];
      let cursor = 0;
      for (;;) {
        const c = await svc.changes(cursor, pr, 1000);
        for (const x of c.changes as Json[]) if (x.id === id) out.push({ id: `${id}:${x.cursor}`, memory_id: id, event: x.op.startsWith("forget") ? "DELETE" : x.version > 1 ? "UPDATE" : "ADD", created_at: x.at + "Z" });
        if (c.cursor === cursor || !(c.changes as Json[]).length) break;
        cursor = c.cursor;
      }
      return ok(out);
    }
  }
  throw new ORPError("not_found", url.pathname);
}
