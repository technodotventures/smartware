"""Basic Memory (basicmachines-co/basic-memory, HEAD 53d1c46, v0.23.x/0.24.0) LLM-free retrieval baseline.

Implements section 13 of /home/claude/mp/reports/basic-memory.md: ``search_notes(search_type="hybrid")`` with
reranking off, over corpora ingested the way Basic Memory's own benchmark harness ingests conversations
(benchmarks/src/basic_memory_benchmarks/converters/locomo_to_corpus.py, longmemeval_to_corpus.py).

FAITHFUL
- Unit = one Markdown note per session (the BM benchmark corpus format):
  - frontmatter title ``{doc_id} ({session_date})``;
  - body ``# Chat session at {date}`` / ``## Conversation`` / one ``- **{speaker}:** {text}`` line per turn.
  - Search defaults to ``entity_types=["entity"]`` (mcp/tools/search.py), so only note-level rows are returned.
    Observation and relation rows are never returned in this mode, so they are not built.
- Default mode is HYBRID. That is the default when the semantic deps are installed (config_models.py:64). The
  cross-encoder reranker is off by default and is not used.
- Keyword leg = SQLite FTS5 search_index (models/search.py, repository/sqlite_search_query.py).
  - Columns: title, content_stems, content_snippet, permalink. Tokeniser unicode61 (lowercase, diacritics folded,
    '/' is a token char). No stemming, no stopword removal.
  - content_stems = title variants + body + permalink variants + file-path variants, cut to
    MAX_CONTENT_STEMS_SIZE = 6000 chars (search_service.py:880-938). content_snippet = full body.
  - Query preparation follows ``prepare_single_term``:
    - edge punctuation ``?!.,;:`` is stripped;
    - plain multi-word text becomes ``w1* AND w2* ...`` (prefix + AND, stopwords included);
    - text with problematic characters (apostrophe, quotes, brackets, + ! @ # ...) or words containing
      ``. : ; , < > ? / -`` becomes one quoted phrase with a prefix on its last token;
    - a single word becomes ``word*``.
  - Match = ``title MATCH t OR content_stems MATCH t OR content_snippet MATCH t``.
  - Relaxed fallback (search_query.py ``relaxed_query_words``). It runs only when strict returns 0 rows and the
    query has no quotes, no AND/OR/NOT, at least 3 word tokens and no purely numeric token. It drops
    RELAXATION_STOPWORDS, dedupes, and builds an OR of prefix terms. The hybrid leg always allows it.
- FTS5 bm25 is REIMPLEMENTED, not taken from the shared BM25, because its semantics change the candidate set
  and the normalised score. The fts5_aux.c formula is:
  - k1 = 1.2, b = 0.75;
  - idf = ln((N - nHit + 0.5)/(nHit + 0.5)), floored at 1e-6;
  - D = token count of the whole row; avgD = mean row length.
  - The SQLite OR-of-column MATCH returns the bm25 of the FIRST branch (title, then stems, then snippet) that
    matches the row, with phrase frequencies and nHit restricted to that column. I verified this against
    stdlib sqlite3. A row matches a branch only if every AND phrase (or any OR phrase) occurs in that column.
- Vector leg (repository/search_reader.py, semantic_chunking.py):
  - Embedded source text = ``title\\n\\npermalink\\n\\nbody``.
  - Chunking:
    - split at Markdown headings;
    - pack sections into chunks of at most MAX_VECTOR_CHUNK_CHARS = 900, joined with blank lines;
    - long sections are packed line by line;
    - a single line over 900 chars falls back to a 900-char window with VECTOR_CHUNK_OVERLAP_CHARS = 120.
  - Window = max(vector_k=100, C*10) chunks. Row similarity = best chunk cosine, filtered at the min_similarity
    floor (see THRESHOLDS). Rows are sorted by similarity and the first C are kept.
- Candidate size C = max(100, (limit+offset)*10) with offset = 0 and limit = k.
- Fusion (FUSION_FORMULA_VERSION "max+0.3*min/v1"):
  - f = |bm25| / max|bm25| over the FTS rows; v = raw cosine (not renormalised);
  - fused = max(v,f) + 0.3*min(v,f); FTS_GATE_THRESHOLD = 0 (no-op).
  - Sort descending, ties broken by row id (session order). Return the first k rows.
- Result text:
  - vector-leg rows carry ``matched_chunk``: the full body if it is at most SMALL_NOTE_CONTENT_LIMIT = 2000
    chars, else the top TOP_CHUNKS_PER_RESULT = 5 chunks by similarity joined with ``\\n---\\n``;
  - FTS-only rows carry ``content`` = body truncated to CONTENT_DISPLAY_LIMIT = 4000.
  - Item.text = title line + (matched_chunk or content). This is the hit text BM's own bm-local provider uses,
    plus the title it surfaces for dated sessions.
  - Item.turn_ids = the turns whose lines appear in that text.
- No recency decay, no graph expansion and no query-time date parsing. BM search does none of these, so
  q.time is unused.

APPROXIMATED / DEVIATIONS
- Embedder: the shared SIF embedder stands in for fastembed bge-small-en-v1.5 (384-d). The query is embedded
  without a prefix, as in BM's default.
- THRESHOLDS: the shipped min_similarity of 0.55 is set for bge-small. bge-small cosines sit high: unrelated
  in-domain text is about 0.4-0.55 and related text about 0.6-0.8. So 0.55 keeps roughly the more-related half
  of chunks.
  - Our embedder's cosines are much lower (DEV LoCoMo median chunk cosine is about 0.05-0.23), so a raw 0.55
    would remove nearly everything.
  - The floor is therefore converted to a per-query percentile: a row survives if its best-chunk cosine is at
    least the MIN_SIM_PERCENTILE = 50th percentile of all chunk cosines for that query.
  - I checked this on one DEV corpus only (no scoring). It keeps about half the chunks, never zero.
- FTS5 rows: N and avgD are computed over this corpus's notes only, as in a fresh per-group benchmark project.
- Not modelled, because benchmark questions do not contain them: explicit boolean operators (AND/OR/NOT),
  ``tag:`` shorthand, CJK/Thai script n-gram channel, and FTS5 syntax-error fallbacks.
- Session doc ids are synthesised (``locomo-<conv>-sNN`` / ``<qid>-sNNN``). The project slug ``bench``
  prefixes permalinks (permalinks_include_project defaults to True).
- Date strings: LoCoMo uses the dataset's ``h:mm am on d Month, yyyy``; LME uses ``yyyy/mm/dd (Day) HH:MM``.
- Turn text is used as the harness supplies it, including LoCoMo photo captions. Whitespace is collapsed, as in
  the LME converter.
"""
from __future__ import annotations

