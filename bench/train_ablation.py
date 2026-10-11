"""Fit the ablated learned ranker (no position/length-correlated features) with the same model family,
regularisation and packing as the shipped Smartware ranker, on dev only. Writes results/smartware_ablate_params.json."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import replace

import numpy as np

from smartware.engine import Engine

from .encoders import DenseEncoder, Encoders
from .ltr import collect, fit, fit_gbdt
from .train_smartware import ABLATE, R, eval_runs
from .tune import objective


def main():
    shipped = json.load(open(R("smartware_params.json")))
    kind, cfg = shipped["model"]
    base = json.load(open(R("tuned_params.json")))["params"]
    runs = collect(base, Encoders(DenseEncoder.load()))
    idx = [i for i, n in enumerate(Engine.FEATURES) if n in ABLATE]
    abl = []
    for r in runs:
        X = r[5].copy()
        X[:, idx] = 0.0
        abl.append(r[:5] + (X, r[6]))
    groups = sorted({r[1] for r in runs})
    rng = np.random.default_rng(0)
    rng.shuffle(groups)
    folds = [set(groups[i::5]) for i in range(5)]
    p0 = replace(runs[0][2].eng.p)
    pack = {k: shipped["params"][k] for k in ("expand_next", "expand_prev_q", "long_unit_tokens", "snippet_sentences")
            if k in shipped["params"]}

    def model(rs):
        if kind == "gbdt":
            return dict(ranker="gbdt", gbdt=fit_gbdt(rs, cfg))
        w, b, mu, sd = fit(rs, cfg)
        return dict(ranker="linear", lin_w=tuple(map(float, w)), lin_b=b, lin_mu=tuple(map(float, mu)),
                    lin_sd=tuple(map(float, sd)))
    plist = [(f, replace(p0, drop_features=ABLATE, **pack, **model([r for r in abl if r[1] not in f]))) for f in folds]
    j = objective(eval_runs(runs, plist))
    final = dict(base, drop_features=list(ABLATE), **pack, **{k: (list(v) if isinstance(v, tuple) else v)
                                                              for k, v in model(abl).items()})
    json.dump({"params": final, "dropped": list(ABLATE), "cv_J": j, "shipped_cv_J": shipped["cv_J"]},
              open(R("smartware_ablate_params.json"), "w"))
    print("ablation CV J", round(j, 4), "vs shipped", round(shipped["cv_J"], 4))
    if "--ship" in sys.argv and j > shipped["cv_J"]:
        # step 2 of the pipeline (python -m bench.train_smartware && python -m bench.train_ablation --ship): keep the
        # all-feature model as smartware_allfeat_params.json and ship the ablated one if its dev CV objective is higher
        json.dump(shipped, open(R("smartware_allfeat_params.json"), "w"))
        json.dump({"params": final, "model": [kind, cfg], "cv_J": j, "log": shipped.get("log"),
                   "selection": f"Grouped 5-fold CV on dev: the ranker without position/length-correlated features "
                                f"{list(ABLATE)} scored {j:.4f} vs {shipped['cv_J']:.4f} with all features, so it ships."},
                  open(R("smartware_params.json"), "w"))
        print("shipped the ablated ranker")


if __name__ == "__main__":
    main()
