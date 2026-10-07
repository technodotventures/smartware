"""Hindsight (vectorize-io/hindsight v0.10.2, commit ea5ab30) LLM-free retrieval baseline.

Implements section 13 of /home/claude/mp/reports/hindsight.md: ``MemoryEngine.recall_async`` with the
default budget (MID -> thinking_budget B = 300), ``max_tokens = 4096`` and all fact types, over units
built the way retain would build them without an LLM.

FAITHFUL
- Units: one memory unit per turn. Hindsight's no-LLM retain mode (``retain_extraction_mode="chunks"``)
  stores each chunk verbatim, and a turn longer than ``retain_chunk_size = 3000`` chars is split with the same
  recursive separators. fact_type is ``experience`` for assistant turns and ``world`` for everyone else.
  ``mentioned_at`` is the turn timestamp. ``occurred_start/end`` stay empty because only the LLM sets them.
- Embedded text = ``text + " (happened in <Month YYYY>)" + " [ent1, ent2]"`` (augment_texts_with_dates, which
  uses mentioned_at when there is no occurred_start). Stored text stays bare.
- Keyword arm = Postgres native ``ts_rank_cd(search_vector, to_tsquery('t1 | t2 | ...'))``, reimplemented
  rather than replaced by BM25 because it differs materially: it has no IDF and no length normalisation. With
  unweighted lexemes (weight D = 0.1) an OR query makes every matched position a one-lexeme cover, so the
  score is 0.1 x (occurrences of query lexemes). The ``@@`` gate requires at least one match.
  ``search_vector = text || context || text_signals``, where text_signals = entity names (dates only come from
  occurred_start, which is empty here). If the query has more than 16 tokens, only the 16 non-stopword tokens
  with the lowest document frequency are kept (bm25_term_selection; corpus df stands in for pg_stats).
- Arms run per fact type, each limited to B. The dense arm uses a cosine floor (semantic_min_similarity, see
  thresholds). Graph seeds are the first 20 dense rows at or above graph_seed_min_similarity.
- Graph arm (LinkExpansionRetriever):
  - Entity score = tanh(0.5 x distinct shared seed entities), over at most 200 units per entity. Hindsight
    orders those by ``unit_id DESC`` on random v4 UUIDs; here a hash of the unit stands in for the UUID.
  - Semantic-link score = MAX(weight) over kNN links to or from the seeds.
  - There are no causal links. activation = entity + semantic; keep the top B.
- Links built at retain time, one retain batch per session in session order:
  - Temporal: from each new unit to its 20 nearest same-type units in each time direction among the units
    stored so far (the batch included), weight max(0.3, 1 - dh/24), keeping the top 20 per unit by weight.
  - Semantic: from each new unit to the top-50 same-type earlier units with cosine >= the link threshold
    (ANN), plus the top 50 within the batch.
- Temporal arm (retrieve_temporal_combined_sql):
  1. Pool: units dated in the query window above the temporal cosine floor, top 60 by cosine.
  2. Entry points: 10, chosen by round-robin over 8 equal time buckets of the window.
  3. Proximity: prox = 1 - min(|t - mid| / (span/2), 1).
  4. Spreading: BFS over temporal links (weight >= 0.1, top 10 per node), propagated score = parent x w x 0.7,
     score = max(prox, propagated), continue only above 0.2. At most 5 iterations, batches of 20, B nodes.
  5. Neighbours are not filtered to the window, as in Hindsight.
- Query window: Hindsight's English period rules (yesterday, today, couple/few days|weeks|months ago,
  last week|month|year|weekend, "Month YYYY", "in|during YYYY" within ref-120..ref+20 years), then a small
  dateparser stand-in that scores spans the same way (digit 100, month 50, relative 50, weekday 30, period 20;
  a bare year scores 0). The best span becomes a single-day window.
- Fusion and scoring:
  - RRF k=60 over [semantic, keyword, graph, temporal], then trim to reranker_max_candidates = 300.
  - Base score from RRF rank, base = 1 - 0.9 i/(n-1). This is Hindsight's ``reranking="rrf"`` / rrf-provider
    passthrough path.
  - final = base x (1 + 0.2(recency - 0.5)) x (1 + 0.2(temporal - 0.5)), with linear recency
    max(0.1, min(1, 1 - days/365)). The proof-count boost is neutral because there are no observations.
  - Truncate to 2B.
  - Token budget of 4096 on fact text only: skip what does not fit and continue, and if nothing fits return
    the top unit (fact_budget.select_facts_within_budget).
- Reproduces Hindsight's fusion bug by default: each result keeps the first arm's copy (arm order semantic,
  keyword, graph, temporal), so a unit also found by an earlier arm loses its temporal proximity and gets
  the neutral 0.5 temporal boost.
- "now" for recency and relative dates is Hindsight's ``question_date`` (wall-clock now when unset). We pass
  q.time, the benchmark's "now".
- Item.text = one memory line as Hindsight's own LLM wrapper puts it into context
  (hindsight-integrations/litellm wrappers.py:985), minus the list number:
  ``[WORLD] (mentioned: <iso mentioned_at>) <fact text>``.

APPROXIMATED (no LLM, no neural models)
- LLM fact extraction (5W facts, absolute-date rewriting, occurred spans, causal links) is replaced by turn
  units, so there are no occurred dates, no causal links, no text_signals dates and no observations.
  ``prefer_observations`` and the proof boost are therefore inert.
- Entities: the LLM entity list is replaced by regex. Capitalised spans, numbers and the speaker name
  ("always include 'user'"). Resolution is exact case-insensitive name match. Hindsight's 0.6 merge score
  cannot fire usefully without co-occurrence history, and identical names score 1.0 in Hindsight anyway.
- The cross-encoder (default local ms-marco-MiniLM-L-6-v2) cannot run here, so the RRF passthrough path that
  Hindsight ships is used instead. This is a forced deviation from the default.
- dateparser is replaced by a stand-in covering ISO dates, "Month D[, YYYY]", "D Month [YYYY]", a bare
  month, weekdays, "N days|weeks|months|years ago" and tomorrow. dateparser's 200-language detection is
  not reproduced.

THRESHOLDS (the documented embedder is BAAI/bge-small-en-v1.5, whose cosines are compressed upward)
- semantic_min_similarity = graph_seed_min_similarity = 0.3. On bge-small almost every query-passage pair
  is above 0.3 (unrelated text sits around 0.4-0.6), so the floor is nearly inert. It maps to the 1st
  percentile of this query's cosines over the corpus.
- temporal_semantic_min_similarity = 0.1. On bge-small this passes everything, so no floor is applied.
- semantic_link_min_similarity = 0.7 for doc-doc pairs. Related bge-small pairs score at or above about 0.7,
  roughly the top decile within a conversation. It maps to the 90th percentile of this corpus's pairwise unit
  cosines (estimated from a sample).
- DEV check: on our embedder the 1st percentile of query cosines is about -0.15 and the 90th of pairwise
  cosines is about 0.31 (LME) to 0.48 (LoCoMo). Neither threshold removes everything or nothing.

PARAMETERS: B = 300 (low 100 / high 1000), RRF k = 60, rerank cap 300, final truncation 2B, max_tokens 4096,
graph seeds 20, per-entity cap 200, kNN links 50, temporal links 20/unit over 24 h with floor 0.3, temporal pool
60, 10 entries, 8 buckets, decay 0.7, BFS 5 x 20, continue > 0.2, recency window 365 d, boost alphas 0.2/0.2.

OPTIONS (not default): budget="low"|"high"; fix_temporal_boost=True (temporal proximity from any arm);
entities=False (pure "chunks" mode, which has no entity data).
"""
from __future__ import annotations

