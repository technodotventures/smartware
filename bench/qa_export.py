"""Export blind QA batches: the same test questions, each system's packed context at a fixed
budget, shuffled together with system labels hidden. Readers (LLM agents) answer from the
context only; a separate judge grades answers against gold without seeing system names.

python -m bench.qa_export --systems orp,supermemory-company-brain,cognee,graphiti,mem0,bm25 \
    --locomo 150 --lme 120 --budget 1000 --batch 60
"""
from __future__ import annotations

import argparse
import json
import os
import random
from collections import defaultdict

from .data import load_lme, load_locomo
from .encoders import DenseEncoder, Encoders
from .metrics import chars4, pack
from .run import make

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "results", "qa")


def sample(tasks, n, seed):
    by_cat = defaultdict(list)
    for t in tasks:
        for q in t.queries:
            if q.split == "test":
                by_cat[q.category].append((t, q))
    rng = random.Random(seed)
    tot = sum(len(v) for v in by_cat.values())
    out = []
    for c, v in sorted(by_cat.items()):
        rng.shuffle(v)
        out += v[: max(1, round(n * len(v) / tot))]
    return out[:n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--systems", required=True)
    ap.add_argument("--locomo", type=int, default=150)
    ap.add_argument("--lme", type=int, default=120)
    ap.add_argument("--budget", type=int, default=1000)
    ap.add_argument("--batch", type=int, default=60)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    enc = Encoders(DenseEncoder.load())
    pairs = [("locomo", t, q) for t, q in sample(load_locomo(), a.locomo, 1)] + \
            [("lme", t, q) for t, q in sample(load_lme(), a.lme, 2)]
    items, key = [], {}
    for name in a.systems.split(","):
        built = {}
        for bench, t, q in pairs:
            s = built.get(t.corpus.id)
            if s is None:
                s = make(name)
                s.build(t.corpus, enc)
                built[t.corpus.id] = s
            _, used, kept = pack(s.retrieve(q, k=50), a.budget, chars4, True)
            uid = f"u{len(key):05d}"
            key[uid] = {"system": name, "bench": bench, "qid": q.qid, "category": q.category, "gold": q.answer,
                        "question": q.text, "tokens": used}
            items.append({"uid": uid, "question": q.text, "asked_on": str(q.time.date()),
                          "context": "\n".join(i.text for i in kept)})
        print("exported", name, flush=True)
    random.Random(42).shuffle(items)
    batches = [items[i: i + a.batch] for i in range(0, len(items), a.batch)]
    for i, b in enumerate(batches):
        json.dump(b, open(os.path.join(OUT, f"batch_{i:03d}.json"), "w"), indent=0)
    json.dump(key, open(os.path.join(OUT, "_key.json"), "w"), indent=0)
    print(len(items), "items in", len(batches), "batches")


if __name__ == "__main__":
    main()
