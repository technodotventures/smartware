"""Blind QA v3 on official LongMemEval-S (results/qa3/PREREGISTRATION.md).

  python -m bench.qa3 sample                 # fix the 150 questions (seed 3) -> results/qa3/questions.json
  python -m bench.qa3 export SYSTEM          # one system's contexts -> results/qa3/ctx/SYSTEM.json ("oracle" = gold turns)
  python -m bench.qa3 batches                # shuffle all, hide systems -> batches/bNNN.json + _key.json
  python -m bench.qa3 judge                  # answers -> judge/jNN.json (+ judge2/ seeded 20% second pass)
  python -m bench.qa3 score                  # -> results/qa3_blind.json
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
QA = os.path.join(ROOT, "results", "qa3")
N, SEED, BUDGET, BATCH = 150, 3, 1000, 15


def _tasks():
    from .data import load_lme_s
    return {t.queries[0].qid: t for t in load_lme_s(split="test", day=True)}


def sample():
    tasks = _tasks()
    by = defaultdict(list)
    for qid, t in sorted(tasks.items()):
        by[t.queries[0].category].append(qid)
    rng = random.Random(SEED)
    tot = sum(len(v) for v in by.values())
    out = []
    for c, v in sorted(by.items()):
        rng.shuffle(v)
        out += v[: max(1, round(N * len(v) / tot))]
    out = sorted(out[:N])
    json.dump(out, open(os.path.join(QA, "questions.json"), "w"), indent=0)
    print(len(out), {c: sum(1 for q in out if q in v) for c, v in by.items()})


def export(system):
    from .encoders import DenseEncoder, Encoders
    from .metrics import chars4, pack
    from .run import make
    from .system import Item
    qids = set(json.load(open(os.path.join(QA, "questions.json"))))
    enc = Encoders(DenseEncoder.load())
    out = {}
    for qid, t in _tasks().items():
        if qid not in qids:
            continue
        q = t.queries[0]
        if system == "oracle":
            by = {x.id: x for x in t.corpus.turns}
            items, seen = [], set()
            for g in sorted(q.gold_turns, key=lambda g: (by[g].ts, g)):
                x = by[g]
                head = "" if x.session_id in seen else f"## {t.corpus.session_ts[x.session_id].strftime('%Y-%m-%d (%a)')}\n"
                seen.add(x.session_id)
                items.append(Item([g], f"{head}{x.speaker}: {x.text}", 1.0))
        else:
            s = make(system)
            s.build(t.corpus, enc)
            items = s.retrieve(q, k=50)
        _, used, kept = pack(items, BUDGET, chars4, True)
        out[qid] = {"context": "\n".join(i.text for i in kept), "tokens": used}
        enc.dense._cache.clear()
    os.makedirs(os.path.join(QA, "ctx"), exist_ok=True)
    json.dump(out, open(os.path.join(QA, "ctx", f"{system}.json"), "w"))
    print("exported", system, len(out))


def batches():
    tasks = _tasks()
    qids = json.load(open(os.path.join(QA, "questions.json")))
    items, key = [], {}
    for f in sorted(glob.glob(os.path.join(QA, "ctx", "*.json"))):
        system = os.path.basename(f)[:-5]
        ctx = json.load(open(f))
        assert set(ctx) == set(qids), system
        for qid in qids:
            items.append((system, qid, ctx[qid]))
    random.Random(SEED).shuffle(items)
    out = []
    for i, (system, qid, c) in enumerate(items):
        q = tasks[qid].queries[0]
        uid = f"v{i:05d}"
        key[uid] = {"system": system, "qid": qid, "category": q.category, "question": q.text, "gold": q.answer,
                    "tokens": c["tokens"]}
        out.append({"uid": uid, "question": q.text, "asked_on": q.time.strftime("%Y-%m-%d (%a)"),
                    "context": c["context"]})
    pending, bs = out, []
    while pending:
        b, seen, rest = [], set(), []
        for it in pending:
            qid = key[it["uid"]]["qid"]
            if len(b) < BATCH and qid not in seen:
                b.append(it)
                seen.add(qid)
            else:
                rest.append(it)
        bs.append(b)
        pending = rest
    os.makedirs(os.path.join(QA, "batches"), exist_ok=True)
    for i, b in enumerate(bs):
        json.dump(b, open(os.path.join(QA, "batches", f"b{i:03d}.json"), "w"), indent=0)
    json.dump(key, open(os.path.join(QA, "_key.json"), "w"), indent=0)
    print(len(out), "items,", len(bs), "batches")


def _answers():
    a = {}
    for f in glob.glob(os.path.join(QA, "answers", "a*.json")):
        a.update(json.load(open(f)))
    return a


def judge(size: int = 150):
    key = json.load(open(os.path.join(QA, "_key.json")))
    ans = _answers()
    missing = sorted(set(key) - set(ans))
    if missing:
        sys.exit(f"{len(missing)} answers missing, e.g. {missing[:5]}")
    items = [{"uid": u, "question": k["question"], "question_type": k["category"], "gold": k["gold"],
              "answer": ans[u]} for u, k in key.items()]
    random.Random(7).shuffle(items)
    for d, its in (("judge", items), ("judge2", random.Random(8).sample(items, len(items) // 5))):
        os.makedirs(os.path.join(QA, d), exist_ok=True)
        for i in range(0, len(its), size):
            json.dump(its[i:i + size], open(os.path.join(QA, d, f"j{i // size:02d}.json"), "w"), indent=0)
        print(d, (len(its) + size - 1) // size, "batches")


def _verdicts(d):
    v = {}
    for f in glob.glob(os.path.join(QA, d, "v*.json")):
        v.update(json.load(open(f)))
    return {u: 1.0 if str(x).upper().startswith("CORRECT") else 0.0 for u, x in v.items()}


def score():
    key = json.load(open(os.path.join(QA, "_key.json")))
    v, v2 = _verdicts("judge"), _verdicts("judge2")
    if set(key) - set(v):
        sys.exit("missing verdicts")
    systems = sorted({k["system"] for k in key.values()})
    per_q = defaultdict(dict)
    cat = defaultdict(list)
    for u, k in key.items():
        per_q[k["qid"]][k["system"]] = v[u]
        cat[(k["category"], k["system"])].append(v[u])
    qs = sorted(per_q)
    acc = {s: float(np.mean([per_q[q][s] for q in qs])) for s in systems}
    rng = np.random.default_rng(0)
    idx = rng.integers(0, len(qs), (10000, len(qs)))
    comp = {}
    for s in systems:
        if s in ("orp", "oracle"):
            continue
        d = np.array([per_q[q]["orp"] - per_q[q][s] for q in qs])
        bs = d[idx].mean(1)
        p = float(min(1.0, 2 * min((bs <= 0).mean(), (bs >= 0).mean())))
        comp[s] = {"diff": float(d.mean()), "ci95": [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))],
                   "p": p}
    order = sorted(comp, key=lambda s: comp[s]["p"])  # Holm
    m = len(order)
    run = 0.0
    for i, s in enumerate(order):
        run = max(run, min(1.0, (m - i) * comp[s]["p"]))
        comp[s]["p_holm"] = run
    both = [u for u in v2 if u in v]
    a = np.array([v[u] for u in both])
    b = np.array([v2[u] for u in both])
    po = float((a == b).mean()) if len(both) else None
    pe = float(a.mean() * b.mean() + (1 - a.mean()) * (1 - b.mean())) if len(both) else None
    kappa = (po - pe) / (1 - pe) if len(both) and pe < 1 else None
    out = {"n_questions": len(qs), "accuracy": acc, "orp_minus": comp,
           "by_category": {c: {s: float(np.mean(x)) for (cc, s), x in cat.items() if cc == c}
                           for c in sorted({c for c, _ in cat})},
           "n_by_category": {c: len([q for q in qs if key[next(u for u in key if key[u]["qid"] == q)]["category"] == c])
                             for c in sorted({c for c, _ in cat})},
           "judge_agreement": {"n": len(both), "agreement": po, "kappa": kappa},
           "tokens": {s: float(np.mean([k["tokens"] for k in key.values() if k["system"] == s])) for s in systems}}
    json.dump(out, open(os.path.join(ROOT, "results", "qa3_blind.json"), "w"), indent=1)
    for s in sorted(acc, key=lambda s: -acc[s]):
        c = comp.get(s)
        print(f"{s:34s} {acc[s] * 100:5.1f}" + (f"  ORP-minus {c['diff'] * 100:+.1f} [{c['ci95'][0] * 100:+.1f}, "
                                                 f"{c['ci95'][1] * 100:+.1f}] p_holm={c['p_holm']:.3f}" if c else ""))
    print("judge agreement", out["judge_agreement"])


if __name__ == "__main__":
    f = {"sample": sample, "export": export, "batches": batches, "judge": judge, "score": score}[sys.argv[1]]
    f(*sys.argv[2:])
