"""Coordinate-descent tuning of the reference engine on DEV splits only.

Objective: mean over {LoCoMo-dev, LME-dev} of mean(recall@500, recall@1000, recall@2000)
plus the same for answer-token recall (so token-cheap snippeting cannot win on claims alone).
Test splits are never touched here.
"""
from __future__ import annotations

import json
import os
import sys
import time

from smartware.engine import SmartwareSystem, Params

from .devtune import evaluate
from .encoders import DenseEncoder, Encoders

GRID = {
    "w_bm25": [0.6, 1.0, 1.4],
    "w_bm25_seg": [0.0, 0.3, 0.6, 1.0],
    "w_bm25_win": [0.0, 0.3, 0.6, 1.0],
    "w_dense": [0.3, 0.6, 1.0],
    "w_dense_seg": [0.0, 0.4, 0.8],
    "w_dense_win": [0.0, 0.4, 0.8],
    "w_thread": [0.0, 0.25, 0.5, 1.0],
    "w_prf": [0.0, 0.3, 0.6, 1.0],
    "w_entity": [0.0, 0.2, 0.4, 0.8],
    "w_time": [0.0, 0.25, 0.5, 1.0],
    "rrf_k": [20.0, 60.0, 100.0],
    "f_cover": [0.0, 0.3, 0.6, 1.0],
    "f_actor": [0.0, 0.25, 0.5],
    "f_role": [0.0, 0.25, 0.5],
    "f_order": [0.0, 0.15, 0.3],
    "long_unit_tokens": [150, 220, 350],
    "snippet_sentences": [3, 4, 6],
    "prompt_merge_words": [8, 14, 20],
    "expand_next": [True, False],
    "count_div": [True, False],
}


def objective(r: dict) -> float:
    tot = 0.0
    for b in r:
        rec = sum(r[b][f"recall@{x}"] for x in (500, 1000, 2000)) / 3
        ans = sum(r[b][f"ans@{x}"] for x in (500, 1000, 2000)) / 3
        tot += 0.5 * rec + 0.5 * ans
    return tot / len(r)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "results", "tuned_params.json")
    passes = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    enc = Encoders(DenseEncoder.load())
    best = {}
    if os.path.exists(out):
        best = json.load(open(out))["params"]

    def run(cfg):
        return evaluate(lambda: SmartwareSystem(**cfg), enc=enc)

    r = run(best)
    bj = objective(r)
    print(f"start J={bj:.4f}", json.dumps(best), flush=True)
    for ps in range(passes):
        improved = False
        for key, vals in GRID.items():
            cur = best.get(key, getattr(Params(), key))
            for v in vals:
                if v == cur:
                    continue
                cfg = dict(best, **{key: v})
                t0 = time.time()
                rr = run(cfg)
                j = objective(rr)
                print(f"pass{ps} {key}={v} J={j:.4f} (best {bj:.4f}) {time.time() - t0:.0f}s", flush=True)
                if j > bj + 1e-4:
                    bj, best, r = j, cfg, rr
                    improved = True
                    json.dump({"params": best, "J": bj, "dev": r}, open(out, "w"), indent=1)
                    print(f"  -> new best {key}={v}", flush=True)
        if not improved:
            break
    json.dump({"params": best, "J": bj, "dev": r}, open(out, "w"), indent=1)
    print("final", bj, json.dumps(best), flush=True)


if __name__ == "__main__":
    main()