import calendar
import hashlib
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import numpy as np

from bench.system import Item, System
from bench.text import STOPWORDS, count_tokens, stem, terms

# ---------------------------------------------------------------- constants (Hindsight defaults)
BUDGETS = {"low": 100, "mid": 300, "high": 1000}       # recall_budget_fixed_*
RRF_K = 60
RERANK_MAX_CANDIDATES = 300
MAX_TOKENS = 4096
GRAPH_SEED_LIMIT = 20
PER_ENTITY_LIMIT = 200
BM25_MAX_QUERY_TERMS = 16
TS_RANK_D_WEIGHT = 0.1
RETAIN_CHUNK_SIZE = 3000
SEM_LINK_TOPK = 50
TEMPORAL_LINKS_PER_UNIT = 20
TEMPORAL_WINDOW_H = 24.0
TEMPORAL_POOL = 60
TEMPORAL_ENTRIES = 10
TEMPORAL_BUCKETS = 8
TEMPORAL_DECAY = 0.7
SPREAD_BATCH = 20
SPREAD_PER_SOURCE = 10
SPREAD_MAX_ITERS = 5
SPREAD_MIN_LINK_W = 0.1
SPREAD_CONTINUE = 0.2
RECENCY_WINDOW_DAYS = 365.0
RECENCY_ALPHA = 0.2
TEMPORAL_ALPHA = 0.2
# threshold -> percentile conversions (see module docstring)
SEM_FLOOR_PCTL = 0.01        # 0.3 on bge-small
SEM_LINK_PCTL = 0.90         # 0.7 on bge-small (doc-doc)
FACT_TYPES = ("world", "experience")

