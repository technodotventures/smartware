"""Dev-only variant runner for the answer-recall diagnosis (results/dev_diag/PLAN.md). Never reads test splits.

    python -m bench.dev_diag NAME '{"long_unit_tokens": 100000}' [benches] [system]
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

from .encoders import DenseEncoder, Encoders
from .metrics import score_query
from .run import get_tasks, make

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYS = ["recall@500", "recall@1000", "recall@2000", "e_recall@1000", "ans@500", "ans@1000", "ans@2000", "ans@4000",
        "sess_all@5"]


def run(system, kw, bench):
    enc = Encoders(DenseEncoder.load())
    rows = []
    for task in get_tasks(bench, "dev") if bench.startswith("lme") and bench != "lme" else get_tasks(bench):
        qs = [q for q in task.queries if q.split == "dev"]
        if not qs:
            continue
        s = make(system, **kw)
        s.build(task.corpus, enc)
        t2s = {t.id: t.session_id for t in task.corpus.turns}
        tt = {t.id: t.text for t in task.corpus.turns}
        for q in qs:
            m = score_query(s.retrieve(q, k=50), q.gold_turns, q.gold_sessions, t2s, q.answer, tt)
            m.update(qid=q.qid, category=q.category)
            rows.append(m)
        if bench.startswith("lme") and bench != "lme":
            enc.dense._cache.clear()
    return rows


def main():
    name, kw = sys.argv[1], json.loads(sys.argv[2])
    benches = (sys.argv[3] if len(sys.argv) > 3 else "locomo,lme,lmes").split(",")
    system = sys.argv[4] if len(sys.argv) > 4 else "orp"
    out = os.path.join(ROOT, "results", "dev_diag", f"{name}.json")
    res = json.load(open(out)) if os.path.exists(out) else {"system": system, "kw": kw, "rows": {}}
    for b in benches:
        rows = run(system, kw, b)
        res["rows"][b] = rows
        print(name, b, " ".join(f"{k.replace('recall', 'R')}={np.mean([r[k] for r in rows if k in r]) * 100:.1f}"
                                for k in KEYS), flush=True)
    json.dump(res, open(out, "w"))


if __name__ == "__main__":
    main()
