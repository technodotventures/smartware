// Random Smartware op generator shared by the persistence fuzzer (JS) and exported as JSON for the differential fuzzer.
// Ops are plain JSON: { verb, pr: {id, groups, tenant}, args: [...] }. Bodies use client ids so both engines agree.
export function rng(seed) {
  let s = seed >>> 0 || 1;
  const next = () => { s ^= s << 13; s >>>= 0; s ^= s >>> 17; s ^= s << 5; s >>>= 0; return s / 4294967296; };
  return {
    f: next,
    i: (n) => Math.floor(next() * n),
    pick: (a) => a[Math.floor(next() * a.length)],
    chance: (p) => next() < p,
    some: (a, k) => { const out = []; for (const x of a) if (next() < k) out.push(x); return out; },
  };
}

export const TENANTS = ["t1", "t2"];
export const PRINCIPALS = [
  { id: "alice", groups: [] }, { id: "bob", groups: ["team"] }, { id: "carol", groups: ["approver", "label:secret"] },
  { id: "admin", groups: ["admin"] }, { id: "root", groups: ["*"] },
];
const WORDS = ["paris", "rome", "budget", "contract", "friday", "meeting", "alice", "bob", "moved", "office", "dinner",
  "launch", "project", "berlin", "agreed", "send", "the", "will", "in", "march"];
const SENTS = ["Alice moved to Paris in March.", "Bob will send the contract on Friday.", "We agreed the budget is 40k.",
  "The launch meeting is in Berlin.", "Dinner with the project team on Friday.", "The office moved to Rome."];
export const QUERIES = ["where does alice live", "when is the contract due", "budget", "launch meeting berlin", "friday dinner"];
export const AS_OF = ["2030-01-01T00:00:00", "2021-06-01T00:00:00", "2022-06-01T00:00:00"];

