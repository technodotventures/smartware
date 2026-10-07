"""mem0 (mem0ai/mem0, mem0ai 2.2.1, commit d62ce07) LLM-free retrieval baseline.

Implements section 13 of /home/claude/mp/reports/mem0.md. That section reproduces ``Memory.search`` (OSS, V3
pipeline; mem0/memory/main.py:1393-1827, mem0/utils/scoring.py) over memory units built the way ``add()`` would
build them without its LLM. The default is the FAITHFUL dense-pool variant.

FAITHFUL
- Candidate generation is dense only. The dense top N = max(4*top_k, 60) is the sole candidate pool
  (main.py:1655, 1680-1691). BM25 and entity signals only re-score inside that pool. A BM25-only or entity-only
  hit can never be returned.
- Threshold gate on RAW cosine, before fusion (scoring.py:111-113). The default threshold is 0.1, converted to
  a percentile (see THRESHOLDS).
- Additive fusion (scoring.py:59-139). Units without a BM25 or entity hit get 0 for that signal.
  ``score = min((cos + bm25_norm + entity_boost) / maxp, 1.0)``, where ``maxp = 1 + (1 if any BM25 hit) +
  (0.5 if any entity boost)``. Sort descending and return the top_k.
- BM25 normalisation: sigmoid ``1/(1+exp(-s*(raw-mid)))``, with (mid, s) chosen by the number of lemmatised
  query terms: <=3 -> (5,0.7), <=6 -> (7,0.6), <=9 -> (9,0.5), <=15 -> (10,0.5), else (12,0.5). Only raw > 0
  counts.
- Keyword arm = Qdrant (mem0's default store) sparse ``bm25`` vector. This is fastembed ``Qdrant/bm25`` over
  ``text_lemmatized``, with a collection-level IDF modifier. REIMPLEMENTED rather than taken from the shared
  BM25 because the raw scale feeds a fixed-midpoint sigmoid, and the scale differs materially:
  - fastembed uses a FIXED avg_len = 256 (k1=1.2, b=0.75). Short memories therefore get doc weights of about
    1.6 per term instead of about 1.0, which roughly doubles the raw score against the midpoint of 5.
  - The query sparse vector holds each unique term with weight 1.
  - IDF is ln(1 + (N - df + 0.5)/(df + 0.5)), the same as the shared BM25.
  - Tokens come from the shared ``bench.text.terms`` (stopwords removed, shared stemmer), standing in for
    spaCy lemmas re-stemmed by fastembed's snowball stemmer.
  - lemmatize_for_bm25 also appends the raw "-ing" token when it differs from its lemma. After fastembed
    stemming that only doubles the stem's tf, so a non-stopword "-ing" word whose stem differs gets tf + 1.
  - The keyword arm searches the same scope and keeps the top N with raw > 0.
- Entity boost (main.py:1747-1827):
  1. Take up to 8 query entities, deduplicated by normalised text.
  2. Search the scope's entity store for each one, top 500, keeping matches with similarity >= 0.5.
  3. boost = sim * 0.5 * 1/(1 + 0.001*(n_linked - 1)^2), max-pooled per memory.
- Entity store build (main.py:1100-1204), one add() batch per session:
  1. Deduplicate entities across the batch by normalised text.
  2. Match each against existing entities, by exact normalised text or else top-1 cosine >= 0.95.
  3. On a match, union the linked_memory_ids. Otherwise insert a new entity.
- Hash dedup: md5 of the memory text, skipped if already stored (main.py:1022-1026). mem0 only checks the 10
  nearest existing memories plus the same batch. We check all stored units, a superset that is harmless for
  exact duplicates.
- Item.text = the memory string mem0 returns (``memory`` / payload ``data``), i.e. what mem0's own proxy puts
  into context (mem0/proxy/main.py:82, newline-joined memories). Score = mem0's fused score. meta holds the
  ``explain`` score_details.
- No recency, no decay, no graph traversal, no token budgeting, no reranker (``rerank=False`` default). These
  match mem0 OSS. Time plays no part in ranking, so q.time is not used.

APPROXIMATED (no LLM)
- ADDITIVE_EXTRACTION_PROMPT facts are replaced by turn-level units:
  - Each turn is split on sentence boundaries into chunks of <= 80 words. A turn of <= 100 words stays whole.
  - Unit text = ``"<speaker>: <chunk> (on YYYY-MM-DD)"``. The date suffix stands in for the prompt's
    absolute-date grounding.
  - Reference resolution: a chunk under 12 words that starts with a pronoun gets the previous turn's first
    300 chars prepended as ``"[<prev speaker>: ...] "``. That turn is added to turn_ids.
  - Purely phatic units (fewer than 4 content terms and no entity) are skipped, as the prompt says.
  - Deviation: mem0 OSS grounds relative dates against wall-clock "today" (Observation Date defaults to now,
    prompts.py:1007-1013). We date units with the session date, which is what an extractor given the
    conversation date would do.
- spaCy entity extraction (utils/entity_extraction.py) is replaced by a regex/lexicon heuristic:
  - PROPER: runs of name-like capitalised tokens, with inner connectors of/the/for/at/in. A sentence-initial
    single token counts only if it is in a corpus proper-noun lexicon: words seen capitalised in a
    non-initial position at least half the time, plus the speaker names. This stands in for NER.
  - QUOTED: text in quotes.
  - IDENTIFIER: dotted identifiers.
  - TOPIC: maximal runs of 2-3 adjacent lowercase content words not overlapping a PROPER span. The head must
    not be in mem0's generic heads/endings, the modifier must not be a non-specific adjective, and words
    ending in -ly/-ed, plus a short list of common verbs, temporal adverbs and intensifiers (by shared stem),
    are excluded, since there is no POS tagger. This stands in for spaCy noun-chunk analysis.
  - mem0's generic single-term list, months/weekdays and spans of <= 2 chars are dropped.
  - Entities are extracted from the unit text without the date suffix and context prefix, as mem0 does on the
    memory text.
  - An exact normalised-text match counts as similarity 1.0, because identical strings embed identically in
    mem0. This matters for out-of-vocabulary words, which embed to zero here.

THRESHOLDS (the documented embedder is OpenAI text-embedding-3-small)
- Search threshold 0.1 (raw cosine):
  - On text-embedding-3-small, query-to-unrelated-text cosines sit around 0.05-0.2, so 0.1 is roughly the 25th
    percentile of a query's cosines over a conversation. In mem0 it almost never removes anything inside the
    top-N pool.
  - Mapped to the 25th percentile of this query's cosines over the scope (``threshold_pctl=0.25``).
  - DEV check: on this embedder that is about 0.006 (LoCoMo) and -0.018 (LME). With N = 200 it trims at most
    the tail of the pool, never everything. A raw 0.1 here would cut about 11% (LoCoMo) and 40% (LME) of the
    top-200 pool, far stricter than mem0. ``threshold_mode="raw"`` keeps the raw 0.1.
- Entity-search floor 0.5 (short entity strings): kept as is.
  - On text-embedding-3-small, unrelated short names score about 0.1-0.35 and close variants >= 0.5.
  - DEV check: about 1.0% (LoCoMo) and 0.18% (LME) of entity-entity pairs reach 0.5 here. Exact matches
    score 1.0. Neither "nothing" nor "everything" passes, so no conversion is needed.
- Entity dedup 0.95: kept. DEV: about 0.004% of LME pairs, all near-identical strings
  ("ancient egypt" / "egypt").

PARAMETERS: top_k = k (caller), N = max(4k, 60), entity weight 0.5, damping 0.001, entity top_k 500,
query entities <= 8, BM25 k1=1.2 b=0.75 avg_len=256, unit chunk <= 80 words (<= 100 kept whole),
context prefix 300 chars.

OPTIONS (not default): variant="fixed" (BM25 top-N and entity-linked units also nominate candidates, and the
threshold applies to the fused score); threshold_mode="raw".
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict

import numpy as np
import scipy.sparse as sp

from bench.system import Item, System
from bench.text import STOPWORDS, sentences, stem, terms, words

# ---------------------------------------------------------------- mem0 constants
ENTITY_BOOST_WEIGHT = 0.5             # utils/scoring.py
ENTITY_SIM_FLOOR = 0.5                # main.py:1807
ENTITY_DEDUP_SIM = 0.95               # main.py:1166
ENTITY_DAMPING = 0.001                # main.py:1816
ENTITY_SEARCH_TOPK = 500              # main.py:1789
MAX_QUERY_ENTITIES = 8                # main.py:1761
SEARCH_THRESHOLD = 0.1                # main.py:1399
THRESHOLD_PCTL = 0.25                 # 0.1 on text-embedding-3-small -> percentile (see docstring)
BM25_K1, BM25_B, BM25_AVG_LEN = 1.2, 0.75, 256.0   # fastembed Qdrant/bm25
UNIT_MAX_WORDS, UNIT_WHOLE_WORDS = 80, 100
CTX_CHARS = 300                       # prompts.py PAST_MESSAGE_TRUNCATION_LIMIT

_BM25_PARAMS = ((3, 5.0, 0.7), (6, 7.0, 0.6), (9, 9.0, 0.5), (15, 10.0, 0.5))

# mem0 entity_extraction.py filter lists (abridged to what a regex heuristic can use)
_GENERIC_HEADS = set("""thing stuff way time experience situation case fact matter issue idea thought feeling place
area part kind type sort lot bit day year week month moment instance example technique method approach process step
tool result outcome goal task item topic scale size level degree amount number style look color colour shape form
piece section side end edge surface point""".split())
_GENERIC_ENDINGS = set("""work works job jobs task tasks stuff things thing info information details data content
material materials activities activity efforts effort options option choices choice results result output outputs
products product items item""".split())
_NON_SPECIFIC_ADJ = set("""many few several some any all most more less much little enough various numerous multiple
countless great good bad nice terrible awful awesome amazing wonderful horrible excellent poor best worst fine okay new
old recent past future current previous next last first latest early late former modern ancient big small large tiny
huge enormous long short tall high low wide narrow thick thin deep shallow similar different same other another such
certain important main major minor key primary real actual true whole entire full complete total basic simple
interesting boring exciting special particular general common unique rare typical usual normal regular possible likely
potential available necessary only solo individual team group joint collaborative final initial side""".split())
_GENERIC_SINGLE = {"user", "assistant", "agent", "customer", "client", "person", "people", "human", "memory",
                   "message", "conversation", "chat", "session", "system", "top"}
_GENERIC_CAPS = set("""works items things stuff resources options tips ideas steps ways methods tools features benefits
examples details notes instructions guidelines recommendations suggestions overview summary conclusion introduction
pros cons advantages disadvantages""".split())
_MONTHS = {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
_COMMON_CAPS = {"i", "im", "i'm", "ive", "i've", "id", "i'd", "ill", "i'll", "the", "a", "an", "yes", "no", "thanks",
                "thank", "wow", "sure", "great", "awesome", "hey", "hi", "hello", "oh", "ok", "okay", "well", "so",
                "but", "and", "or", "that", "this", "it", "my", "we", "you", "what", "how", "when", "where", "why",
                "who", "which", "congrats", "congratulations", "haha", "lol", "sounds", "absolutely", "definitely",
                "totally", "exactly", "right", "true", "good", "nice", "cool", "glad", "love", "here", "there"}
_BAD_SINGLE = STOPWORDS | _GENERIC_SINGLE | _GENERIC_CAPS | _MONTHS | _COMMON_CAPS
# Non-noun words that spaCy's noun_chunks would never put in a compound (no POS tagger here):
# temporal adverbs, intensifiers and the most common verbs (matched on the shared stem).
_NOT_IN_CHUNK = {stem(w) for w in """yesterday today tomorrow tonight ago last next week weekend month year
pretty super quite rather always never often sometimes usually ever still already soon recently later lately
take make go come see know think feel want need try keep give find tell ask seem look sound use work call let put
mean become leave start begin help show hear play run move live believe hold bring happen write sit stand lose pay
meet include continue set learn change lead understand watch follow stop create speak read allow add spend grow
open walk win offer remember love consider appear buy wait serve die send expect build stay fall cut reach kill
remain suggest raise pass sell require report decide pull enjoy hope plan share check visit care sure glad happy
exciting excited amazing awesome great nice cool fun hard easy tough""".split()}
_CONNECTORS = {"of", "the", "for", "at", "in"}
_PRONOUN_START = re.compile(r"^(he|she|they|it|this|that|those|these|him|her|them|his|its|their)\b", re.I)
_TOKEN = re.compile(r"[A-Za-z0-9][\w'.-]*[A-Za-z0-9]|[A-Za-z0-9]|[^\w\s]")
_QUOTED = (re.compile(r'"([^"]+)"'), re.compile(r"(?:^|[\s(\[{,;])'([^']+)'(?=[\s.,;:!?)\]]|$)"),
           re.compile(r"[“]([^”]+)[”]"))
_IDENT = re.compile(r"[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)+")


def _norm(s: str) -> str:
    return " ".join(s.strip().lower().split())


def _bm25_params(n_terms: int) -> tuple[float, float]:
    for lim, mid, k in _BM25_PARAMS:
        if n_terms <= lim:
            return mid, k
    return 12.0, 0.5


def _lemma_terms(text: str) -> list[str]:
    """lemmatize_for_bm25 + fastembed stemming: shared terms, plus an extra '-ing' occurrence when it differs."""
    out = terms(text)
    for w in words(text):
        if w.endswith("ing") and len(w) >= 5 and w not in STOPWORDS and w.isalnum() and stem(w) != w:
            out.append(stem(w))
    return out


# ---------------------------------------------------------------- entity heuristic
def _proper_lexicon(texts: list[str], speakers: list[str]) -> set[str]:
    """Words capitalised mid-sentence at least half the time (stand-in for spaCy NER/PROPN)."""
    cap, tot = Counter(), Counter()
    for text in texts:
        for sent in sentences(text):
            toks = [t for t in _TOKEN.findall(sent) if t[0].isalnum()]
            for i, t in enumerate(toks):
                lw = t.lower()
                if i > 0:
                    tot[lw] += 1
                    if t[0].isupper():
                        cap[lw] += 1
    lex = {w for w, c in cap.items() if c >= 1 and c >= 0.5 * tot[w] and w not in _BAD_SINGLE and len(w) > 2}
    for s in speakers:
        if s and s.lower() not in _BAD_SINGLE:
            lex.add(s.lower())
    return lex


def extract_entities(text: str, lexicon: set[str]) -> list[tuple[str, str]]:
    cands: list[tuple[int, str, str]] = []  # (priority, type, text)
    spans_proper: list[tuple[int, int, int]] = []  # (sentence index, start, end) token spans
    for m in _IDENT.finditer(text):
        cands.append((1, "IDENTIFIER", m.group(0)))
    for si, sent in enumerate(sentences(text)):
        toks = _TOKEN.findall(sent)
        n = len(toks)

        def name_like(i: int) -> bool:
            t = toks[i]
            if not t[0].isalpha() or not t[0].isupper():
                return False
            lw = t.lower().strip("'")
            if lw in _BAD_SINGLE:
                return False
            initial = i == 0 or toks[i - 1] in {".", "!", "?", ":", "-", "*", "#", '"'}
            if initial:
                return lw in lexicon or any(c.isupper() or c.isdigit() for c in t[1:])
            return True

        i = 0
        while i < n:
            if not name_like(i):
                i += 1
                continue
            j = i + 1
            while j < n:
                if name_like(j):
                    j += 1
                elif toks[j].lower() in _CONNECTORS and j + 1 < n and name_like(j + 1):
                    j += 2
                else:
                    break
            span = " ".join(toks[i:j])
            if len(span) > 2 and not (j - i == 1 and toks[i].lower() in _BAD_SINGLE):
                cands.append((2, "PROPER", span))
                spans_proper.append((si, i, j))
            i = j
        # TOPIC: maximal runs of lowercase content words, 2-3 tokens, not overlapping a PROPER span
        covered = {k for s, a, b in spans_proper if s == si for k in range(a, b)}
        run: list[int] = []
        for k in range(n + 1):
            ok = False
            if k < n:
                t = toks[k]
                lw = t.lower()
                ok = (t.isalpha() and t.islower() and len(t) >= 3 and lw not in STOPWORDS and k not in covered
                      and not lw.endswith("ly") and not lw.endswith("ed") and stem(lw) not in _NOT_IN_CHUNK)
            if ok:
                run.append(k)
                continue
            if len(run) >= 2:
                r = run[-3:]
                while len(r) >= 2 and toks[r[0]].lower() in _NON_SPECIFIC_ADJ:
                    r = r[1:]
                if len(r) >= 2 and toks[r[-1]].lower() in _GENERIC_ENDINGS and len(r) > 2:
                    r = r[:-1]
                head = toks[r[-1]].lower() if r else ""
                if len(r) >= 2 and head not in _GENERIC_HEADS and head not in _GENERIC_ENDINGS:
                    cands.append((4, "TOPIC", " ".join(toks[x] for x in r)))
            run = []
    for rx in _QUOTED:
        for m in rx.finditer(text):
            q = m.group(1).strip()
            if len(q) > 2 and len(q) <= 100:
                cands.append((3, "QUOTED", q))
    best: dict[str, tuple[int, str, str]] = {}
    for c in cands:
        key = _norm(c[2])
        if len(key) <= 2 or key in _BAD_SINGLE:
            continue
        if key not in best or c[0] < best[key][0]:
            best[key] = c
    return [(c[1], c[2]) for c in sorted(best.values(), key=lambda c: c[0])]


# ---------------------------------------------------------------- system
class Mem0(System):
    name = "mem0"
    family = "memory-sdk"
    faithful_notes = (
        "Reproduces mem0 OSS 2.2.1 Memory.search (V3): the dense top N = max(4k, 60) is the only candidate pool; "
        "the raw-cosine threshold 0.1 (mapped to the 25th percentile of the query's cosines, since 0.1 on "
        "text-embedding-3-small is about that) gates before fusion. Score = min((cos + sigmoid-normalised BM25 + "
        "entity boost)/maxp, 1), maxp = 1 (+1 if any BM25 hit, +0.5 if any entity boost), with mem0's "
        "query-length-adaptive sigmoid (mid 5/7/9/10/12, slope 0.7/0.6/0.5). The keyword arm reimplements "
        "Qdrant's fastembed BM25 sparse vectors (fixed avg_len 256, unique query terms, IDF modifier) because "
        "its raw scale matters to the fixed sigmoid. Entity boost = sim*0.5/(1+0.001(n-1)^2), max-pooled, sim >= "
        "0.5, top 500, <= 8 query entities, over a per-scope entity store deduplicated by exact text or cosine "
        ">= 0.95 per session batch. Approximations: LLM fact extraction replaced by <= 80-word turn/sentence units "
        "'speaker: text (on date)' with pronoun-context prefixes, phatic skip and md5 dedup; spaCy entities "
        "replaced by a capitalisation/lexicon/quote/compound heuristic. No recency, graph traversal, reranker or "
        "budget (as in mem0 OSS); q.time unused."
    )

    def __init__(self, variant: str = "faithful", threshold_mode: str = "pctl"):
        assert variant in ("faithful", "fixed")
        assert threshold_mode in ("pctl", "raw")
        self.variant = variant
        self.threshold_mode = threshold_mode

    # -------------------------------------------------------------- build (add() without the LLM)
    def _units(self, corpus):
        order = {sid: i for i, sid in enumerate(corpus.session_order)}
        turns = sorted(corpus.turns, key=lambda t: (order.get(t.session_id, 1 << 30), t.idx))
        prev_in_session = {}
        units = []  # (session, turn_ids, text, core)
        for t in turns:
            prev = prev_in_session.get(t.session_id)
            prev_in_session[t.session_id] = t
            body = t.text.strip()
            if not body:
                continue
            if len(body.split()) <= UNIT_WHOLE_WORDS:
                chunks = [body]
            else:
                chunks, cur, cw = [], [], 0
                for s in sentences(body) or [body]:
                    sw = len(s.split())
                    if cur and cw + sw > UNIT_MAX_WORDS:
                        chunks.append(" ".join(cur))
                        cur, cw = [], 0
                    cur.append(s)
                    cw += sw
                if cur:
                    chunks.append(" ".join(cur))
            date = t.ts.strftime("%Y-%m-%d")
            for ch in chunks:
                core = f"{t.speaker}: {ch}"
                tids = [t.id]
                prefix = ""
                if prev is not None and _PRONOUN_START.match(ch) and len(ch.split()) < 12:
                    prefix = f"[{prev.speaker}: {prev.text[:CTX_CHARS]}] "
                    tids = [prev.id, t.id]
                units.append((t.session_id, tids, f"{prefix}{core} (on {date})", core, ch))
        return units

    def build(self, corpus, enc):
        self.enc = enc
        raw_units = self._units(corpus)
        self.lexicon = _proper_lexicon([t.text for t in corpus.turns], list(corpus.speakers))

        seen_hash = set()
        U = []  # (session, turn_ids, text, core, ents)
        for sid, tids, text, core, body in raw_units:
            h = hashlib.md5(core.lower().strip().encode()).hexdigest()
            if h in seen_hash:
                continue
            ents = extract_entities(core, self.lexicon)
            if len(terms(body)) < 4 and not [e for e in ents if _norm(e[1]) not in self.lexicon_speakers(corpus)]:
                continue  # phatic: no memorable content
            seen_hash.add(h)
            U.append((sid, tids, text, core, ents))
        if not U:  # degenerate corpus: keep everything rather than return nothing
            U = [(s, ti, tx, c, extract_entities(c, self.lexicon)) for s, ti, tx, c, _ in raw_units]

        self.n = len(U)
        self.turn_ids = [u[1] for u in U]
        self.text = [u[2] for u in U]
        self.E = enc.embed_many([u[2] for u in U])

        # ---- Qdrant/fastembed BM25 sparse index over text_lemmatized (core text, no date suffix/context)
        vocab: dict[str, int] = {}
        rows, cols, vals = [], [], []
        for i, u in enumerate(U):
            c = Counter(_lemma_terms(u[3]))
            dl = sum(c.values())
            norm = BM25_K1 * (1 - BM25_B + BM25_B * dl / BM25_AVG_LEN)
            for tkn, tf in c.items():
                j = vocab.setdefault(tkn, len(vocab))
                rows.append(i)
                cols.append(j)
                vals.append(tf * (BM25_K1 + 1) / (tf + norm))
        V = max(1, len(vocab))
        self.vocab = vocab
        self.kw = sp.csc_matrix((np.array(vals, np.float32), (rows, cols)), shape=(self.n, V))
        df = np.bincount(np.array(cols, dtype=np.int64), minlength=V).astype(np.float32)
        self.idf = np.log(1 + (self.n - df + 0.5) / (df + 0.5)).astype(np.float32)

        # ---- entity store, one add() batch per session (main.py Phase 7)
        self.ent_text: list[str] = []
        self.ent_key: dict[str, int] = {}
        self.ent_links: list[set[int]] = []
        ent_vecs: list[np.ndarray] = []
        by_session: dict[str, list[int]] = defaultdict(list)
        for i, u in enumerate(U):
            by_session[u[0]].append(i)
        dim = self.E.shape[1] if self.n else 1
        for sid in [s for s in corpus.session_order if s in by_session] + \
                   [s for s in by_session if s not in set(corpus.session_order)]:
            batch: dict[str, list] = {}
            for i in by_session[sid]:
                for _typ, et in U[i][4]:
                    k = _norm(et)
                    if k in batch:
                        batch[k][1].add(i)
                    else:
                        batch[k] = [et, {i}]
            if not batch:
                continue
            keys = list(batch)
            vecs = self.enc.embed_many([batch[k][0] for k in keys])
            existing = np.stack(ent_vecs) if ent_vecs else np.zeros((0, dim), np.float32)
            for k, v in zip(keys, vecs):
                et, mids = batch[k]
                idx = self.ent_key.get(k)
                if idx is None and len(existing):
                    sims = existing @ v
                    j = int(np.argmax(sims))
                    if sims[j] >= ENTITY_DEDUP_SIM:
                        idx = j
                if idx is not None:
                    self.ent_links[idx] |= mids
                else:
                    self.ent_key[k] = len(self.ent_text)
                    self.ent_text.append(et)
                    self.ent_links.append(set(mids))
                    ent_vecs.append(v)
        self.EV = np.stack(ent_vecs) if ent_vecs else np.zeros((0, dim), np.float32)
        self.ent_norm = [_norm(t) for t in self.ent_text]

    @staticmethod
    def lexicon_speakers(corpus) -> set[str]:
        return {s.lower() for s in corpus.speakers}

    # -------------------------------------------------------------- search()
    def _bm25(self, q_terms: list[str], N: int) -> dict[int, float]:
        uniq = list(dict.fromkeys(q_terms))
        raw = np.zeros(self.n, np.float32)
        for t in uniq:
            j = self.vocab.get(t)
            if j is None:
                continue
            col = self.kw.getcol(j)
            raw[col.indices] += self.idf[j] * col.data
        hit = np.nonzero(raw > 0)[0]
        if len(hit) == 0:
            return {}
        top = hit[np.argsort(-raw[hit])[:N]]
        mid, k = _bm25_params(len(q_terms) or 1)
        return {int(i): 1.0 / (1.0 + math.exp(-k * (float(raw[i]) - mid))) for i in top}

    def _entity_boosts(self, query: str) -> dict[int, float]:
        q_ents = extract_entities(query, self.lexicon)[:MAX_QUERY_ENTITIES]
        seen, ded = set(), []
        for _t, et in q_ents:
            k = _norm(et)
            if k and k not in seen:
                seen.add(k)
                ded.append(et)
        if not ded or not len(self.EV):
            return {}
        boosts: dict[int, float] = {}
        qv = self.enc.embed_many(ded)
        for et, v in zip(ded, qv):
            sims = self.EV @ v
            exact = self.ent_key.get(_norm(et))
            if exact is not None:
                sims[exact] = 1.0
            top = np.argsort(-sims)[:ENTITY_SEARCH_TOPK]
            for j in top:
                s = float(sims[j])
                if s < ENTITY_SIM_FLOOR:
                    break
                n_linked = max(len(self.ent_links[j]), 1)
                w = 1.0 / (1.0 + ENTITY_DAMPING * (n_linked - 1) ** 2)
                b = s * ENTITY_BOOST_WEIGHT * w
                for mid in self.ent_links[j]:
                    if b > boosts.get(mid, 0.0):
                        boosts[mid] = b
        return boosts

    def retrieve(self, q, k: int = 30) -> list[Item]:
        if self.n == 0:
            return []
        top_k = k
        N = max(top_k * 4, 60)
        cos = self.E @ self.enc.embed(q.text)
        thr = SEARCH_THRESHOLD if self.threshold_mode == "raw" else float(np.quantile(cos, THRESHOLD_PCTL))
        q_terms = _lemma_terms(q.text)
        bm25 = self._bm25(q_terms, N)
        ent = self._entity_boosts(q.text)

        dense = np.argsort(-cos)[:N]
        if self.variant == "fixed":
            cands = list(dict.fromkeys([int(i) for i in dense] + list(bm25) + list(ent)))
        else:
            cands = [int(i) for i in dense]

        maxp = 1.0 + (1.0 if bm25 else 0.0) + (ENTITY_BOOST_WEIGHT if ent else 0.0)
        scored = []
        for i in cands:
            sem = float(cos[i])
            if self.variant == "faithful" and sem < thr:
                continue
            b, e = bm25.get(i, 0.0), ent.get(i, 0.0)
            raw = sem + b + e
            final = min(raw / maxp, 1.0)
            if self.variant == "fixed" and final < thr:
                continue
            scored.append((final, i, sem, b, e, raw))
        scored.sort(key=lambda x: -x[0])
        out = []
        for final, i, sem, b, e, raw in scored[:top_k]:
            out.append(Item(list(self.turn_ids[i]), self.text[i], final,
                            {"semantic_score": sem, "bm25_score": b, "entity_boost": e, "raw_score": raw,
                             "max_possible_score": maxp, "threshold": thr}))
        return out
