"""Smartware Gov v2: permission-aware recall for multi-tenant SaaS.

Each LoCoMo test conversation becomes a workspace (tenant). Its sessions are channels; each of
6 principals belongs to a seeded random share of channels (visibility). Inside channels, 20% of
turns are additionally private to a random half of the principals (mixed-ACL threads, like DMs or
restricted replies inside a shared thread). A second tenant reuses the SAME thread ids with a
different conversation, to test that threads never cross tenants.

Every test question is asked by every principal:
  permitted  = the principal can read every gold turn  -> recall@1000 (primary packing)
  forbidden  = the principal can read no gold turn       -> leakage
Measured per strategy:
  recall@1000         on permitted questions
  text_leak           share of questions whose packed context contains text from any unit the asker cannot read, in
                      this tenant or the other one (per line: 8-word shingles, or the whole line when it is shorter,
                      counted only when that text occurs nowhere the asker can read)
  answer_leak         share of forbidden questions whose context contains a gold turn's text
  rank_side_channel   share of sampled questions whose top-10 items (memory ids, text and scores, at unit and auto
                      granularity) change when the asker's inadmissible memories are removed from the store

Turns are inserted in a seeded interleaved order across sessions (as channels interleave in a real workspace), so
tie-breaks that depend on hidden units would show up.

Strategies (same ranker everywhere; only access control differs):
  smartware                Smartware §5.1/§6.3: admissibility before ranking, admissible-set statistics, masked expansion
  global_stats       admissibility before ranking, but collection statistics and neighbour features computed over
                     all memories (common when a search engine filters by ACL inside a shared index)
  post_filter@10/@50 rank with the shared index (global statistics, same spans), cut to the top k items, then drop
                     every unit the asker cannot read from each item (e.g. Letta tag filters after the limit, Hindsight graph-arm
                     tag filtering after its cut; reports/letta.md, hindsight.md §12)
  scope_only         no per-item ACL inside a workspace (tenant/user scoping only)
  derived_bank       per-item ACL on raw units, but derived summaries are workspace-scoped (bank/dataset-level
                     consolidation, emulated with the engine's own extractive summaries)
"""
from __future__ import annotations

import json
import random
import re
import sys
from collections import defaultdict
from dataclasses import replace

from smartware.api import Service
from smartware.engine import Params
from smartware.model import Principal, RecallRequest

from .data import load_locomo
from .encoders import DenseEncoder, Encoders
from .metrics import chars4, pack
from .system import Item

N_PRINCIPALS = 6


def shingles(text: str, n: int = 8, speakers: frozenset = frozenset()) -> set[str]:
    """Per line, without headers and without a leading '<speaker>: ' for a known speaker: 8-word shingles, or the
    whole line if shorter. Turn texts are passed without speakers (nothing stripped); rendered context with them."""
    out = set()
    for line in text.split("\n"):
        if line.startswith("## "):
            continue
        line = re.sub(r"^\[[a-z]+\] ", "", line)
        head, sep, rest = line.partition(": ")
        if sep and head in speakers:
            line = rest
        w = re.findall(r"[a-z0-9']+", line.lower())
        if not w:
            continue
        if len(w) < n:
            out.add(" ".join(w))
        else:
            out |= {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}
    return out


