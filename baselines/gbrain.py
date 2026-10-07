"""GBrain (garrytan/gbrain v0.60.102.0, commit 5b58910) LLM-free retrieval baseline.

Implements section 13 of /home/claude/mp/reports/gbrain.md. The configuration is the `balanced` search mode
(the shipped default) with the cross-encoder reranker OFF and no LLM query expansion. This is the like-for-like
"A1" row of GBrain's own LongMemEval receipt. Results are delivered the way the agent-facing `search`
operation delivers them (evidence delivery `return_unit=auto`).

FAITHFUL
- Ingest, as the project's LongMemEval adapter does it (src/eval/longmemeval/adapter.ts):
  - One page per session.
  - slug = ``chat/s-<sha256(corpus:sanitized_session)[:10]>``.
  - Body = ``**<speaker>:** <text>`` lines separated by blank lines.
  - type = note. effective_date = the session date.
  - title = the humanised filename (markdown.ts inferTitle), e.g. "S 3f2a9c1b0d".
- Chunking (chunkers/recursive.ts):
  - 300 words with 50-word sentence-aware overlap.
  - Delimiter ladder: paragraph > line > sentence > clause > words.
  - Greedy merge capped at 1.5 x target. A page of at most 300 words is one chunk.
  - Char cap 6000 with a 5500 stride.
  - All chunks are chunk_source=compiled_truth.
- Embedding input (balanced contextual_retrieval='title', embedding-context.ts):
  ``<context>{title}\\n</context>\\n{chunk}``. Query is embedded bare (no query prefix configured by default).
- Request (hybrid/request.ts): limit = k. Per-arm page pool P = min(max(2k, 50, k), 100) (PRE_FUSION_POOL_FLOOR = 50,
  MAX_SEARCH_LIMIT = 100).
- Vector arm (vector-statement.ts, vector-pool.ts):
  1. Chunk pool max(5P, 100), escalated x4 up to 3 times (cap 20000) while fewer than P pages are found.
  2. Score = cosine x source factor. The `chat/` prefix has no source boost, so the factor is 1.0.
  3. Best chunk per page (DISTINCT ON), then the top P pages.
  4. Ties break on page id, then chunk id.
- Keyword arm (postgres-engine.ts searchKeyword):
  1. Strict AND match (every query term in the chunk), chunk pool min(3P, 300), best chunk per page, top P.
  2. If that returns 0 rows, retry once with OR-of-tokens. Those rows are tagged `relaxed`, and they are DROPPED
     before fusion whenever the vector arm returned anything (rank.ts:fuseArms).
- Intent (query-intent.ts): the regex banks are ported verbatim (full_context, temporal, event, concept,
  entity, canonical, recency banks).
  - Intent weights (intent-weights.ts):
    - entity: kw 1.15, vec 1.0
    - temporal: kw 1, vec 1, recency on
    - event: kw 1.20, vec 0.95, recency on
    - concept: kw 0.9, vec 1.2
    - general: 1, 1
  - Effective k = 60 / weight.
  - Auto detail: entity gives a soft 1.2 compiled-truth tilt; temporal and event bypass the source factor.
    Both are no-ops on this corpus but are kept.
- Fusion (rrf-page-fusion.ts, hybrid.ts rrfFusionWeighted):
  - Lists in order [vector, keyword]. A row votes 1/(k_list + rank), with rank 0-based.
  - Each list votes once per page. The page total goes to the page's lead chunk (largest own vote, ties to the
    first-seen chunk, which is the vector chunk). Other chunks keep their own vote.
  - Normalise by the maximum, apply the compiled-truth factor, then sort.
- Cosine re-score (hybrid.ts cosineReScore): s = 0.7 * s / max(s) + 0.3 * cos(q, chunk).
- Metadata boost gate (balanced = 'lexical'): the metadata stages below run only when a STRICT keyword row fused.
  - Recency (recency 'on' / 'strong', from the query banks or the intent): x (1 + m * 1.0 * 7 / (7 + days_old)),
    with the `chat/` profile (half-life 7 d, coefficient 1.0) and m = 1 / 1.5.
  - Graph signals, balanced = on (graph-signals.ts):
    - Top 20 in current order.
    - Session groups use sessionPrefix(`chat/<id>`), which is the page itself.
    - Non-best members of a group x 0.95.
    - Adjacency and cross-source boosts need links, which a conversation corpus has none of.
- Dedup (dedup.ts):
  1. Top 3 chunks per page.
  2. Drop a chunk whose word-set Jaccard with a kept chunk of the same page is > 0.85.
  3. Type diversity is skipped (one type).
  4. At most 2 chunks per page. The compiled-truth guarantee is a no-op.
- Slice [0, k], then the balanced token budget of 12000 at ceil(chars / 4) on title + chunk_text. A row that does
  not fit is skipped and packing continues (token-budget.ts).
- Evidence delivery (search op default return_unit='auto', search/evidence-delivery.ts):
  - chat/ pages are conversation pages, so hits are grouped per page at the page's best rank.
  - Budget: 24000 tokens (DEFAULT_CONVERSATION_BUDGET).
  - Pieces are lines, and lines over 400 chars split at whitespace.
  - Reserve phase, in rank order: each page reserves title + the pieces of its best hit chunk. A page whose
    reservation does not fit spills to raw chunk rows (`conversation_over_budget`).
  - Enrichment phase, in rank order: other hits' pieces first, then pieces by distance from the hits.
    Enrichment stops at the first piece that does not fit.
  - Gaps are joined with "\\n\\n[...]\\n\\n".
- "now": recency in GBrain uses wall-clock Date.now(); per the harness rule we pass q.time.

APPROXIMATED
- Postgres ts_rank(search_vector, websearch_to_tsquery('english', q)) is replaced by the shared BM25 for ORDERING
  within the matched set. The AND/OR match gate is kept exactly, on the shared stemmed and stopworded terms.
  Only the rank order feeds RRF, so the scale difference does not matter.
- HNSW is replaced by an exact cosine scan. On these small corpora pool escalation always reaches the whole
  corpus anyway.
- cl100k token counts (evidence delivery) use bench.text.count_tokens.
- Title arm, alias hop, exact-lookup tier, relational arm, backlink, salience, title-phrase / identity boosts,
  supersede downrank and declared-name fan-out are all implemented by GBrain but are provably inert on a
  linkless, alias-less conversation corpus with hash titles. They are omitted and noted here.
- Item.text = the lean result row the agent reads, rendered compactly as a header line
  ``<slug> | <title> | <effective_date>`` followed by the delivered chunk_text.

THRESHOLDS
- No embedder-scale threshold affects ranking in this path. evidence_cosine_floor = 0.80 only labels results.
  The Jaccard 0.85 is lexical. The 0.7 / 0.3 cosine blend is a weight, kept as is (our cosine scale is lower,
  which slightly reduces the cosine term's influence).

PARAMETERS: RRF k=60; P floor 50 / cap 100; vector pool max(5P,100) x4^3; keyword pool 3P; blend 0.7/0.3;
recency chat/ (7 d, 1.0); session demote 0.95 over the top 20; dedup 3 / 0.85 / 2; budget 12000 (chars/4);
delivery budget 24000.

OPTIONS (not default): return_unit='chunk' returns the ranked chunk rows (the LongMemEval retrieval-harness
shape); token_budget=None disables the 12000 cap.

Tunable knobs (TUNABLE; the defaults reproduce the frozen baseline exactly)
- return_unit in {auto, window, chunk}. This is the `return_unit` request parameter of search/query/recall
  (src/core/ops/search.ts:634, RETURN_UNIT_PARAM :114) and the operator config `search.return_unit`
  (src/core/search/evidence-delivery.ts:38-43).
  - auto: whole conversation pages.
  - window: the hit chunk plus +/- `search.return_window` (default 1, max 3) neighbour chunks, cut by
    windowCandidates. With an explicit unit there is no spill to chunk rows: a block whose floor does not fit is
    dropped, except that rank 1 is cut to fit (allocate, evidence-delivery.ts:680).
  - chunk: the raw ranked chunk rows (stage inert).
- delivery_budget in {None, 3000, 12000}. This is the evidence token budget: the `token_budget` request parameter
  (ops/search.ts:636) or the operator configs `search.return_budget_conversation` / `search.return_budget_default`
  (evidence-delivery.ts:45-50). None = the project default for the unit: 24000 for auto, 6000 for window.
  It has no effect when return_unit='chunk'.
- limit in {None, 20}. This is the `limit` request parameter of the search op (ops/search.ts:624, which defaults
  to 20 when the caller omits it; ops/search.ts:649). None = the harness's k.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime

import numpy as np

from bench.system import Item, System
from bench.text import count_tokens, terms

# ---------------------------------------------------------------- constants
RRF_K = 60
PRE_FUSION_POOL_FLOOR = 50
MAX_SEARCH_LIMIT = 100
CHUNK_WORDS, CHUNK_OVERLAP, MAX_CHARS = 300, 50, 6000
COMPILED_TRUTH_TILT = 1.2           # auto-detected low (entity)
BLEND_RRF, BLEND_COS = 0.7, 0.3
RECENCY_CHAT = (7.0, 1.0)           # chat/ prefix: halflife days, coefficient
SESSION_DEMOTE, GRAPH_TOP_K = 0.95, 20
DEDUP_JACCARD, DEDUP_PER_PAGE = 0.85, 2
TOKEN_BUDGET = 12000                # balanced bundle
DELIVERY_BUDGET = 24000             # DEFAULT_CONVERSATION_BUDGET (auto)
DEFAULT_RETURN_BUDGET = 6000        # window/section/page
MAX_WINDOW = 3
PIECE_MAX_CHARS = 400
EVIDENCE_BLOCK_CHAR_CAP = 60000
OMISSION = "\n\n[…]\n\n"

INTENT_WEIGHTS = {  # (keywordWeight, vectorWeight, suggestedRecency)
    "entity": (1.15, 1.0, None), "temporal": (1.0, 1.0, "on"), "event": (1.20, 0.95, "on"),
    "concept": (0.9, 1.2, None), "general": (1.0, 1.0, None),
}

_I = re.IGNORECASE
TEMPORAL = [re.compile(p, _I) for p in [
    r"\bwhen\b", r"\blast\s+(met|meeting|call|conversation|chat|talked|spoke|seen|heard|time)\b",
    r"\brecent(ly)?\b", r"\bhistory\b", r"\btimeline\b", r"\bmeeting\s+notes?\b", r"\bwhat('s| is| was)\s+new\b",
    r"\blatest\b", r"\bupdate(s)?\s+(on|from|about)\b", r"\bhow\s+long\s+(ago|since)\b", r"\b\d{4}[-/]\d{2}\b",
    r"\blast\s+(week|month|quarter|year)\b"]]
EVENT = [re.compile(p, _I) for p in [
    r"\bannounce[ds]?(ment)?\b", r"\blaunch(ed|es|ing)?\b", r"\braised?\s+\$?\d", r"\bfund(ing|raise)\b",
    r"\bIPO\b", r"\bacquisition\b", r"\bmerge[drs]?\b", r"\bnews\b", r"\bhappened?\b"]]
ENTITY = [re.compile(p, _I) for p in [
    r"\bwho\s+is\b", r"\bwhat\s+(is|does|are)\b", r"\btell\s+me\s+about\b", r"\bdescribe\b", r"\bsummar(y|ize)\b",
    r"\boverview\b", r"\bbackground\b", r"\bprofile\b", r"\bwhat\s+do\s+(i|you|we)\s+know\b"]]
FULL_CONTEXT = [re.compile(p, _I) for p in [
    r"\beverything\b", r"\ball\s+(about|info|information|details)\b",
    r"\bfull\s+(history|context|picture|story|details)\b", r"\bcomprehensive\b", r"\bdeep\s+dive\b",
    r"\bgive\s+me\s+everything\b"]]
CANONICAL = [re.compile(p, _I) for p in [
    r"\bwho\s+is\b", r"\bwhat\s+(is|are|does|means?)\b", r"\bdefin(e|ition|ing)\b", r"\bexplain\s+(what|how|why)\b",
    r"\b(history|origin|background)\s+of\b", r"\bconcept\s+of\b", r"\boverview\s+of\b", r"\btell\s+me\s+about\b",
    r"\bcompiled\s+truth\b", r"::|->|\.\w+\(", r"\b(function|class|method|module)\s+\w+",
    r"\b(graph|traversal|backlinks?|inbound|outbound)\b"]]
STRONG_RECENCY = [re.compile(p, _I) for p in [r"\btoday\b", r"\bright\s+now\b", r"\bthis\s+morning\b", r"\bjust\s+now\b"]]
RECENCY_ON = [re.compile(p, _I) for p in [
    r"\bwhat'?s\s+(going\s+on|happening|new|latest|up)\b", r"\b(latest|recent(ly)?|currently)\b",
    r"\b(this|last|past)\s+(week|month|few\s+days|couple\s+days)\b", r"\bmeeting\s+(prep|with|for|notes?|brief)\b",
    r"\bbefore\s+(my|the|our)\s+(meeting|call|sync|chat)\b", r"\bprep(are)?\s+(for|me)\b",
    r"\bcatch(es|ing)?\b[\s\w]{0,15}\bup\b", r"\bremind\s+me\s+(what|about|of)\b",
    r"\b(update|status|progress)\s+(on|with|from)\b"]]
TEMPORAL_BOUND = [re.compile(p, _I) for p in [
    r"\btoday\b", r"\bright\s+now\b", r"\bthis\s+morning\b", r"\bthis\s+week\b", r"\bsince\s+(launch|last|the|\d)",
    r"\blast\s+\d+\s+(day|days|week|weeks|month|months)\b"]]
CONCEPT_CUE = [re.compile(p, _I) for p in [
    r"\b(all|every)\b.+\b(that|who|which|doing|with|about|related to)\b", r"\b(find|list|show)\s+(all|every|everything)\b",
    r"\beverything\s+(about|on|matching|related)\b", r"\bthe\s+(landscape|ecosystem|space|universe)\s+of\b",
    r"\b(landscape|ecosystem)\s+(of|around)\b", r"\bwhich\s+\w+[\w\s]*\b(do|does|are|have|use|work)\b"]]
CONCEPT_DEF = [re.compile(r"\b[Ww]hat\s+(is|are)\s+(the\s+)?[a-z][\w'’-]*\s+[a-z]"),
               re.compile(r"\b[Ww]hat\s+do\s+(i|you|we)\s+know\s+about\s+[a-z][\w'’-]*\s+[a-z]"),
               re.compile(r"\b(notes|ideas|thinking|thoughts|writing)\s+(on|about)\b", _I),
               re.compile(r"\bways\s+to\b", _I), re.compile(r"\bhow\s+to\s+think\s+about\b", _I),
               re.compile(r"\bconcept\s+of\b", _I)]
CONCEPT_STATUS_ANTI = re.compile(r"\b(working on|up to|doing|saying|talking about|focused on|meeting with)\b", _I)
CONCEPT_ANTI = [re.compile(r"[\"'“”][^\"'“”]+[\"'“”]"), re.compile(r"\b[a-z0-9]+(?:-[a-z0-9]+){1,}\b")]


def _any(pats, q):
    return any(p.search(q) for p in pats)


def _mid_sentence_capital(q: str) -> bool:
    for m in re.finditer(r"\s(\w)", q):
        if not m.group(1).isupper():
            continue
        i = m.start()
        while i >= 0 and q[i].isspace():
            i -= 1
        if i < 0:
            continue
        if q[i] not in ".!?:;\n\r•-([\"“":
            return True
    return False


def _concept_shaped(q: str) -> bool:
    q = q.strip()
    if len(q.split()) < 3:
        return False
    definitional = _any(CONCEPT_DEF, q)
    cue = _any(CONCEPT_CUE, q)
    if not cue and not definitional:
        return False
    if _any(CONCEPT_ANTI, q) or _mid_sentence_capital(q):
        return False
    if definitional and not cue and CONCEPT_STATUS_ANTI.search(q):
        return False
    return True


def classify(q: str) -> tuple[str, str]:
    """-> (intent, effective recency mode) per query-intent.ts + effective-modes.ts."""
    if _any(FULL_CONTEXT, q) or _any(TEMPORAL, q):
        intent = "temporal"
    elif _any(EVENT, q):
        intent = "event"
    elif _concept_shaped(q):
        intent = "concept"
    elif _any(ENTITY, q):
        intent = "entity"
    else:
        intent = "general"
    if _any(CANONICAL, q) and not _any(TEMPORAL_BOUND, q):
        sugg = "off"
    elif _any(STRONG_RECENCY, q):
        sugg = "strong"
    elif _any(RECENCY_ON, q):
        sugg = "on"
    else:
        sugg = "off"
    rec = sugg if sugg != "off" else (INTENT_WEIGHTS[intent][2] or sugg)
    return intent, rec


# ---------------------------------------------------------------- chunker (recursive.ts) with char spans
_DELIMS = [["\n\n"], ["\n"], [". ", "! ", "? ", ".\n", "!\n", "?\n", "。", "！", "？"],
           ["; ", ": ", ", ", "；", "：", "，", "、"], []]


def _nwords(s: str) -> int:
    return len(re.findall(r"\S+", s))


def _split_delims(text, start, delims):
    out, pos = [], 0
    while pos < len(text):
        best, bd = -1, ""
        for d in delims:
            i = text.find(d, pos)
            if i != -1 and (best == -1 or i < best):
                best, bd = i, d
        if best == -1:
            if text[pos:].strip():
                out.append((start + pos, start + len(text)))
            break
        end = best + len(bd)
        if text[pos:end].strip():
            out.append((start + pos, start + end))
        pos = end
    return out


def _split_ws(page, s, e, target):
    toks = [(m.start() + s, m.end() + s) for m in re.finditer(r"\S+\s*", page[s:e])]
    return [(toks[i][0], toks[min(i + target, len(toks)) - 1][1]) for i in range(0, len(toks), target)]


def _rsplit(page, s, e, level, target):
    if level >= len(_DELIMS) or not _DELIMS[level]:
        return _split_ws(page, s, e, target)
    pieces = _split_delims(page[s:e], s, _DELIMS[level])
    if len(pieces) <= 1:
        return _rsplit(page, s, e, level + 1, target)
    out = []
    for a, b in pieces:
        out.extend(_rsplit(page, a, b, level + 1, target) if _nwords(page[a:b]) > target else [(a, b)])
    return out


def _trailing(text: str, n: int) -> str:
    ws = re.findall(r"\S+\s*", text)
    if len(ws) <= n:
        return ""
    tr = "".join(ws[-n:])
    m = re.search(r"[.!?]\s+", tr)
    if m and m.start() < len(tr) / 2:
        after = re.sub(r"^[.!?]\s+", "", tr[m.start():])
        if after.strip():
            return after
    return tr


def chunk_page(page: str) -> list[tuple[str, int, int]]:
    """-> [(chunk_text, start, end)] char spans in `page`."""
    if not page.strip():
        return []
    if _nwords(page) <= CHUNK_WORDS:
        chunks = [(page, 0, len(page))]
    else:
        pieces = _rsplit(page, 0, len(page), 0, CHUNK_WORDS)
        merged, cs, ce = [], pieces[0][0], pieces[0][1]
        cur = page[cs:ce]
        for a, b in pieces[1:]:
            if _nwords(cur + page[a:b]) <= math.ceil(CHUNK_WORDS * 1.5):
                cur += page[a:b]
                ce = b
            else:
                merged.append((cur, cs, ce))
                cur, cs, ce = page[a:b], a, b
        if cur.strip():
            merged.append((cur, cs, ce))
        chunks = [merged[0]]
        for i in range(1, len(merged)):
            tr = _trailing(merged[i - 1][0], CHUNK_OVERLAP)
            t, s, e = merged[i]
            chunks.append((tr + t, max(0, s - len(tr)), e))
    out = []
    for t, s, e in chunks:
        lead = len(t) - len(t.lstrip())
        t2 = t.strip()
        s2 = s + lead
        if len(t2) <= MAX_CHARS:
            out.append((t2, s2, s2 + len(t2)))
            continue
        i = 0
        while i < len(t2):
            end = min(len(t2), i + MAX_CHARS)
            sl = t2[i:end]
            if sl.strip():
                out.append((sl.strip(), s2 + i, s2 + end))
            if end >= len(t2):
                break
            i += MAX_CHARS - min(500, MAX_CHARS // 10)
    return out


def _est4(s: str) -> int:
    return math.ceil(len(s) / 4) if s else 0


def _slug_title(corpus_id: str, session_id: str) -> tuple[str, str]:
    san = re.sub(r"[^a-z0-9-]", "-", re.sub(r"[_.]", "-", session_id.lower()))
    oid = "s-" + hashlib.sha256(f"{corpus_id}:{san}".encode()).hexdigest()[:10]
    title = re.sub(r"\b\w", lambda m: m.group(0).upper(), oid.replace("-", " ").replace("_", " ").strip())
    return f"chat/{oid}", title


class GBrain(System):
    name = "gbrain"
    family = "personal-brain"
    faithful_notes = (
        "GBrain balanced hybrid search with the reranker off and no LLM expansion (its LongMemEval A1 row). "
        "One chat/ page per session is chunked at 300 words with 50-word overlap and embedded with a "
        "<context>title</context> prefix. Vector and strict-AND keyword arms each return their best chunk per "
        "page, with a pool floor of 50; OR-relaxed keyword rows are dropped when the vector arm is non-empty. "
        "Fusion is page-grain RRF with k = 60 divided by the regex intent weight; the page vote goes to the lead "
        "chunk. Then a 0.7*RRF + 0.3*cosine re-blend; recency (chat/ half-life 7 d, measured from q.time instead "
        "of wall-clock now) and a 0.95 session demote, both gated on a strict keyword hit; 4-layer dedup; and a "
        "12000-token budget at chars/4. Results go out through the search op's default evidence delivery, which "
        "expands conversation pages within a 24000-token budget. Approximations: ts_rank ordering is replaced by "
        "shared BM25 (the AND/OR gate is kept), HNSW by an exact scan, and stages that are inert on a linkless "
        "corpus (title arm, aliases, graph, backlinks) are omitted.")

    TUNABLE: dict[str, list] = {
        "return_unit": ["auto", "window", "chunk"],
        "delivery_budget": [None, 3000, 12000],
        "limit": [None, 20],
    }

    def __init__(self, return_unit: str = "auto", token_budget: int | None = TOKEN_BUDGET,
                 delivery_budget: int | None = None, limit: int | None = None, return_window: int = 1):
        if return_unit not in ("auto", "window", "chunk"):
            raise ValueError(f"return_unit must be auto|window|chunk (got {return_unit!r})")
        self.return_unit = return_unit
        self.token_budget = token_budget
        self.delivery_budget = delivery_budget
        self.limit = limit
        self.return_window = max(1, min(MAX_WINDOW, int(return_window)))

    def __repr__(self):
        return (f"GBrain(return_unit={self.return_unit!r}, delivery_budget={self.delivery_budget!r}, "
                f"limit={self.limit!r}, token_budget={self.token_budget!r}, return_window={self.return_window})")

    # ------------------------------------------------------------ build
    def build(self, corpus, enc):
        self.enc = enc
        sess_turns: dict[str, list] = {}
        for t in corpus.turns:
            sess_turns.setdefault(t.session_id, []).append(t)
        self.pages = []  # dict(slug,title,date,text,turn_spans)
        ch_text, ch_page, ch_span, ch_turns, emb_in, ch_idx = [], [], [], [], [], []
        for sid in corpus.session_order:
            ts = sorted(sess_turns.get(sid, []), key=lambda t: t.idx)
            if not ts:
                continue
            slug, title = _slug_title(corpus.id, sid)
            body, spans = "", []
            for t in ts:
                line = f"**{t.speaker}:** {t.text}"
                spans.append((len(body), len(body) + len(line), t.id))
                body += line + "\n\n"
            body = body.rstrip()
            pid = len(self.pages)
            self.pages.append(dict(slug=slug, title=title, date=corpus.session_ts.get(sid, ts[0].ts), text=body,
                                   spans=spans))
            prefix = f"<context>{title}\n</context>\n"
            for ci_, (ctext, s, e) in enumerate(chunk_page(body)):
                ch_idx.append(ci_)
                ch_text.append(ctext)
                ch_page.append(pid)
                ch_span.append((s, e))
                ch_turns.append([tid for a, b, tid in spans if a < e and b > s])
                emb_in.append(prefix + ctext)
        self.ch_text, self.ch_page, self.ch_span, self.ch_turns = ch_text, np.array(ch_page), ch_span, ch_turns
        self.ch_idx = ch_idx
        self.page_chunks: dict[int, list[int]] = {}
        for ci_, p_ in enumerate(ch_page):
            self.page_chunks.setdefault(p_, []).append(ci_)
        self.E = enc.embed_many(emb_in)
        self.bm25 = enc.bm25(ch_text)
        self.ch_terms = [set(terms(t)) for t in ch_text]
        self.ch_words = [set(t.lower().split()) for t in ch_text]

    # ------------------------------------------------------------ arms
    def _best_per_page(self, idx_scores, P):
        best = {}
        for ci, sc in idx_scores:
            p = int(self.ch_page[ci])
            if p not in best or sc > best[p][1] or (sc == best[p][1] and ci < best[p][0]):
                best[p] = (ci, sc)
        rows = sorted(best.values(), key=lambda x: (-x[1], int(self.ch_page[x[0]]), x[0]))
        return rows[:P]

    def _vector_arm(self, cos, P):
        n = len(cos)
        order = np.lexsort((np.arange(n), -cos))
        inner = max(5 * P, 100)
        for esc in range(4):
            pool = order[:inner]
            rows = self._best_per_page([(int(i), float(cos[i])) for i in pool], P)
            if len(rows) >= P or inner >= n or esc == 3:
                return rows
            inner = min(inner * 4, max(max(5 * P, 100), 20000))
        return rows

    def _keyword_arm(self, q, P):
        qt = set(terms(q))
        if not qt:
            return [], False
        bm = self.bm25.score(q)
        inner = min(3 * P, 3 * MAX_SEARCH_LIMIT)
        strict = [i for i in range(len(self.ch_text)) if qt <= self.ch_terms[i]]
        if strict:
            strict.sort(key=lambda i: (-bm[i], int(self.ch_page[i]), i))
            return self._best_per_page([(i, float(bm[i])) for i in strict[:inner]], P), False
        if '"' in q or re.search(r"(^|\s)-\S", q):
            return [], False
        toks = [t for t in re.split(r"[^\w]+", q) if t and t.upper() not in ("OR", "AND")]
        if len(toks) < 2:
            return [], False
        orq = set(terms(" ".join(toks)))
        rel = [i for i in range(len(self.ch_text)) if orq & self.ch_terms[i]]
        rel.sort(key=lambda i: (-bm[i], int(self.ch_page[i]), i))
        return self._best_per_page([(i, float(bm[i])) for i in rel[:inner]], P), True

    # ------------------------------------------------------------ retrieve
    def retrieve(self, q, k: int = 30) -> list[Item]:
        limit = self.limit if self.limit else k
        P = min(max(limit * 2, PRE_FUSION_POOL_FLOOR, limit), MAX_SEARCH_LIMIT)
        intent, recency = classify(q.text)
        kw_w, vec_w, _ = INTENT_WEIGHTS[intent]
        qv = self.enc.embed(q.text)
        cos = self.E @ qv
        vec = self._vector_arm(cos, P)
        kw, relaxed = self._keyword_arm(q.text, P)
        if relaxed and vec:
            kw = []  # OR-relaxed rows dropped when the text vector arm is healthy
        lexical_voted = bool(kw)

        # page-grain weighted RRF
        lists = [(vec, RRF_K / vec_w), (kw, RRF_K / kw_w)]
        own, order, page_tot = {}, [], {}
        for rows, kk in lists:
            voted = set()
            for rank, (ci, _) in enumerate(rows):
                v = 1.0 / (kk + rank)
                p = int(self.ch_page[ci])
                if p not in voted:
                    voted.add(p)
                    page_tot[p] = page_tot.get(p, 0.0) + v
                if ci not in own:
                    own[ci] = 0.0
                    order.append(ci)
                own[ci] += v
        if not order:
            return []
        score = dict(own)
        leads = {}
        for ci in order:
            p = int(self.ch_page[ci])
            if p not in leads or own[ci] > own[leads[p]]:
                leads[p] = ci
        for p, ci in leads.items():
            score[ci] = page_tot[p]
        mx = max(score.values())
        tilt = COMPILED_TRUTH_TILT if intent == "entity" else 1.0
        fused = sorted(order, key=lambda c: (-(score[c] / mx * tilt), -own[c]))
        s = {c: score[c] / mx * tilt for c in fused}
        # cosine re-score
        mr = max(s.values())
        s = {c: BLEND_RRF * (s[c] / mr if mr > 0 else 0) + BLEND_COS * float(cos[c]) for c in fused}
        fused.sort(key=lambda c: -s[c])

        # post-fusion metadata stages (gated on a strict lexical vote)
        if lexical_voted:
            if recency in ("on", "strong"):
                mul = 1.5 if recency == "strong" else 1.0
                h, coef = RECENCY_CHAT
                for c in fused:
                    d = self.pages[int(self.ch_page[c])]["date"]
                    days = max(0.0, (q.time - d).total_seconds() / 86400.0)
                    s[c] *= 1.0 + mul * coef * h / (h + days)
            groups: dict[int, list] = {}
            for c in fused[:GRAPH_TOP_K]:
                groups.setdefault(int(self.ch_page[c]), []).append(c)
            for p, mem in groups.items():
                if len(mem) < 2:
                    continue
                mem.sort(key=lambda c: -s[c])
                for c in mem[1:]:
                    s[c] *= SESSION_DEMOTE
        fused.sort(key=lambda c: -s[c])

        # 4-layer dedup
        byp: dict[int, list] = {}
        for c in fused:
            byp.setdefault(int(self.ch_page[c]), []).append(c)
        kept = sorted([c for cs in byp.values() for c in cs[:3]], key=lambda c: -s[c])
        kw_by_page: dict[int, list] = {}
        out = []
        for c in kept:
            p = int(self.ch_page[c])
            ws = self.ch_words[c]
            dup = False
            for kws in kw_by_page.get(p, []):
                inter = len(ws & kws)
                union = len(ws) + len(kws) - inter
                if union and inter / union > DEDUP_JACCARD:
                    dup = True
                    break
            if not dup:
                out.append(c)
                kw_by_page.setdefault(p, []).append(ws)
        cnt: dict[int, int] = {}
        deduped = []
        for c in out:
            p = int(self.ch_page[c])
            if cnt.get(p, 0) < DEDUP_PER_PAGE:
                deduped.append(c)
                cnt[p] = cnt.get(p, 0) + 1
        deduped.sort(key=lambda c: -s[c])

        sliced = deduped[:limit]
        # balanced token budget (chars/4 on title + chunk_text): skip non-fitting rows, keep packing
        if self.token_budget:
            rows, used = [], 0
            for c in sliced:
                cost = _est4(self.pages[int(self.ch_page[c])]["title"]) + _est4(self.ch_text[c])
                if used + cost <= self.token_budget:
                    rows.append(c)
                    used += cost
                elif not rows:
                    break
            sliced = rows if rows or not sliced else sliced[:1]
        if self.return_unit == "chunk":
            return [self._chunk_item(c, s[c]) for c in sliced]
        return self._deliver(sliced, s)

    # ------------------------------------------------------------ evidence delivery (return_unit=auto)
    def _header(self, p):
        pg = self.pages[p]
        return f"{pg['slug']} | {pg['title']} | {pg['date'].strftime('%Y-%m-%d')}\n"

    def _chunk_item(self, c, sc):
        p = int(self.ch_page[c])
        return Item(list(self.ch_turns[c]), self._header(p) + self.ch_text[c], float(sc),
                    {"slug": self.pages[p]["slug"], "unit": "chunk"})

    @staticmethod
    def _pieces(text):
        out, i = [], 0
        while i < len(text):
            nl = text.find("\n", i)
            le = len(text) if nl == -1 else nl + 1
            st = i
            while st < le:
                e = le
                if e - st > PIECE_MAX_CHARS:
                    e = st + PIECE_MAX_CHARS
                    win = text[st + PIECE_MAX_CHARS - 80:e]
                    m = re.search(r"\s\S*$", win)
                    e = st + PIECE_MAX_CHARS - 80 + m.start() + 1 if m else e
                    if e <= st:
                        e = min(le, st + PIECE_MAX_CHARS)
                out.append((st, e))
                st = e
            i = le
        return out

    def _deliver(self, hits, s):
        groups, byp = [], {}
        for rank, c in enumerate(hits):
            p = int(self.ch_page[c])
            if p in byp:
                byp[p]["hits"].append(c)
                continue
            g = {"page": p, "hits": [c], "rank": rank}
            byp[p] = g
            groups.append(g)
        blocks, passthrough = [], []
        auto = self.return_unit == "auto"
        budget = self.delivery_budget or (DELIVERY_BUDGET if auto else DEFAULT_RETURN_BUDGET)
        remaining = budget
        dropped_any = False
        omit_tok = count_tokens(OMISSION)
        for g in groups:
            pg = self.pages[g["page"]]
            text = pg["text"]
            pieces = self._pieces(text)
            ptok = [count_tokens(text[a:b]) for a, b in pieces]
            anchors = []
            for c in g["hits"]:
                cs, ce = self.ch_span[c]
                anchors.append([i for i, (a, b) in enumerate(pieces) if a < ce and b > cs])
            core = anchors[0]
            if auto:
                cands = list(range(len(pieces)))
            else:  # window: pieces of chunks within +/- return_window of each hit chunk (windowCandidates)
                cs_ = set()
                for c in g["hits"]:
                    for oc in self.page_chunks[g["page"]]:
                        if abs(self.ch_idx[oc] - self.ch_idx[c]) <= self.return_window:
                            a_, e_ = self.ch_span[oc]
                            cs_.update(i for i, (a, b) in enumerate(pieces) if a < e_ and b > a_)
                for an in anchors:
                    cs_.update(an)
                cands = sorted(cs_)
            title_tok = count_tokens(pg["title"])
            floor = title_tok + sum(ptok[i] for i in core)
            floor_chars = sum(pieces[i][1] - pieces[i][0] for i in core)
            blk = dict(g=g, pieces=pieces, ptok=ptok, anchors=anchors, sel=set(core), cands=cands, cut=False)
            if floor <= remaining and floor_chars <= EVIDENCE_BLOCK_CHAR_CAP:
                remaining -= floor
                blocks.append(blk)
            elif auto:  # conversation over budget -> raw chunk rows
                for c in g["hits"]:
                    passthrough.append((hits.index(c), c))
            elif not blocks and not dropped_any:  # rank one alone exceeds the budget: cut it to fit
                remaining -= min(title_tok, remaining)
                sel, chars = set(), 0
                for i in core:
                    ln = pieces[i][1] - pieces[i][0]
                    if ptok[i] <= remaining and chars + ln <= EVIDENCE_BLOCK_CHAR_CAP:
                        sel.add(i)
                        remaining -= ptok[i]
                        chars += ln
                    else:
                        if not sel:
                            sel.add(i)
                            remaining = 0
                        break
                blk.update(sel=sel, cut=True)
                blocks.append(blk)
            else:
                dropped_any = True
        for b in blocks:  # enrichment, rank order
            if b["cut"]:
                continue
            pieces, ptok, sel, anchors = b["pieces"], b["ptok"], b["sel"], b["anchors"]
            core = set(anchors[0])
            core_rank = {}
            for r, a in enumerate(anchors[1:]):
                for pi in a:
                    core_rank.setdefault(pi, r)
            cl = b["cands"]
            n = len(cl)
            dist = [0 if (c in core or c in core_rank) else 10**9 for c in cl]
            for i in range(1, n):
                dist[i] = min(dist[i], dist[i - 1] + 1)
            for i in range(n - 2, -1, -1):
                dist[i] = min(dist[i], dist[i + 1] + 1)
            pos = {c: j for j, c in enumerate(cl)}
            cand = [c for c in cl if c not in core]
            cand.sort(key=lambda i: (0 if i in core_rank else 1, core_rank.get(i, 0), dist[pos[i]], i))
            chars = sum(pieces[i][1] - pieces[i][0] for i in sel)
            for i in cand:
                adj = (i - 1) in sel or (i + 1) in sel
                cost = ptok[i] + (0 if adj else omit_tok)
                ln = pieces[i][1] - pieces[i][0] + (0 if adj else len(OMISSION))
                if cost > remaining or chars + ln > EVIDENCE_BLOCK_CHAR_CAP:
                    break
                sel.add(i)
                remaining -= cost
                chars += ln
        ranked = []
        for b in blocks:
            p = b["g"]["page"]
            pg = self.pages[p]
            text, pieces = pg["text"], b["pieces"]
            order = sorted(b["sel"])
            out, prev = "", None
            for i in order:
                if prev is not None and prev != i - 1:
                    out += OMISSION
                out += text[pieces[i][0]:pieces[i][1]]
                prev = i
            out = out.rstrip()
            covered = [tid for a, e, tid in pg["spans"]
                       if any(pieces[i][0] < e and pieces[i][1] > a for i in order)]
            ranked.append((b["g"]["rank"], Item(covered, self._header(p) + out, float(s[b["g"]["hits"][0]]),
                                                {"slug": pg["slug"], "unit": "page" if auto else "window"})))
        for rank, c in passthrough:
            ranked.append((rank, self._chunk_item(c, s[c])))
        ranked.sort(key=lambda x: x[0])
        return [it for _, it in ranked if it.turn_ids]