_SEPARATORS = ["\n\n", "\n", ". ", "! ", "? ", "; ", ", ", " "]
_CAP_SPAN = re.compile(r"\b[A-Z][\w'-]*(?:\s+[A-Z][\w'-]*)*")
_NUM = re.compile(r"\b\d[\d.,:/-]*\b")
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december"]
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_NON_ENTITY = STOPWORDS | set(_MONTHS) | set(_WEEKDAYS) | {
    "yes", "no", "thanks", "thank", "wow", "sure", "great", "awesome", "sounds", "absolutely", "please", "hello",
    "good", "nice", "cool", "haha", "lol", "definitely", "totally", "agreed", "exactly", "right", "true",
}
_RELATIVE = {"today", "yesterday", "tomorrow", "tonight", "now"}
_PERIOD = {"last", "next", "this", "past", "coming", "ago", "week", "weeks", "month", "months", "year", "years",
           "day", "days", "hour", "hours", "minute", "minutes", "quarter", "decade", "century", "weekend",
           "morning", "afternoon", "evening", "night", "noon", "midnight"}
_NUMWORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}


# ---------------------------------------------------------------- retain-side helpers
def _split_chunk(text: str, max_chars: int = RETAIN_CHUNK_SIZE) -> list[str]:
    """Recursive separator split, as fact_extraction._iter_recursive_splits."""
    if len(text) <= max_chars:
        return [text]
    for sep in _SEPARATORS:
        if sep in text:
            parts, cur = [], ""
            for piece in text.split(sep):
                cand = piece if not cur else cur + sep + piece
                if len(cand) <= max_chars:
                    cur = cand
                else:
                    if cur:
                        parts.append(cur)
                    cur = piece
            if cur:
                parts.append(cur)
            out = []
            for p in parts:
                out.extend(_split_chunk(p, max_chars) if len(p) > max_chars else [p])
            return out
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def _entities(text: str, speaker: str) -> list[str]:
    seen, out = set(), []

    def add(name: str):
        key = name.lower().strip()
        if key and key not in seen:
            seen.add(key)
            out.append(name.strip())

    add(speaker)
    for m in _CAP_SPAN.finditer(text):
        span = m.group(0)
        toks = span.split()
        while toks and toks[0].lower().strip("'") in _NON_ENTITY:
            toks = toks[1:]
        if not toks:
            continue
        name = " ".join(toks)
        if len(name) < 2 or name.lower() in _NON_ENTITY:
            continue
        add(name)
    for m in _NUM.finditer(text):
        add(m.group(0).rstrip(".,"))
    return out


def _pseudo_uuid(uid: str) -> int:
    return int(hashlib.md5(uid.encode()).hexdigest()[:15], 16)


# ---------------------------------------------------------------- query-time temporal parsing
def _day(d: datetime) -> tuple[datetime, datetime]:
    return (d.replace(hour=0, minute=0, second=0, microsecond=0),
            d.replace(hour=23, minute=59, second=59, microsecond=999999))


def _span(a: datetime, b: datetime) -> tuple[datetime, datetime]:
    return _day(a)[0], _day(b)[1]


