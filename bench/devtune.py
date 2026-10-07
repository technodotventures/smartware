"""Evaluate the reference engine on the DEV splits only (used for all tuning).

python -m bench.devtune '{"w_prf": 0.5}'
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

from .data import load_lme, load_locomo
from .encoders import DenseEncoder, Encoders
from .metrics import score_query

_cache = {}


def tasks(bench):
    if bench not in _cache:
        ts = load_locomo() if bench == "locomo" else load_lme()
        _cache[bench] = [t for t in ts if any(q.split == "dev" for q in t.queries)]
    return _cache[bench]


def evaluate(make_system, benches=("locomo", "lme"), enc=None, cats=False, subset: dict | None = None):
    """subset: optional {"locomo": n_tasks, "lme": n_tasks} to evaluate on the first n dev tasks only."""
    enc = enc or Encoders(DenseEncoder.load())
    out = {}
    for b in benches:
        rows = []
        for task in (tasks(b)[: subset[b]] if subset else tasks(b)):
            s = make_system()
            s.build(task.corpus, enc)
            t2s = {t.id: t.session_id for t in task.corpus.turns}
            for q in task.queries:
                if q.split != "dev":
                    continue
                items = s.retrieve(q, k=50)
                m = score_query(items, q.gold_turns, q.gold_sessions, t2s, q.answer)
                m["category"] = q.category
                rows.append(m)
        agg = {k: sum(r[k] for r in rows if k in r) / max(1, sum(1 for r in rows if k in r))
               for k in ("recall@500", "recall@1000", "recall@2000", "full@1000", "sess_all@5", "mrr", "ans@500", "ans@1000", "ans@2000")}
        if cats:
            bc = defaultdict(list)
            for r in rows:
                bc[r["category"]].append(r.get("recall@1000", 0))
            agg["cats"] = {c: round(sum(v) / len(v), 3) for c, v in bc.items()}
        agg["n"] = len(rows)
        out[b] = agg
    return out


if __name__ == "__main__":
    from orp.engine import ORPSystem
    kw = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    r = evaluate(lambda: ORPSystem(**kw), cats=True)
    print(json.dumps(r, indent=1))
