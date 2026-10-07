"""LME-R construction quirks (second audit), for a sensitivity analysis in the report.

late_gold:    test questions with a gold turn timestamped after the question time (inadmissible to any engine
              that honours as_of = question time; time-agnostic systems are unaffected)
future_haystack: test questions whose haystack contains sessions dated after the question (the distractor pool ran
              short, so the builder fell back to later sessions; an as_of-aware engine filters them)

python -m bench.lme_quirks  -> results/lme_quirks.json
"""
from __future__ import annotations

import json
import os

from .data import load_lme

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    late, future = [], []
    for t in load_lme():
        q = t.queries[0]
        if q.split != "test":
            continue
        ts = {x.id: x.ts for x in t.corpus.turns}
        if any(ts[g] > q.time for g in q.gold_turns):
            late.append(q.qid)
        if any(x.ts > q.time and x.session_id not in q.gold_sessions for x in t.corpus.turns):
            future.append(q.qid)
    out = {"late_gold": late, "future_haystack": future,
           "excluded": sorted(set(late) | set(future))}
    json.dump(out, open(os.path.join(ROOT, "results", "lme_quirks.json"), "w"), indent=1)
    print(len(late), "late gold;", len(future), "future haystack;", len(out["excluded"]), "excluded in total")


if __name__ == "__main__":
    main()
