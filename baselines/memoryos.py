"""MemoryOS (BAI-LAB/MemoryOS, commit 587ed77) LLM-free retrieval baseline.

Implements section 13 of /home/claude/mp/reports/memoryos.md. The DEFAULT is the PAPER/eval parameter set,
i.e. the code path in eval/ (main_loco_parse.py, mid_term_memory.py, retrieval_and_answer.py,
long_term_memory.py) that produced the paper's LoCoMo numbers. That is the strongest config the project
ships. The pypi-library defaults are available as mode="lib".

FAITHFUL (paper/eval mode)
- Page construction emulates eval/main_loco_parse.py:process_conversation over the whole conversation in
  session order:
  - A turn by the first speaker ("user" role: LoCoMo speaker_a, LME "user") opens a page.
  - The next other-speaker turn fills agent_response. A further other-speaker turn OVERWRITES it, so the
    earlier reply is lost. This also happens across a session boundary, and the page keeps the timestamp of
    its opening turn.
  - An other-speaker turn with no open page opens a page with an empty user side.
  - Pages with an empty user or agent side are dropped at STM eviction (dynamic_update.py:126; the library
    does the same at updater.py:104).
  - The timestamp is the SESSION date string (the harness uses session_N_date_time), rendered in LoCoMo's
    format "%I:%M %p on %d %B, %Y".
- STM capacity is 1, so each page is evicted to MTM immediately and STM is empty at question time.
- Segment assignment (mid_term_memory.py:128-183), one page at a time in order:
  - v = E(user_input). This approximates the LLM multi-summary, which sees user lines only.
  - best = argmax cosine over ALL segments. Require best_sim >= 0.
  - score = best_sim + 1.0 * 0.5*(|K∩Ks|/|Ks| + |K∩Kn|/|Kn|), the overlap coefficient, not Jaccard.
  - Append if score >= 0.6, else create a new segment with vec = v and keywords frozen at creation.
  - The eval bug is reproduced: best_sid's L_interaction is incremented even when a new segment was created.
- Heat = 0.8*N_visit + 0.8*L_interaction + 0.0001*R_recency (R stays 1).
  - After each page, only the heap top is checked (ties go to the earliest segment).
  - If heat >= 5 and the segment has unanalysed pages, they are "promoted" to the user KB. Then all its pages
    are marked analysed and N = L = 0, H = 0.
  - The KB is unbounded in eval (long_term_memory.py:9).
- Query (retrieval_and_answer.py, mid_term_memory.py:185-248):
  - Take the top m=5 segments by cosine of the query to the segment vector (FAISS IP).
  - Add the query-keyword overlap coefficient, with lambda_t = 1 (decay commented out in the source).
  - Keep segments with score >= segment threshold.
  - Score every page in the kept segments by cosine of E(f"{user_input}{timestamp}{agent_response}") with
    the query. Concatenation with no separators is exactly what eval re-embeds at query time.
  - Keep pages >= page threshold. Take the global top 10 (queue_capacity=10) by page cosine alone (the
    segment score is not combined).
  - User KB: top 10 by cosine, threshold 0.1 (knowledge_threshold=0.1).
- Query side effects (N_visit, LFU) are skipped. In eval, promotion is only checked during ingestion and the
  2000-segment cap is never reached, so they cannot change any later result.
- Item text is what eval puts in context:
  - pages: "【Historical Memory】 {A}: {u}\\n{B}: {r}\\nTime:({ts})\\nConversation chain overview:({meta})\\n"
  - KB facts: one line each.
  - Order is pages (by score) then facts (by score). At most 10+10 items, i.e. fewer than k by design.
- q.time is unused: eval's time decay is disabled and no "now" enters ranking.

APPROXIMATED (no LLM)
- Multi-summary content, the topic vector source: E(user_input), the user line only.
- LLM keywords (<=3, llm_extract_keywords): the top-3 stemmed non-stopword terms by tf-idf. IDF is taken
  over this corpus's pages; segment keywords come from the first page's user_input.
- Dialogue chain and meta_info:
  - The LLM continuity check is replaced by "previous kept page in the same session".
  - The LLM 1-2 sentence running summary is replaced by the nearest previous chain lines (nearest first,
    whole "Speaker: text" lines, <= 300 chars total; a single over-long line is cut to 300).
  - Those lines' turns are added to Item.turn_ids because their text is in the item.
  - meta="none" disables this.
- LPM fact extraction (gpt_personality_analysis 【User Data】 + assistant knowledge):
  - Approximated by the sentences (>= 4 words) of the promoted pages' turns, from both speakers.
  - Each fact is f"{speaker}: {sentence} ({d Month YYYY})" with turn_ids = its turn.
  - In eval, assistant-side knowledge is NOT ranked: it is all dumped into the system prompt. A verbatim dump
    of every sentence is unbounded, so instead both speakers' sentences share the ranked top-10 KB.
    Deliberate deviation, documented.
- The free-text user profile, always dumped whole in eval, is omitted. It is not query-ranked, and it is an
  LLM trait summary with no turn-level evidence.
- The duplicate insertion of a page under two LLM themes cannot happen here (one pseudo-theme per page).

THRESHOLDS (embedder-scale)
- Merge theta 0.6 is kept RAW.
  - The documented eval encoder is all-MiniLM-L6-v2. On it, 0.6 between two short same-conversation
    utterances is about the 97-99th percentile of pairs.
  - DEV check: on this embedder raw 0.6 is about the 99th percentile of turn pairs on LoCoMo (0.7% of
    pairs >= 0.6) and LME (1.5%), so the scales match.
  - The keyword bonus (0, 0.33, 0.67 or 1.0 with 3 keywords) is scale-free.
  - DEV check (structure only, no scores): LoCoMo dev has 117-224 segments per conversation, max size
    9-13, about 77% singletons. LME dev has 102-110 segments, max 9-15, 42-51% singletons. So the
    threshold neither collapses everything into one segment nor leaves only singletons.
- Page / segment / KB thresholds of 0.1 are mapped to a percentile (threshold_mode="pctl").
  - On MiniLM, question-vs-turn cosines within a conversation centre around 0.1-0.2 (sd about 0.1), so 0.1
    sits at about the 35th percentile and drops roughly a third of candidates.
  - On this embedder raw 0.1 keeps only about 23% (DEV), which is far stricter than in the project.
  - So the cutoff = the 35th percentile of the query's cosines over all pages / segments / KB facts.
  - threshold_mode="raw" keeps the literal 0.1.

PARAMETERS (paper default | lib):
- STM capacity 1 | 10
- theta 0.6 | 0.6
- keyword score: overlap coefficient | Jaccard (dead at query time in lib)
- heat weights (0.8, 0.8, 0.0001) | (1, 1, 1); heat threshold 5 | 5
- top-m segments 5 | 5; pages top-k 10 | 7
- KB top 10, unbounded | top 20, FIFO 100
- thresholds 0.1/0.1/0.1 | 0.1/0.1/0.01
- page text "{u}{ts}{r}" | "User: {u} Assistant: {r}"

OPTIONS (not default): mode="lib"; threshold_mode="raw"; meta="none".
"""
from __future__ import annotations

