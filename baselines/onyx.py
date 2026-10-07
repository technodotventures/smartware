"""Onyx (onyx-dot-app/onyx @744ca37, OpenSearch backend): LLM-free retrieval baseline.

Implements section 13 of /home/claude/mp/reports/onyx.md. Without the LLM stages, Onyx's agentic search tool
(SearchTool.run) reduces to: per-query OpenSearch hybrid retrieval -> weighted RRF -> adjacent-chunk section merging.
As instructed, there is no LLM section selection: merged sections are returned in fused-score order.

FAITHFUL (project citations)
- Document model. One Onyx document per session, as the Slack connector builds one per thread
  (connectors/slack/connector.py:300-360):
  - one TextSection per turn, holding the turn text only (no speaker; Slack indexes message text only);
  - semantic_identifier = "{first speaker} in #{channel}: {first 50 chars}[...]"; the channel is the session id;
  - metadata = {"Channel": channel};
  - doc_updated_at = last turn time; doc_created_at = first turn time.
- Chunking (indexing/chunker.py, indexing/chunking/text_section_chunker.py):
  - chunk_token_limit = 512, overlap = 0;
  - title_prefix = title + "\\n\\r\\n" (title blurb <= 128 tokens);
  - semantic metadata suffix "\\n\\r\\nMetadata:\\n\\tChannel - x" is used for the embedding;
  - keyword suffix "\\n\\r\\nx" is used for BM25;
  - the semantic suffix is dropped if >= 0.25*512 tokens;
  - content_limit = 512 - title - metadata; if <= 256 (CHUNK_MIN_CONTENT), fall back to 512 with no title and no
    semantic suffix;
  - sections are packed greedily with "\\n\\n" (the separator is counted even for an empty buffer);
  - a section that does not fit flushes the buffer;
  - an oversized section flushes, then is sentence-split into pieces <= content_limit (one sentence stays whole
    when STRICT_CHUNK_TOKEN_LIMIT is false).
  - Contextual RAG is off (the default; it needs an LLM).
- Index text (document_index/chunk_content_enrichment.py):
  - content = title_prefix + chunk + keyword_suffix;
  - title field = title;
  - embedded text = title_prefix + chunk + semantic_suffix, trimmed to 512 tokens (retrim_content).
- Hybrid query (document_index/opensearch/search.py, default CONTENT_VECTOR_TITLE_CONTENT_COMBINED_KEYWORD +
  MIN_MAX):
  - subquery 1: knn(content_vector, k=500);
  - subquery 2: bool.should, min_should_match=1:
    - match(title) with boost 0.1;
    - match_phrase(title, slop=1) with boost 0.2;
    - match(content) with boost 1.0;
    - match_phrase(content, slop=1) with boost 1.5.
  - The keyword text is the query with stopwords stripped (pipeline.py strip_stopwords) and space-joined.
  - Each subquery keeps its own top 500 (pagination_depth). Each is min-max normalised over its own result list
    (OpenSearch MinMax: a 0 becomes 0.001; a single-valued list becomes 1.0). A doc missing from one subquery
    scores 0 there.
  - The two are combined by weighted arithmetic mean [0.5, 0.5]; the top num_hits = 50 are kept.
  - No time decay, no recency bias, no doc boost, no rerank (none is applied in the OpenSearch path).
  - hybrid_alpha=0.2 for keyword queries is a no-op in Onyx, so those queries also run as plain hybrid.
- Fusion (tools/tool_implementations/search/search_utils.py):
  - weighted RRF, score = sum w/(50 + rank), id = (doc, chunk);
  - ties go to the lower first-seen rank, then the earlier list;
  - queries that are equal case-insensitively are merged, with their weights summed.
- Sections (context/search/pipeline.py merge_individual_chunks):
  - adjacent chunk ids of the same doc merge into one section;
  - each section sits at the position of its best-ranked chunk;
  - the first num_hits = 50 are kept;
  - combined_content = "\\n".join(chunk contents).
- Item text = the per-section JSON object of convert_inference_sections_to_llm_string
  (tools/tool_implementations/utils.py), compact separators:
  {"document": citation id per doc, "title", "updated_at" (ISO, UTC), "source_type": "slack",
   "content" (cleaned chunk text: title prefix and metadata suffix stripped), "metadata": "{\\"Channel\\": ...}"}.

APPROXIMATED (no LLM)
- The LLM semantic rephrase, the LLM tool queries and the original query collapse into ONE query, the raw question,
  with weight 1.3 + 0.7 + 0.5 = 2.5. The rephrase prompt says the output should "in most cases be exactly the same as
  the last user query"; identical queries are merged with summed weights.
- The LLM keyword expansion (<= 3 keyword lines, weight 1.0 each) becomes one keyword line: the 3 highest-IDF
  non-stopword query terms, in query order, weight 1.0. A second line of the top 2 terms is optional
  (keyword_lines=2).
- OpenSearch "english" analyser -> the shared bench tokeniser/stemmer/stopwords. Okapi BM25 uses k1=1.2, b=0.75 (same
  as Lucene defaults); the shared BM25 idf is log(1+(N-df+.5)/(df+.5)), the same as Lucene's.
- match_phrase (slop 1) is reimplemented: sloppy frequency sum 1/(1+spread) over matches with spread <= 1. Positions
  are counted over all words, so stopword gaps are kept as Lucene keeps position increments. The phrase is scored with
  Lucene BM25 using the summed idf of its terms. A one-term phrase equals the term match.
- Token counts use bench.text.count_tokens instead of the embedding model's tokenizer. Sentence splitting uses
  bench.text.sentences instead of chonkie.
- The dense encoder is the shared one. There is no nomic query/passage prefix, and kNN is exact, not HNSW.
- The LLM time filter (auto_detect_filters) is not reproduced by default. A conservative regex stand-in uses q.time as
  "now" (Onyx uses wall-clock now) and applies Onyx's overlap rule (last_updated >= start AND created_at <= end). It is
  off by default (time_filter=True enables it).
- Multipass large chunks are not built (default self-hosted without GPU has multipass off; on OpenSearch, mini-chunk
  vectors are discarded and large chunks collide with normal chunk ids anyway).

DELIBERATE DEVIATIONS
- No LLM section selection (max 10) and no LLM context expansion (per coordinator instruction).
- Returned: the merged sections in fused order, up to min(k, 50) (rich_response.search_docs = top_sections[:num_hits]).
  The 25-section max_llm_chunks cap and the 25,600-token pre-selection trim only apply around the LLM selection step,
  so they are not applied.

PARAMETERS
- chunk_tokens=512, min_content=256, meta_pct=0.25, blurb=128, cand=500, w_vec=0.5, w_kw=0.5, num_hits=50, rrf_k=50,
  w_main=2.5, w_kw_line=1.0, kw_terms=3, keyword_lines=1, speaker_prefix=False, time_filter=False.
"""
from __future__ import annotations

