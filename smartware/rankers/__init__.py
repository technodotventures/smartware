"""Shipped rankers, as data (spec §5.10, M53): named, versioned parameter sets for Engine Params.

smartware-linear-1 is the configuration that was benchmarked (learned linear ranker over declared features, fit with the
sif-ppmi-256 encoder). smartware-rrf-1 is weighted reciprocal-rank fusion with multiplicative features, which needs no
fitted weights and is the safe choice for any other encoder.
"""
from __future__ import annotations

import json
import os
from dataclasses import fields

from ..engine import Params

HERE = os.path.dirname(os.path.abspath(__file__))
NAMES = ("smartware-linear-1", "smartware-rrf-1")


def spec(name: str) -> dict:
    if name not in NAMES:
        raise ValueError(f"unknown ranker {name!r}; shipped: {', '.join(NAMES)}")
    with open(os.path.join(HERE, name + ".json")) as f:
        return json.load(f)


def params(name: str) -> Params:
    p = Params()
    ok = {f.name for f in fields(Params)}
    for k, v in spec(name)["params"].items():
        if k not in ok:
            raise ValueError(f"{name}: unknown parameter {k}")
        setattr(p, k, tuple(v) if isinstance(v, list) else v)
    return p


def choose(name: str | None, embedder) -> tuple[str, Params, list[str]]:
    """auto: the learned ranker only with the encoder it was fit with, otherwise RRF (and say why)."""
    notes = []
    if name in (None, "", "auto"):
        enc = getattr(embedder, "name", "")
        name = "smartware-linear-1" if enc == spec("smartware-linear-1")["fit_encoder"] else "smartware-rrf-1"
        if name == "smartware-rrf-1":
            notes.append(f"ranker auto: encoder {enc or 'unknown'} is not the one smartware-linear-1 was fit with, so "
                         "smartware-rrf-1 is used")
    return name, params(name), notes