def _period(q: str, ref: datetime):
    """temporal_periods._extract_non_chinese_period (English rules)."""
    if re.search(r"\byesterday\b", q):
        d = ref - timedelta(days=1)
        return _span(d, d)
    if re.search(r"\btoday\b", q):
        return _span(ref, ref)
    for pat, a, b in ((r"\b(a\s+)?couple\s+(of\s+)?days?\s+ago\b", 3, 1), (r"\b(a\s+)?few\s+days?\s+ago\b", 5, 2),
                      (r"\b(a\s+)?couple\s+(of\s+)?weeks?\s+ago\b", 21, 7), (r"\b(a\s+)?few\s+weeks?\s+ago\b", 35, 14),
                      (r"\b(a\s+)?couple\s+(of\s+)?months?\s+ago\b", 90, 30),
                      (r"\b(a\s+)?few\s+months?\s+ago\b", 150, 60)):
        if re.search(pat, q):
            return _span(ref - timedelta(days=a), ref - timedelta(days=b))
    if re.search(r"\blast\s+week\b", q):
        s = ref - timedelta(days=ref.weekday() + 7)
        return _span(s, s + timedelta(days=6))
    if re.search(r"\blast\s+month\b", q):
        end = ref.replace(day=1) - timedelta(days=1)
        return _span(end.replace(day=1), end)
    if re.search(r"\blast\s+year\b", q):
        return _span(datetime(ref.year - 1, 1, 1), datetime(ref.year - 1, 12, 31))
    if re.search(r"\blast\s+weekend\b", q):
        back = (ref.weekday() + 2) % 7
        if back <= 1:
            back += 7
        sat = ref - timedelta(days=back)
        return _span(sat, sat + timedelta(days=1))
    for mi, mname in enumerate(_MONTHS, 1):
        if re.search(rf"\b\d{{1,2}}\s+{mname}\b", q):
            continue
        m = re.search(rf"\b{mname}\s+(\d{{4}})\b", q)
        if m:
            y = int(m.group(1))
            if y < 1:
                return "none"
            return _span(datetime(y, mi, 1), datetime(y, mi, calendar.monthrange(y, mi)[1]))
    m = re.search(r"\b(?:in|during|throughout|year)\s+(\d{4})\b(?![-/.]\d)", q)
    if m:
        y = int(m.group(1))
        if max(1, ref.year - 120) <= y <= ref.year + 20:
            return _span(datetime(y, 1, 1), datetime(y, 12, 31))
    return None


def _score_span(text: str) -> int:
    toks = re.findall(r"[a-z0-9]+", text.lower())
    ts = set(toks)
    s = 0
    if any(any(c.isdigit() for c in t) for t in toks):
        nums = [t for t in toks if t.isdigit()]
        if len(nums) == 1 and len(nums[0]) == 4 and len(toks) == 1:
            return 0  # bare four-digit integer
        s += 100
    if ts & set(_MONTHS):
        s += 50
    if ts & _RELATIVE:
        s += 50
    if ts & set(_WEEKDAYS):
        s += 30
    if ts & _PERIOD:
        s += 20
    return s


def _past_date(ref: datetime, month: int, day: int, year: int | None) -> datetime | None:
    try:
        if year is not None:
            return datetime(year, month, day)
        d = datetime(ref.year, month, min(day, calendar.monthrange(ref.year, month)[1]))
        if d.date() > ref.date():  # PREFER_DATES_FROM = past
            d = datetime(ref.year - 1, month, min(day, calendar.monthrange(ref.year - 1, month)[1]))
        return d
    except ValueError:
        return None


def _dateparser_like(q: str, ref: datetime):
    """Stand-in for dateparser.search_dates + DateparserQueryAnalyzer scoring -> single-day window."""
    cands: list[tuple[str, datetime]] = []
    mon = "|".join(_MONTHS)
    for m in re.finditer(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", q):
        try:
            cands.append((m.group(0), datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))))
        except ValueError:
            pass
    for m in re.finditer(rf"\b({mon})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b", q):
        d = _past_date(ref, _MONTHS.index(m.group(1)) + 1, int(m.group(2)), int(m.group(3)) if m.group(3) else None)
        if d:
            cands.append((m.group(0), d))
    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({mon})(?:,?\s+(\d{{4}}))?\b", q):
        d = _past_date(ref, _MONTHS.index(m.group(2)) + 1, int(m.group(1)), int(m.group(3)) if m.group(3) else None)
        if d:
            cands.append((m.group(0), d))
    for m in re.finditer(rf"\b({mon})\b", q):
        d = _past_date(ref, _MONTHS.index(m.group(1)) + 1, ref.day, None)
        if d:
            cands.append((m.group(0), d))
    for m in re.finditer(r"\b(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
                         r"(day|week|month|year)s?\s+ago\b", q):
        n = int(m.group(1)) if m.group(1).isdigit() else _NUMWORDS[m.group(1)]
        unit = m.group(2)
        try:
            if unit == "day":
                d = ref - timedelta(days=n)
            elif unit == "week":
                d = ref - timedelta(weeks=n)
            elif unit == "month":
                y, mo = divmod(ref.month - 1 - n, 12)
                y += ref.year
                d = ref.replace(year=y, month=mo + 1, day=min(ref.day, calendar.monthrange(y, mo + 1)[1]))
            else:
                d = ref.replace(year=ref.year - n)
        except ValueError:
            continue
        cands.append((m.group(0), d))
    for m in re.finditer(r"\b(?:last\s+)?(" + "|".join(_WEEKDAYS) + r")\b", q):
        wd = _WEEKDAYS.index(m.group(1))
        back = (ref.weekday() - wd) % 7
        if m.group(0).startswith("last") and back == 0:
            back = 7
        cands.append((m.group(0), ref - timedelta(days=back)))
    if re.search(r"\btomorrow\b", q):
        cands.append(("tomorrow", ref + timedelta(days=1)))
    scored = [(_score_span(t), len(t), d) for t, d in cands]
    scored = [s for s in scored if s[0] > 0]
    if not scored:
        return None
    _, _, d = max(scored, key=lambda s: (s[0], s[1]))
    return _day(d)


