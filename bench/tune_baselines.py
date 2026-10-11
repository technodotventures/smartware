"""Give every baseline the same dev-only tuning budget the Smartware engine had.

For each baseline, every combination in its class attribute TUNABLE (documented, user- or
operator-exposed knobs chosen by the engineer who studied that project) is screened on a fixed
dev subset, and the best 4 are evaluated on the full DEV splits with the same objective used for
Smartware (bench.tune.objective); the best full-dev configuration is recorded. (Graphiti and Hindsight
were evaluated on the full grid before screening was introduced to save time.)
Test splits are never read.

python -m bench.tune_baselines [names] [--workers 2]
"""
from __future__ import annotations

import argparse
import importlib
import itertools
import json
import os
import time
from multiprocessing import Pool

from .devtune import evaluate
from .encoders import DenseEncoder, Encoders
from .run import REGISTRY
from .tune import objective

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "results", "baseline_tuning.json")
_ENC = None


def cls_of(name):
    mod, c = REGISTRY[name].split(":")
    return getattr(importlib.import_module(mod), c)


SUBSET = {"locomo": 1, "lme": 20}  # stage-1 screen: 1 LoCoMo dev conversation + 20 LME-R dev questions
TOP = 4  # configurations promoted to the full dev evaluation


FACTS = False  # --facts: tune each baseline's knobs on the shared LLM-extracted fact stores (bench/facts.py)


def job(args):
    global _ENC
    name, cfg, subset = args
    if _ENC is None:
        _ENC = Encoders(DenseEncoder.load())
    C = cls_of(name)
    t0 = time.time()
    try:
        if FACTS:
            from .facts import FactStore
            r = evaluate(lambda: FactStore(C(**cfg)), enc=_ENC, subset=subset)
        else:
            r = evaluate(lambda: C(**cfg), enc=_ENC, subset=subset)
        return name, cfg, objective(r), r, time.time() - t0, subset is not None
    except Exception as e:  # a crashing combination is recorded, not fatal
        return name, cfg, None, {"error": repr(e)}, time.time() - t0, subset is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="?", default="all")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--facts", action="store_true")
    a = ap.parse_args()
    global FACTS, OUT
    if a.facts:
        FACTS, OUT = True, os.path.join(ROOT, "results", "baseline_tuning_facts.json")
    base = ["graphiti", "hindsight", "mem0", "letta", "supermemory-company-brain", "gbrain", "supermemory", "onyx",
            "cognee", "basic-memory", "sylph", "mcp-memory", "memos", "langmem", "memoryos"]
    names = base if a.names == "all" else a.names.split(",")
    res = json.load(open(OUT)) if os.path.exists(OUT) else {}
    pool = Pool(a.workers)

    def record(name, cfg, j, r, dt, screened):
        e = res.setdefault(name, {"all": []})
        (e.setdefault("screen", []) if screened else e["all"]).append({"cfg": cfg, "J": j, "dev": r,
                                                                       "secs": round(dt, 1)})
        ok = [x for x in e["all"] if x["J"] is not None]
        if ok:
            best = max(ok, key=lambda x: x["J"])
            e["best"], e["best_J"] = best["cfg"], best["J"]
        print(f"{name:26s} {'screen' if screened else 'full  '} J={j if j is None else round(j, 4)} "
              f"{json.dumps(cfg)} {dt:.0f}s", flush=True)
        with open(OUT + ".tmp", "w") as fh:  # atomic replace: readers never see a partial file
            json.dump(res, fh, indent=1, default=str)
        os.replace(OUT + ".tmp", OUT)

    for n in names:
        if res.get(n, {}).get("best") is not None and len(res[n]["all"]) >= TOP:
            continue  # already tuned (complete grid on full dev, or screened and promoted)
        tun = getattr(cls_of(n), "TUNABLE", {})
        keys = sorted(tun)
        cfgs = [dict(zip(keys, combo)) for combo in itertools.product(*[tun[k] for k in keys])]
        scr = []  # stage 1: screen every configuration on a fixed dev subset
        for name, cfg, j, r, dt, s_ in pool.imap_unordered(job, [(n, c, SUBSET) for c in cfgs]):
            record(name, cfg, j, r, dt, True)
            scr.append((j if j is not None else -1, cfg))
        top = [c for _, c in sorted(scr, key=lambda x: -x[0])[:TOP]]
        # stage 2: full dev for the promoted configurations
        for name, cfg, j, r, dt, s_ in pool.imap_unordered(job, [(n, c, None) for c in top]):
            record(name, cfg, j, r, dt, False)
    pool.close()


if __name__ == "__main__":
    main()
