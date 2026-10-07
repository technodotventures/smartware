"""Tune packing-only parameters (they do not change ranking) for the learned-ranker engine, on dev."""
from __future__ import annotations

import json
import os

from .devtune import evaluate
from .encoders import DenseEncoder, Encoders
from .orp_variants import ORPLtr
from .tune import objective

GRID = {"long_unit_tokens": [150, 350, 500], "snippet_sentences": [3, 6], "prompt_merge_words": [8, 20],
        "expand_next": [False], "expand_prev_q": [False], "headers": [False], "resolve_dates": [False]}


def main():
    enc = Encoders(DenseEncoder.load())
    best = {}
    bj = objective(evaluate(lambda: ORPLtr(), enc=enc))
    print("start", round(bj, 4), flush=True)
    for k, vals in GRID.items():
        for v in vals:
            cfg = dict(best, **{k: v})
            j = objective(evaluate(lambda: ORPLtr(**cfg), enc=enc))
            print(k, v, round(j, 4), "best", round(bj, 4), flush=True)
            if j > bj + 1e-4:
                bj, best = j, cfg
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results", "pack_params.json")
    json.dump({"params": best, "J": bj}, open(out, "w"), indent=1)
    print("final", best, bj)


if __name__ == "__main__":
    main()