def extract_window(query: str, ref: datetime):
    q = query.lower()
    p = _period(q, ref)
    if p == "none":
        return None
    if p is not None:
        return p
    return _dateparser_like(q, ref)


# ---------------------------------------------------------------- the system
class Hindsight(System):
    name = "hindsight"
    family = "memory-sdk"
    faithful_notes = (
        "Reproduces Hindsight recall_async (budget MID, B=300, max_tokens 4096) over turn-level units (the no-LLM "
        "'chunks' retain mode at turn granularity; world/experience by speaker role). Four arms per fact type: "
        "dense (cosine floor), Postgres native ts_rank_cd reimplemented (0.1 x matched lexeme occurrences, OR "
        "query, 16 lowest-df terms for long queries), graph link expansion (tanh(0.5 x shared entities) over 200 "
        "units/entity + max kNN-link weight from 20 dense seeds), and a temporal arm (60-pool, 10 bucket-spread "
        "entries, BFS over 24h temporal links with decay 0.7) when the query parses to a window via Hindsight's "
        "period rules or a dateparser stand-in. RRF k=60, trim 300, RRF-passthrough base score (the cross-encoder "
        "cannot run) times +/-10% linear-365d recency and +/-10% temporal proximity (keeping Hindsight's "
        "first-arm-wins fusion bug), truncation to 2B and a 4096-token skip-and-continue budget. Approximations: "
        "regex entities instead of LLM extraction, no occurred dates/causal links/observations, embedder-scale "
        "thresholds mapped to percentiles (0.3 -> 1st pct of query cosines, 0.7 link -> 90th pct of pairwise, "
        "0.1 -> none). q.time is used as Hindsight's question_date 'now'."
    )

    def __init__(self, budget: str = "mid", fix_temporal_boost: bool = False, entities: bool = True,
                 max_tokens: int = MAX_TOKENS):
        self.B = BUDGETS[budget]
        self.fix_temporal_boost = fix_temporal_boost
        self.use_entities = entities
        self.max_tokens = max_tokens

    # ------------------------------------------------------------ build (retain)
    def build(self, corpus, enc):
        self.enc = enc
        order = {sid: i for i, sid in enumerate(corpus.session_order)}
        turns = sorted(corpus.turns, key=lambda t: (order.get(t.session_id, 1 << 30), t.idx))
        U = []  # (turn_id, text, ft, ts, session)
        for t in turns:
            ft = "experience" if t.role == "assistant" else "world"
            for piece in _split_chunk(t.text):
                U.append((t.id, f"{t.speaker}: {piece}", ft, t.ts, t.session_id, t.speaker))
        n = len(U)
        self.tid = [u[0] for u in U]
        self.text = [u[1] for u in U]
        self.ft = np.array([u[2] for u in U])
        self.ts = [u[3] for u in U]
        self.tsec = np.array([u[3].timestamp() for u in U], dtype=np.float64)
        sess = [u[4] for u in U]
        self.ents = [_entities(u[1].split(": ", 1)[1], u[5]) if self.use_entities else [] for u in U]
        self.uuid = np.array([_pseudo_uuid(f"{u[0]}#{i}") for i, u in enumerate(U)], dtype=np.int64)

        # embeddings: text + (happened in Month YYYY) + [entities]
        dense_texts = []
        for i in range(n):
            s = f"{self.text[i]} (happened in {self.ts[i]:%B %Y})"
            if self.ents[i]:
                s += f" [{', '.join(self.ents[i])}]"
            dense_texts.append(s)
        self.E = enc.embed_many(dense_texts).astype(np.float32)

        # search_vector = text || context || text_signals(entity names) ; ts_rank_cd needs per-doc tf
        self.post: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        tmp: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i in range(n):
            c = Counter(terms(self.text[i] + " " + " ".join(self.ents[i])))
            for term, k in c.items():
                tmp[term].append((i, k))
        for term, lst in tmp.items():
            a = np.array(lst, dtype=np.int64)
            self.post[term] = (a[:, 0], a[:, 1].astype(np.float32))
        self.n = n

        # entity postings (unit_entities), ordered by pseudo-uuid DESC
        ent_units: dict[str, list[int]] = defaultdict(list)
        self.unit_ents: list[set[str]] = []
        for i in range(n):
            keys = {e.lower() for e in self.ents[i]}
            self.unit_ents.append(keys)
            for k in keys:
                ent_units[k].append(i)
        self.ent_units = {k: sorted(v, key=lambda j: -self.uuid[j]) for k, v in ent_units.items()}

        # semantic-link threshold: 90th percentile of pairwise cosine (sampled)
        rng = np.random.default_rng(0)
        if n > 1:
            a = rng.integers(0, n, 20000)
            b = rng.integers(0, n, 20000)
            m = a != b
            pair = np.einsum("ij,ij->i", self.E[a[m]], self.E[b[m]])
            self.link_thr = float(np.quantile(pair, SEM_LINK_PCTL)) if len(pair) else 1.0
        else:
            self.link_thr = 1.0

        # links, one retain batch per session
        self.tlinks: dict[int, list[tuple[int, float]]] = defaultdict(list)   # from -> [(to, w)]
        sem_out: dict[int, dict[int, float]] = defaultdict(dict)
        stored: dict[str, list[int]] = {ft: [] for ft in FACT_TYPES}
        batches: dict[str, list[int]] = defaultdict(list)
        for i in range(n):
            batches[sess[i]].append(i)
        for sid in sorted(batches, key=lambda s: order.get(s, 1 << 30)):
            batch = batches[sid]
            prior = {ft: np.array(stored[ft], dtype=np.int64) for ft in FACT_TYPES}
            for ft in FACT_TYPES:
                new = [i for i in batch if self.ft[i] == ft]
                if not new:
                    continue
                new_a = np.array(new, dtype=np.int64)
                # semantic: ANN over prior units + within batch, top 50 each, >= threshold
                for i in new:
                    for pool in (prior[ft], new_a[new_a != i]):
                        if len(pool) == 0:
                            continue
                        sims = self.E[pool] @ self.E[i]
                        top = np.argsort(-sims, kind="stable")[:SEM_LINK_TOPK]
                        for j in top:
                            if sims[j] >= self.link_thr:
                                to = int(pool[j])
                                w = float(min(1.0, max(0.0, sims[j])))
                                sem_out[i][to] = max(sem_out[i].get(to, 0.0), w)
                stored[ft].extend(new)
                # temporal: 20 nearest earlier/later among everything stored so far, weight, cap 20
                allft = np.array(stored[ft], dtype=np.int64)
                for i in new:
                    others = allft[allft != i]
                    ot = self.tsec[others]
                    earlier = others[ot <= self.tsec[i]]
                    later = others[ot > self.tsec[i]]
                    e_sel = earlier[np.argsort(-self.tsec[earlier], kind="stable")[:TEMPORAL_LINKS_PER_UNIT]]
                    l_sel = later[np.argsort(self.tsec[later], kind="stable")[:TEMPORAL_LINKS_PER_UNIT]]
                    cand = []
                    for j in np.concatenate([e_sel, l_sel]):
                        dh = abs(self.tsec[j] - self.tsec[i]) / 3600.0
                        cand.append((dh, int(j), max(0.3, 1.0 - dh / TEMPORAL_WINDOW_H)))
                    cand.sort(key=lambda x: x[0])
                    self.tlinks[i] = [(j, w) for _, j, w in cand[:TEMPORAL_LINKS_PER_UNIT]]
        # spreading reads top-10 links per source by weight DESC (weight >= 0.1)
        self.tlinks_top = {i: sorted([x for x in v if x[1] >= SPREAD_MIN_LINK_W], key=lambda x: -x[1])[:SPREAD_PER_SOURCE]
                           for i, v in self.tlinks.items()}
        # semantic links usable in both directions
        self.sem_out = sem_out
        sem_in: dict[int, dict[int, float]] = defaultdict(dict)
        for f, d in sem_out.items():
            for t, w in d.items():
                sem_in[t][f] = max(sem_in[t].get(f, 0.0), w)
        self.sem_in = sem_in
        self.ft_idx = {ft: np.where(self.ft == ft)[0] for ft in FACT_TYPES}

    # ------------------------------------------------------------ arms
    def _keyword(self, query: str, ft_mask: np.ndarray) -> list[int]:
        tokens = re.sub(r"[^\w\s]", " ", query.lower()).split()
        if len(tokens) > BM25_MAX_QUERY_TERMS:
            lex = []
            for pos, tok in enumerate(tokens):
                if tok in STOPWORDS:
                    continue
                st = stem(tok)
                df = len(self.post[st][0]) if st in self.post else 0
                lex.append((df, pos, st))
            lex.sort()
            qterms = {st for _, _, st in lex[:BM25_MAX_QUERY_TERMS]}
        else:
            qterms = {stem(t) for t in tokens if t not in STOPWORDS}
        score = np.zeros(self.n, dtype=np.float32)
        for t in qterms:
            p = self.post.get(t)
            if p is not None:
                score[p[0]] += TS_RANK_D_WEIGHT * p[1]
        cand = np.where((score > 0) & ft_mask)[0]
        return [int(i) for i in cand[np.argsort(-score[cand], kind="stable")][: self.B]]

    def _graph(self, seeds: list[int], ft: str) -> list[tuple[int, float]]:
        if not seeds:
            return []
        seed_set = set(seeds)
        seed_ents = set().union(*(self.unit_ents[s] for s in seeds))
        counts: dict[int, int] = defaultdict(int)
        for e in seed_ents:
            taken = 0
            for u in self.ent_units.get(e, []):
                if u in seed_set or self.ft[u] != ft:
                    continue
                counts[u] += 1
                taken += 1
                if taken >= PER_ENTITY_LIMIT:
                    break
        ent_rows = sorted(counts.items(), key=lambda x: (-x[1], -self.uuid[x[0]]))[: self.B]
        sem: dict[int, float] = {}
        for s in seeds:
            for d in (self.sem_out.get(s, {}), self.sem_in.get(s, {})):
                for u, w in d.items():
                    if u in seed_set or self.ft[u] != ft:
                        continue
                    if w > sem.get(u, -1.0):
                        sem[u] = w
        sem_rows = sorted(sem.items(), key=lambda x: -x[1])[: self.B]
        act: dict[int, float] = defaultdict(float)
        for u, c in ent_rows:
            act[u] += math.tanh(c * 0.5)
        for u, w in sem_rows:
            act[u] += w
        return sorted(act.items(), key=lambda x: -x[1])[: self.B]

    def _temporal(self, ft: str, cos: np.ndarray, win) -> list[tuple[int, float, float]]:
        start, end = win
        s0, s1 = start.timestamp(), end.timestamp()
        idx = self.ft_idx[ft]
        inwin = idx[(self.tsec[idx] >= s0) & (self.tsec[idx] <= s1)]
        if len(inwin) == 0:
            return []
        pool = inwin[np.argsort(-cos[inwin], kind="stable")[:TEMPORAL_POOL]]
        span = s1 - s0
        # coverage selection
        if len(pool) <= TEMPORAL_ENTRIES:
            entries = list(pool)
        else:
            buckets: dict[int, list[int]] = {}
            for u in pool:  # pool is similarity-desc
                b = 0 if span <= 0 else max(0, min(int((self.tsec[u] - s0) / span * TEMPORAL_BUCKETS),
                                                   TEMPORAL_BUCKETS - 1))
                buckets.setdefault(b, []).append(int(u))
            entries, tier = [], 0
            while len(entries) < TEMPORAL_ENTRIES and any(len(b) > tier for b in buckets.values()):
                row = sorted([b[tier] for b in buckets.values() if len(b) > tier], key=lambda u: -cos[u])
                for u in row:
                    if len(entries) < TEMPORAL_ENTRIES:
                        entries.append(u)
                tier += 1
        total_days = span / 86400.0
        mid = s0 + span / 2

        def prox(u: int) -> float:
            if total_days <= 0:
                return 1.0
            days = abs(self.tsec[u] - mid) / 86400.0
            return 1.0 - min(days / (total_days / 2), 1.0)

        results: list[tuple[int, float, float]] = []
        visited: set[int] = set()
        node_scores: dict[int, float] = {}
        for u in entries:
            u = int(u)
            visited.add(u)
            p = prox(u)
            results.append((u, p, p))
            node_scores[u] = 1.0
        frontier = list(node_scores)
        budget = self.B - len(entries)
        it = 0
        while frontier and budget > 0 and it < SPREAD_MAX_ITERS:
            it += 1
            batch, frontier = frontier[:SPREAD_BATCH], frontier[SPREAD_BATCH:]
            best: dict[int, float] = {}
            for src in batch:
                ps = node_scores.get(src, 0.5)
                for to, w in self.tlinks_top.get(src, []):
                    if to in visited or self.ft[to] != ft:
                        continue
                    prop = ps * w * TEMPORAL_DECAY
                    if prop > best.get(to, -1.0):
                        best[to] = prop
            for to, prop in sorted(best.items(), key=lambda x: -x[1]):
                visited.add(to)
                budget -= 1
                p = prox(to)
                sc = max(p, prop)
                results.append((to, sc, p))
                if budget > 0 and sc > SPREAD_CONTINUE:
                    node_scores[to] = sc
                    frontier.append(to)
                if budget <= 0:
                    break
        return results

    # ------------------------------------------------------------ recall
    def retrieve(self, q, k: int = 30) -> list[Item]:
        now = q.time
        qv = self.enc.embed(q.text)
        cos = self.E @ qv
        floor = float(np.quantile(cos, SEM_FLOOR_PCTL)) if self.n else 0.0
        win = extract_window(q.text, now)

        semantic, keyword, graph, temporal = [], [], [], []   # (unit, arm_score)
        tprox: dict[int, float] = {}
        for ft in FACT_TYPES:
            idx = self.ft_idx[ft]
            if len(idx) == 0:
                continue
            ok = idx[cos[idx] >= floor]
            fetch = max(self.B, GRAPH_SEED_LIMIT)
            rows = ok[np.argsort(-cos[ok], kind="stable")[:fetch]]
            semantic += [(int(u), float(cos[u])) for u in rows[: self.B]]
            seeds = [int(u) for u in rows if cos[u] >= floor][:GRAPH_SEED_LIMIT]
            mask = self.ft == ft
            keyword += [(u, 0.0) for u in self._keyword(q.text, mask)]
            graph += self._graph(seeds, ft)
            if win is not None:
                for u, sc, p in self._temporal(ft, cos, win):
                    temporal.append((u, sc))
                    tprox[u] = p
        semantic.sort(key=lambda x: -x[1])
        graph.sort(key=lambda x: -x[1])
        temporal.sort(key=lambda x: -x[1])
        lists = [[u for u, _ in semantic], [u for u, _ in keyword], [u for u, _ in graph]]
        if temporal:
            lists.append([u for u, _ in temporal])

        # RRF; first occurrence (arm order) owns the result object
        rrf: dict[int, float] = {}
        first_arm: dict[int, int] = {}
        for ai, lst in enumerate(lists):
            for r, u in enumerate(lst, 1):
                rrf[u] = rrf.get(u, 0.0) + 1.0 / (RRF_K + r)
                first_arm.setdefault(u, ai)
        merged = sorted(rrf, key=lambda u: -rrf[u])[:RERANK_MAX_CANDIDATES]
        if not merged:
            return []

        # RRF passthrough base, then multiplicative boosts
        nn = len(merged)
        denom = max(1, nn - 1)
        scored = []
        for rank, u in enumerate(merged):
            base = 1.0 - 0.9 * rank / denom
            days = (now - self.ts[u]).total_seconds() / 86400.0
            rec = max(0.1, min(1.0, 1.0 - days / RECENCY_WINDOW_DAYS))
            has_t = u in tprox and (self.fix_temporal_boost or first_arm[u] == 3)
            t = tprox[u] if has_t else 0.5
            final = base * (1 + RECENCY_ALPHA * (rec - 0.5)) * (1 + TEMPORAL_ALPHA * (t - 0.5))
            scored.append((u, final))
        scored.sort(key=lambda x: -x[1])
        top = scored[: self.B * 2]

        # token budget on fact text: skip-and-continue, floor of one
        sel, used = [], 0
        for u, s in top:
            c = count_tokens(self.text[u])
            if used + c > self.max_tokens:
                continue
            used += c
            sel.append((u, s))
        if not sel and top:
            sel = [top[0]]

        out = []
        for u, s in sel[:k]:
            text = f"[{self.ft[u].upper()}] (mentioned: {self.ts[u].isoformat()}) {self.text[u]}"
            out.append(Item([self.tid[u]], text, float(s),
                            {"rrf": rrf[u], "first_arm": ["semantic", "keyword", "graph", "temporal"][first_arm[u]]}))
        return out
