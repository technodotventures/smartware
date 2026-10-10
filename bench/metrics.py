"""Retrieval metrics.

Systems return context items of different granularity (a fact, a turn, a
session chunk, a whole page). To compare fairly we charge every item its token
cost and measure how much of the gold evidence lands inside a fixed context
budget, which is the quantity that actually matters to an agent.

Primary metrics (no prefix): tokens = characters/4 (format-neutral; does not penalise
JSON or ids), greedy skip-and-continue packing (charitable to systems that return
large items). Secondary metrics (prefix s_): word-and-punctuation token estimate,
stop-at-first-overflow packing.

Evidence-text recall (prefix e_, primary packing): a gold turn counts only if an item that claims it is in the
context AND those items' text contains at least half of the turn's distinct content terms. This removes credit
for snippets or sentence-level units that claim a long turn without carrying its content (same rule for every
system).
"""
from __future__ import annotations

from .system import Item
from .text import count_tokens, terms

BUDGETS = (500, 1000, 2000, 4000)


def chars4(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def pack(items: list[Item], budget: int, counter=None, skip: bool = False) -> tuple[set[str], int, list[Item]]:
    """Fill a context budget with items in rank order.
    skip=False: stop at the first item that does not fit (truncation).
    skip=True : greedy fill, skipping items that do not fit and continuing (charitable to large items)."""
    counter = counter or count_tokens
    if skip:
        covered, used, kept = set(), 0, []
        for it in items:
            c = counter(it.text)
            if used + c > budget:
                continue
            used += c
            covered.update(it.turn_ids)
            kept.append(it)
        if not kept and items:
            covered.update(items[0].turn_ids[:1])
            kept.append(items[0])
            used = budget
        return covered, used, kept
    covered: set[str] = set()
    used = 0
    kept = []
    for i, it in enumerate(items):
        c = counter(it.text)
        if used + c > budget:
            if i == 0:  # always allow the first item, truncated
                kept.append(it)
                covered.update(it.turn_ids[: max(1, int(len(it.turn_ids) * budget / max(c, 1)))])
                used = budget
            break
        used += c
        covered.update(it.turn_ids)
        kept.append(it)
    return covered, used, kept


E_SHARE = 0.5


def evidence_hit(kept: list[Item], tid: str, text: str) -> bool:
    need = set(terms(text))
    have = set()
    for it in kept:
        if tid in it.turn_ids:
            have.update(terms(it.text))
    if not need:
        return any(tid in it.turn_ids for it in kept)
    return len(need & have) >= E_SHARE * len(need)


def score_query(items: list[Item], gold_turns: set[str], gold_sessions: set[str], turn2sess: dict[str, str],
                answer: str = "", turn_text: dict[str, str] | None = None) -> dict:
    out = {}
    ans = set(terms(answer)) if answer else set()
    answ = {w for w in ans if not w.isdigit()}
    for prefix, counter, skip in (("", chars4, True), ("s_", count_tokens, False)):
        for b in BUDGETS:
            cov, used, kept = pack(items, b, counter, skip)
            if ans:
                ctx = set()
                for it in kept:
                    ctx.update(terms(it.text))
                out[f"{prefix}ans@{b}"] = len(ans & ctx) / len(ans)
                if not prefix and answ:  # answer words without bare numbers (stray list numbers, scores, ids)
                    out[f"answ@{b}"] = len(answ & ctx) / len(answ)
            if gold_turns:
                hit = len(cov & gold_turns)
                out[f"{prefix}recall@{b}"] = hit / len(gold_turns)
                out[f"{prefix}full@{b}"] = float(hit == len(gold_turns))
                if turn_text is not None and not prefix:
                    eh = sum(evidence_hit(kept, g, turn_text.get(g, "")) for g in gold_turns if g in cov)
                    out[f"e_recall@{b}"] = eh / len(gold_turns)
            out[f"{prefix}tokens@{b}"] = used
    # session-level ranking (LongMemEval style recall_any / recall_all @k)
    ranked_sess = []
    for it in items:
        for tid in it.turn_ids:
            s = turn2sess.get(tid)
            if s and s not in ranked_sess:
                ranked_sess.append(s)
    for k in (5, 10):
        top = set(ranked_sess[:k])
        out[f"sess_any@{k}"] = float(bool(top & gold_sessions))
        out[f"sess_all@{k}"] = float(gold_sessions <= top)
    # rank of first gold turn (by item order), MRR
    rr = 0.0
    for i, it in enumerate(items):
        if gold_turns & set(it.turn_ids):
            rr = 1.0 / (i + 1)
            break
    out["mrr"] = rr
    return out
