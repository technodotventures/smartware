"""Give every baseline the same dev-only tuning budget the ORP engine had.

For each baseline, evaluate every combination in its class attribute TUNABLE (documented,
user- or operator-exposed knobs chosen by the engineer who studied that project) on the DEV
splits with the same objective used for ORP (bench.tune.objective), and record the best.
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


def job(args):
    global _ENC
    name, cfg = args
    if _ENC is None:
        _ENC = Encoders(DenseEncoder.load())
    C = cls_of(name)
    t0 = time.time()
    try:
        r = evaluate(lambda: C(**cfg), enc=_ENC)
        return name, cfg, objective(r), r, time.time() - t0
    except Exception as e:  # a crashing combination is recorded, not fatal
        return name, cfg, None, {"error": repr(e)}, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="?", default="all")
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args()
    base = ["graphiti", "hindsight", "mem0", "letta", "supermemory-company-brain", "gbrain", "supermemory", "onyx",
            "cognee", "basic-memory", "sylph", "mcp-memory", "memos", "langmem", "memoryos"]
    names = base if a.names == "all" else a.names.split(",")
    jobs = []
    for n in names:
        tun = getattr(cls_of(n), "TUNABLE", {})
        keys = sorted(tun)
        for combo in itertools.product(*[tun[k] for k in keys]):
            jobs.append((n, dict(zip(keys, combo))))
    res = json.load(open(OUT)) if os.path.exists(OUT) else {}
    done = {(n, json.dumps(r["cfg"], sort_keys=True)) for n, v in res.items() for r in v.get("all", [])}
    jobs = [j for j in jobs if (j[0], json.dumps(j[1], sort_keys=True)) not in done]
    print(f"{len(jobs)} configurations to evaluate", flush=True)
    with Pool(a.workers) as p:
        for name, cfg, j, r, dt in p.imap_unordered(job, jobs):
            e = res.setdefault(name, {"all": []})
            e["all"].append({"cfg": cfg, "J": j, "dev": r, "secs": round(dt, 1)})
            ok = [x for x in e["all"] if x["J"] is not None]
            if ok:
                best = max(ok, key=lambda x: x["J"])
                e["best"], e["best_J"] = best["cfg"], best["J"]
            print(f"{name:26s} J={j if j is None else round(j, 4)} {json.dumps(cfg)} {dt:.0f}s", flush=True)
            json.dump(res, open(OUT, "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