import json
import math
import re
from calendar import monthrange
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np

from bench.system import Item, System
from bench.text import STOPWORDS, count_tokens, sentences, stem, words

RETURN_SEPARATOR = "\n\r\n"
SECTION_SEPARATOR = "\n\n"
_TOK = re.compile(r"\w+|[^\w\s]")
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def _trim_tokens(text: str, limit: int) -> str:
    """tokenizer_trim_content: keep roughly the first `limit` tokens (count_tokens counts regex tokens * 1.1)."""
    if count_tokens(text) <= limit:
        return text
    keep = int(limit / 1.1)
    ms = list(_TOK.finditer(text))
    if len(ms) <= keep:
        return text
    return text[: ms[keep - 1].end()]


def _strip_stopwords(text: str) -> list[str]:
    """pipeline.py strip_stopwords: drop words whose punctuation-trimmed lowercase core is a stopword."""
    out = []
    for w in text.split():
        core = re.sub(r"^[^\w']+|[^\w']+$", "", w)
        if core.lower() not in STOPWORDS:
            out.append(w)
    return out


def _positions(text: str) -> dict[str, list[int]]:
    """Analysed term -> positions; stopwords are dropped but keep their position (Lucene StopFilter)."""
    pos: dict[str, list[int]] = defaultdict(list)
    for i, w in enumerate(_WORD.findall(text.lower())):
        if w in STOPWORDS:
            continue
        pos[stem(w)].append(i)
    return pos