import bisect
import math
import re
import unicodedata
from collections import Counter

import numpy as np

from bench.system import Item, System

# --- Basic Memory constants -------------------------------------------------------------------------------
MAX_CONTENT_STEMS_SIZE = 6000          # services/search_service.py:44
CONTENT_DISPLAY_LIMIT = 4000           # repository/search_index_row.py:47
MAX_VECTOR_CHUNK_CHARS = 900           # repository/semantic_chunking.py
VECTOR_CHUNK_OVERLAP_CHARS = 120
SMALL_NOTE_CONTENT_LIMIT = 2000        # repository/search_reader.py
TOP_CHUNKS_PER_RESULT = 5
FUSION_BONUS = 0.3
FTS_GATE_THRESHOLD = 0.0
SEMANTIC_VECTOR_K = 100                # config_models.py semantic_vector_k
SEMANTIC_MIN_SIMILARITY_BGE = 0.55     # config_models.py semantic_min_similarity (bge-small scale)
MIN_SIM_PERCENTILE = 50.0              # conversion of 0.55 to our embedder (see docstring)
FTS5_K1, FTS5_B = 1.2, 0.75
PROJECT_SLUG = "bench"

RELAXATION_STOPWORDS = frozenset(
    "a an and are as at be but by did do does for from had has have how i in is it of on "
    "or our that the their they this to was we were what when where which who whom whose why "
    "will with you your".split()
)
_PROBLEMATIC = frozenset("\"'()[]{}+!@#$%^&=|\\~`")
_SPACE_OR_SPECIAL = frozenset(" .:;,<>?/-")
_HEADER = re.compile(r"^\s*#{1,6}\s+")
_TOKEN = re.compile(r"(?:[^\W_]|/)+")
_REL_TOKEN = re.compile(r"[^\W_]*[^\W\d_](?:['‘’＇·][^\W\d_][^\W_]*)+|[^\W_]+")
_HI = "\U0010ffff"


