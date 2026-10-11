"""Dev-only selection for the LLM-extraction track: for every system, choose between storing raw turns and storing
the extracted facts (Smartware: raw turns, facts, or turns as evidence plus facts as derived memories) by the LoCoMo dev
objective. Test splits are not read. Writes results/facts_dev.json."""
from __future__ import annotations

import json
import os
import sys
from multiprocessing import Pool

from .devtune import evaluate
from .run import REGISTRY, make

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = [n for n in REGISTRY if not n.startswith("smartware") and n not in ("bm25", "dense")]


BENCH = "locomo"


def J(r):
    l = r[BENCH] if BENCH in r else r
    return 0.5 * (l["recall@500"] + l["recall@1000"] + l["recall@2000"]) / 3 + \
        0.5 * (l["ans@500"] + l["ans@1000"] + l["ans@2000"]) / 3


def job(name):
    r = evaluate(lambda: make(name), benches=(BENCH,))
    return name, J(r), r[BENCH]


def main():
    global BENCH
    BENCH = sys.argv[2] if len(sys.argv) > 2 else "locomo"
    names = ["smartware", "smartware+facts", "smartware+evidence", "bm25", "bm25+facts", "dense", "dense+facts"]
    out = {}
    tun = json.load(open(os.path.join(ROOT, "results", "baseline_tuning.json")))
    for b in BASE:
        names.append(f"{b}@tuned+facts")
        best = max(tun[b]["all"], key=lambda x: x["J"] if x["J"] is not None else -1)
        if best["cfg"] == tun[b]["best"] and BENCH in best["dev"]:  # same tuned config, same dev split: reuse
            out[f"{b}@tuned"] = {"J": J(best["dev"]), "dev": best["dev"][BENCH], "reused": "baseline_tuning.json"}
        else:
            names.append(f"{b}@tuned")
    with Pool(int(sys.argv[1]) if len(sys.argv) > 1 else 2) as p:  # usage: facts_dev [workers] [bench]
        for n, j, r in p.imap_unordered(job, names):
            out[n] = {"J": j, "dev": r}
            print(f"{n:36s} J={j:.4f} R@1000={r['recall@1000']:.3f} ans@1000={r['ans@1000']:.3f}", flush=True)
    choice = {}
    for b in ["smartware", "bm25", "dense"] + [f"{b}@tuned" for b in BASE]:
        opts = [b, b + "+facts"] + (["smartware+evidence"] if b == "smartware" else [])
        choice[b] = max(opts, key=lambda o: out[o]["J"])
    fn = "facts_dev.json" if BENCH == "locomo" else f"facts_dev_{BENCH}.json"
    json.dump({"runs": out, "choice": choice}, open(os.path.join(ROOT, "results", fn), "w"), indent=1)
    print(json.dumps(choice, indent=1))


if __name__ == "__main__":
    main()
