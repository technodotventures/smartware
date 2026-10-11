// Test bridge: drives the TypeScript Service from Python over JSON lines on stdin/stdout, so the oracle's own
// conformance suite runs unchanged against the product engine (tools/bridge/run_suite.py). Test use only: the
// principal is passed in each call, which no real binding ever allows.
import { createHash } from "node:crypto";
import { createInterface } from "node:readline";
import { Service, SqliteStore, Principal, HashingEncoder, defaultParams, withParams, memoryFromJson, memoryToJson, recallRequest,
  parseTime, isoformat, ORPError } from "../../packages/core/dist/index.js";

class Md5Bag { // tests' HashEmbedder: md5 bag of words, 128 dims
  name = "test-md5-128"; dim = 128;
  embed(text) {
    const v = new Float32Array(128);
    for (const w0 of text.toLowerCase().split(/\s+/).filter(Boolean)) {
      const w = w0.replace(/^[.,!?]+|[.,!?]+$/g, "");
      const h = BigInt("0x" + createHash("md5").update(w).digest("hex"));
      v[Number(h % 128n)] += 1;
    }
    let s = 0; for (const x of v) s += x * x; const n = Math.fround(Math.sqrt(Math.fround(s)));
    if (n) for (let i = 0; i < 128; i++) v[i] = v[i] / n;
    return v;
  }
}
class RandomVec extends Md5Bag { // tests' RandomEmbedder: a dense channel with no signal
  name = "test-random-128";
  embed(text) {
    const v = new Float32Array(128); let seed = createHash("sha256").update(text).digest();
    for (let i = 0; i < 128; i += 2) {
      if (i % 32 === 0) seed = createHash("sha256").update(seed).digest();
      const u1 = (seed.readUInt16LE(i % 32) + 1) / 65537, u2 = (seed.readUInt16LE((i + 2) % 32) + 1) / 65537;
      const r = Math.sqrt(-2 * Math.log(u1)); v[i] = r * Math.cos(2 * Math.PI * u2); v[i + 1] = r * Math.sin(2 * Math.PI * u2);
    }
    let s = 0; for (const x of v) s += x * x; const n = Math.sqrt(s); for (let i = 0; i < 128; i++) v[i] /= n;
    return v;
  }
}
class Broken extends Md5Bag { name = "test-broken"; embed(text) { if (text.startsWith("Q:")) throw new Error("embedder down"); return super.embed(text); } }

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
  import_: (s, a, p) => s.import(a[0], p) };

const rl = createInterface({ input: process.stdin });
for await (const line of rl) {
  const m = JSON.parse(line);
  try {
    let out;
    if (m.op === "new") {
      const enc = m.encoder === "random" ? new RandomVec() : m.encoder === "broken" ? new Broken() : m.encoder === "hashing" ? new HashingEncoder() : new Md5Bag();
      const params = withParams(defaultParams(), m.params ?? {});
      const s = new Service(enc, { params, secretScan: false });
      svcs.set(nextH, s); out = { handle: nextH++ };
    } else {
      const s = svcs.get(m.h);
      if (m.op === "call") out = await VERBS[m.verb](s, m.args, pr(m.principal));
      else if (m.op === "mem_get") { const x = s.engine.mem.get(m.mid); out = x ? memoryToJson(x) : null; }
      else if (m.op === "mem_ids") out = [...s.engine.mem.keys()];
      else if (m.op === "engine_remember") out = s.engine.remember(m.memories.map(memoryFromJson), pr(m.principal));
      else if (m.op === "engine_recall") {
        const r = m.req;
        const pack = s.engine.recall(recallRequest({ ...r, principal: pr(r.principal), as_of: r.as_of ? parseTime(r.as_of) : null,
          known_at: r.known_at ? parseTime(r.known_at) : null }));
        out = pack;
      } else if (m.op === "mem_patch") { const x = s.engine.mem.get(m.mid); const v = m.value; x[m.field] = (m.field.endsWith("_at") || m.field.startsWith("valid_") || m.field === "event_time") && v !== null ? parseTime(v) : v; s.engine.dirty(m.mid); s.engine.invalidate(x.tenant); out = true; }
      else if (m.op === "touch") { s.engine.touch(m.tenant); out = true; }
      else if (m.op === "save") { await s.saveTo(m.path); out = true; }
      else if (m.op === "load") { const n = new Service(s.enc, { params: s.engine.p, secretScan: false, store: new SqliteStore(m.path) }); svcs.set(m.h, n); out = true; }
      else if (m.op === "edges_raw") out = Object.fromEntries(s.edgesMap);
      else if (m.op === "readable") out = s.engine.readable(memoryFromJson(m.memory), pr(m.principal), m.labels ?? []);
      else throw new Error("bad op " + m.op);
    }
    send({ id: m.id, ok: out ?? null });
  } catch (e) {
    if (e instanceof ORPError) send({ id: m.id, err: e.toJSON().error });
    else send({ id: m.id, crash: String(e && e.stack || e) });
  }
}
