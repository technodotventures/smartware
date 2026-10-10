"""Benchmark runner.

python -m bench.run --bench locomo,lme --systems all [--split test] [--workers 2]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib
import subprocess
import json
import os
import statistics
import sys
import time
import traceback
from collections import defaultdict
from multiprocessing import Pool

from .data import load_lme, load_lme_m, load_lme_s, load_locomo, load_hotpot
from .encoders import DenseEncoder, Encoders
from .metrics import score_query

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "results", "raw")

# name -> "module:Class"
REGISTRY = {
    "graphiti": "baselines.graphiti:Graphiti",
    "hindsight": "baselines.hindsight:Hindsight",
    "mem0": "baselines.mem0:Mem0",
    "letta": "baselines.letta:Letta",
    "supermemory-company-brain": "baselines.supermemory_company_brain:CompanyBrain",
    "gbrain": "baselines.gbrain:GBrain",
    "supermemory": "baselines.supermemory:Supermemory",
    "onyx": "baselines.onyx:Onyx",
    "cognee": "baselines.cognee:Cognee",
    "basic-memory": "baselines.basic_memory:BasicMemory",
    "sylph": "baselines.sylph:Sylph",
    "mcp-memory": "baselines.mcp_memory:MCPMemory",
    "memos": "baselines.memos:MemOS",
    "langmem": "baselines.langmem:LangMem",
    "memoryos": "baselines.memoryos:MemoryOS",
    "bm25": "baselines.reference:BM25Only",
    "dense": "baselines.reference:DenseOnly",
    "orp": "bench.orp_variants:ORPLtr",
    "orp-rrf": "bench.orp_variants:ORPRrf",
    "orp-allfeat": "bench.orp_variants:ORPAllFeatures",
    "orp-ft": "bench.orp_variants:ORPFactsRanker",
}

_TASKS = {}


def get_tasks(bench: str, split: str = "all"):
    if bench == "lmem":  # official LongMemEval-M: too large to hold, streamed afresh on every call
        return load_lme_m(split=split)
    if bench == "lmes":  # official LongMemEval-S, streamed the same way
        return load_lme_s(split=split)
    if bench not in _TASKS:
        _TASKS[bench] = {"locomo": load_locomo, "lme": load_lme, "hotpot": load_hotpot}[bench]()
    return _TASKS[bench]


_TUNED = None


def make(name: str, **kw):
    """`<system>@tuned` builds the baseline with its dev-tuned configuration (results/baseline_tuning.json).
    `<system>+facts` stores the shared LLM-extracted facts instead of raw turns; `orp+evidence` keeps both
    (bench/facts.py)."""
    if name.endswith("+facts"):
        from .facts import FactStore
        return FactStore(make(name[: -len("+facts")], **kw))
    if name.startswith("orp+evidence"):
        from .facts import orp_with_evidence
        return orp_with_evidence(**kw)
    if name.endswith("@ftuned"):  # knobs tuned on the fact stores (results/baseline_tuning_facts.json)
        base = name[: -len("@ftuned")]
        with open(os.path.join(ROOT, "results", "baseline_tuning_facts.json")) as fh:
            return make(base, **{**json.load(fh)[base]["best"], **kw})
    if name.endswith("@tuned"):
        base = name[: -len("@tuned")]
        global _TUNED
        if _TUNED is None:  # read once per process (the tuning file must not change under a running job)
            with open(os.path.join(ROOT, "results", "baseline_tuning.json")) as fh:
                _TUNED = json.load(fh)
        tuned = _TUNED[base]["best"]
        return make(base, **{**tuned, **kw})
    mod, cls = REGISTRY[name].split(":")
    return getattr(importlib.import_module(mod), cls)(**kw)


def run_one(args):
    name, bench, split, opts = args
    raw_dir = opts.pop("_raw", RAW)
    enc = Encoders(DenseEncoder.load())
    tasks = get_tasks(bench, split)
    rows, build_ms, q_ms = [], [], []
    tag = opts.get("_tag", "")
    os.makedirs(os.path.join(raw_dir, bench), exist_ok=True)
    pp = os.path.join(raw_dir, bench, f"{name}{tag}.{split}.partial")  # progress, for long runs; resumed if present
    if opts.pop("_resume", False) and os.path.exists(pp):
        rows = [json.loads(l) for l in open(pp) if l.strip().endswith("}")]
    done = {r["qid"] for r in rows}
    part = open(pp, "w")
    for r in rows:
        part.write(json.dumps(r) + "\n")
    try:
        for task in tasks:
            qs = [q for q in task.queries if (split == "all" or q.split == split) and q.qid not in done]
            if not qs:
                continue
            sysm = make(name, **opts)
            t0 = time.perf_counter()
            sysm.build(task.corpus, enc)
            build_ms.append((time.perf_counter() - t0) * 1000)
            t2s = {t.id: t.session_id for t in task.corpus.turns}
            ttext = {t.id: t.text for t in task.corpus.turns}
            for q in qs:
                t1 = time.perf_counter()
                items = sysm.retrieve(q, k=50)
                q_ms.append((time.perf_counter() - t1) * 1000)
                m = score_query(items, q.gold_turns, q.gold_sessions, t2s, q.answer, ttext)
                m.update(qid=q.qid, category=q.category, split=q.split, n_items=len(items))
                rows.append(m)
                part.write(json.dumps(m) + "\n")
                part.flush()
            if bench in ("lmem", "lmes"):  # large haystacks: drop the embedding cache between them (outputs unchanged)
                enc.dense._cache.clear()
    except Exception:
        traceback.print_exc()
        return name, bench, None
    part.close()
    os.remove(part.name)
    with open(os.path.join(raw_dir, bench, f"{name}{tag}.{split}.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    lat = sorted(q_ms)
    summ = summarise(rows)
    summ["latency_p50_ms"] = lat[len(lat) // 2] if lat else 0
    summ["latency_p95_ms"] = lat[int(len(lat) * 0.95)] if lat else 0
    summ["build_ms_per_corpus"] = statistics.mean(build_ms) if build_ms else 0
    return name + tag, bench, summ


def summarise(rows: list[dict]) -> dict:
    keys = [k for k in rows[0] if k not in ("qid", "category", "split", "n_items")] if rows else []
    out = {"n": len(rows)}
    for k in keys:
        vals = [r[k] for r in rows if k in r]
        out[k] = sum(vals) / len(vals) if vals else None
    bycat = defaultdict(list)
    for r in rows:
        bycat[r["category"]].append(r)
    out["by_category"] = {
        c: {"n": len(rs), "recall@1000": sum(r.get("recall@1000", 0) for r in rs) / len(rs),
            "recall@2000": sum(r.get("recall@2000", 0) for r in rs) / len(rs),
            "ans@1000": sum(r.get("ans@1000", 0) for r in rs) / len(rs),
            "s_recall@1000": sum(r.get("s_recall@1000", 0) for r in rs) / len(rs),
            "sess_all@5": sum(r["sess_all@5"] for r in rs) / len(rs)}
        for c, rs in sorted(bycat.items())
    }
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default="locomo,lme")
    ap.add_argument("--systems", default="all")
    ap.add_argument("--split", default="test")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--out", default=os.path.join(ROOT, "results", "summary.json"))
    ap.add_argument("--raw", default=RAW, help="directory for per-query results")
    ap.add_argument("--resume", action="store_true", help="continue from .partial files (after an interrupted run)")
    a = ap.parse_args(argv)
    names = list(REGISTRY) if a.systems == "all" else a.systems.split(",")
    jobs = [(n, b, a.split, {"_raw": a.raw, "_resume": a.resume}) for b in a.bench.split(",") for n in names]
    if a.split == "test":  # append-only record of every test-split invocation
        try:
            head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                                  text=True).stdout.strip()
            dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True,
                                        text=True).stdout.strip())
        except Exception:
            head, dirty = "?", True
        with open(os.path.join(ROOT, "results", "TEST_RUNS.log"), "a") as f:
            f.write(f"{_dt.datetime.now().isoformat()} git={head}{'+dirty' if dirty else ''} bench={a.bench} "
                    f"systems={','.join(names)} out={os.path.relpath(a.out, ROOT)}\n")
    summary = json.load(open(a.out)) if os.path.exists(a.out) else {}
    with Pool(a.workers) as p:
        for name, bench, s in p.imap_unordered(run_one, jobs):
            if s is None:
                print(f"FAILED {name} {bench}", flush=True)
                continue
            summary.setdefault(bench, {}).setdefault(a.split, {})[name] = s
            print(f"{bench:7s} {name:28s} R@500={s.get('recall@500', 0):.3f} R@1000={s.get('recall@1000', 0):.3f} "
                  f"R@2000={s.get('recall@2000', 0):.3f} sR@1000={s.get('s_recall@1000', 0):.3f} "
                  f"ans@1000={s.get('ans@1000', 0):.3f} sessAll@5={s['sess_all@5']:.3f} mrr={s['mrr']:.3f} "
                  f"p50={s['latency_p50_ms']:.1f}ms", flush=True)
            json.dump(summary, open(a.out, "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