# --- unicode61-like tokeniser -----------------------------------------------------------------------------
def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def fts_tokens(s: str) -> list[str]:
    return _TOKEN.findall(_fold(s))


def _variants(text: str) -> list[str]:
    """SearchService._generate_variants (a set; order is irrelevant except for the 6000-char cut)."""
    v = [text, text.lower()]
    if "/" in text:
        v += [p.strip() for p in text.split("/") if p.strip()]
    v += [w.strip() for w in text.lower().split() if w.strip()]
    return list(dict.fromkeys(v))


class _Column:
    """One FTS5 column of one row: token sequence plus a sorted prefix-count index."""

    __slots__ = ("toks", "keys", "cum")

    def __init__(self, text: str):
        self.toks = fts_tokens(text)
        c = Counter(self.toks)
        self.keys = sorted(c)
        cum, s = [0], 0
        for k in self.keys:
            s += c[k]
            cum.append(s)
        self.cum = cum

    def freq(self, phrase: tuple[str, ...]) -> int:
        """Occurrences of a phrase whose last token is a prefix (FTS5 ``"a b c"*`` / ``word*``)."""
        if len(phrase) == 1:
            p = phrase[0]
            lo = bisect.bisect_left(self.keys, p)
            hi = bisect.bisect_left(self.keys, p + _HI)
            return self.cum[hi] - self.cum[lo]
        head, last = phrase[:-1], phrase[-1]
        n, t, m = 0, self.toks, len(phrase)
        for i in range(len(t) - m + 1):
            if t[i] == head[0] and tuple(t[i:i + m - 1]) == head and t[i + m - 1].startswith(last):
                n += 1
        return n


# --- query preparation (sqlite_search_query.prepare_single_term / search_query.relaxed_query_words) -------
def _phrase(text: str) -> tuple[str, ...]:
    return tuple(fts_tokens(text))


def strict_phrases(query: str) -> list[tuple[str, ...]]:
    """Prepared strict FTS5 expression as a list of AND-ed prefix phrases."""
    # analyze_script_query: keep whitespace tokens that carry a word character.
    toks = [t for t in query.split() if any(c.isalnum() for c in t)]
    term = (" ".join(toks) or query).strip()
    if not term:
        return []
    if " " in term:
        words = [w.strip("?!.,;:") for w in term.split()]
        term = " ".join(w for w in words if w)
        if not term:
            return []
    has_problematic = any(c in _PROBLEMATIC for c in term)
    has_special = any(c in _SPACE_OR_SPECIAL for c in term)
    if has_problematic or has_special:
        if " " in term and not has_problematic:
            words = term.split()
            if not any(any(c in w for c in _SPACE_OR_SPECIAL if c != " ") for w in words):
                out = [_phrase(w) for w in words]  # "w1* AND w2* ..."
                return [p for p in out if p]
        p = _phrase(term)  # quoted phrase with prefix on its last token
        return [p] if p else []
    p = _phrase(term)
    return [p] if p else []


def _is_numeric(tok: str) -> bool:
    return bool(tok) and all(unicodedata.category(c).startswith("N") for c in tok)


def relaxed_phrases(query: str) -> list[tuple[str, ...]] | None:
    s = query.strip()
    if not s or '"' in s or any(op in f" {s} " for op in (" AND ", " OR ", " NOT ")):
        return None
    toks = _REL_TOKEN.findall(s.lower())
    if len(toks) < 3 or any(_is_numeric(t) for t in toks):
        return None
    pruned = [t for t in toks if t not in RELAXATION_STOPWORDS] or toks
    pruned = list(dict.fromkeys(pruned))
    out = [_phrase(t) for t in pruned]
    out = [p for p in out if p]
    return out or None


