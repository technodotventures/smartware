"""Choose packing parameters with held-out (cross-validated) ranker models, on dev only.
Packing does not change ranking, but evaluating it with a ranker fitted on the same queries would
overstate ranking quality and understate the value of safety nets like dialogue expansion."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import replace

import numpy as np

from .encoders import DenseEncoder, Encoders
from .ltr import GBDT_GRID, collect, fit_gbdt
from .metrics import score_query
from .tune import objective

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VARIANTS = [{}, {"expand_next": False}, {"expand_prev_q": False}, {"expand_next": False, "expand_prev_q": False},
            {"long_unit_tokens": 150}, {"snippet_sentences": 3}, {"long_unit_tokens": 150, "snippet_sentences": 3},
            {"headers": False}, {"resolve_dates": False}]


def main():
    base = json.load(open(os.path.join(ROOT, "results", "tuned_params.json")))["params"]
    cfg = json.load(open(os.path.join(ROOT, "results", "ltr_params_v1.json")))["C"]
    runs = collect(base, Encoders(DenseEncoder.load()))
    groups = sorted({r[1] for r in runs})
    rng = np.random.default_rng(0)
    rng.shuffle(groups)
    folds = [set(groups[i::5]) for i in range(5)]
    p0 = replace(runs[0][2].eng.p)
    models = []
    for f in folds:
        tr = [r for r in runs if r[1] not in f]
        models.append((f, fit_gbdt(tr, cfg)))
    print("fitted fold models", flush=True)
    res = []
    for v in VARIANTS:
        pooled = {}
        for b in ("locomo", "lme"):
            rows = []
            for f, model in models:
                pl = replace(p0, ranker="gbdt", gbdt=model, w_sub=0.0, w_qexp=0.0, **v)
                for (bb, cid, s, task, q, X, y) in runs:
                    if bb != b or cid not in f:
                        continue
                    old = s.eng.p
                    s.eng.p = pl
                    items = s.retrieve(q, k=50)
                    s.eng.p = old
                    t2s = {t.id: t.session_id for t in task.corpus.turns}
                    rows.append(score_query(items, q.gold_turns, q.gold_sessions, t2s, q.answer))
            pooled[b] = {k: float(np.mean([r[k] for r in rows if k in r])) for k in rows[0]}
        j = objective(pooled)
        res.append((j, v))
        print(json.dumps(v), round(j, 4), "locomo R@1000", round(pooled["locomo"]["recall@1000"], 3),
              "ans@1000", round(pooled["locomo"]["ans@1000"], 3), "| lme R@1000", round(pooled["lme"]["recall@1000"], 3),
              "ans@1000", round(pooled["lme"]["ans@1000"], 3), flush=True)
    json.dump([{"J": j, "variant": v} for j, v in res], open(os.path.join(ROOT, "results", "pack_cv.json"), "w"),
              indent=1)


if __name__ == "__main__":
    main()
