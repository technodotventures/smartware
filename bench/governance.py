"""ORP-Gov: permission-aware recall for multi-tenant SaaS.

Setup: each LoCoMo test conversation becomes a workspace whose sessions are
channels. Each of 6 principals is a member of a seeded random half of the
channels. Every test question is asked by every principal:
  - "permitted" when the principal can read all gold channels: we measure recall@1000;
  - "forbidden" when it can read none of them: we measure leakage (returned text
    from channels the asker cannot read) on raw units and on derived summaries.

All strategies use the SAME ranker (the ORP reference engine), so differences
come only from where and how access control is applied. Strategies:
  pre_filter        ORP §5.1: admissibility before ranking (+ §6.3 closure for derived)
  post_filter@k     rank everything, keep top-k, then drop unreadable items
  scope_only        no per-item ACL inside a workspace (whole-workspace scope)
  derived_bank      raw units pre-filtered, but derived summaries scoped to the workspace
                    (consolidated memories inherit the bank/dataset scope, not their sources')
"""
from __future__ import annotations

import json
import random
import sys
from collections import defaultdict

from orp.api import Service
from orp.engine import Params
from orp.model import Principal

from .data import load_locomo
from .encoders import DenseEncoder, Encoders
from .metrics import pack
from .system import Item

N_PRINCIPALS = 6


def run(params: Params | None = None, visibility: float = 0.5):
    enc = Encoders(DenseEncoder.load())
    tasks = [t for t in load_locomo() if t.queries and t.queries[0].split == "test"]
    rows = defaultdict(list)
    for task in tasks:
        c = task.corpus
        rng = random.Random(f"gov:{c.id}")
        sessions = list(c.session_order)
        members = {f"u{i}": set(rng.sample(sessions, max(1, int(len(sessions) * visibility)))) for i in range(N_PRINCIPALS)}
        prins = {u: Principal(u, frozenset(f"ch:{s}" for s in chans), tenant="org") for u, chans in members.items()}
        admin = Principal("admin", frozenset({"*"}), tenant="org")

        def build(acl: bool, derived: str):
            svc = Service(enc.dense, params)
            mems = [{"kind": "event", "content": t.text, "actor": t.speaker, "thread": t.session_id, "seq": t.idx,
                     "readers": [f"group:ch:{t.session_id}"] if acl else None, "meta": {"turn_id": t.id}}
                    for t in c.turns]
            ids = svc.remember({"memories": mems}, admin)["ids"]
            for m, t in zip(ids, c.turns):  # observed_at = conversation time
                svc.engine.mem[m].observed_at = t.ts
            svc.engine._dirty = True
            if derived != "none":
                svc.reflect({}, admin)  # one extractive summary per channel, derived_from its units
                if derived == "bank":
                    for m in svc.engine.mem.values():
                        if m.kind == "summary":
                            m.readers = None  # workspace-scoped, like bank/dataset-level consolidation
                svc.engine._dirty = True
            return svc

        variants = {"pre_filter": build(True, "closed"), "scope_only": build(False, "none"),
                    "derived_bank": build(True, "bank")}
        t2s = {t.id: t.session_id for t in c.turns}
        mem2turn = {}
        for v in variants.values():
            for m in v.engine.mem.values():
                mem2turn[m.id] = m.meta.get("turn_id")

        def items_from(pack_json):
            out = []
            for it in pack_json["items"]:
                tids = [mem2turn.get(i) for i in it["memory_ids"] if mem2turn.get(i)]
                kinds = it["kind"]
                out.append((Item(tids, it["text"]), it["memory_ids"], kinds))
            return out

        for q in task.queries:
            gold_ch = {t2s[g] for g in q.gold_turns}
            for u, chans in members.items():
                if gold_ch <= chans:
                    kind = "permitted"
                elif not (gold_ch & chans):
                    kind = "forbidden"
                else:
                    continue
                pr = prins[u]
                body = {"query": q.text, "as_of": q.time.isoformat(), "budget": {"tokens": 4000}}

                def readable_turn(tid):
                    return tid is None or t2s[tid] in chans

                def leak(itms, svc):
                    bad = 0
                    for it, mids, kind_ in itms[:]:
                        for mid in mids:
                            m = svc.engine.mem.get(mid)
                            if m is None:
                                continue
                            if m.kind == "summary":
                                src_ch = {svc.engine.mem[s].thread for s in m.derived_from if s in svc.engine.mem}
                                if not src_ch <= chans:
                                    bad += 1
                            elif t2s.get(m.meta.get("turn_id")) not in chans:
                                bad += 1
                    return bad

                res = {}
                p_pre = items_from(variants["pre_filter"].recall(body, pr))
                res["pre_filter"] = p_pre
                all_rank = items_from(variants["pre_filter"].recall(body, admin))
                pre_svc = variants["pre_filter"]
                for k in (10, 50):  # correct ACL check, applied after the top-k cut
                    res[f"post_filter@{k}"] = [x for x in all_rank[:k] if all(
                        pre_svc._can_read(pre_svc.engine.mem[i], pr) for i in x[1] if i in pre_svc.engine.mem)]
                res["scope_only"] = items_from(variants["scope_only"].recall(body, pr))
                res["derived_bank"] = items_from(variants["derived_bank"].recall(body, pr))
                for name, itms in res.items():
                    svc = variants["scope_only"] if name == "scope_only" else variants[
                        "derived_bank" if name == "derived_bank" else "pre_filter"]
                    its = [x[0] for x in itms]
                    cov, used, kept = pack(its, 1000)
                    kept_n = len(kept)
                    r = {"kind": kind}
                    if kind == "permitted":
                        r["recall@1000"] = len(cov & q.gold_turns) / len(q.gold_turns)
                    lk = leak(itms[:kept_n], svc)
                    r["leaked_items@1000"] = lk
                    r["any_leak"] = float(lk > 0)
                    if kind == "forbidden":
                        gold_text_leak = len(cov & q.gold_turns) > 0
                        r["answer_evidence_leaked"] = float(gold_text_leak)
                    rows[name].append(r)
    out = {}
    for name, rs in rows.items():
        per = [r for r in rs if r["kind"] == "permitted"]
        forb = [r for r in rs if r["kind"] == "forbidden"]
        out[name] = {
            "permitted_n": len(per), "forbidden_n": len(forb),
            "recall@1000_permitted": sum(r["recall@1000"] for r in per) / max(1, len(per)),
            "queries_with_any_leak": sum(r["any_leak"] for r in rs) / max(1, len(rs)),
            "forbidden_answer_evidence_leaked": sum(r["answer_evidence_leaked"] for r in forb) / max(1, len(forb)),
        }
    return out


def final_params() -> Params:
    from .orp_variants import ORPLtr
    return ORPLtr().params


if __name__ == "__main__":
    vis = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
    r = run(final_params(), vis)
    print(json.dumps(r, indent=1))
    json.dump(r, open(sys.argv[1] if len(sys.argv) > 1 else "results/governance.json", "w"), indent=1)