import math
from collections import Counter, deque

import numpy as np

from bench.system import Item, System
from bench.text import sentences, terms, words

PARAMS = {
    "paper": dict(stm_cap=1, theta=0.6, kw_mode="overlap", heat=(0.8, 0.8, 0.0001), heat_thr=5.0,
                  top_m=5, top_pages=10, kb_top=10, kb_cap=None, seg_thr=0.1, page_thr=0.1, kb_thr=0.1,
                  page_fmt="paper", query_kw=True),
    "lib": dict(stm_cap=10, theta=0.6, kw_mode="jaccard", heat=(1.0, 1.0, 1.0), heat_thr=5.0,
                top_m=5, top_pages=7, kb_top=20, kb_cap=100, seg_thr=0.1, page_thr=0.1, kb_thr=0.01,
                page_fmt="lib", query_kw=False),
}
N_KEYWORDS = 3
THRESHOLD_PCTL = 0.35       # raw 0.1 on all-MiniLM-L6-v2 -> ~35th percentile (see docstring)
META_MAX_CHARS = 300
FACT_MIN_WORDS = 4


def _locomo_ts(dt) -> str:
    """LoCoMo session_N_date_time style, e.g. '1:56 pm on 8 May, 2023'."""
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d} {'am' if dt.hour < 12 else 'pm'} on {dt.day} {dt.strftime('%B')}, {dt.year}"


