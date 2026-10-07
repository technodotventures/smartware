"""Fit the ORP reference engine on DEV splits only (two commands, reproducible):

    python -m bench.train_orp && python -m bench.train_ablation --ship


1. Hand-tuned ranker: coordinate descent over channel weights and features (bench.tune).
2. Learned ranker: grouped 5-fold CV on dev over linear and GBDT models; fit the winner on all dev.
3. Packing: chosen with held-out fold models (packing does not change ranking).
4. Ablation (bench.train_ablation): the same learned ranker without position- and length-correlated features;
   with --ship it replaces orp_params.json when its dev CV objective is higher (it was: 0.7995 vs 0.7952).
Writes results/tuned_params.json and results/orp_params.json; train_ablation writes orp_ablate_params.json and,
with --ship, orp_allfeat_params.json.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace

import numpy as np

from .encoders import DenseEncoder, Encoders
from .ltr import GBDT_GRID, collect, fit, fit_gbdt
from .metrics import score_query
from .tune import objective

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
R = lambda f: os.path.join(ROOT, "results", f)
PACK_VARIANTS = [{}, {"expand_next": False}, {"expand_next": False, "expand_prev_q": False},
                 {"long_unit_tokens": 150}, {"snippet_sentences": 3}]
ABLATE = ("log_tokens", "prev_is_question", "next_reply", "is_question", "order_pos")


def eval_runs(runs, plist):
    """plist: list of (fold_set_or_None, params). Score each run with the params of the fold it is held out in."""
    pooled = {}
    for b in ("locomo", "lme"):
        rows = []
        for (bb, cid, s, task, q, X, y) in runs:
            if bb != b:
                continue
            p = next(p for f, p in plist if f is None or cid in f)
            old = s.eng.p
            s.eng.p = p
            items = s.retrieve(q, k=50)
            s.eng.p = old
            t2s = {t.id: t.session_id for t in task.corpus.turns}
            rows.append(score_query(items, q.gold_turns, q.gold_sessions, t2s, q.answer))
        pooled[b] = {k: float(np.mean([r[k] for r in rows if k in r])) for k in rows[0]}
    return pooled


def main():
    if "--skip-rrf" not in sys.argv:
        if os.path.exists(R("tuned_params.json")):
            os.remove(R("tuned_params.json"))
        subprocess.run([sys.executable, "-m", "bench.tune", R("tuned_params.json"), "1"], cwd=ROOT, check=True)
    base = json.load(open(R("tuned_params.json")))["params"]
    runs = collect(base, Encoders(DenseEncoder.load()))
    print(f"collected {len(runs)} dev queries", flush=True)
    groups = sorted({r[1] for r in runs})
    rng = np.random.default_rng(0)
    rng.shuffle(groups)
    folds = [set(groups[i::5]) for i in range(5)]
    p0 = replace(runs[0][2].eng.p)
    log = {"rrf": objective(eval_runs(runs, [(None, p0)]))}
    print("rrf dev J", round(log["rrf"], 4), flush=True)
    best = None
    for C in (0.01, 0.1):
        plist = []
        for f in folds:
            w, b, mu, sd = fit([r for r in runs if r[1] not in f], C)
            plist.append((f, replace(p0, ranker="linear", lin_w=tuple(map(float, w)), lin_b=b,
                                     lin_mu=tuple(map(float, mu)), lin_sd=tuple(map(float, sd)))))
        j = objective(eval_runs(runs, plist))
        log[f"linear C={C}"] = j
        print("linear", C, round(j, 4), flush=True)
        if best is None or j > best[0]:
            best = (j, ("linear", C), plist)
    for cfg in GBDT_GRID:
        plist = [(f, replace(p0, ranker="gbdt", gbdt=fit_gbdt([r for r in runs if r[1] not in f], cfg)))
                 for f in folds]
        j = objective(eval_runs(runs, plist))
        log[f"gbdt {json.dumps(cfg)}"] = j
        print("gbdt", cfg, round(j, 4), flush=True)
        if j > best[0]:
            best = (j, ("gbdt", cfg), plist)
    j, (kind, cfg), plist = best
    pack_best = ({}, j)
    for v in PACK_VARIANTS[1:]:
        jv = objective(eval_runs(runs, [(f, replace(p, **v)) for f, p in plist]))
        log[f"pack {json.dumps(v)}"] = jv
        print("pack", v, round(jv, 4), flush=True)
        if jv > pack_best[1] + 1e-4:
            pack_best = (v, jv)
    if kind == "gbdt":
        final = dict(base, ranker="gbdt", gbdt=fit_gbdt(runs, cfg), **pack_best[0])
    else:
        w, b, mu, sd = fit(runs, cfg)
        final = dict(base, ranker="linear", lin_w=list(map(float, w)), lin_b=b, lin_mu=list(map(float, mu)),
                     lin_sd=list(map(float, sd)), **pack_best[0])
    json.dump({"params": final, "model": [kind, cfg], "cv_J": pack_best[1], "log": log}, open(R("orp_params.json"), "w"))
    # ablation without position/length-correlated features (same model family and packing)
    abl_runs = []
    idx = [i for i, n in enumerate(__import__("orp.engine", fromlist=["Engine"]).Engine.FEATURES) if n in ABLATE]
    for r in runs:
        X = r[5].copy()
        X[:, idx] = 0.0
        abl_runs.append(r[:5] + (X, r[6]))
    if kind == "gbdt":
        plist_a = [(f, replace(p0, ranker="gbdt", drop_features=ABLATE, **pack_best[0],
                               gbdt=fit_gbdt([r for r in abl_runs if r[1] not in f], cfg))) for f in folds]
        ja = objective(eval_runs(runs, plist_a))
        abl = dict(base, ranker="gbdt", drop_features=list(ABLATE), gbdt=fit_gbdt(abl_runs, cfg), **pack_best[0])
    else:
        ja, abl = None, None
    log["ablation_cv_J"] = ja
    json.dump({"params": abl, "dropped": list(ABLATE), "cv_J": ja}, open(R("orp_ablate_params.json"), "w"))
    json.dump({"params": final, "model": [kind, cfg], "cv_J": pack_best[1], "log": log}, open(R("orp_params.json"), "w"))
    print("final", kind, cfg, pack_best, "ablation", ja, flush=True)


if __name__ == "__main__":
    main()
