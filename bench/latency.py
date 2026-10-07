"""Single-process latency on LoCoMo test (2 conversations), run with nothing else on the machine.

python -m bench.latency orp,orp-rrf,mem0,...
"""
from __future__ import annotations

import json
import os
import sys
import time

from .data import load_locomo
from .encoders import DenseEncoder, Encoders
from .run import make

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    names = sys.argv[1].split(",")
    enc = Encoders(DenseEncoder.load())
    tasks = [t for t in load_locomo() if t.queries[0].split == "test"][:2]
    out = json.load(open(os.path.join(ROOT, "results", "latency.json"))) if os.path.exists(
        os.path.join(ROOT, "results", "latency.json")) else {}
    for n in names:
        q_ms, b_ms = [], []
        for t in tasks:
            s = make(n)
            t0 = time.perf_counter()
            s.build(t.corpus, enc)
            b_ms.append((time.perf_counter() - t0) * 1000)
            for q in t.queries:
                t1 = time.perf_counter()
                s.retrieve(q, k=50)
                q_ms.append((time.perf_counter() - t1) * 1000)
        q_ms.sort()
        out[n] = {"p50": q_ms[len(q_ms) // 2], "p95": q_ms[int(len(q_ms) * 0.95)], "build": sum(b_ms) / len(b_ms),
                  "n": len(q_ms)}
        print(n, out[n], flush=True)
        json.dump(out, open(os.path.join(ROOT, "results", "latency.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