# --- chunking (semantic_chunking.split_text_into_chunks with turn tracking) -------------------------------
def _char_window(line: str, tid):
    text = line.strip()
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + MAX_VECTOR_CHUNK_CHARS)
        c = text[start:end].strip()
        if c:
            out.append((c, {tid} if tid else set()))
        if end >= len(text):
            break
        start = max(0, end - VECTOR_CHUNK_OVERLAP_CHARS)
    return out


def _split_long_section(lines):
    chunks, cur, cur_len = [], [], 0

    def flush():
        nonlocal cur, cur_len
        s, e = 0, len(cur)
        while s < e and not cur[s][0].strip():
            s += 1
        while e > s and not cur[e - 1][0].strip():
            e -= 1
        packed = "\n".join(x[0] for x in cur[s:e]).rstrip()
        if packed:
            chunks.append((packed, {x[1] for x in cur[s:e] if x[1]}))
        cur, cur_len = [], 0

    for ln in lines:
        if len(ln[0]) > MAX_VECTOR_CHUNK_CHARS:
            flush()
            chunks.extend(_char_window(ln[0], ln[1]))
            continue
        sep = 1 if cur else 0
        if cur and cur_len + sep + len(ln[0]) > MAX_VECTOR_CHUNK_CHARS:
            flush()
            sep = 0
        cur.append(ln)
        cur_len += sep + len(ln[0])
    flush()
    return chunks


def split_chunks(lines):
    """lines: list of (text, turn_id|None). Returns [(chunk_text, set(turn_ids))]."""
    sections, cur = [], []
    for ln in lines:
        if _HEADER.match(ln[0]) and cur:
            sections.append(cur)
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        sections.append(cur)

    def trim(sec):
        s, e = 0, len(sec)
        while s < e and not sec[s][0].strip():
            s += 1
        while e > s and not sec[e - 1][0].strip():
            e -= 1
        return sec[s:e]

    out, cur_text, cur_ids = [], "", set()
    for sec in sections:
        sec = trim(sec)
        text = "\n".join(x[0] for x in sec)
        ids = {x[1] for x in sec if x[1]}
        if len(text) > MAX_VECTOR_CHUNK_CHARS:
            if cur_text:
                out.append((cur_text, cur_ids))
                cur_text, cur_ids = "", set()
            long_chunks = _split_long_section(sec)
            if long_chunks:
                out.extend(long_chunks[:-1])
                cur_text, cur_ids = long_chunks[-1][0], set(long_chunks[-1][1])
            continue
        cand = text if not cur_text else f"{cur_text}\n\n{text}"
        if len(cand) <= MAX_VECTOR_CHUNK_CHARS:
            cur_text, cur_ids = cand, cur_ids | ids
            continue
        out.append((cur_text, cur_ids))
        cur_text, cur_ids = text, set(ids)
    if cur_text:
        out.append((cur_text, cur_ids))
    return [(t, i) for t, i in out if t.strip()]


