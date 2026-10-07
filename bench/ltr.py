"""Learn the reference engine's linear ranker on DEV splits only.

1. Collect candidate features from the engine (RRF candidate pool) for every dev query.
2. Grouped cross-validation (by corpus) over C to estimate held-out gain vs. the RRF ranker.
3. Fit on all dev data and write results/ltr_params.json (full engine params incl. weights).

Test splits are never read here.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, replace

import numpy as np
from sklearn.linear_model import LogisticRegression

from orp.engine import Engine, ORPSystem, Params

from .devtune import tasks
from .encoders import DenseEncoder, Encoders
from .metrics import score_query
from .tune import objective

GBDT_GRID = [
    {"max_iter": 150, "learning_rate": 0.05, "max_leaf_nodes": 15, "min_samples_leaf": 40, "l2_regularization": 1.0},
    {"max_iter": 300, "learning_rate": 0.05, "max_leaf_nodes": 15, "min_samples_leaf": 80, "l2_regularization": 1.0},
    {"max_iter": 150, "learning_rate": 0.05, "max_leaf_nodes": 7, "min_samples_leaf": 80, "l2_regularization": 1.0},
]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def collect(base: dict, enc):
    runs = []  # (bench, corpus_id, system, query, cand_turn_ids, X, y)
    for b in ("locomo", "lme"):
        for task in tasks(b):
            s = ORPSystem(**base)
            s.build(task.corpus, enc)
            s.eng.collect = True
            for q in task.queries:
                if q.split != "dev":
                    continue
                s.retrieve(q, k=50)
                cand, X = s.eng.last
                tids = [s.eng.mem[s.eng.ids[k]].meta.get("turn_id") for k in cand]
                y = np.array([t in q.gold_turns for t in tids], dtype=int)
                runs.append((b, task.corpus.id, s, task, q, X, y))
            s.eng.collect = False
    return runs


def fit(runs, C):
    X = np.concatenate([r[5] for r in runs])
    y = np.concatenate([r[6] for r in runs])
    mu, sd = X.mean(0), X.std(0) + 1e-6
    clf = LogisticRegression(C=C, max_iter=2000)
    clf.fit((X - mu) / sd, y)
    return clf.coef_[0], float(clf.intercept_[0]), mu, sd


def fit_gbdt(runs, cfg):
    from sklearn.ensemble import HistGradientBoostingClassifier
    X = np.concatenate([r[5] for r in runs])
    y = np.concatenate([r[6] for r in runs])
    m = HistGradientBoostingClassifier(random_state=0, **cfg).fit(X, y)
    trees = []
    for it in m._predictors:
        nd = it[0].nodes
        trees.append({"f": nd["feature_idx"].astype(int).tolist(), "t": nd["num_threshold"].astype(float).tolist(),
                      "l": nd["left"].astype(int).tolist(), "r": nd["right"].astype(int).tolist(),
                      "v": nd["value"].astype(float).tolist(), "leaf": nd["is_leaf"].astype(int).tolist()})
    model = {"base": float(np.ravel(m._baseline_prediction)[0]), "trees": trees}
    from orp.engine import _gbdt_predict
    chk = _gbdt_predict(model, X[:2000])
    ref = m.decision_function(X[:2000])
    assert np.allclose(chk, ref, atol=1e-4), float(np.abs(chk - ref).max())
    return model


def evaluate(runs, params_override):
    agg = {}
    for b in ("locomo", "lme"):
        rows = []
        for (bb, cid, s, task, q, X, y) in runs:
            if bb != b:
                continue
            old = s.eng.p
            s.eng.p = params_override
            items = s.retrieve(q, k=50)
            s.eng.p = old
            t2s = {t.id: t.session_id for t in task.corpus.turns}
            rows.append(score_query(items, q.gold_turns, q.gold_sessions, t2s, q.answer))
        if rows:
            agg[b] = {k: float(np.mean([r[k] for r in rows if k in r])) for k in rows[0]}
    return agg


def main():
    base = json.load(open(os.path.join(ROOT, "results", "tuned_params.json")))["params"]
    enc = Encoders(DenseEncoder.load())
    runs = collect(base, enc)
    print(f"collected {len(runs)} dev queries, {sum(len(r[6]) for r in runs)} candidates, "
          f"{sum(r[6].sum() for r in runs)} positives", flush=True)
    p0 = replace(runs[0][2].eng.p)
    rrf = evaluate(runs, p0)
    print("RRF dev J=%.4f" % objective(rrf), flush=True)
    groups = sorted({r[1] for r in runs})
    rng = np.random.default_rng(0)
    rng.shuffle(groups)
    folds = [set(groups[i::5]) for i in range(5)]
    best = None
    for C in (0.01, 0.1):
        held = []
        for f in folds:
            tr = [r for r in runs if r[1] not in f]
            te = [r for r in runs if r[1] in f]
            w, b, mu, sd = fit(tr, C)
            pl = replace(p0, ranker="linear", lin_w=tuple(map(float, w)), lin_b=b, lin_mu=tuple(map(float, mu)),
                         lin_sd=tuple(map(float, sd)))
            held.append((te, pl))
        # evaluate each fold's held-out runs with that fold's model, then pool
        pooled = {}
        for b in ("locomo", "lme"):
            rows = []
            for te, pl in held:
                for (bb, cid, s, task, q, X, y) in te:
                    if bb != b:
                        continue
                    old = s.eng.p
                    s.eng.p = pl
                    items = s.retrieve(q, k=50)
                    s.eng.p = old
                    t2s = {t.id: t.session_id for t in task.corpus.turns}
                    rows.append(score_query(items, q.gold_turns, q.gold_sessions, t2s, q.answer))
            pooled[b] = {k: float(np.mean([r[k] for r in rows if k in r])) for k in rows[0]}
        j = objective(pooled)
        print(f"C={C} CV J={j:.4f}  locomo R@1000={pooled['locomo']['recall@1000']:.3f} "
              f"lme R@1000={pooled['lme']['recall@1000']:.3f}", flush=True)
        if best is None or j > best[0]:
            best = (j, C, pooled)
    lin_best = best
    for cfg in GBDT_GRID:
        held = []
        for f in folds:
            tr = [r for r in runs if r[1] not in f]
            te = [r for r in runs if r[1] in f]
            held.append((te, replace(p0, ranker="gbdt", gbdt=fit_gbdt(tr, cfg))))
        pooled = {}
        for b in ("locomo", "lme"):
            rows = []
            for te, pl in held:
                for (bb, cid, s, task, q, X, y) in te:
                    if bb != b:
                        continue
                    old = s.eng.p
                    s.eng.p = pl
                    items = s.retrieve(q, k=50)
                    s.eng.p = old
                    t2s = {t.id: t.session_id for t in task.corpus.turns}
                    rows.append(score_query(items, q.gold_turns, q.gold_sessions, t2s, q.answer))
            pooled[b] = {k: float(np.mean([r[k] for r in rows if k in r])) for k in rows[0]}
        j = objective(pooled)
        print(f"gbdt {cfg} CV J={j:.4f}  locomo R@1000={pooled['locomo']['recall@1000']:.3f} "
              f"lme R@1000={pooled['lme']['recall@1000']:.3f}", flush=True)
        if j > best[0]:
            best = (j, cfg, pooled)
    j, C, pooled = best
    if isinstance(C, dict):
        final = dict(base, ranker="gbdt", gbdt=fit_gbdt(runs, C))
    else:
        w, b, mu, sd = fit(runs, C)
        final = dict(base, ranker="linear", lin_w=[float(x) for x in w], lin_b=b, lin_mu=[float(x) for x in mu],
                     lin_sd=[float(x) for x in sd])
    w, b, mu, sd = fit(runs, lin_best[1])
    out = {"params": final, "C": C, "cv_J": j, "rrf_dev_J": objective(rrf), "cv": pooled, "rrf_dev": rrf,
           "features": list(Engine.FEATURES), "weights": dict(zip(Engine.FEATURES, map(float, w)))}
    json.dump(out, open(os.path.join(ROOT, "results", "ltr_params.json"), "w"), indent=1)
    print("chosen C", C, "CV J", round(j, 4), "vs RRF", round(objective(rrf), 4))
    print(json.dumps(out["weights"], indent=1))


if __name__ == "__main__":
    main()
