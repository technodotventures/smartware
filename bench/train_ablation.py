"""Fit the ablated learned ranker (no position/length-correlated features) with the same model family,
regularisation and packing as the shipped ORP ranker, on dev only. Writes results/orp_ablate_params.json."""
from __future__ import annotations

import json
import os
from dataclasses import replace

import numpy as np

from orp.engine import Engine

from .encoders import DenseEncoder, Encoders
from .ltr import collect, fit, fit_gbdt
from .train_orp import ABLATE, R, eval_runs
from .tune import objective


def main():
    shipped = json.load(open(R("orp_params.json")))
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
              open(R("orp_ablate_params.json"), "w"))
    print("ablation CV J", round(j, 4), "vs shipped", round(shipped["cv_J"], 4))


if __name__ == "__main__":
    main()
