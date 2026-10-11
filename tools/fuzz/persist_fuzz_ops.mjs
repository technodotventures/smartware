// Executes a generated op against a TS Service (used by persist_fuzz.mjs and debugging scripts).
import { star, Principal } from "./lib.mjs";
const pr = (p) => new Principal(p.id, p.groups, p.tenant);
export async function call(s, op, state) {
  const p = pr(op.pr), a = op.args;
  switch (op.verb) {
    case "remember": return s.remember(a[0], p);
    case "forget": return s.forget(a[0], p);
    case "feedback": return s.feedback(a[0], p);
    case "approve": return s.approve(a[0], p);
    case "reflect": return s.reflect(a[0], p);
    case "link": { const r = await s.link(a[0], p); state.edges.push([p.tenant, r.id]); return r; }
    case "unlink": { const e = state.edges.length ? state.edges[state.rand(state.edges.length)] : null; return s.unlink(e && e[0] === p.tenant ? e[1] : "edge_none", p); }
    case "import": { const other = p.tenant === "t1" ? "t2" : "t1"; const b = await s.export(star(other)); return s.import(retenant(b, p.tenant), p); }
    case "recall": return s.recall(a[0], p);
    case "get": return s.get(a[0], p, a[1]);
    case "history": return s.history(a[0], a[1], p);
    case "neighbours": return s.neighbours(a[0], a[1], a[2], p);
    case "thread": return s.thread(a[0], p);
    case "profile": return s.profile(a[0], p);
    case "edges": return s.edges(a[0], p);
    case "changes": return s.changes(a[0], p, a[1]);
    case "list": return s.list(a[0], p);
    case "export": return s.export(p);
  }
  throw new Error("verb " + op.verb);
}
export function retenant(b, t) {
  const fix = (l) => { const d = JSON.parse(l); d.tenant = t; return JSON.stringify(d); };
  return { manifest: { ...b.manifest, tenant: t }, "memories.jsonl": b["memories.jsonl"].split("\n").filter(Boolean).map(fix).join("\n"),
    "edges.jsonl": b["edges.jsonl"].split("\n").filter(Boolean).map(fix).join("\n") };
}