def run(params: Params, visibility: float = 0.5, private_share: float = 0.2, max_tasks: int | None = None,
        max_queries: int | None = None):
    enc = Encoders(DenseEncoder.load())
    tasks = [t for t in load_locomo() if t.queries and t.queries[0].split == "test"]
    if max_queries:  # smoke runs only
        tasks = [type(t)(t.corpus, t.queries[:max_queries]) if hasattr(t, "corpus") else t for t in tasks]
    rows = defaultdict(list)
    examples: list = []
    for ti, task in enumerate(tasks[:max_tasks] if max_tasks else tasks):
        c = task.corpus
        other = tasks[(ti + 1) % len(tasks)].corpus  # second tenant, same thread ids
        rng = random.Random(f"gov2:{c.id}:{visibility}")
        t2s = {t.id: t.session_id for t in c.turns}
        ts_of = {t.id: t.ts for t in c.turns}
        sessions = list(c.session_order)
        members = {f"u{i}": set(rng.sample(sessions, max(1, int(len(sessions) * visibility))))
                   for i in range(N_PRINCIPALS)}
        private = {t.id: set(rng.sample(sorted(members), N_PRINCIPALS // 2)) for t in c.turns
                   if rng.random() < private_share}

        def can_read(u, tid):
            return t2s[tid] in members[u] and (tid not in private or u in private[tid])

        prins = {u: Principal(u, frozenset(f"ch:{s}" for s in chans) |
                              frozenset(f"dm:{tid}" for tid in private if can_read(u, tid)), tenant="org")
                 for u, chans in members.items()}
        admin = Principal("admin", frozenset({"*"}), tenant="org")
        admin2 = Principal("admin2", frozenset({"*"}), tenant="org2")

        # interleave sessions: a seeded merge that keeps each session's own order
        queues = {s: [t for t in c.turns if t.session_id == s] for s in sessions}
        orng = random.Random(f"gov2-merge:{c.id}")
        order = []
        while any(queues.values()):
            live_s = [s for s in sessions if queues[s]]
            order.append(queues[orng.choice(live_s)].pop(0))

        def build(acl: bool, derived: str, p: Params, exclude_for: str | None = None):
            svc = Service(enc.dense, p)
            mems = []
            for t in order:
                if exclude_for and not can_read(exclude_for, t.id):
                    continue
                rd = None if not acl else [f"group:dm:{t.id}"] if t.id in private else [f"group:ch:{t.session_id}"]
                mems.append({"kind": "event", "content": t.text, "actor": t.speaker,
                             "thread": t.session_id.split(":")[-1], "seq": t.idx, "readers": rd,
                             "meta": {"turn_id": t.id, "idempotency_key": t.id}})
            for m in svc.remember({"memories": mems}, admin)["ids"]:
                mm = svc.engine.mem[m]
                mm.observed_at = mm.valid_from = ts_of[mm.meta["turn_id"]]
            mems2 = [{"kind": "event", "content": t.text, "actor": t.speaker, "thread": t.session_id.split(":")[-1],
                      "seq": t.idx, "meta": {"turn_id": "other:" + t.id, "idempotency_key": "o" + t.id}}
                     for t in other.turns]
            for m, t in zip(svc.remember({"memories": mems2}, admin2)["ids"], other.turns):
                svc.engine.mem[m].observed_at = svc.engine.mem[m].valid_from = t.ts
            svc.engine.touch()
            if derived != "none":
                svc.reflect({}, admin)
                if derived == "bank":
                    for m in svc.engine.mem.values():
                        if m.kind == "summary":
                            m.readers = None
                svc.engine.touch()
            return svc

        gp = replace(params, admissible_stats=False)
        V = {"smartware": build(True, "closed", params), "global_stats": build(True, "closed", gp),
             "scope_only": build(False, "none", params), "derived_bank": build(True, "bank", params)}
        # leak evidence = shingles that occur ONLY in text the asker cannot read
        read_text = {u: set().union(*(shingles(t.text) for t in c.turns if can_read(u, t.id))) for u in members}
        unread_text = {u: set().union(*(shingles(t.text) for t in c.turns if not can_read(u, t.id))) - read_text[u]
                       for u in members}
        other_all = set().union(*(shingles(t.text) for t in other.turns))
        other_text = other_all - set().union(*(shingles(t.text) for t in c.turns))
        tid_text = {t.id: shingles(t.text) for t in c.turns}
        spk = frozenset({t.speaker for t in c.turns} | {t.speaker for t in other.turns})

        def to_items(pk, svc):
            out = []
            for it in pk["items"]:
                tids = [svc.engine.mem[mid].meta.get("turn_id") for mid in it["memory_ids"] if mid in svc.engine.mem]
                out.append(Item([x for x in tids if x and x in t2s], it["text"]))
            return out

        for q in task.queries:
            for u in members:
                rg = [can_read(u, g) for g in q.gold_turns]
                kind = "permitted" if all(rg) else "forbidden" if not any(rg) else None
                if kind is None:
                    continue
                pr = prins[u]
                body = {"query": q.text, "as_of": q.time.isoformat(), "budget": {"tokens": 4000}}
                res = {name: to_items(V[name].recall(body, pr), V[name])
                       for name in ("smartware", "global_stats", "scope_only", "derived_bank")}
                gl = V["global_stats"].engine  # shared index: statistics over every memory
                upk = gl.recall(RecallRequest(query=q.text, principal=admin, tenant="org", as_of=q.time,
                                              budget_tokens=None, max_items=50, filters={"granularity": "auto"}))
                for k in (10, 50):  # shared-index ranking, cut to top k, then a per-unit ACL check inside each item
                    keep = []
                    for it in upk.items[:k]:
                        ok = [mid for mid in it.memory_ids if gl.readable(gl.mem[mid], pr)]
                        if ok:
                            keep.append({"memory_ids": ok,
                                         "text": "\n".join(f"{gl.mem[m].actor}: {gl.mem[m].content}" for m in ok)})
                    res[f"post_filter@{k}"] = to_items({"items": keep}, V["global_stats"])
                for name, its in res.items():
                    cov, used, kept = pack(its, 1000, chars4, True)
                    ctx = set().union(*(shingles(it.text, speakers=spk) for it in kept)) if kept else set()
                    leak = ctx & (unread_text[u] | other_text)
                    r = {"kind": kind, "text_leak": float(bool(leak))}
                    if leak and name == "smartware" and len(examples) < 40:
                        examples.append({"u": u, "qid": q.qid, "shingles": sorted(leak)[:3],
                                         "lines": [ln for it in kept for ln in it.text.split("\n")
                                                   if shingles(ln, speakers=spk) & leak][:3]})
                    if kind == "permitted":
                        r["recall@1000"] = len(cov & q.gold_turns) / len(q.gold_turns)
                    else:
                        r["answer_leak"] = float(any(ctx & (tid_text[g] - read_text[u]) for g in q.gold_turns))
                    rows[name].append(r)
        for name, p_ in (("smartware", params), ("global_stats", gp)):
            for u in list(members)[:2]:
                clean = build(True, "none", p_, exclude_for=u)
                full_store = build(True, "none", p_)
                changed = n = 0
                for q in task.queries[::3]:
                    diff = False
                    for g in ("unit", "auto"):
                        body = {"query": q.text, "as_of": q.time.isoformat(), "budget": {"tokens": 4000},
                                "granularity": g}

                        def sig(svc):
                            return [([svc.engine.mem[m].meta.get("turn_id") for m in it["memory_ids"]], it["text"],
                                     it["score"]) for it in svc.recall(body, prins[u])["items"][:10]]
                        diff |= sig(full_store) != sig(clean)
                    changed += diff
                    n += 1
                rows[name + ":side"].append(changed / max(1, n))
    out = {}
    for name, rs in rows.items():
        if name.endswith(":side"):
            continue
        per = [r for r in rs if r["kind"] == "permitted"]
        forb = [r for r in rs if r["kind"] == "forbidden"]
        side = rows.get(name + ":side")
        out[name] = {"permitted_n": len(per), "forbidden_n": len(forb),
                     "recall@1000": sum(r["recall@1000"] for r in per) / max(1, len(per)),
                     "text_leak": sum(r["text_leak"] for r in rs) / max(1, len(rs)),
                     "answer_leak": sum(r["answer_leak"] for r in forb) / max(1, len(forb)),
                     "rank_side_channel": (sum(side) / len(side)) if side else None}
    out["_smartware_leak_examples"] = examples
    return out


def final_params() -> Params:
    from .smartware_variants import SmartwareLtr
    return SmartwareLtr().params


if __name__ == "__main__":
    vis = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
    smoke = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    r = run(final_params(), vis, max_tasks=smoke or None, max_queries=int(sys.argv[4]) if len(sys.argv) > 4 else (12 if smoke else None))
    print(json.dumps(r, indent=1))
    json.dump(r, open(sys.argv[1] if len(sys.argv) > 1 else "results/governance.json", "w"), indent=1)
