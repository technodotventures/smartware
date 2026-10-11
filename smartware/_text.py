"""Shared text utilities: tokeniser, stemmer, stopwords, token counting.

Every system in the benchmark uses these, so differences in results come from
architecture, not from preprocessing luck.
"""
from __future__ import annotations

import re
from functools import lru_cache

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_TOKCOUNT = re.compile(r"\w+|[^\w\s]")

STOPWORDS = frozenset(
    """a about above after again against all am an and any are aren't as at be because been before being
    below between both but by can can't cannot could couldn't did didn't do does doesn't doing don't down during
    each few for from further had hadn't has hasn't have haven't having he he'd he'll he's her here here's hers
    herself him himself his how how's i i'd i'll i'm i've if in into is isn't it it's its itself let's me more most
    mustn't my myself no nor not of off on once only or other ought our ours ourselves out over own same shan't she
    she'd she'll she's should shouldn't so some such than that that's the their theirs them themselves then there
    there's these they they'd they'll they're they've this those through to too under until up very was wasn't we
    we'd we'll we're we've were weren't what what's when when's where where's which while who who's whom why why's
    with won't would wouldn't you you'd you'll you're you've your yours yourself yourselves also just really yeah
    oh hey hi okay ok so um uh like well get got gonna wanna lot lots thing things""".split()
)


@lru_cache(maxsize=200_000)
def stem(w: str) -> str:
    """Small, deterministic suffix stripper (Porter-lite). Good enough and shared by all systems."""
    if len(w) <= 3 or w.isdigit():
        return w
    if w.endswith("'s"):
        w = w[:-2]
    for suf, rep in (("ational", "ate"), ("ization", "ize"), ("fulness", "ful"), ("iveness", "ive"),
                     ("ousness", "ous"), ("ies", "y"), ("sses", "ss"), ("ness", ""), ("ments", "ment"),
                     ("ingly", ""), ("edly", ""), ("ing", ""), ("ed", ""), ("ly", ""), ("es", "e"), ("s", "")):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            base = w[: len(w) - len(suf)] + rep
            if suf in ("ing", "ed") and len(base) > 2 and base[-1] == base[-2] and base[-1] not in "lsz":
                base = base[:-1]  # running -> run
            if suf == "s" and w.endswith("ss"):
                return w
            return base
    return w


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def terms(text: str) -> list[str]:
    """BM25 terms: lowercase, stopwords removed, stemmed."""
    return [stem(w) for w in _WORD.findall(text.lower()) if w not in STOPWORDS]


def count_tokens(text: str) -> int:
    """Approximate LLM token count (word pieces + punctuation), ~1.25 per word for English."""
    n = len(_TOKCOUNT.findall(text))
    return max(1, int(n * 1.1))


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])|\n+", text.strip())
    return [p.strip() for p in parts if len(p.strip()) > 1]
