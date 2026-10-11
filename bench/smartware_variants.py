"""Benchmark entries for the Smartware reference engine, loading parameters fixed on dev splits
by bench/train_smartware.py."""
from __future__ import annotations

import json
import os

from smartware.engine import SmartwareSystem

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    return json.load(open(os.path.join(ROOT, "results", name)))["params"]


class SmartwareRrf(SmartwareSystem):
    """Hand-tuned weighted RRF (no learned model)."""
    name = "smartware-rrf"

    def __init__(self, **kw):
        super().__init__(**{**_load("tuned_params.json"), **kw})


class SmartwareLtr(SmartwareSystem):
    """Learned ranker fitted on dev splits only (model family and packing chosen by grouped CV)."""
    name = "smartware"

    def __init__(self, **kw):
        super().__init__(**{**_load("smartware_params.json"), **kw})


class SmartwareAllFeatures(SmartwareSystem):
    """Learned ranker WITH position- and length-correlated features (lost to the shipped model in dev CV)."""
    name = "smartware-allfeat"

    def __init__(self, **kw):
        super().__init__(**{**_load("smartware_allfeat_params.json"), **kw})


class SmartwareFactsRanker(SmartwareSystem):
    """Ranker refitted on dev FACT stores (bench/train_smartware_facts.py); used as "smartware-ft+facts"."""
    name = "smartware-ft"

    def __init__(self, **kw):
        super().__init__(**{**_load("smartware_facts_params.json"), **kw})