def _query_terms_with_offsets(text: str) -> list[tuple[str, int]]:
    out = []
    for i, w in enumerate(_WORD.findall(text.lower())):
        if w in STOPWORDS:
            continue
        out.append((stem(w), i))
    return out


def _sloppy_freq(qt: list[tuple[str, int]], pos: dict[str, list[int]], slop: int = 1) -> float:
    """Sloppy phrase frequency: for each anchor of the first term, find a match of every term within spread<=slop."""
    if not qt:
        return 0.0
    sets = []
    for t, o in qt:
        p = pos.get(t)
        if not p:
            return 0.0
        sets.append({x - o for x in p})
    freq = 0.0
    for v in sorted(sets[0]):
        best = None
        for lo in range(v - slop, v + 1):  # windows [lo, lo+slop] containing v
            hi = lo + slop
            chosen = []
            ok = True
            for s in sets:
                cands = [x for x in range(lo, hi + 1) if x in s]
                if not cands:
                    ok = False
                    break
                chosen.append(min(cands, key=lambda x: abs(x - v)))
            if ok:
                spread = max(chosen) - min(chosen)
                best = spread if best is None else min(best, spread)
        if best is not None:
            freq += 1.0 / (1.0 + best)
    return freq


class _Field:
    """A text field with BM25 (shared encoder) plus positional data for match_phrase."""

    def __init__(self, enc, texts: list[str], k1: float = 1.2, b: float = 0.75):
        self.bm25 = enc.bm25(texts)
        self.pos = [_positions(t) for t in texts]
        lens = np.array([sum(len(v) for v in p.values()) for p in self.pos], dtype=np.float32)
        self.norm = k1 * (1 - b + b * lens / max(float(lens.mean()) if len(lens) else 1.0, 1e-6))
        self.k1 = k1

    def match(self, q: str) -> np.ndarray:
        return self.bm25.score(q)

    def phrase(self, q: str, cand: np.ndarray) -> np.ndarray:
        out = np.zeros(self.bm25.n, dtype=np.float32)
        qt = _query_terms_with_offsets(q)
        if not qt:
            return out
        idf = 0.0
        for t, _ in qt:
            j = self.bm25.vocab.get(t)
            if j is None:
                return out
            idf += float(self.bm25.idf[j])
        for i in cand:
            f = _sloppy_freq(qt, self.pos[i])
            if f > 0:
                out[i] = idf * f * (self.k1 + 1) / (f + self.norm[i])
        return out


def _minmax(scores: np.ndarray) -> np.ndarray:
    """OpenSearch MinMaxScoreNormalizationTechnique over one subquery's result list."""
    lo, hi = float(scores.min()), float(scores.max())
    if hi == lo:
        return np.ones_like(scores)
    n = (scores - lo) / (hi - lo)
    n[n == 0.0] = 0.001
    return n


_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"], 1)}


