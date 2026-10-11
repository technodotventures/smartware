"""Write an ORP-Bench set in the neutral eval format (orp-bench-1) that `orp eval` reads, from the same loaders the
benchmark used, so the TypeScript engine is measured on exactly the published data.

    python3 tools/eval/export_bench.py locomo test out/locomo_test.orpbench.jsonl
    python3 tools/eval/export_bench.py lmes test out/lme_s_test.orpbench.jsonl      (needs ../data/lme_s)

Format, one haystack per line:
    {"format": "orp-bench-1", "id": ..., "memories": [{id, content, kind, actor, role, thread, seq, observed_at,
     meta}], "questions": [{id, question, as_of, answer, evidence: [memory ids], category}]}
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from bench import data  # noqa: E402

bench, split, out = sys.argv[1], sys.argv[2], sys.argv[3]
if bench == "locomo":
    tasks = data.load_locomo()
elif bench == "lmes":
    tasks = list(data.load_lme_s(split=split, day=True))
elif bench == "lmem":
    tasks = list(data.load_lme_m(split=split, day=True))
else:
    raise SystemExit("bench: locomo | lmes | lmem")
os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
h = hashlib.sha256()
nq = nh = 0
with open(out, "w") as f:
    for task in tasks:
        qs = [q for q in task.queries if split == "all" or q.split == split]
        if not qs:
            continue
        c = task.corpus
        label = {s: f"S{i + 1}" for i, s in enumerate(c.session_order)}
        mems = [{"id": t.id, "content": t.text, "kind": "event", "actor": t.speaker, "role": t.role,
                 "thread": t.session_id, "seq": t.idx, "observed_at": t.ts.isoformat(),
                 "meta": {"thread_label": label.get(t.session_id, "")}} for t in c.turns]
        rec = {"format": "orp-bench-1", "id": c.id, "memories": mems,
               "questions": [{"id": q.qid, "question": q.text, "as_of": q.time.isoformat() if q.time else None,
                              "answer": q.answer, "evidence": sorted(q.gold_turns), "category": q.category}
                             for q in qs]}
        line = json.dumps(rec, sort_keys=True)
        h.update(line.encode())
        f.write(line + "\n")
        nq += len(qs)
        nh += 1
print(json.dumps({"file": out, "haystacks": nh, "questions": nq, "sha256": h.hexdigest()}))
