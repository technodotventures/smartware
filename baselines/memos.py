"""MemOS (MemTensor/MemOS, commit a7367d07, "MemOS 2.0") LLM-free retrieval baseline.

Implements section 13 of /home/claude/mp/reports/memos.md. It reproduces the DEFAULT server search
(POST /product/search with mode="fast", dedup="mmr", relativity=0.45, rerank=True, which is what the
project's own LoCoMo/LongMemEval harness calls with SEARCH_MODE=fast). The search runs over the memories
that MemOS stores WITHOUT an LLM: the fast-mode "raw window" items written synchronously by add() before the
async fine-extraction job replaces them.

FAITHFUL
- Units = MemOS fast-mode windows (mem_reader/multi_modal_struct.py:194-430, read_multi_modal/user_parser.py:150-212):
  - The eval harness adds 2 messages per call (batch_size=2, evaluation/scripts/locomo/locomo_ingestion.py,
    lme_ingestion.py), so each add batch = 2 consecutive turns of a session.
  - Each message becomes the line f"{role}: [{chat_time}]: {content}\n", where chat_time = the session date as
    ISO-8601 UTC (the harness sends session_date.isoformat()) and content is truncated to 8000 chars (LME harness).
  - Items over 1024 tokens are split with the sentence chunker (chunk_size 512, overlap 128 tokens).
  - Lines are packed into windows of <= 1024 tokens with the 200-token front-pop overlap rule.
  - The window text is the concatenation of its lines.
  - Bucket: UserMemory if every line is from a "user" role, else LongTermMemory.
- Candidate generation = path B of Searcher._retrieve_from_long_term_and_user (tree_text_memory/retrieve/searcher.py:733):
  - K_ret = 3*k, because dedup="mmr" triples top_k (search_handler.py:91).
  - Per bucket, take the dense top 2*K_ret by cosine with the query embedding (recall.py:370).
  - Union the buckets by id. The cosine_local reranker (reranker/cosine_local.py; all level weights are 1.0)
    rescores by plain cosine and keeps the top K_ret.
  - WorkingMemory is empty: the WM copy is disabled in MemoryManager._add_memories_batch. Graph, BM25,
    fulltext, internet, tool, skill and preference paths are off or LLM-only by default.
- post_retrieve: exact-text dedupe keeping the max score, sort, keep the top K_ret (searcher.py:1148-1280).
- Relevance floor: drop items with relativity < 0.45 (search_handler.py:254). Converted to a percentile; see
  THRESHOLDS.
- MMR (search_handler.py:331-565), exact constants:
  - Prefill min(2, k) by relevance, skipping exact-text duplicates and "highly similar" items.
  - Then pick argmax of 0.8*rel - 0.2*div, where div = d*exp(10*(d-0.9)) if d > 0.9 else d, and
    d = max cosine to the already selected items.
  - "Highly similar" = cosine to the most similar selected item > 0.9 AND
    0.40*Dice(char sets) + 0.35*charTFIDFcos (idf 1.0 if the char is shared, else 1.5)
    + 0.25*Jaccard(char bigrams) >= 0.92.
  - Stop at k picks, then re-sort the picks by relevance.
- rerank_knowledge_mem: no file-sourced knowledge memories here, so the order is unchanged and capped.
- Item.text = the stored memory string, which is exactly what the harness concatenates into the context
  ("\n".join(memories)). turn_ids = the turns whose lines are in the window. score = relativity (cosine).
- Returns FEWER than k when the 0.45 floor or MMR near-duplicate skipping leaves fewer, as MemOS does.
- No recency, decay or time filter: MemOS has none, and `reference_time` is unused in its code. q.time is unused.

APPROXIMATED
- LLM fine extraction (key/value/tags facts) is NOT emulated. In MemOS the fast windows are exactly what is
  searchable until the scheduler finishes, so this is MemOS's own LLM-free state, not a proxy.
- Tokens are counted with bench.text.count_tokens (MemOS uses tiktoken cl100k), and the sentence chunker is
  bench.text.sentences packed greedily (MemOS uses chonkie). This only matters for very long LME messages.
- The project's LoCoMo harness ingests every conversation twice, once into a cube per speaker, with roles
  user/assistant by perspective and each speaker searched with top_k each.
  - Here there is ONE pool. role = the corpus speaker name (LoCoMo) or user/assistant (LME), and a window
    whose lines all come from one LoCoMo speaker counts as UserMemory.
  - Both buckets get identical parameters and are unioned before reranking, so bucketing only changes the
    per-bucket pool cap.

THRESHOLDS
- relativity 0.45 is a raw cosine on the Docker-default embedder (DashScope text-embedding-v4, Qwen3-Embedding
  family, 1024-d). On that family a query's cosines to unrelated chat text are about 0.2-0.4 and related about
  0.5+, so 0.45 is about the 90th percentile of a query's cosines over a conversation.
  - Mapped to the 90th percentile of this query's cosines over all units in scope (threshold_mode="pctl",
    THRESHOLD_PCTL=0.90).
  - DEV check (dev corpora only): raw 0.45 on this embedder keeps 0.05% of query-unit pairs on LoCoMo and
    0-1.3% on LME, i.e. almost nothing. That is why it is converted.
  - threshold_mode="raw" keeps the literal 0.45.
- The MMR / near-duplicate gates at cosine 0.9 are kept raw. DEV check: on this embedder 0.9 is about the
  99.9th percentile of unit-unit similarity (0.01-0.3% of pairs), i.e. near-duplicates only, matching its role
  in MemOS.
- The MMR relevance/diversity balance uses raw cosines, as MemOS does.

PARAMETERS: k (caller); K_ret = 3k; per-bucket pool 2*K_ret; window 1024 tokens, overlap 200; split items
> 1024 tokens into 512-token sentence chunks with 128 overlap; relativity percentile 0.90; MMR lambda 0.8,
prefill 2, penalty start 0.9, alpha 10; near-dup gate 0.9 / 0.92 with weights 0.40/0.35/0.25.

OPTIONS (not default):
- threshold_mode="raw"
- backfill=True: refill to k from below-floor items, as section 13 step 6 suggests for recall@k. MemOS does not
  do this.
- hybrid=True: an added BM25 top-2*K_ret candidate channel over unit text, rescored by cosine, per section 13
  step 10. This is not a MemOS default; MemOS's BM25 is off and indexes key+tags only.
"""
from __future__ import annotations