def _time_window(q: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    """Conservative regex stand-in for decide_time_filter (explicit references only)."""
    s = q.lower()
    m = re.search(r"\b(?:in the )?(?:last|past) (\d+) (day|week|month|year)s?\b", s)
    if m:
        n, u = int(m.group(1)), m.group(2)
        d = {"day": 1, "week": 7, "month": 30, "year": 365}[u] * n
        return now - timedelta(days=d), None
    m = re.search(r"\b(\d+) (day|week|month|year)s? ago\b", s)
    if m:
        n, u = int(m.group(1)), m.group(2)
        d = {"day": 1, "week": 7, "month": 30, "year": 365}[u] * n
        day = now - timedelta(days=d)
        return datetime(day.year, day.month, day.day), datetime(day.year, day.month, day.day, 23, 59, 59)
    m = re.search(r"\blast (week|month|year)\b", s)
    if m:
        u = m.group(1)
        if u == "week":
            start = (now - timedelta(days=now.weekday() + 7)).replace(hour=0, minute=0, second=0, microsecond=0)
            return start, start + timedelta(days=6, hours=23, minutes=59, seconds=59)
        if u == "month":
            y, mo = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
            return datetime(y, mo, 1), datetime(y, mo, monthrange(y, mo)[1], 23, 59, 59)
        return datetime(now.year - 1, 1, 1), datetime(now.year - 1, 12, 31, 23, 59, 59)
    m = re.search(r"\b(since|before|in|during) (" + "|".join(_MONTHS) + r")(?:,? (\d{4}))?\b", s)
    if m:
        mo = _MONTHS[m.group(2)]
        y = int(m.group(3)) if m.group(3) else (now.year if mo <= now.month else now.year - 1)
        start, end = datetime(y, mo, 1), datetime(y, mo, monthrange(y, mo)[1], 23, 59, 59)
        if m.group(1) == "since":
            return start, None
        if m.group(1) == "before":
            return None, start - timedelta(seconds=1)
        return start, end
    m = re.search(r"\b(since|before|in|during) (\d{4})\b", s)
    if m:
        y = int(m.group(2))
        if m.group(1) == "since":
            return datetime(y, 1, 1), None
        if m.group(1) == "before":
            return None, datetime(y - 1, 12, 31, 23, 59, 59)
        return datetime(y, 1, 1), datetime(y, 12, 31, 23, 59, 59)
    return None, None


class Onyx(System):
    name = "onyx"
    family = "company-brain"
    faithful_notes = (
        "Onyx SearchTool without LLM stages. Each session is one Slack-style document: a turn per section, "
        "title '{speaker} in #{session}: snippet', metadata Channel. Chunks are 512 tokens with no overlap, a title "
        "prefix and a metadata suffix (semantic form for the embedding, keyword form for BM25), packed greedily by "
        "section. OpenSearch hybrid per query: knn(content, k=500) plus bool[match title 0.1, phrase title 0.2, match "
        "content 1.0, phrase content (slop 1) 1.5], each min-max normalised over its own top 500 (0 -> 0.001, "
        "missing = 0), combined 0.5/0.5, top 50. Weighted RRF (k=50) over the raw question (weight 2.5 = merged "
        "rephrase 1.3 + tool query 0.7 + original 0.5) and an IDF keyword line (weight 1.0, standing in for the "
        "LLM keyword expansion). Adjacent chunks merge into sections at their best rank. No decay, boost or rerank, "
        "since Onyx applies none. No LLM selection/expansion; sections are returned in fused order as Onyx's "
        "per-section JSON (document, title, updated_at, source_type, content, metadata). LLM time filter is off by "
        "default (regex stand-in optional)."
    )

    def __init__(self, chunk_tokens: int = 512, min_content: int = 256, meta_pct: float = 0.25, cand: int = 500,
                 w_vec: float = 0.5, w_kw: float = 0.5, num_hits: int = 50, rrf_k: int = 50, w_main: float = 2.5,
                 w_kw_line: float = 1.0, kw_terms: int = 3, keyword_lines: int = 1, speaker_prefix: bool = False,
                 time_filter: bool = False):
        self.chunk_tokens, self.min_content, self.meta_pct = chunk_tokens, min_content, meta_pct
        self.cand, self.w_vec, self.w_kw, self.num_hits, self.rrf_k = cand, w_vec, w_kw, num_hits, rrf_k
        self.w_main, self.w_kw_line, self.kw_terms, self.keyword_lines = w_main, w_kw_line, kw_terms, keyword_lines
        self.speaker_prefix, self.time_filter = speaker_prefix, time_filter

    # ------------------------------------------------------------------ indexing
    def _chunk_session(self, sid: str, turns) -> list[dict]:
        first = turns[0]
        first_msg = first.text
        snippet = first_msg[:50].rstrip() + "..." if len(first_msg) > 50 else first_msg
        title = f"{first.speaker} in #{sid}: {snippet}".replace("\n", " ")
        title = title.replace("\r", " ").strip()
        title_prefix = title + RETURN_SEPARATOR if title else ""
        meta_sem = RETURN_SEPARATOR + f"Metadata:\n\tChannel - {sid}"
        meta_kw = RETURN_SEPARATOR + sid
        title_tokens = count_tokens(title_prefix)
        meta_tokens = count_tokens(meta_sem)
        if meta_tokens >= self.chunk_tokens * self.meta_pct:
            meta_sem, meta_tokens = "", 0
        limit = self.chunk_tokens - title_tokens - meta_tokens
        if limit <= self.min_content:
            limit = self.chunk_tokens
            title_prefix, meta_sem = "", ""
        sep_tok = count_tokens(SECTION_SEPARATOR)

        payloads: list[tuple[str, list[str]]] = []
        acc_text, acc_ids = "", []
        for t in turns:
            text = f"{t.speaker}: {t.text}" if self.speaker_prefix else t.text
            text = text.strip()
            if not text:
                continue
            n = count_tokens(text)
            if n > limit:  # oversized: flush, then sentence-split
                if acc_text.strip():
                    payloads.append((acc_text, acc_ids))
                acc_text, acc_ids = "", []
                buf = ""
                for s in sentences(text) or [text]:
                    cand = (buf + " " + s).strip() if buf else s
                    if buf and count_tokens(cand) > limit:
                        payloads.append((buf, [t.id]))
                        buf = s
                    else:
                        buf = cand
                if buf:
                    payloads.append((buf, [t.id]))
                continue
            cur = count_tokens(acc_text) if acc_text else 0
            if sep_tok + n + cur <= limit:
                acc_text = acc_text + SECTION_SEPARATOR + text if acc_text else text
                acc_ids = acc_ids + [t.id]
            else:
                if acc_text.strip():
                    payloads.append((acc_text, acc_ids))
                acc_text, acc_ids = text, [t.id]
        if acc_text.strip():
            payloads.append((acc_text, acc_ids))

        out = []
        for ci, (text, ids) in enumerate(payloads):
            out.append(dict(doc=sid, cid=ci, content=text, turn_ids=ids, title=title,
                            kw_text=title_prefix + text + meta_kw,
                            emb_text=_trim_tokens(title_prefix + text + meta_sem, self.chunk_tokens)))
        return out

    def build(self, corpus, enc):
        self.enc = enc
        by_sess = defaultdict(list)
        for t in corpus.turns:
            by_sess[t.session_id].append(t)
        self.chunks: list[dict] = []
        self.doc_info: dict[str, tuple[datetime, datetime]] = {}
        for sid in corpus.session_order:
            ts = sorted(by_sess.get(sid, []), key=lambda x: (x.ts, x.idx))
            if not ts:
                continue
            self.doc_info[sid] = (ts[0].ts, ts[-1].ts)
            self.chunks.extend(self._chunk_session(sid, ts))
        self.content = _Field(enc, [c["kw_text"] for c in self.chunks])
        self.title = _Field(enc, [c["title"] for c in self.chunks])
        self.E = enc.embed_many([c["emb_text"] for c in self.chunks])
        self.key_to_idx = {(c["doc"], c["cid"]): i for i, c in enumerate(self.chunks)}
        self.created = np.array([self.doc_info[c["doc"]][0].timestamp() for c in self.chunks])
        self.updated = np.array([self.doc_info[c["doc"]][1].timestamp() for c in self.chunks])

    # ------------------------------------------------------------------ retrieval
    def _hybrid(self, query: str, mask: np.ndarray | None) -> list[int]:
        kw_words = _strip_stopwords(query)
        kw = " ".join(kw_words) if kw_words else query
        n = len(self.chunks)
        allowed = np.ones(n, bool) if mask is None else mask
        # vector subquery
        v = self.E @ self.enc.embed(query)
        v_idx = np.where(allowed)[0]
        v_idx = v_idx[np.argsort(-v[v_idx], kind="stable")][: self.cand]
        # keyword subquery: bool.should of 4 clauses, min_should_match=1
        mc = self.content.match(kw)
        mt = self.title.match(kw)
        any_hit = np.where(((mc > 0) | (mt > 0)) & allowed)[0]
        pc = self.content.phrase(kw, any_hit)
        pt = self.title.phrase(kw, any_hit)
        k = 1.0 * mc + 1.5 * pc + 0.1 * mt + 0.2 * pt
        k_idx = any_hit[np.argsort(-k[any_hit], kind="stable")][: self.cand]
        comb: dict[int, float] = defaultdict(float)
        if len(v_idx):
            for i, s in zip(v_idx, _minmax(v[v_idx])):
                comb[int(i)] += self.w_vec * float(s)
        if len(k_idx):
            for i, s in zip(k_idx, _minmax(k[k_idx])):
                comb[int(i)] += self.w_kw * float(s)
        tot = self.w_vec + self.w_kw
        ranked = sorted(comb, key=lambda i: -comb[i] / tot)
        return ranked[: self.num_hits]

    def _keyword_lines(self, q: str) -> list[str]:
        toks = []
        seen = set()
        for w in _WORD.findall(q.lower()):
            if w in STOPWORDS or w in seen:
                continue
            seen.add(w)
            j = self.content.bm25.vocab.get(stem(w))
            idf = float(self.content.bm25.idf[j]) if j is not None else 0.0
            toks.append((w, idf))
        lines = []
        for n in [self.kw_terms, self.kw_terms - 1][: self.keyword_lines]:
            if n <= 0 or not toks:
                continue
            top = {w for w, _ in sorted(toks, key=lambda x: -x[1])[:n]}
            lines.append(" ".join(w for w, _ in toks if w in top))
        return lines

    def retrieve(self, q, k=30):
        mask = None
        if self.time_filter:
            start, end = _time_window(q.text, q.time)
            if start or end:
                m = np.ones(len(self.chunks), bool)
                if start:
                    m &= self.updated >= start.timestamp()
                if end:
                    m &= self.created <= end.timestamp()
                if m.any():
                    mask = m
        # query set with case-insensitive dedupe (weights summed)
        qmap: dict[str, list] = {}
        for text, w in [(q.text, self.w_main)] + [(l, self.w_kw_line) for l in self._keyword_lines(q.text)]:
            key = text.lower()
            if key in qmap:
                qmap[key][1] += w
            else:
                qmap[key] = [text, w]
        lists = [(self._hybrid(text, mask), w) for text, w in qmap.values()]
        # weighted RRF
        rrf: dict[int, float] = defaultdict(float)
        first_rank: dict[int, int] = {}
        first_src: dict[int, int] = {}
        for si, (lst, w) in enumerate(lists):
            for r, i in enumerate(lst, start=1):
                rrf[i] += w / (self.rrf_k + r)
                if i not in first_rank:
                    first_rank[i], first_src[i] = r, si
        fused = sorted(rrf, key=lambda i: (-rrf[i], first_rank[i], first_src[i]))
        # merge_individual_chunks
        pos = {i: p for p, i in enumerate(fused)}
        by_doc = defaultdict(list)
        for i in fused:
            by_doc[self.chunks[i]["doc"]].append(i)
        chunk_to_sec: dict[int, tuple] = {}
        for doc, idxs in by_doc.items():
            idxs.sort(key=lambda i: self.chunks[i]["cid"])
            run = [idxs[0]]
            groups = []
            for a, b in zip(idxs, idxs[1:]):
                if self.chunks[b]["cid"] == self.chunks[a]["cid"] + 1:
                    run.append(b)
                else:
                    groups.append(run)
                    run = [b]
            groups.append(run)
            for g in groups:
                center = min(g, key=lambda i: pos[i])
                sec = (center, tuple(g))
                for i in g:
                    chunk_to_sec[i] = sec
        sections, seen = [], set()
        for i in fused:
            sec = chunk_to_sec[i]
            if sec[0] not in seen:
                seen.add(sec[0])
                sections.append(sec)
        sections = sections[: min(self.num_hits, k)]
        # convert_inference_sections_to_llm_string
        cite: dict[str, int] = {}
        items = []
        for center, group in sections:
            c = self.chunks[center]
            doc = c["doc"]
            if doc not in cite:
                cite[doc] = len(cite) + 1
            content = "\n".join(self.chunks[i]["content"] for i in group)
            upd = self.doc_info[doc][1]
            entry = {
                "document": cite[doc],
                "title": c["title"],
                "updated_at": upd.replace(microsecond=0).isoformat() + "+00:00",
                "source_type": "slack",
                "content": content,
                "metadata": json.dumps({"Channel": doc}, ensure_ascii=False),
            }
            tids = []
            for i in group:
                for t in self.chunks[i]["turn_ids"]:
                    if t not in tids:
                        tids.append(t)
            items.append(Item(tids, json.dumps(entry, separators=(",", ":"), ensure_ascii=False),
                              float(rrf[center]), {"doc": doc, "chunks": [self.chunks[i]["cid"] for i in group]}))
        return items