export function makeGen(seed, nIds = 8, idProb = 0.6) {
  const r = rng(seed);
  const ids = Array.from({ length: nIds }, (_, i) => "r" + i);
  const threads = ["th1", "th2", ""];
  let k = 0;
  const content = () => r.chance(0.5) ? r.pick(SENTS) : Array.from({ length: 3 + r.i(8) }, () => r.pick(WORDS)).join(" ") + (r.chance(0.3) ? ". " + r.pick(SENTS) : "");
  const time = () => `202${r.i(4)}-0${1 + r.i(9)}-1${r.i(9)}T0${r.i(9)}:00:00`;
  const pr = (tenant) => ({ ...(r.chance(0.35) ? PRINCIPALS[4] : r.chance(0.4) ? PRINCIPALS[0] : r.pick(PRINCIPALS)), tenant: tenant ?? (r.chance(0.8) ? "t1" : "t2") });
  const readers = () => r.pick([null, null, null, ["principal:alice"], ["group:team"], ["public"], ["principal:bob", "principal:alice"]]);
  const mem = (kind0) => {
    const kind = kind0 ?? r.pick(["event", "event", "fact", "fact", "preference", "procedure", "document", "summary"]);
    const m = { content: content(), kind };
    if (r.chance(idProb)) m.id = r.pick(ids);
    const rd = readers(); if (rd !== null || r.chance(0.2)) m.readers = rd;
    if (r.chance(0.15)) m.writers = r.pick([["group:team"], ["principal:alice"], []]);
    if (r.chance(0.15)) m.labels = r.pick([["secret"], ["pii"], ["x"]]);
    if (r.chance(0.6)) { m.thread = r.pick(threads); m.seq = r.i(6); }
    if (r.chance(0.7)) m.event_time = time();
    if (r.chance(0.2)) m.valid_from = time();
    if (r.chance(0.05)) m.valid_to = time();
    if (r.chance(0.05)) m.expires_at = r.pick(["2020-01-01T00:00:00", "2099-01-01T00:00:00"]);
    if (r.chance(0.2)) m.actor = r.pick(["alice", "bob", "user"]);
    if (r.chance(0.15)) m.role = r.pick(["user", "assistant"]);
    if (r.chance(0.1)) m.load = "always";
    if (r.chance(0.08)) m.scope = r.pick(["a", "a/b", "c"]);
    if (kind !== "event" && kind !== "document" && r.chance(0.6)) {
      m.claim = { subject: r.pick(["alice", "Alice ", "bob"]), predicate: r.pick(["city", "job"]), object: r.pick(["paris", "rome", "Paris", "oslo"]) };
      if (r.chance(0.3)) m.claim.cardinality = r.pick(["one", "many"]);
    }
    if (r.chance(0.2)) { m.derived_from = r.some(ids, 0.15).slice(0, 3); if (m.derived_from.length) m.origin = r.pick(["extracted", "inferred"]); }
    if (r.chance(0.05)) m.meta = { note: "n" + r.i(3) };
    return m;
  };
  return {
    r, ids,
    next(versions) {
      k++;
      const x = r.f();
      const p = pr();
      if (x < 0.36) {
        const mode = r.pick(["append", "append", "upsert", "supersede", "propose"]);
        const n = 1 + (r.chance(0.25) ? r.i(3) : 0);
        const memories = Array.from({ length: n }, () => mem(mode === "supersede" ? r.pick(["fact", "preference", "summary"]) : undefined));
        if (mode === "supersede") for (const m of memories) m.supersedes = r.some(ids, 0.12).slice(0, 2);
        if (mode === "upsert") for (const m of memories) m.id = m.id ?? r.pick(ids);
        const body = { memories, mode };
        if (r.chance(0.25)) body.idempotency_key = "k" + r.i(6);
        if (mode === "upsert" && r.chance(0.85)) {
          const v = versions?.(p.tenant, memories[0].id);
          body.if_version = v != null && r.chance(0.85) ? v : 1 + r.i(3);
        }
        if (r.chance(0.08)) body.allow_reingest = true;
        return { verb: "remember", pr: p, args: [body] };
      }
      if (x < 0.46) {
        const body = { ids: r.some(ids, 0.12).slice(0, 3), mode: r.pick(["retract", "retract", "redact", "purge"]) };
        if (!body.ids.length) body.ids = [r.pick(ids)];
        if (r.chance(0.3)) body.cascade = r.chance(0.5);
        if (r.chance(0.1)) body.dry_run = true;
        if (r.chance(0.15)) body.allow_reingest = true;
        if (r.chance(0.3)) body.reason = "because";
        return { verb: "forget", pr: p, args: [body] };
      }
      if (x < 0.5) {
        const body = { target: r.pick(ids), signal: r.pick(["used", "helpful", "draft_final", "draft_final"]) };
        if (body.signal === "draft_final") { body.draft = content(); body.final = content(); }
        return { verb: "feedback", pr: p, args: [body] };
      }
      if (x < 0.54) return { verb: "approve", pr: p, args: [r.pick(ids)] };
      if (x < 0.57) return { verb: "reflect", pr: p, args: [r.chance(0.5) ? {} : { threads: [r.pick(threads)] }] };
      if (x < 0.62) return { verb: "link", pr: p, args: [{ type: r.pick(["relates", "supports", "x-knows"]), from: r.pick(ids), to: r.pick(ids) }] };
      if (x < 0.64) return { verb: "unlink", pr: p, args: [null] }; // edge chosen by the driver
      if (x < 0.67) return { verb: "import", pr: { id: "admin", groups: ["admin"], tenant: p.tenant === "t1" ? "t2" : "t1" }, args: [null] }; // bundle = export of the other tenant
      // reads
      if (x < 0.78) return { verb: "recall", pr: p, args: [{ query: r.pick(QUERIES), as_of: r.pick(AS_OF), ...(r.chance(0.3) ? { include: { history: true } } : {}),
        ...(r.chance(0.2) ? { kinds: ["fact", "preference"] } : {}), ...(r.chance(0.15) ? { mode: "fast" } : {}), ...(r.chance(0.15) ? { known_at: r.pick(AS_OF) } : {}) }] };
      if (x < 0.83) return { verb: "get", pr: p, args: [r.some(ids, 0.3), r.chance(0.3) ? ["secret"] : []] };
      if (x < 0.86) return { verb: "history", pr: p, args: [r.pick(["alice", "bob"]), r.pick(["city", "job"])] };
      if (x < 0.88) return { verb: "neighbours", pr: p, args: [r.pick(ids), r.i(3), r.i(3)] };
      if (x < 0.9) return { verb: "thread", pr: p, args: [r.pick(["th1", "th2"])] };
      if (x < 0.92) return { verb: "profile", pr: p, args: [{ budget: { tokens: 200 } }] };
      if (x < 0.94) return { verb: "edges", pr: p, args: [r.pick(ids)] };
      if (x < 0.96) return { verb: "changes", pr: p, args: [r.i(5), 50] };
      if (x < 0.98) return { verb: "list", pr: p, args: [{ limit: 5, cursor: r.i(3) }] };
      return { verb: "export", pr: p, args: [] };
    },
  };
}