import math
from collections import Counter

import numpy as np

from bench.system import Item, System
from bench.text import count_tokens, sentences

# ---------------------------------------------------------------- MemOS constants
CONTENT_MAX_CHARS = 8000          # harness msg["content"][:8000]
WINDOW_MAX_TOKENS = 1024          # SimpleStructMemReader.chat_window_max_tokens default
WINDOW_OVERLAP = 200              # _concat_multi_modal_memories(overlap=200)
SPLIT_CHUNK, SPLIT_OVERLAP = 512, 128   # sentence chunker config (api/config.py:1051-1052)
ADD_BATCH = 2                     # harness batch_size=2
DEDUP_MULT = 3                    # top_k * 3 when dedup in (sim, mmr)
POOL_MULT = 2                     # graph_retriever.retrieve(top_k=top_k*2)
RELATIVITY = 0.45                 # APISearchRequest.relativity default
THRESHOLD_PCTL = 0.90             # 0.45 on text-embedding-v4 -> percentile (see docstring)
MMR_LAMBDA = 0.8
MMR_SIM_THRESHOLD = 0.9
MMR_ALPHA = 10.0
NEARDUP_EMB = 0.9
NEARDUP_TEXT = 0.92


def _dice(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    s1, s2 = set(a), set(b)
    return 2 * len(s1 & s2) / (len(s1) + len(s2))


def _bigram(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    g1 = {a[i:i + 2] for i in range(len(a) - 1)} if len(a) >= 2 else {a}
    g2 = {b[i:i + 2] for i in range(len(b) - 1)} if len(b) >= 2 else {b}
    u = len(g1 | g2)
    return len(g1 & g2) / u if u else 0.0


def _tfidf(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    t1, t2 = Counter(a), Counter(b)
    vocab = set(t1) | set(t2)
    idf = {c: (1.0 if c in t1 and c in t2 else 1.5) for c in vocab}
    v1 = {c: t1.get(c, 0) * idf[c] for c in vocab}
    v2 = {c: t2.get(c, 0) * idf[c] for c in vocab}
    dot = sum(v1[c] * v2[c] for c in vocab)
    n1 = math.sqrt(sum(x * x for x in v1.values()))
    n2 = math.sqrt(sum(x * x for x in v2.values()))
    return dot / (n1 * n2) if n1 and n2 else 0.0


class MemOS(System):
    name = "memos"
    family = "memory-os"
    faithful_notes = (
        "Reproduces MemOS default /product/search (mode=fast, dedup=mmr, relativity 0.45, cosine_local rerank) over "
        "the memories MemOS stores without an LLM: fast-mode windows built from the harness's 2-message add batches, "
        "lines 'role: [ISO session time]: content', packed to <=1024 tokens (200 overlap; >1024-token items split "
        "into 512/128 sentence chunks), bucketed UserMemory/LongTermMemory. Per bucket the dense top 2*K_ret "
        "(K_ret=3k) is unioned, rescored by cosine, deduped by exact text and cut to K_ret. The 0.45 relativity "
        "floor is applied (mapped to the 90th percentile of the query's cosines, since raw 0.45 removes ~everything "
        "on this embedder). Then MemOS's MMR (lambda 0.8, prefill 2, exp penalty alpha 10 above 0.9, char "
        "Dice/TF-IDF/bigram near-dup gate 0.92 behind emb>0.9) picks <=k, re-sorted by relevance. Approximations: "
        "LLM fact extraction not emulated (fast windows are MemOS's own pre-LLM state), shared token counter and "
        "sentence splitter, one pool instead of the harness's per-speaker cubes. No recency/decay (none in MemOS); "
        "q.time unused; may return fewer than k by design."
    )

    def __init__(self, threshold_mode: str = "pctl", backfill: bool = False, hybrid: bool = False):
        assert threshold_mode in ("pctl", "raw")
        self.threshold_mode = threshold_mode
        self.backfill = backfill
        self.hybrid = hybrid

    # -------------------------------------------------------------- build (fast-mode add)
    @staticmethod
    def _split_large(text: str) -> list[str]:
        """_split_large_memory_item: sentence chunker, chunk 512 tokens, overlap 128."""
        sents = sentences(text) or [text]
        chunks, buf = [], []
        for s in sents:
            if buf and count_tokens(" ".join(buf + [s])) > SPLIT_CHUNK:
                chunks.append(" ".join(buf))
                while buf and count_tokens(" ".join(buf)) > SPLIT_OVERLAP:
                    buf.pop(0)
            buf.append(s)
        if buf:
            chunks.append(" ".join(buf))
        return [c for c in chunks if c.strip()] or [text]

    def _windows(self, items: list[tuple[str, str, str]]):
        """_concat_multi_modal_memories over one add batch. items = (turn_id, role, line)."""
        processed = []
        for tid, role, line in items:
            if count_tokens(line) > WINDOW_MAX_TOKENS:
                processed += [(tid, role, c) for c in self._split_large(line)]
            else:
                processed.append((tid, role, line))
        if len(processed) == 1:
            return [processed]
        windows, buf = [], []
        cur = ""
        for it in processed:
            line = it[2] if it[2].endswith("\n") else it[2] + "\n"
            if count_tokens(cur + line) > WINDOW_MAX_TOKENS and cur:
                windows.append(list(buf))
                while buf and count_tokens("".join(x[2] for x in buf)) > WINDOW_OVERLAP:
                    buf.pop(0)
            buf.append((it[0], it[1], line))
            cur = "".join(x[2] for x in buf)
        if buf:
            windows.append(list(buf))
        return windows

    def build(self, corpus, enc):
        self.enc = enc
        lme_like = set(corpus.speakers) == {"user", "assistant"}
        by_sess: dict[str, list] = {}
        for t in corpus.turns:
            by_sess.setdefault(t.session_id, []).append(t)
        order = {s: i for i, s in enumerate(corpus.session_order)}
        texts, tids, buckets = [], [], []
        for sid in sorted(by_sess, key=lambda s: order.get(s, 1 << 30)):
            turns = sorted(by_sess[sid], key=lambda t: t.idx)
            chat_time = corpus.session_ts[sid].isoformat() + "+00:00"
            for i in range(0, len(turns), ADD_BATCH):
                batch = [(t.id, t.role, f"{t.speaker}: [{chat_time}]: {t.text[:CONTENT_MAX_CHARS]}\n")
                         for t in turns[i:i + ADD_BATCH]]
                for w in self._windows(batch):
                    text = "".join(x[2] for x in w)
                    if not text.strip():
                        continue
                    roles = {x[1] for x in w}
                    if lme_like:
                        bucket = "UserMemory" if roles == {"user"} else "LongTermMemory"
                    else:
                        bucket = "UserMemory" if len(roles) == 1 else "LongTermMemory"
                    texts.append(text)
                    tids.append(list(dict.fromkeys(x[0] for x in w)))
                    buckets.append(bucket)
        self.texts, self.tids = texts, tids
        self.buckets = np.array(buckets)
        self.E = enc.embed_many(texts).astype(np.float32)
        self.bm25 = enc.bm25(texts) if self.hybrid else None

    # -------------------------------------------------------------- search
    def _near_dup(self, i: int, selected: list[int], S: np.ndarray, cand: list[int]) -> bool:
        if not selected:
            return False
        j = max(selected, key=lambda s: S[i, s])
        if S[i, j] <= NEARDUP_EMB:
            return False
        a, b = self.texts[cand[i]].strip(), self.texts[cand[j]].strip()
        return 0.40 * _dice(a, b) + 0.35 * _tfidf(a, b) + 0.25 * _bigram(a, b) >= NEARDUP_TEXT

    def retrieve(self, q, k=30):
        if not self.texts:
            return []
        K = k
        k_ret = DEDUP_MULT * K
        pool_k = POOL_MULT * k_ret
        cos = self.E @ self.enc.embed(q.text)

        # path B: per-bucket dense recall (+ optional BM25 channel), union by id
        cand: set[int] = set()
        bm = self.bm25.score(q.text) if self.bm25 is not None else None
        for b in ("LongTermMemory", "UserMemory"):
            idx = np.where(self.buckets == b)[0]
            if not len(idx):
                continue
            cand.update(idx[np.argsort(-cos[idx])[:pool_k]].tolist())
            if bm is not None:
                sb = bm[idx]
                top = idx[np.argsort(-sb)[:pool_k]]
                cand.update(int(i) for i in top if bm[i] > 0)
        # cosine_local rerank -> top K_ret
        ranked = sorted(cand, key=lambda i: -cos[i])[:k_ret]
        # post_retrieve: exact-text dedupe (max score), sort, trim
        seen, deduped = set(), []
        for i in ranked:
            if self.texts[i] in seen:
                continue
            seen.add(self.texts[i])
            deduped.append(i)
        deduped = deduped[:k_ret]

        # relativity floor
        thr = RELATIVITY if self.threshold_mode == "raw" else float(np.quantile(cos, THRESHOLD_PCTL))
        kept = [i for i in deduped if cos[i] >= thr]
        dropped = [i for i in deduped if cos[i] < thr]
        if not kept:
            return []

        # MMR dedup (search_handler._mmr_dedup_text_memories)
        rel = np.array([cos[i] for i in kept], dtype=np.float64)
        Ek = self.E[kept]
        S = Ek @ Ek.T
        n = len(kept)
        selected: list[int] = []
        sel_text: set[str] = set()
        prefill = min(2, K)
        for li in sorted(range(n), key=lambda x: -rel[x]):
            if len(selected) >= prefill:
                break
            t = self.texts[kept[li]].strip()
            if t in sel_text or self._near_dup(li, selected, S, kept):
                continue
            selected.append(li)
            sel_text.add(t)
        remaining = set(range(n)) - set(selected)
        while remaining and len(selected) < K:
            best, best_mmr = None, None
            for li in remaining:
                t = self.texts[kept[li]].strip()
                if t in sel_text or self._near_dup(li, selected, S, kept):
                    continue
                d = max((S[li, j] for j in selected), default=0.0)
                div = d * math.exp(MMR_ALPHA * (d - MMR_SIM_THRESHOLD)) if d > MMR_SIM_THRESHOLD else d
                m = MMR_LAMBDA * rel[li] - (1 - MMR_LAMBDA) * div
                if best_mmr is None or m > best_mmr:
                    best, best_mmr = li, m
            if best is None:
                break
            selected.append(best)
            sel_text.add(self.texts[kept[best]].strip())
            remaining.discard(best)
            if len(selected) >= min(K, n):
                break
        selected.sort(key=lambda x: -rel[x])
        out = [kept[li] for li in selected][:K]

        items = [Item(list(self.tids[i]), self.texts[i], float(cos[i]),
                      meta={"memory_type": str(self.buckets[i])}) for i in out]
        if self.backfill and len(items) < K:
            for i in dropped:
                if len(items) >= K:
                    break
                items.append(Item(list(self.tids[i]), self.texts[i], float(cos[i]),
                                  meta={"memory_type": str(self.buckets[i]), "backfill": True}))
        return items
