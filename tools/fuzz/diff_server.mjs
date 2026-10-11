// Differential bridge (audit copy of tools/bridge/server.mjs): the TypeScript Service over JSON lines, with a
// controllable clock (op "clock" freezes Date.now at a given epoch-ms) and the TS-only verbs list/unlink.
import { createInterface } from "node:readline";
import { Service, SqliteStore, Principal, HashingEncoder, defaultParams, withParams, ORPError } from "../../packages/core/dist/index.js";
let frozen = null;
const realNow = Date.now;
Date.now = () => (frozen === null ? realNow() : frozen);
const svcs = new Map();
let nextH = 1;
const pr = (p) => new Principal(p.id, p.groups, p.tenant);
const send = (o) => process.stdout.write(JSON.stringify(o) + "\n");
const VERBS = { capabilities: (s, a, p) => s.capabilities(p), remember: (s, a, p) => s.remember(a[0], p), recall: (s, a, p) => s.recall(a[0], p),
  profile: (s, a, p) => s.profile(a[0], p), get: (s, a, p) => s.get(a[0], p, a[1] ?? []), neighbours: (s, a, p) => s.neighbours(a[0], a[1], a[2], p),
  thread: (s, a, p) => s.thread(a[0], p), history: (s, a, p) => s.history(a[0], a[1], p), link: (s, a, p) => s.link(a[0], p),
  edges: (s, a, p) => s.edges(a[0], p), forget: (s, a, p) => s.forget(a[0], p), feedback: (s, a, p) => s.feedback(a[0], p),
  approve: (s, a, p) => s.approve(a[0], p), reflect: (s, a, p) => s.reflect(a[0], p), job: (s, a, p) => s.job(a[0], p),
  changes: (s, a, p) => s.changes(a[0], p, a[1] ?? 500), audit_log: (s, a, p) => s.auditLog(p), export: (s, a, p) => s.export(p),
  import_: (s, a, p) => s.import(a[0], p), list: (s, a, p) => s.list(a[0], p), unlink: (s, a, p) => s.unlink(a[0], p) };
const rl = createInterface({ input: process.stdin });
for await (const line of rl) {
  const m = JSON.parse(line);
  try {
    let out;
    if (m.op === "new") {
      const s = new Service(new HashingEncoder(), { params: withParams(defaultParams(), m.params ?? {}), secretScan: false,
        store: m.store ? new SqliteStore(m.store) : null });
      svcs.set(nextH, s); out = { handle: nextH++ };
    } else if (m.op === "clock") { frozen = m.ms; out = true; }
    else {
      const s = svcs.get(m.h);
      if (m.op === "call") out = await VERBS[m.verb](s, m.args, pr(m.principal));
      else if (m.op === "reopen") { s.close(); svcs.set(m.h, new Service(s.enc, { params: s.engine.p, secretScan: false, store: new SqliteStore(m.store) })); out = true; }
      else if (m.op === "save") { await s.saveTo(m.path); out = true; }
      else throw new Error("bad op " + m.op);
    }
    send({ id: m.id, ok: out ?? null });
  } catch (e) {
    if (e instanceof ORPError) send({ id: m.id, err: e.toJSON().error });
    else send({ id: m.id, crash: String(e && e.stack || e) });
  }
}
