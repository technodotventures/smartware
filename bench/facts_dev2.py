"""Dev-only selection for the re-tuned fact track (see METHODOLOGY, "Re-tuned fact track").

Adds `<name>@ftuned+facts` and `orp-ft+facts` dev runs to the earlier ones (results/facts_dev.json,
results/facts_dev_lme.json) and picks, per benchmark, each system's configuration by the dev objective; for
HotpotQA (no dev split) the configuration with the highest mean dev objective over both benchmarks.
Writes results/facts_dev2.json. Test splits are not read.
"""
from __future__ import annotations

import json
import os
import sys
from multiprocessing import Pool

from . import facts_dev
from .run import REGISTRY

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = [n for n in REGISTRY if not n.startswith("orp") and n not in ("bm25", "dense")]


def job(args):
    bench, name = args
    facts_dev.BENCH = bench
    n, j, r = facts_dev.job(name)
    return bench, n, j, r


def main():
    prev = {"locomo": json.load(open(os.path.join(ROOT, "results", "facts_dev.json")))["runs"],
            "lme": json.load(open(os.path.join(ROOT, "results", "facts_dev_lme.json")))["runs"]}
    new = ["orp-ft+facts"] + [f"{b}@ftuned+facts" for b in BASE]
    runs = {b: {k: v["J"] for k, v in prev[b].items()} for b in prev}
    with Pool(int(sys.argv[1]) if len(sys.argv) > 1 else 2) as p:
        for b, n, j, r in p.imap_unordered(job, [(b, n) for b in ("locomo", "lme") for n in new]):
            runs[b][n] = j
            print(f"{b:6s} {n:36s} J={j:.4f} R@1000={r['recall@1000']:.3f} ans@1000={r['ans@1000']:.3f}", flush=True)
    opts = {"orp": ["orp", "orp+facts", "orp+evidence", "orp-ft+facts"]}
    for b in BASE:
        opts[b] = [f"{b}@tuned", f"{b}@tuned+facts", f"{b}@ftuned+facts"]
    choice = {}
    for bench in ("locomo", "lme"):
        choice[bench] = {s: max(o, key=lambda x: runs[bench].get(x, -1)) for s, o in opts.items()}
    choice["hotpot"] = {s: max(o, key=lambda x: (runs["locomo"].get(x, -1) + runs["lme"].get(x, -1)) / 2)
                        for s, o in opts.items()}
    json.dump({"dev_J": runs, "choice": choice}, open(os.path.join(ROOT, "results", "facts_dev2.json"), "w"), indent=1)
    print(json.dumps(choice, indent=1))


if __name__ == "__main__":
    main()
