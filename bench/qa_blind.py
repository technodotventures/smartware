"""Blind QA pipeline helpers (the reading and judging is done by LLM agents following the briefs in bench/qa_briefs/).

  python -m bench.qa_blind batches   # results/qa/items.json -> results/qa/batches/bNNN.json (15 items, no question twice per batch)
  python -m bench.qa_blind judge     # answers -> results/qa/judge/jNN.json (question, gold, answer; no system info)
  python -m bench.qa_blind score     # verdicts -> results/qa_blind.json (accuracy, paired bootstrap CIs, per category)

Items come from bench.qa_export (same test questions for every system, context packed at a fixed budget with the
primary packer). Readers see only {uid, question, asked_on, context}; judges see only {uid, question, gold, answer}.
The system key lives in results/qa/_key.json and is read only by `score`.
"""
from __future__ import annotations

import glob
import json
import os
import random
import sys
from collections import defaultdict

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QA = os.path.join(ROOT, "results", "qa")


def batches(size: int = 15):
    items = []
    for f in sorted(glob.glob(os.path.join(QA, "batch_*.json"))):
        items += json.load(open(f))
    key = json.load(open(os.path.join(QA, "_key.json")))
    random.Random(11).shuffle(items)
    out, pending = [], list(items)
    while pending:
        b, qs, rest = [], set(), []
        for it in pending:
            qid = key[it["uid"]]["qid"]
            if len(b) < size and qid not in qs:
                b.append(it)
                qs.add(qid)
            else:
                rest.append(it)
        out.append(b)
        pending = rest
    os.makedirs(os.path.join(QA, "batches"), exist_ok=True)
    for i, b in enumerate(out):
        json.dump(b, open(os.path.join(QA, "batches", f"b{i:03d}.json"), "w"), indent=0)
    print(len(items), "items,", len(out), "batches")


def judge(size: int = 150):
    key = json.load(open(os.path.join(QA, "_key.json")))
    ans = {}
    for f in glob.glob(os.path.join(QA, "answers", "a*.json")):
        ans.update(json.load(open(f)))
    missing = set(key) - set(ans)
    if missing:
        sys.exit(f"{len(missing)} answers missing")
    items = [{"uid": u, "question": k["question"], "gold": k["gold"], "answer": ans[u]} for u, k in key.items()]
    random.Random(7).shuffle(items)
    os.makedirs(os.path.join(QA, "judge"), exist_ok=True)
    for i in range(0, len(items), size):
        json.dump(items[i:i + size], open(os.path.join(QA, "judge", f"j{i // size:02d}.json"), "w"), indent=0)
    print((len(items) + size - 1) // size, "judge batches")


def score():
    key = json.load(open(os.path.join(QA, "_key.json")))
    v = {}
    for f in glob.glob(os.path.join(QA, "judge", "v*.json")):
        v.update(json.load(open(f)))
    if set(key) - set(v):
        sys.exit("missing verdicts")
    systems = sorted({k["system"] for k in key.values()})
    per_q = defaultdict(dict)
    acc, cat = defaultdict(list), defaultdict(list)
    for u, k in key.items():
        c = 1.0 if str(v[u]).upper().startswith("CORRECT") else 0.0
        acc[(k["bench"], k["system"])].append(c)
        cat[(k["bench"], k["category"], k["system"])].append(c)
        per_q[(k["bench"], k["qid"])][k["system"]] = c
    out = {"systems": systems, "accuracy": {}, "smartware_minus": {}, "by_category": {}}
    for b in ("locomo", "lme"):
        out["accuracy"][b] = {s: {"acc": float(np.mean(acc[(b, s)])), "n": len(acc[(b, s)])} for s in systems}
        qs = [q for (bb, q) in per_q if bb == b]
        for s in systems:
            if s == "smartware":
                continue
            d = np.array([per_q[(b, q)]["smartware"] - per_q[(b, q)][s] for q in qs])
            rng = np.random.default_rng(0)
            bs = [d[rng.integers(0, len(d), len(d))].mean() for _ in range(5000)]
            out["smartware_minus"].setdefault(b, {})[s] = {"diff": float(d.mean()), "ci95": [float(np.percentile(bs, 2.5)),
                                                                                      float(np.percentile(bs, 97.5))]}
    for (b, c, s), xs in cat.items():
        out["by_category"].setdefault(f"{b}:{c}", {})[s] = {"acc": float(np.mean(xs)), "n": len(xs)}
    json.dump(out, open(os.path.join(ROOT, "results", "qa_blind.json"), "w"), indent=1)
    for b in ("locomo", "lme"):
        print(b, {s: round(x["acc"] * 100, 1) for s, x in out["accuracy"][b].items()})


if __name__ == "__main__":
    {"batches": batches, "judge": judge, "score": score}[sys.argv[1]]()
