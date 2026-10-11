"""Differential parity, oracle side: build each benchmark haystack in the Python engine, recall every question, and
write the corpus, the questions and the oracle's packs as JSONL for tools/parity/compare.mjs.

    python3 tools/parity/dump.py locomo test smartware-linear-1 out/   (config: smartware-linear-1 | smartware-rrf-1 | fast)
"""
import gzip
import json
import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from smartware import rankers  # noqa: E402
from smartware.embedders import SIFEmbedder  # noqa: E402
from smartware.engine import Engine  # noqa: E402
from smartware.model import Memory, RecallRequest  # noqa: E402
from bench import data  # noqa: E402

bench, split, config, out = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
limit = int(sys.argv[5]) if len(sys.argv) > 5 else None
os.makedirs(out, exist_ok=True)
enc = SIFEmbedder(os.path.join(os.path.dirname(__file__), "..", "..", "results", "cache", "sif-ppmi-256.swenc"),
                  exact=os.environ.get("SMARTWARE_PARITY_BLAS") != "1")
params = rankers.params("smartware-rrf-1" if config == "fast" else config)
if config == "fast":
    params = replace(params, w_prf=0.0, ranker="rrf")
params = replace(params, exact_dot=os.environ.get("SMARTWARE_PARITY_BLAS") != "1")  # position-independent dense scores
if bench == "locomo":
    tasks = [t for t in data.load_locomo() if split == "all" or all(q.split == split for q in t.queries)]
elif bench == "lmes":
    tasks = list(data.load_lme_s(limit=limit, split=split, day=True))
else:
    raise SystemExit("bench: locomo | lmes")
K = 30
fo = gzip.open(os.path.join(out, f"{bench}_{split}_{config}.jsonl.gz"), "wt")
nq = 0
for ti, task in enumerate(tasks):
    if limit and bench == "locomo" and ti >= limit:
        break
    corpus = task.corpus
    label = {s: f"S{i + 1}" for i, s in enumerate(corpus.session_order)}
    mems = [Memory(content=t.text, kind="event", actor=t.speaker, role=t.role, thread=t.session_id, seq=t.idx,
                   observed_at=t.ts, id=f"t:{t.id}",
                   meta={"turn_id": t.id, "_idempotency_key": t.id, "thread_label": label.get(t.session_id, "")})
            for t in corpus.turns]
    rec = [m.to_json() for m in mems]
    eng = Engine(enc, params)
    eng.remember(mems)
    qs = []
    for q in task.queries:
        if split != "all" and q.split != split:
            continue
        pack = eng.recall(RecallRequest(query=q.text, as_of=q.time, budget_tokens=None, max_items=K))
        qs.append({"qid": q.qid, "query": q.text, "as_of": q.time.isoformat() if q.time else None,
                   "items": [{"ids": it.memory_ids, "text": it.text, "score": it.score, "why": it.why}
                             for it in pack.items], "coverage": pack.coverage, "notes": pack.notes})
        nq += 1
    fo.write(json.dumps({"task": ti, "memories": rec, "queries": qs, "k": K}) + "\n")
fo.close()
print(f"{bench} {split} {config}: {len(tasks)} haystacks, {nq} questions")
