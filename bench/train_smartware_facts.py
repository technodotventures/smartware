"""Refit the Smartware ranker for fact stores, on DEV splits only (the counterpart of `tune_baselines --facts`).

The shipped ranker was fitted on raw turns. Here the same engine stores the shared LLM-extracted facts
(bench/facts.py) and the ranker is refitted on dev fact stores: candidates are labelled positive when the fact cites a
gold turn. Grouped 5-fold CV (by corpus) over {shipped ranker unchanged, hand-tuned RRF, logistic regression C=0.01,
C=0.1}; the best by the shared dev objective is fitted on all dev data and written to results/smartware_facts_params.json.
Test splits are never read.

    python -m bench.train_smartware_facts
"""
from __future__ import annotations

import json
import os
from dataclasses import replace

import numpy as np

from smartware.engine import SmartwareSystem

from .devtune import tasks
from .encoders import DenseEncoder, Encoders
from .facts import fact_corpus
from .ltr import fit
from .metrics import score_query
from .system import Item
from .tune import objective

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def R(name):
    return os.path.join(ROOT, "results", name)


def collect(base, enc):
    runs = []
    for b in ("locomo", "lme"):
        for task in tasks(b):
            fc, cites = fact_corpus(task.corpus)
            s = SmartwareSystem(**base)
            s.build(fc, enc)
            s.eng.collect = True
            for q in task.queries:
                if q.split != "dev":
                    continue
                s.retrieve(q, k=50)
                cand, X = s.eng.last
                fids = [s.eng.mem[s.eng.ids[k]].meta.get("turn_id") for k in cand]
                y = np.array([bool(set(cites.get(f, [])) & q.gold_turns) for f in fids], dtype=int)
                runs.append((b, task.corpus.id, s, task, q, X, y, cites))
            s.eng.collect = False
    return runs


def score(runs, plist):
    """plist: [(fold set or None, params)]; each run is scored with the params of the fold that holds it out."""
    out = {}
    for b in ("locomo", "lme"):
        rows = []
        for (bb, cid, s, task, q, X, y, cites) in runs:
            if bb != b:
                continue
            p = next(pp for f, pp in plist if f is None or cid in f)
            old, s.eng.p = s.eng.p, p
            items = s.retrieve(q, k=50)
            s.eng.p = old
            items = [Item(list(dict.fromkeys(t for f in it.turn_ids for t in cites.get(f, [f]))), it.text, it.score)
                     for it in items]
            t2s = {t.id: t.session_id for t in task.corpus.turns}
            rows.append(score_query(items, q.gold_turns, q.gold_sessions, t2s, q.answer))
        out[b] = {k: float(np.mean([r[k] for r in rows if k in r])) for k in rows[0]}
    return out


def main():
    shipped = json.load(open(R("smartware_params.json")))["params"]
    rrf = json.load(open(R("tuned_params.json")))["params"]
    enc = Encoders(DenseEncoder.load())
    runs = collect(shipped, enc)
    print(f"collected {len(runs)} dev queries, {sum(len(r[6]) for r in runs)} candidates, "
          f"{sum(int(r[6].sum()) for r in runs)} positives", flush=True)
    p0 = replace(runs[0][2].eng.p)
    groups = sorted({r[1] for r in runs})
    rng = np.random.default_rng(0)
    rng.shuffle(groups)
    folds = [set(groups[i::5]) for i in range(5)]
    from smartware.engine import Params
    cands = {"shipped": [(None, p0)],
             "rrf": [(None, Params(**{k: tuple(v) if isinstance(v, list) else v for k, v in rrf.items()}))]}
    for C in (0.01, 0.1):
        pl = []
        for f in folds:
            w, b, mu, sd = fit([r for r in runs if r[1] not in f], C)
            pl.append((f, replace(p0, ranker="linear", lin_w=tuple(map(float, w)), lin_b=b,
                                  lin_mu=tuple(map(float, mu)), lin_sd=tuple(map(float, sd)))))
        cands[f"linear C={C}"] = pl
    log = {}
    for name, pl in cands.items():
        j = objective(score(runs, pl))
        log[name] = j
        print(f"{name:14s} CV J={j:.4f}", flush=True)
    best = max(log, key=log.get)
    if best.startswith("linear"):
        C = float(best.split("=")[1])
        w, b, mu, sd = fit(runs, C)
        final = dict(shipped, ranker="linear", lin_w=list(map(float, w)), lin_b=b, lin_mu=list(map(float, mu)),
                     lin_sd=list(map(float, sd)))
    elif best == "rrf":
        final = rrf
    else:
        final = shipped
    json.dump({"params": final, "selected": best, "cv_J": log}, open(R("smartware_facts_params.json"), "w"))
    print("selected", best)


if __name__ == "__main__":
    main()
