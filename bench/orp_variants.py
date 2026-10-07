"""Benchmark entries for the ORP reference engine, loading parameters fixed on dev splits
by bench/train_orp.py."""
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
    """Learned ranker fitted on dev splits only (model family and packing chosen by grouped CV)."""
    name = "orp"

    def __init__(self, **kw):
        super().__init__(**{**_load("orp_params.json"), **kw})


class ORPAllFeatures(ORPSystem):
    """Learned ranker WITH position- and length-correlated features (lost to the shipped model in dev CV)."""
    name = "orp-allfeat"

    def __init__(self, **kw):
        super().__init__(**{**_load("orp_allfeat_params.json"), **kw})
