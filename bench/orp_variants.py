"""Benchmark entries for the ORP reference engine, loading parameters fixed on dev splits."""
from __future__ import annotations

import json
import os

from orp.engine import ORPSystem

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    return json.load(open(os.path.join(ROOT, "results", name)))["params"]


class ORPRrf(ORPSystem):
    """Hand-tuned weighted RRF (no learned model)."""
    name = "orp-rrf"

    def __init__(self, **kw):
        super().__init__(**{**_load("tuned_params.json"), **kw})


class ORPLtr(ORPSystem):
    """Learned ranker (gradient-boosted trees over declared features), fitted on dev splits only."""
    name = "orp"

    def __init__(self, **kw):
        p = _load("ltr_params_v1.json")
        # packing chosen by held-out CV on dev (results/pack_cv.json): with the learned ranker, dialogue-span
        # expansion costs more tokens than it recovers
        p.update(w_sub=0.0, w_qexp=0.0, expand_next=False, expand_prev_q=False)
        super().__init__(**{**p, **kw})
