"""Generic blind QA round (same blinding, briefs and statistics as bench/qa3.py), for dev rounds and confirmations.

  python -m bench.qa_round ROUND export NAME     # NAME from VARIANTS (or any registry name, or "oracle")
  python -m bench.qa_round ROUND batches | judge | score [claude|external] | external_inputs
ROUND is a folder under results/ holding questions.json (list of qids) and meta.json ({"split": "dev"|"test"}).
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
BUDGET, BATCH, SEED = 1000, 15, 3
CLEAN = {"render": "clean"}
VARIANTS = {  # name -> (registry system, kwargs, chronological display)
    "orp": ("orp", {}, False),
    "orp-clean": ("orp", CLEAN, False),
    "orp-clean-chrono": ("orp", CLEAN, True),
    "orp-clean-nodates": ("orp", {**CLEAN, "resolve_dates": False}, True),
    "orp-clean-full": ("orp", {**CLEAN, "long_unit_tokens": 10 ** 6}, True),
}


def _dir(r):
    return os.path.join(ROOT, "results", r)


def _tasks(r):
    from .data import load_lme_s
    split = json.load(open(os.path.join(_dir(r), "meta.json")))["split"]
    return {t.queries[0].qid: t for t in load_lme_s(split=split, day=True)}


def export(r, name):
    from .encoders import DenseEncoder, Encoders
    from .metrics import chars4, pack
    from .run import make
    from .system import Item
    qids = set(json.load(open(os.path.join(_dir(r), "questions.json"))))
    system, kw, chrono = VARIANTS.get(name, (name, {}, False))
    enc = Encoders(DenseEncoder.load())
    out = {}
    for qid, t in _tasks(r).items():
        if qid not in qids:
            continue
        q = t.queries[0]
        ts = {x.id: x.ts for x in t.corpus.turns}
        if system == "oracle":
            by = {x.id: x for x in t.corpus.turns}
            items, seen = [], set()
            for g in sorted(q.gold_turns, key=lambda g: (by[g].ts, g)):
                x = by[g]
                head = "" if x.session_id in seen else \
                    f"## {t.corpus.session_ts[x.session_id].strftime('%Y-%m-%d (%a)')}\n"
                seen.add(x.session_id)
                items.append(Item([g], f"{head}{x.speaker}: {x.text}", 1.0))
        else:
            s = make(system, **kw)
            s.build(t.corpus, enc)
            items = s.retrieve(q, k=50)
        _, used, kept = pack(items, BUDGET, chars4, True)
        if chrono:
            kept = sorted(kept, key=lambda i: min((ts[x] for x in i.turn_ids if x in ts), default=q.time))
        out[qid] = {"context": "\n".join(i.text for i in kept), "tokens": used}
        enc.dense._cache.clear()
    os.makedirs(os.path.join(_dir(r), "ctx"), exist_ok=True)
    json.dump(out, open(os.path.join(_dir(r), "ctx", f"{name}.json"), "w"))
    print("exported", name, len(out))


def batches(r):
    d = _dir(r)
    tasks = _tasks(r)
    qids = json.load(open(os.path.join(d, "questions.json")))
    items, key = [], {}
    for f in sorted(glob.glob(os.path.join(d, "ctx", "*.json"))):
        name = os.path.basename(f)[:-5]
        ctx = json.load(open(f))
        assert set(ctx) == set(qids), name
        items += [(name, qid, ctx[qid]) for qid in qids]
    random.Random(SEED).shuffle(items)
    out = []
    for i, (name, qid, c) in enumerate(items):
        q = tasks[qid].queries[0]
        uid = f"{r[:2]}{i:05d}"
        key[uid] = {"system": name, "qid": qid, "category": q.category, "question": q.text, "gold": q.answer,
                    "tokens": c["tokens"]}
        out.append({"uid": uid, "question": q.text, "asked_on": q.time.strftime("%Y-%m-%d (%a)"), "context": c["context"]})
    bs, pending = [], out
    while pending:
        b, seen, rest = [], set(), []
        for it in pending:
            qid = key[it["uid"]]["qid"]
            (b.append(it), seen.add(qid)) if len(b) < BATCH and qid not in seen else rest.append(it)
        bs.append(b)
        pending = rest
    os.makedirs(os.path.join(d, "batches"), exist_ok=True)
    for i, b in enumerate(bs):
        json.dump(b, open(os.path.join(d, "batches", f"b{i:03d}.json"), "w"), indent=0)
    json.dump(key, open(os.path.join(d, "_key.json"), "w"), indent=0)
    os.makedirs(os.path.join(d, "answers"), exist_ok=True)
    print(len(out), "items,", len(bs), "batches")


def external_inputs(r):
    d = _dir(r)
    items = []
    for f in sorted(glob.glob(os.path.join(d, "batches", "b[0-9][0-9][0-9].json"))):
        items += json.load(open(f))
    key = json.load(open(os.path.join(d, "_key.json")))
    os.makedirs(os.path.join(d, "external"), exist_ok=True)
    json.dump(items, open(os.path.join(d, "external", "items.json"), "w"))
    json.dump({u: {"question": k["question"], "question_type": k["category"], "gold": k["gold"]} for u, k in key.items()},
              open(os.path.join(d, "external", "judge_meta.json"), "w"))
    print(len(items), "external items")


def judge(r, size: int = 150):
    d = _dir(r)
    key = json.load(open(os.path.join(d, "_key.json")))
    ans = {}
    for f in glob.glob(os.path.join(d, "answers", "a*.json")):
        ans.update(json.load(open(f)))
    missing = sorted(set(key) - set(ans))
    if missing:
        sys.exit(f"{len(missing)} answers missing, e.g. {missing[:5]}")
    items = [{"uid": u, "question": k["question"], "question_type": k["category"], "gold": k["gold"], "answer": ans[u]}
             for u, k in key.items()]
    random.Random(7).shuffle(items)
    os.makedirs(os.path.join(d, "judge"), exist_ok=True)
    for i in range(0, len(items), size):
        json.dump(items[i:i + size], open(os.path.join(d, "judge", f"j{i // size:02d}.json"), "w"), indent=0)
    print((len(items) + size - 1) // size, "judge batches")


def score(r, source="claude", ref="orp"):
    d = _dir(r)
    key = json.load(open(os.path.join(d, "_key.json")))
    v = {}
    if source == "external":
        for line in open(os.path.join(d, "external", "verdicts.jsonl")):
            x = json.loads(line)
            v[x["uid"]] = 1.0 if x["verdict"] == "CORRECT" else 0.0
    else:
        for f in glob.glob(os.path.join(d, "judge", "v*.json")):
            v.update({u: 1.0 if str(x).upper().startswith("CORRECT") else 0.0 for u, x in json.load(open(f)).items()})
    if set(key) - set(v):
        sys.exit(f"{len(set(key) - set(v))} verdicts missing")
    per_q, cat = defaultdict(dict), defaultdict(list)
    for u, k in key.items():
        per_q[k["qid"]][k["system"]] = v[u]
        cat[(k["category"], k["system"])].append(v[u])
    qs = sorted(per_q)
    systems = sorted({k["system"] for k in key.values()})
    acc = {s: float(np.mean([per_q[q][s] for q in qs])) for s in systems}
    idx = np.random.default_rng(0).integers(0, len(qs), (10000, len(qs)))
    comp = {}
    for s in systems:
        if s in (ref, "oracle"):
            continue
        dd = np.array([per_q[q][ref] - per_q[q][s] for q in qs])
        bs = dd[idx].mean(1)
        comp[s] = {"diff": float(dd.mean()), "ci95": [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))],
                   "p": float(min(1.0, 2 * min((bs <= 0).mean(), (bs >= 0).mean())))}
    run = 0.0
    for i, s in enumerate(sorted(comp, key=lambda s: comp[s]["p"])):
        run = max(run, min(1.0, (len(comp) - i) * comp[s]["p"]))
        comp[s]["p_holm"] = run
    cats = sorted({c for c, _ in cat})
    out = {"source": source, "ref": ref, "n_questions": len(qs), "accuracy": acc, "ref_minus": comp,
           "by_category": {c: {s: float(np.mean(cat[(c, s)])) for s in systems} for c in cats},
           "n_by_category": {c: len(cat[(c, systems[0])]) for c in cats}}
    if source == "external" and os.path.exists(os.path.join(d, "external", "run_info.json")):
        out["run"] = json.load(open(os.path.join(d, "external", "run_info.json")))
    json.dump(out, open(os.path.join(d, f"score_{source}_{ref}.json"), "w"), indent=1)
    for s in sorted(acc, key=lambda s: -acc[s]):
        c = comp.get(s)
        print(f"{s:34s} {acc[s] * 100:5.1f}" + (f"  {ref}-minus {c['diff'] * 100:+.1f} [{c['ci95'][0] * 100:+.1f}, "
                                                 f"{c['ci95'][1] * 100:+.1f}] p_holm={c['p_holm']:.3f}" if c else ""))
    return out


if __name__ == "__main__":
    r, cmd, *rest = sys.argv[1:]
    {"export": export, "batches": batches, "judge": judge, "score": score, "external_inputs": external_inputs}[cmd](r, *rest)
