"""Smoke test: build a system on one DEV corpus of each benchmark and check it
returns well-formed items. Prints NO scores (so it cannot be used for tuning).

python -m bench.smoke <system-name>
"""
from __future__ import annotations

import sys
import time

from .data import load_lme, load_locomo
from .encoders import DenseEncoder, Encoders
from .run import make
from .system import Item


def main():
    name = sys.argv[1]
    enc = Encoders(DenseEncoder.load())
    loc = [t for t in load_locomo() if t.queries[0].split == "dev"][0]
    lme = [t for t in load_lme(limit=60) if t.queries[0].split == "dev"][0]
    for label, task in (("locomo", loc), ("lme", lme)):
        s = make(name)
        t0 = time.perf_counter()
        s.build(task.corpus, enc)
        b = time.perf_counter() - t0
        ids = {t.id for t in task.corpus.turns}
        n = 0
        t0 = time.perf_counter()
        for q in task.queries[:15]:
            items = s.retrieve(q, k=50)
            assert isinstance(items, list) and items, f"no items for {q.qid}"
            for it in items:
                assert isinstance(it, Item) and it.text and it.turn_ids, "malformed item"
                assert set(it.turn_ids) <= ids, "unknown turn ids"
            n += 1
        dt = (time.perf_counter() - t0) / max(n, 1)
        print(f"{label}: ok  build={b:.2f}s  per-query={dt*1000:.1f}ms  items={len(items)}")


if __name__ == "__main__":
    main()