# --- the system -------------------------------------------------------------------------------------------
class BasicMemory(System):
    name = "basic-memory"
    family = "personal-brain"
    faithful_notes = (
        "Reproduces Basic Memory search_notes(search_type='hybrid'), the default when semantic deps are present, "
        "with the reranker off. Notes are one Markdown file per session, in BM's own benchmark corpus format "
        "(dated title, '- **speaker:** text' lines); only note-level rows are searched, as in the default "
        "entity_types=['entity']. The keyword leg reimplements SQLite FTS5: unicode61 tokens, no stemming or "
        "stopwords, prefix terms, strict AND with a stopword-pruned OR fallback (>=3 tokens, no numerals), and "
        "the first-matching-column bm25 of the OR-of-columns MATCH over title/content_stems(6000 chars)/"
        "content_snippet. The vector leg chunks title+permalink+body at Markdown headings into <=900-char "
        "chunks (120 overlap for oversize lines) and keeps the best chunk per note. Fusion is "
        "max(v,f)+0.3*min(v,f), with FTS max-normalised and cosine raw. Returned text is the title plus the "
        "matched chunks (full body if <=2000 chars, else the top-5 chunks) or the body truncated to 4000 chars. "
        "Approximations: the shared SIF embedder replaces bge-small, and its 0.55 similarity floor is converted "
        "to the per-query 50th percentile of chunk cosines; boolean/tag/CJK query syntax is not modelled."
    )

    def __init__(self, min_sim_percentile: float = MIN_SIM_PERCENTILE):
        self.min_sim_percentile = min_sim_percentile

    # ---- build ----
    def build(self, corpus, enc):
        self.enc = enc
        is_lme = list(corpus.speakers) == ["user", "assistant"]
        by_sess: dict[str, list] = {}
        for t in corpus.turns:
            by_sess.setdefault(t.session_id, []).append(t)
        self.docs = []
        chunk_texts, chunk_doc, chunk_ids = [], [], []
        for n, sid in enumerate(corpus.session_order, start=1):
            turns = sorted(by_sess.get(sid, []), key=lambda t: t.idx)
            if not turns:
                continue
            ts = corpus.session_ts.get(sid, turns[0].ts)
            if is_lme:
                date = ts.strftime("%Y/%m/%d (%a) %H:%M")
                doc_id = f"{corpus.id}-s{n - 1:03d}"
            else:
                hour = ts.strftime("%I").lstrip("0") or "12"
                date = f"{hour}:{ts.strftime('%M')} {ts.strftime('%p').lower()} on {ts.day} {ts.strftime('%B, %Y')}"
                doc_id = f"locomo-{corpus.id}-s{n:02d}"
            title = f"{doc_id} ({date})"
            file_path = f"{doc_id}.md"
            permalink = f"{PROJECT_SLUG}/{doc_id}"
            # body lines, tracking which turn each line carries
            lines = [(f"# Chat session at {date}", None), ("", None), ("## Conversation", None)]
            for t in turns:
                text = " ".join(t.text.split())
                if text:
                    lines.append((f"- **{t.speaker}:** {text}", t.id))
            body = "\n".join(x[0] for x in lines)
            # character offset of each turn line in body (for the 4000-char content cut)
            offs, pos = [], 0
            for txt, tid in lines:
                if tid:
                    offs.append((pos, tid))
                pos += len(txt) + 1
            stems = "\n".join(
                p for p in _variants(title) + [body] + _variants(permalink) + _variants(file_path) if p and p.strip()
            )[:MAX_CONTENT_STEMS_SIZE]
            cols = {"title": _Column(title), "stems": _Column(stems), "snippet": _Column(body)}
            plen = len(fts_tokens(permalink))
            d = {
                "title": title, "body": body, "cols": cols, "offs": offs,
                "D": sum(len(c.toks) for c in cols.values()) + plen,
                "all_ids": [tid for _, tid in offs],
            }
            src = [(title, None), ("", None), (permalink, None), ("", None)] + lines
            d["chunk_lo"] = len(chunk_texts)
            for ctext, cids in split_chunks(src):
                chunk_texts.append(ctext)
                chunk_doc.append(len(self.docs))
                chunk_ids.append(cids)
            d["chunk_hi"] = len(chunk_texts)
            self.docs.append(d)
        self.N = len(self.docs)
        self.avgD = (sum(d["D"] for d in self.docs) / self.N) if self.N else 1.0
        self.chunk_texts, self.chunk_ids = chunk_texts, chunk_ids
        self.chunk_doc = np.array(chunk_doc, dtype=np.int64)
        self.E = enc.embed_many(chunk_texts) if chunk_texts else np.zeros((0, 1), np.float32)

    # ---- FTS5 keyword leg ----
    def _fts_branch(self, phrases, col: str, mode: str):
        """Rows matching one column branch, with fts5 bm25 restricted to that column."""
        freqs = [[d["cols"][col].freq(p) for p in phrases] for d in self.docs]
        n_hit = [sum(1 for f in freqs if f[i] > 0) for i in range(len(phrases))]
        idf = []
        for h in n_hit:
            v = math.log((self.N - h + 0.5) / (h + 0.5)) if self.N else 0.0
            idf.append(v if v > 0 else 1e-6)
        out = {}
        for di, f in enumerate(freqs):
            ok = all(x > 0 for x in f) if mode == "and" else any(x > 0 for x in f)
            if not ok:
                continue
            D = self.docs[di]["D"]
            norm = FTS5_K1 * (1 - FTS5_B + FTS5_B * D / max(self.avgD, 1e-9))
            out[di] = sum(idf[i] * (x * (FTS5_K1 + 1)) / (x + norm) for i, x in enumerate(f))
        return out

    def _fts_search(self, phrases, mode):
        scores: dict[int, float] = {}
        for col in ("title", "stems", "snippet"):  # OR branches: first matching branch supplies bm25
            for di, s in self._fts_branch(phrases, col, mode).items():
                scores.setdefault(di, s)
        return scores

    def fts(self, text: str, limit: int):
        phrases = strict_phrases(text)
        scores = self._fts_search(phrases, "and") if phrases else {}
        if not scores and phrases:
            relaxed = relaxed_phrases(text)
            if relaxed:
                scores = self._fts_search(relaxed, "or")
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[:limit]

    # ---- vector leg ----
    def vector(self, text: str, candidate_limit: int, chunk_limit: int):
        if len(self.chunk_texts) == 0:
            return [], {}
        qv = self.enc.embed(text)
        sims = self.E @ qv
        order = np.argsort(-sims, kind="stable")[:chunk_limit]
        thr = float(np.percentile(sims, self.min_sim_percentile))
        best: dict[int, float] = {}
        chunks_by_doc: dict[int, list[int]] = {}
        for ci in order:
            di = int(self.chunk_doc[ci])
            s = float(sims[ci])
            if di not in best or s > best[di]:
                best[di] = s
            chunks_by_doc.setdefault(di, []).append(int(ci))
        best = {di: s for di, s in best.items() if s >= thr}
        ranked = sorted(best.items(), key=lambda kv: -kv[1])[:candidate_limit]
        matched = {}
        for di, _ in ranked:
            d = self.docs[di]
            if len(d["body"]) <= SMALL_NOTE_CONTENT_LIMIT:
                matched[di] = (d["body"], list(d["all_ids"]))
            else:
                top = sorted(chunks_by_doc[di], key=lambda c: -float(sims[c]))[:TOP_CHUNKS_PER_RESULT]
                txt = "\n---\n".join(self.chunk_texts[c] for c in top)
                ids = set().union(*(self.chunk_ids[c] for c in top))
                matched[di] = (txt, [t for t in d["all_ids"] if t in ids])
        return ranked, matched

    # ---- hybrid ----
    def retrieve(self, q, k: int = 30):
        text = (q.text or "").strip()
        if not text or not self.docs:
            return []
        limit, offset = k, 0
        C = max(SEMANTIC_VECTOR_K, (limit + offset) * 10)
        vec_chunk_limit = max(SEMANTIC_VECTOR_K, C * 10)  # rerank disabled -> vector leg expands again
        fts_rows = self.fts(text, C)
        vec_rows, matched = self.vector(text, C, vec_chunk_limit)

        fts_max = max((abs(s) for _, s in fts_rows), default=1.0)
        f = {}
        for di, s in fts_rows:
            n = abs(s) / fts_max if fts_max > 0 else 0.0
            f[di] = 0.0 if n < FTS_GATE_THRESHOLD else n
        v = {di: s for di, s in vec_rows}
        fused = {di: max(v.get(di, 0.0), f.get(di, 0.0)) + FUSION_BONUS * min(v.get(di, 0.0), f.get(di, 0.0))
                 for di in set(f) | set(v)}
        ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[offset:offset + limit]

        items = []
        for di, score in ranked:
            d = self.docs[di]
            if di in matched:
                body_txt, ids = matched[di]
            else:
                body_txt = d["body"][:CONTENT_DISPLAY_LIMIT]
                ids = [tid for off, tid in d["offs"] if off < CONTENT_DISPLAY_LIMIT]
            if not ids:
                continue
            items.append(Item(turn_ids=ids, text=f"{d['title']}\n{body_txt}", score=float(score),
                              meta={"fts": f.get(di, 0.0), "vec": v.get(di, 0.0)}))
        return items
