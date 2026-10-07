"""Sylph (getnao/sylph, commit d31a9c0, 2026-05-21) LLM-free retrieval baseline.

Implements section 13 of /home/claude/mp/reports/sylph.md. Sylph has no retrieval engine: a coding agent reads an
always-loaded root (CLAUDE.md -> AGENTS.md + CONTEXT.md, plus the MEMORY.md standing rules), orients to one
domain/channel folder, then reads that folder's ``_insights.md``, its ``_examples/`` and the last N log files,
and occasionally greps. This module reproduces that read behaviour deterministically.

FAITHFUL (to the recipe, which is itself a mapping of Sylph's prose conventions)
- Folder = session, file = turn. ``doc_t = "{YYYY-MM-DD} {speaker}: {text}"``, sorted by timestamp within the
  session (this mirrors ``YYYY-MM-DD_slug.md`` file ordering).
- Folder card (the ``_insights.md`` analogue) = date range + speakers + top-15 TF-IDF terms of the session (TF over
  the session, IDF over turns) + first 2 and last 2 turns, each cut to 40 tokens. Cards are indexed with the shared
  BM25 and the shared embedder.
- Always-loaded tier (the CONTEXT.md / MEMORY.md analogue) = user turns matching the recipe's self-description /
  preference regex. Pins with the same pattern head that share >= 2 keywords are deduplicated, newest wins (the
  dated latest-wins MEMORY.md lines). At query time the top P=5 pins by BM25 are always emitted, ties broken by
  recency, and they are emitted even at score 0.
- Orientation: RRF(k=60) of the card BM25 rank and the card dense rank picks the top S=3 sessions. The R=2 most
  recent sessions are always added (the "read the 3 most recent logs" continuity step).
- In-folder ranking: RRF(k=60) with weights (BM25 1.0, grep 1.0, dense 0.5).
  - The grep rank orders turns by the count of distinct query keywords (non-stopword, len>=3) found as exact
    substrings, ties broken by BM25.
  - The last N=3 turns of each selected session get a flat +1/(60+10).
- Global grep fallback: rare query tokens are tokens with substring DF <= 1% of turns, or capitalised or
  containing digits in the raw query. Every turn anywhere that contains one gets +0.5/(60+rank by BM25 among grep
  hits) and joins the pool.
- Temporal cue (the recipe's regex, or a date in the query): score *= 1 + 0.5 * recency_pct among the candidates.
- The top K=10 turns are expanded by +/-1 neighbours in their session (the "whole-file read") and deduplicated.
  They are grouped by session, chronological within a group, and groups are ordered by their best hit score.
- Budget B=3000 tokens. Pins are never cut; folder cards are dropped first (lowest group first); then hit groups
  are dropped from the lowest-scoring one (the last remaining group is truncated, not dropped).

APPROXIMATED / DEVIATIONS
- Items: pins are one Item each (the MEMORY.md lines). Each session group is one Item, whose text is the folder
  card header line (if not dropped by the budget) followed by its ``[date] speaker: text`` turns. The recipe emits
  all cards before all hits; here each card sits at the head of its own group, because the harness needs every
  Item to carry turn ids. The emitted turn order is unchanged.
- The card header placed in context is the metadata line only (date range | speakers | key terms), cut to 60
  tokens (the recipe's "<= 60 token" card header). The first/last turn excerpts are used for indexing only.
- Cards are only emitted for sessions picked in the orientation step. Groups reached only through the global grep
  get no card.
- "User turn" for pins: role != "assistant" (both LoCoMo speakers are human users).
- Capitalisation rule for rare tokens ignores the query's first word (sentence-initial capitals are not
  proper-noun signals). Date detection = 4-digit years, numeric d/m, full month names, and "May <digit>".
- "Most recent sessions" uses sessions with start time <= q.time, i.e. files that exist at the benchmark's "now"
  (Sylph reads the latest files at run time). q.time is used nowhere else.
- No embedder-scale thresholds exist in this recipe, so no conversion was needed.

PARAMETERS: S=3, R=2, P=5, N=3, K=10, RRF k=60, weights (1.0, 1.0, 0.5), global-grep weight 0.5,
rare DF <= 1%, neighbour window +/-1, recency tilt 0.5 (temporal queries only), budget 3000, card terms 15,
card excerpt 40 tokens, card header 60 tokens.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

import numpy as np

from bench.system import Item, System, fmt_turn
from bench.text import STOPWORDS, count_tokens, terms, words

S_SEL, R_RECENT, P_PINS, N_LAST, K_TOP = 3, 2, 5, 3, 10
RRF_K = 60
W_BM25, W_GREP, W_DENSE = 1.0, 1.0, 0.5
W_GLOBAL_GREP = 0.5
LAST_N_BONUS = 1.0 / (RRF_K + 10)
RARE_DF = 0.01
NEIGHBOUR = 1
RECENCY_TILT = 0.5
BUDGET = 3000
CARD_TERMS, CARD_EXCERPT_TOK, CARD_HEADER_TOK = 15, 40, 60

PIN_RE = re.compile(
    r"\b(i am|i'm|my name|i live|i work|my (?:wife|husband|partner|son|daughter|job|favourite|favorite)|"
    r"i (?:always|never|prefer|like|love|hate|don't like|can't stand)|please (?:always|never)|call me)\b",
    re.I,
)
TEMPORAL_RE = re.compile(
    r"\b(last|latest|recent(ly)?|yesterday|today|this week|current(ly)?|now|before|after|first|ago|when)\b", re.I
)
DATE_RE = re.compile(
    r"\b(19|20)\d{2}\b|\b\d{1,2}/\d{1,2}(/\d{2,4})?\b|\b(january|february|march|april|june|july|august|september|"
    r"october|november|december)\b|\bmay\s+\d",
    re.I,
)
_RAW_TOK = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?")


def _norm(s: str) -> str:
    return s.replace("’", "'").replace("‘", "'")


def _cut_tokens(text: str, n: int) -> str:
    out = []
    for w in text.split():
        out.append(w)
        if count_tokens(" ".join(out)) > n:
            out.pop()
            break
    return " ".join(out)


def _rrf_ranks(scores: dict[int, float], tiebreak: dict[int, float] | None = None) -> dict[int, int]:
    """1-based ranks of the items with positive score (descending, ties by tiebreak desc, then index)."""
    pos = [i for i, s in scores.items() if s > 0]
    pos.sort(key=lambda i: (-scores[i], -(tiebreak[i] if tiebreak else 0.0), i))
    return {i: r + 1 for r, i in enumerate(pos)}


class Sylph(System):
    name = "sylph"
    family = "company-brain"
    faithful_notes = (
        "Sylph is a Markdown-in-git company brain with no retrieval engine. An agent reads always-loaded identity "
        "and rules files, orients to one domain folder, reads that folder's _insights.md summary, its exemplars "
        "and the latest N logs, and occasionally greps. Mapping: session = folder, turn = file. The always-loaded "
        "tier is regex-detected self-descriptions and preferences with newest-wins dedupe (top 5 by BM25, always "
        "emitted). Orientation is RRF(60) over BM25 and dense scores of extractive session cards (dates, speakers, "
        "top-15 TF-IDF terms, first and last 2 turns) for the top 3 sessions, plus the 2 most recent sessions. In "
        "those sessions, turns are ranked by RRF of BM25, a keyword-substring grep count and dense (weights 1/1/0.5), "
        "with a bonus for the last 3 turns of each session. A global rare-token grep adds 0.5-weighted candidates "
        "from any session, and a mild recency tilt applies only to temporal queries. The top 10 turns are expanded "
        "+/-1 and grouped by session behind a 60-token card header, within a 3000-token budget. All of this "
        "approximates LLM-driven folder choice, LLM-written insights and human-curated exemplars."
    )

    # ---------------------------------------------------------------- build
    def build(self, corpus, enc):
        self.enc = enc
        by_sess: dict[str, list] = defaultdict(list)
        for t in corpus.turns:
            by_sess[t.session_id].append(t)
        for v in by_sess.values():
            v.sort(key=lambda t: (t.ts, t.idx))
        self.sessions = [s for s in corpus.session_order if s in by_sess]
        self.turns = [t for s in self.sessions for t in by_sess[s]]
        self.pos = {t.id: i for i, t in enumerate(self.turns)}
        self.sess_turns = {s: [self.pos[t.id] for t in by_sess[s]] for s in self.sessions}
        self.turn_sess = np.array([self.sessions.index(t.session_id) for t in self.turns])
        self.ts = np.array([t.ts.timestamp() for t in self.turns])

        # 1. files
        self.docs = [f"{t.ts:%Y-%m-%d} {t.speaker}: {_norm(t.text)}" for t in self.turns]
        self.docs_lc = [d.lower() for d in self.docs]
        self.bm25_turn = enc.bm25(self.docs)
        self.E_turn = enc.embed_many(self.docs)

        # 2. folder cards
        n_turns = len(self.turns)
        df = Counter()
        tok_per_turn = []
        surface: dict[str, Counter] = defaultdict(Counter)
        for d in self.docs:
            ws = [w for w in words(d) if w not in STOPWORDS and not w.isdigit()]
            ts_ = [terms(w)[0] if terms(w) else w for w in ws]
            for w, st in zip(ws, ts_):
                surface[st][w] += 1
            tok_per_turn.append(ts_)
            df.update(set(ts_))
        self.card_header, cards = {}, []
        self.sess_last_ts = {}
        for s in self.sessions:
            ids = self.sess_turns[s]
            tf = Counter()
            for i in ids:
                tf.update(tok_per_turn[i])
            tfidf = {w: c * math.log(n_turns / df[w]) for w, c in tf.items() if df[w]}
            top = sorted(tfidf, key=lambda w: (-tfidf[w], w))[:CARD_TERMS]
            top_surface = [surface[w].most_common(1)[0][0] for w in top]
            t0, t1 = self.turns[ids[0]], self.turns[ids[-1]]
            speakers = list(dict.fromkeys(self.turns[i].speaker for i in ids))
            header = (f"## Folder {t0.ts:%Y-%m-%d} to {t1.ts:%Y-%m-%d} | {', '.join(speakers)} | "
                      f"key terms: {', '.join(top_surface)}")
            self.card_header[s] = _cut_tokens(header, CARD_HEADER_TOK)
            ex_ids = list(dict.fromkeys(ids[:2] + ids[-2:]))
            excerpts = [_cut_tokens(f"{self.turns[i].speaker}: {_norm(self.turns[i].text)}", CARD_EXCERPT_TOK)
                        for i in ex_ids]
            cards.append(header + "\n" + "\n".join(excerpts))
            self.sess_last_ts[s] = t1.ts
        self.bm25_card = enc.bm25(cards)
        self.E_card = enc.embed_many(cards)
        self.sess_start = {s: self.turns[self.sess_turns[s][0]].ts for s in self.sessions}

        # 3. always-loaded tier (pins)
        cand = []
        for i, t in enumerate(self.turns):
            if t.role == "assistant":
                continue
            m = PIN_RE.search(_norm(t.text))
            if m:
                cand.append((i, re.sub(r"\s+", " ", m.group(1).lower()), set(terms(t.text))))
        by_head: dict[str, list] = defaultdict(list)
        for c in cand:
            by_head[c[1]].append(c)
        pins = []
        for head, lst in by_head.items():
            kept = []
            for i, _, kw in sorted(lst, key=lambda c: -self.ts[c[0]]):  # newest first
                if any(len(kw & k2) >= 2 for _, k2 in kept):
                    continue
                kept.append((i, kw))
            pins += [i for i, _ in kept]
        self.pins = sorted(pins)
        self.bm25_pin = enc.bm25([self.docs[i] for i in self.pins]) if self.pins else None

    # ---------------------------------------------------------------- retrieve
    def retrieve(self, q, k=30):
        qtext = _norm(q.text)
        qvec = self.enc.embed(qtext)
        bm_turn = self.bm25_turn.score(qtext)

        # 4. pinned layer
        pin_items = []
        if self.pins:
            ps = self.bm25_pin.score(qtext)
            order = sorted(range(len(self.pins)), key=lambda j: (-ps[j], -self.ts[self.pins[j]]))[:P_PINS]
            for j in order:
                t = self.turns[self.pins[j]]
                pin_items.append(Item([t.id], fmt_turn(t), float(ps[j]), {"layer": "pin"}))

        # 5. orient
        cb = self.bm25_card.score(qtext)
        cd = self.E_card @ qvec
        r1 = _rrf_ranks({i: float(cb[i]) for i in range(len(self.sessions))})
        r2 = {i: r + 1 for r, i in enumerate(np.argsort(-cd, kind="stable"))}
        sscore = {i: (1.0 / (RRF_K + r1[i]) if i in r1 else 0.0) + 1.0 / (RRF_K + r2[i])
                  for i in range(len(self.sessions))}
        selected = sorted(sscore, key=lambda i: (-sscore[i], i))[:S_SEL]
        existing = [s for s in self.sessions if self.sess_start[s] <= q.time] or list(self.sessions)
        recent = sorted(existing, key=lambda s: self.sess_last_ts[s], reverse=True)[:R_RECENT]
        sel_sessions = [self.sessions[i] for i in selected]
        for s in recent:
            if s not in sel_sessions:
                sel_sessions.append(s)

        # 6. in-folder candidates
        kws = list(dict.fromkeys(w for w in words(qtext) if w not in STOPWORDS and len(w) >= 3))
        pool = [i for s in sel_sessions for i in self.sess_turns[s]]
        score: dict[int, float] = defaultdict(float)
        bm = {i: float(bm_turn[i]) for i in pool}
        grep = {i: float(sum(1 for w in kws if w in self.docs_lc[i])) for i in pool}
        dense = {i: float(self.E_turn[i] @ qvec) for i in pool}
        ra = _rrf_ranks(bm)
        rb = _rrf_ranks(grep, tiebreak=bm)
        rc = {i: r + 1 for r, i in enumerate(sorted(pool, key=lambda i: (-dense[i], i)))}
        for i in pool:
            score[i] = (W_BM25 / (RRF_K + ra[i]) if i in ra else 0.0) + \
                       (W_GREP / (RRF_K + rb[i]) if i in rb else 0.0) + W_DENSE / (RRF_K + rc[i])
        for s in sel_sessions:
            for i in self.sess_turns[s][-N_LAST:]:
                score[i] += LAST_N_BONUS

        # 7. global grep fallback
        raw = [m.group(0) for m in _RAW_TOK.finditer(qtext)]
        rare = []
        n = len(self.turns)
        for j, w in enumerate(raw):
            lw = w.lower()
            if lw in STOPWORDS or len(lw) < 3:
                continue
            capital = j > 0 and w[0].isupper()
            digit = any(ch.isdigit() for ch in w)
            dfw = sum(1 for d in self.docs_lc if lw in d)
            if dfw and (dfw <= RARE_DF * n or capital or digit):
                rare.append(lw)
        rare = list(dict.fromkeys(rare))
        if rare:
            hits = [i for i, d in enumerate(self.docs_lc) if any(w in d for w in rare)]
            hits.sort(key=lambda i: (-bm_turn[i], i))
            for r, i in enumerate(hits):
                score[i] += W_GLOBAL_GREP / (RRF_K + r + 1)

        # 8. temporal tilt
        if TEMPORAL_RE.search(qtext) or DATE_RE.search(qtext):
            cand = sorted(score, key=lambda i: (self.ts[i], i))
            m = len(cand)
            for r, i in enumerate(cand):
                pct = r / (m - 1) if m > 1 else 1.0
                score[i] *= 1.0 + RECENCY_TILT * pct

        # 9. select + expand
        top = sorted(score, key=lambda i: (-score[i], i))[:K_TOP]
        groups: dict[int, dict] = {}
        for i in top:
            si = int(self.turn_sess[i])
            g = groups.setdefault(si, {"best": score[i], "ids": set()})
            g["best"] = max(g["best"], score[i])
            for d in range(-NEIGHBOUR, NEIGHBOUR + 1):
                jdx = i + d
                if 0 <= jdx < n and self.turn_sess[jdx] == si:
                    g["ids"].add(jdx)
        order = sorted(groups, key=lambda si: (-groups[si]["best"], si))
        sel_set = set(sel_sessions)

        # 10. assemble under budget
        blocks = []
        for si in order:
            s = self.sessions[si]
            ids = sorted(groups[si]["ids"], key=lambda i: (self.ts[i], i))
            lines = [fmt_turn(self.turns[i]) for i in ids]
            blocks.append({"sess": s, "best": groups[si]["best"], "ids": ids, "lines": lines,
                           "card": self.card_header[s] if s in sel_set else None})
        pin_cost = sum(count_tokens(it.text) for it in pin_items)

        def total():
            c = pin_cost
            for b in blocks:
                if b["card"]:
                    c += count_tokens(b["card"])
                c += sum(count_tokens(x) for x in b["lines"])
            return c

        for b in reversed(blocks):  # drop cards first, lowest group first
            if total() <= BUDGET:
                break
            b["card"] = None
        while total() > BUDGET and len(blocks) > 1:  # then drop lowest-scoring hit groups
            blocks.pop()
        while total() > BUDGET and blocks and len(blocks[0]["lines"]) > 1:  # truncate the last group
            blocks[0]["lines"].pop()
            blocks[0]["ids"].pop()

        items = list(pin_items)
        for b in blocks:
            text = "\n".join(([b["card"]] if b["card"] else []) + b["lines"])
            items.append(Item([self.turns[i].id for i in b["ids"]], text, float(b["best"]),
                              {"layer": "folder", "session": b["sess"], "card": bool(b["card"])}))
        return items[:k]
