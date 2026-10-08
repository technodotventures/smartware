"""Dev-only selection for the LLM-extraction track: for every system, choose between storing raw turns and storing
the extracted facts (ORP: raw turns, facts, or turns as evidence plus facts as derived memories) by the LoCoMo dev
objective. Test splits are not read. Writes results/facts_dev.json."""
from __future__ import annotations

import json
import os
import sys
from multiprocessing import Pool

from .devtune import evaluate
from .run import REGISTRY, make

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = [n for n in REGISTRY if not n.startswith("orp") and n not in ("bm25", "dense")]


def J(r):
    l = r["locomo"]
    return 0.5 * (l["recall@500"] + l["recall@1000"] + l["recall@2000"]) / 3 + \
        0.5 * (l["ans@500"] + l["ans@1000"] + l["ans@2000"]) / 3


def job(name):
    r = evaluate(lambda: make(name), benches=("locomo",))
    return name, J(r), r["locomo"]


def main():
    names = ["orp", "orp+facts", "orp+evidence", "bm25", "bm25+facts", "dense", "dense+facts"]
    for b in BASE:
        names += [f"{b}@tuned", f"{b}@tuned+facts"]
    out = {}
    with Pool(int(sys.argv[1]) if len(sys.argv) > 1 else 2) as p:
        for n, j, r in p.imap_unordered(job, names):
            out[n] = {"J": j, "dev": r}
            print(f"{n:36s} J={j:.4f} R@1000={r['recall@1000']:.3f} ans@1000={r['ans@1000']:.3f}", flush=True)
    choice = {}
    for b in ["orp", "bm25", "dense"] + [f"{b}@tuned" for b in BASE]:
        opts = [b, b + "+facts"] + (["orp+evidence"] if b == "orp" else [])
        choice[b] = max(opts, key=lambda o: out[o]["J"])
    json.dump({"runs": out, "choice": choice}, open(os.path.join(ROOT, "results", "facts_dev.json"), "w"), indent=1)
    print(json.dumps(choice, indent=1))


if __name__ == "__main__":
    main()