class _Page:
    __slots__ = ("u_tid", "r_tid", "u", "r", "ts", "sid", "pid", "meta", "meta_tids", "seg", "analyzed")

    def __init__(self, u_tid, r_tid, u, r, ts, sid):
        self.u_tid, self.r_tid, self.u, self.r, self.ts, self.sid = u_tid, r_tid, u, r, ts, sid
        self.pid = -1
        self.meta, self.meta_tids, self.seg, self.analyzed = "", [], -1, False


class MemoryOS(System):
    name = "memoryos"
    family = "memory-os"
    faithful_notes = (
        "Reproduces MemoryOS's paper/eval retrieval (eval/ harness behind the LoCoMo numbers). QA pages are built "
        "exactly as the harness pairs turns (first speaker opens a page, the other's next turn fills/overwrites the "
        "reply, one-sided pages dropped, session-date timestamp). STM=1, so each page goes straight to mid-term. "
        "Pages join topic segments when best cosine(user line, frozen segment vector) + overlap-coefficient of "
        "top-3 tf-idf keywords >= 0.6, else they open a new segment. Heat 0.8N+0.8L+0.0001R (eval's L-increment "
        "bug kept); when the hottest segment reaches 5 its unanalysed pages are promoted into an unbounded fact KB "
        "(LLM extraction approximated by >=4-word sentences of both speakers, dated). Query: top-5 segments by "
        "cosine, + query-keyword overlap, keep >= threshold; score every page in them by cosine of the eval's "
        "re-embedded text '{u}{ts}{r}'; global top-10 pages, then KB top-10. Thresholds 0.1 mapped to the 35th "
        "percentile of the query's cosines (raw 0.1 is far stricter on this embedder); merge theta 0.6 kept raw "
        "(~99th pctl of pairs here, as on MiniLM). Item text = eval's '【Historical Memory】' block with a "
        "meta_info proxy (nearest previous same-session chain lines, <=300 chars) and its turns. Omitted: the "
        "LLM user profile dump and the unranked assistant-knowledge dump (assistant-side sentences share the "
        "ranked KB instead). No recency (disabled in eval); q.time unused; returns <=20 items by design."
    )

    def __init__(self, mode: str = "paper", threshold_mode: str = "pctl", meta: str = "chain"):
        assert mode in PARAMS and threshold_mode in ("pctl", "raw") and meta in ("chain", "none")
        self.mode, self.threshold_mode, self.meta_mode = mode, threshold_mode, meta
        self.P = PARAMS[mode]

    # ------------------------------------------------------------ helpers
    def _keywords(self, text: str) -> set[str]:
        c = Counter(terms(text))
        if not c:
            return set()
        sc = sorted(c.items(), key=lambda kv: (-kv[1] * self.idf.get(kv[0], self.idf_unk), kv[0]))
        return {t for t, _ in sc[:N_KEYWORDS]}

    def _kw_score(self, a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        inter = len(a & b)
        if self.P["kw_mode"] == "overlap":
            return 0.5 * (inter / len(a) + inter / len(b))
        return inter / len(a | b)

    def _page_text_embed(self, p: _Page) -> str:
        if self.P["page_fmt"] == "paper":
            return f"{p.u}{p.ts}{p.r}"
        return f"User: {p.u} Assistant: {p.r}"

    def _cut(self, scores: np.ndarray, raw: float) -> float:
        if self.threshold_mode == "raw" or len(scores) == 0:
            return raw
        return float(np.quantile(scores, THRESHOLD_PCTL))

    # ------------------------------------------------------------ build
    def _make_pages(self, corpus) -> list[_Page]:
        user_sp = corpus.speakers[0] if corpus.speakers else None
        order = {s: i for i, s in enumerate(corpus.session_order)}
        turns = sorted(corpus.turns, key=lambda t: (order.get(t.session_id, 0), t.idx))
        pages: list[_Page] = []
        for t in turns:
            ts = _locomo_ts(corpus.session_ts.get(t.session_id, t.ts))
            if t.speaker == user_sp:
                pages.append(_Page(t.id, None, t.text, "", ts, t.session_id))
            elif pages:
                pages[-1].r, pages[-1].r_tid = t.text, t.id     # overwrite, as the harness does
            else:
                pages.append(_Page(None, t.id, "", t.text, ts, t.session_id))
        return pages

    def build(self, corpus, enc):
        self.enc = enc
        self.by_id = corpus.by_id()
        self.speakers = list(corpus.speakers) if corpus.speakers else ["User", "Assistant"]
        raw_pages = self._make_pages(corpus)
        # STM eviction drops pages with an empty side
        pages = [p for p in raw_pages if p.u and p.r]
        for i, p in enumerate(pages):
            p.pid = i
        self.pages = pages

        # keyword IDF over page user lines (stand-in for the LLM's keyword choice)
        df = Counter()
        for p in pages:
            df.update(set(terms(p.u)))
        n = max(1, len(pages))
        self.idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
        self.idf_unk = math.log(n + 1) + 1.0

        # dialogue chain proxy: previous kept page in the same session
        if self.meta_mode == "chain":
            for i, p in enumerate(pages):
                lines, tids, total, full = [], [], 0, False
                j = i - 1
                while j >= 0 and pages[j].sid == p.sid and not full:
                    q = pages[j]
                    for tid, spk, txt in ((q.r_tid, self.speakers[1], q.r), (q.u_tid, self.speakers[0], q.u)):
                        line = f"{spk}: {txt}"
                        if not lines and len(line) > META_MAX_CHARS:
                            lines.insert(0, line[:META_MAX_CHARS]); tids.append(tid); full = True
                            break
                        if total + len(line) > META_MAX_CHARS:
                            full = True
                            break
                        lines.insert(0, line); tids.append(tid); total += len(line) + 1
                    j -= 1
                p.meta, p.meta_tids = " ".join(lines), tids

        # embeddings
        U = enc.embed_many([p.u for p in pages]) if pages else np.zeros((0, 1))
        self.PE = enc.embed_many([self._page_text_embed(p) for p in pages]) if pages else np.zeros((0, 1))

        # incremental segment assignment + heat-triggered promotion
        a, b, g = self.P["heat"]
        seg_vec: list[np.ndarray] = []
        seg_kw: list[set] = []
        seg_pages: list[list[int]] = []
        seg_L: list[float] = []
        seg_H: list[float] = []
        kb: list[tuple[str, str]] = []      # (fact text, turn id)
        kb_cap = self.P["kb_cap"]
        stm = deque()

        def heat(L):
            return a * 0 + b * L + g * 1.0

        def insert(p: _Page):
            v, K = U[p.pid], self._keywords(p.u)
            best, best_sim, argbest = None, -1.0, None
            if seg_vec:
                sims = np.stack(seg_vec) @ v
                if self.P["kw_mode"] == "overlap":            # eval: argmax by cosine, then add keywords
                    bi = int(np.argmax(sims))
                    argbest = bi
                    if sims[bi] >= 0:
                        best, best_sim = bi, float(sims[bi]) + self._kw_score(K, seg_kw[bi])
                else:                                          # lib: argmax of cosine + Jaccard
                    tot = sims + np.array([self._kw_score(K, k) for k in seg_kw])
                    bi = int(np.argmax(tot))
                    best, best_sim = bi, float(tot[bi])
            if best is not None and best_sim >= self.P["theta"]:
                seg_pages[best].append(p.pid)
                p.seg = best
                if self.mode == "lib":
                    seg_L[best] += 1
                    seg_H[best] = heat(seg_L[best])
            else:
                seg_vec.append(v); seg_kw.append(K); seg_pages.append([p.pid])
                seg_L.append(1.0); seg_H.append(heat(1.0))
                p.seg = len(seg_vec) - 1
            if self.mode == "paper" and argbest is not None:  # eval bug: best_sid's L grows either way
                seg_L[argbest] += 1
                seg_H[argbest] = heat(seg_L[argbest])

        def promote():
            if not seg_H:
                return
            top = int(np.argmax(seg_H))                       # ties -> earliest segment
            if seg_H[top] < self.P["heat_thr"]:
                return
            un = [pid for pid in seg_pages[top] if not pages[pid].analyzed]
            if not un:
                return
            for pid in un:
                q = pages[pid]
                date = self.by_id[q.u_tid].ts.strftime("%d %B %Y").lstrip("0")
                for tid, spk, txt in ((q.u_tid, self.speakers[0], q.u), (q.r_tid, self.speakers[1], q.r)):
                    for s in sentences(txt) or [txt]:
                        if len(words(s)) >= FACT_MIN_WORDS:
                            kb.append((f"{spk}: {s} ({date})", tid))
            for pid in seg_pages[top]:
                pages[pid].analyzed = True
            seg_L[top] = 0.0
            seg_H[top] = 0.0 if self.mode == "paper" else heat(0.0)

        for p in pages:
            if self.mode == "lib":
                # Memoryos.add_memory: if full BEFORE append, pop exactly one page to MTM, then append
                if len(stm) >= self.P["stm_cap"]:
                    insert(stm.popleft())
                stm.append(p)
            else:
                # eval: add, then while is_full() pop everything evicted (cap 1 -> STM empties)
                stm.append(p)
                while len(stm) >= self.P["stm_cap"]:
                    insert(stm.popleft())
            promote()
        if kb_cap is not None and len(kb) > kb_cap:
            kb = kb[-kb_cap:]

        self.stm = list(stm)                                  # empty in paper mode
        self.seg_V = np.stack(seg_vec) if seg_vec else np.zeros((0, U.shape[1] if len(U) else 1))
        self.seg_kw, self.seg_pages = seg_kw, seg_pages
        self.kb = kb
        self.KE = enc.embed_many([f for f, _ in kb]) if kb else None

    # ------------------------------------------------------------ retrieve
    def _page_item(self, p: _Page, score: float) -> Item:
        A, B = self.speakers[0], self.speakers[1]
        if self.P["page_fmt"] == "paper":
            text = (f"【Historical Memory】 {A}: {p.u}\n{B}: {p.r}\nTime:({p.ts})\n"
                    f"Conversation chain overview:({p.meta})\n")
        else:
            text = (f"【Historical Memory】\nUser: {p.u}\nAssistant: {p.r}\nTime: {p.ts}\n"
                    f"Conversation chain overview: {p.meta or 'N/A'}")
        tids = [p.u_tid, p.r_tid] + [t for t in p.meta_tids if t not in (p.u_tid, p.r_tid)]
        return Item(turn_ids=list(dict.fromkeys(tids)), text=text, score=score,
                    meta={"tier": "mtm", "segment": p.seg})

    def retrieve(self, q, k=30):
        out: list[Item] = []
        # STM (always returned whole; empty in paper mode)
        for p in reversed(self.stm):
            out.append(self._page_item(p, 1.0))
        qv = self.enc.embed(q.text)

        # MTM: segment routing then page scoring
        if len(self.seg_V):
            scos = self.seg_V @ qv
            seg_cut = self._cut(scos, self.P["seg_thr"])
            qk = self._keywords(q.text) if self.P["query_kw"] else set()
            top_m = np.argsort(-scos, kind="stable")[: min(self.P["top_m"], len(scos))]
            pcos_all = self.PE @ qv
            page_cut = self._cut(pcos_all, self.P["page_thr"])
            cands = []
            for si in top_m:
                sc = float(scos[si]) + self._kw_score(qk, self.seg_kw[si])
                if sc < seg_cut:
                    continue
                for pid in self.seg_pages[si]:
                    if pcos_all[pid] >= page_cut:
                        cands.append((float(pcos_all[pid]), pid))
            cands.sort(key=lambda x: (-x[0], x[1]))
            for s, pid in cands[: self.P["top_pages"]]:
                out.append(self._page_item(self.pages[pid], s))

        # LPM: user KB
        if self.KE is not None:
            kcos = self.KE @ qv
            kb_cut = self._cut(kcos, self.P["kb_thr"])
            order = np.argsort(-kcos, kind="stable")[: self.P["kb_top"]]
            for i in order:
                if kcos[i] >= kb_cut:
                    f, tid = self.kb[i]
                    out.append(Item(turn_ids=[tid], text=f + "\n", score=float(kcos[i]), meta={"tier": "lpm"}))
        return out[:k]
