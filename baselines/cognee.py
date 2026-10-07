"""Cognee (topoteretes/cognee v1.6.2) LLM-free retrieval baseline.

Implements section 13 of /home/claude/mp/reports/cognee.md: the default ``HYBRID_COMPLETION`` retriever
(``cognee/modules/retrieval/hybrid_retriever.py``) with the lane budgets of Cognee's published BEAM-100K
configuration ``hybrid_completion_20_20_qa_v1`` (chunks_top_k=20, entities_top_k=20, other HybridRetriever
defaults). No reader LLM: the three context sections are returned as ranked items.

FAITHFUL
- Unit = one conversation turn pair (user message + following reply), one Cognee chunk, as in Cognee's BEAM
  ingestion (JsonListChunker, one turn per chunk, no overlap). Text carries Cognee's BEAM location prefix
  ``[Session s, Turn t, date D]`` (conversation_preprocessing._format_chunk_prefix) and ``Speaker: text`` lines.
- Chunk lane (hybrid/chunks.py, ranking.py): dense chunk search with candidate limit 2*CK; second ranked view
  limited to CK; RRF over 0-based ranks, score = sum 1/(rrf_k + rank + 1), rrf_k = max(30, min(60, 20 + 2*CK))
  (= 60 at CK=20); importance factor 0.75 + 0.5*0.5 = 1.0 (neutral default); sort key
  (-score, -rrf, min_rank, chunk_id); keep CK.
- Entity lane (hybrid/entities.py): dense top EK over entity *names*; Entity ids/names normalised exactly as
  Cognee (identity key lower, spaces->_, apostrophes stripped; stored name = lower, apostrophes stripped).
  For each hit, one-hop neighbourhood edges become bullets, deduped by (src, rel, tgt), sorted by the rank of
  the bullet's edge text in the fact search (unranked after, in graph insertion order), capped at MAX_EDGES=10.
  Rendered as ``### <name>`` + ``- <edge text>`` lines. Cognee's LanceDB payload (IndexSchema) carries neither
  description nor type for entities, so neither is rendered (verified in LanceDBAdapter.index_data_points).
- Fact lane (hybrid/facts.py): every distinct edge text (including chunk "contains" edges, text
  ``Document chunk mentions <name>: <description>``) is one EdgeType vector (dedupe by text). Search limit
  EK*MAX_EDGES + FACTS_K = 205; skip texts already shown as bullets and texts < 3 words (MIN_FACT_WORD_COUNT);
  budget FACTS_K=5 when entities exist, else EK*MAX_EDGES; contains texts shown as ``<Name>: <description>``.
- Section order = Cognee's context order: passages, entities, facts. No recency, decay, time filter, token
  budget or validity filtering (Cognee's hybrid has none). q.time is never used.

APPROXIMATED (no LLM)
- LLM graph extraction -> regex entities per sentence (bench.text.sentences): capitalised spans (leading
  stopwords stripped, function words / months / greetings dropped) and explicit dates; plus the speaker name
  (when it is a proper name) for every sentence of that speaker's message, standing in for the LLM's
  coreference of "I". Relations -> one co-occurrence edge per entity pair per sentence, edge text = the sentence.
  Because the LLM would give each distinct fact its own relationship name, the sentence acts as the relation
  identity, so Cognee's (src, rel, tgt) edge dedupe becomes (src, tgt, sentence) here.
  Entity "description" in a contains edge = the first sentence of that unit mentioning the entity.
- The TextSummary_text lane (LLM retrieval summary of each chunk) -> shared BM25 over the unit text: an
  independent second ranking of the same unit, which is the role the summary rank plays in Cognee's RRF.
- Shared dense embedder stands in for text-embedding-3-large; cosine distance = 1 - dot. No thresholds are
  applied in this lane, so no scale conversion is needed.

OPTIONS (not default)
- lanes="search_default": per-lane top-k as cognee.search()/recall() pass them (min(top_k, 10) = 10 for
  chunks, entities and facts budget defaults: CK=10, EK=10, MAX_EDGES=10, FACTS_K=10).
- mode="graph_completion": recipe step 13, Cognee's GRAPH_COMPLETION brute-force triplet search: top-100 hits
  per collection (units, entity names, edge texts); candidate triplets = every edge incident to a seed node;
  distance per element = cosine distance if in its collection's top-100 else 6.5; element distance multiplied
  by (2 - importance 0.5) = 1.5; score = sum of the three; top 15 (search() top_k); text = Cognee's
  ``src --[rel]--> tgt  (edge_text)`` connection line.

PARAMETERS: CK=20, EK=20, MAX_EDGES=10, FACTS_K=5, chunk candidates 2*CK, summary-lane candidates CK,
RRF k=60, MIN_FACT_WORDS=3; graph mode: wide_search_top_k=100, penalty 6.5, importance 0.5, top 15.

DEVIATION from the report's recipe step 12: instead of flattening entity bullets and facts back to their
source units, items keep Cognee's actual context text (entity blocks and fact lines), as required by the
harness rule that Item.text is what the project puts into context.
"""
from __future__ import annotations

import re
from collections import defaultdict

import numpy as np

from bench.system import Item, System
from bench.text import STOPWORDS, sentences

CK, EK, MAX_EDGES, FACTS_K = 20, 20, 10, 5
MIN_FACT_WORD_COUNT = 3
CONTAINS_PREFIX = "Document chunk mentions "
WIDE_SEARCH_TOP_K = 100
TRIPLET_PENALTY = 6.5
IMPORTANCE = 0.5
GRAPH_TOP_K = 15

_CAP_SPAN = re.compile(r"\b[A-Z][\w'&-]*(?:\s+(?:(?:of|the|de|van|von|and)\s+)*[A-Z][\w'&-]*)*")
_MONTHS = ("january february march april may june july august september october november december").split()
_MON = "|".join(m.capitalize() for m in _MONTHS)
_DATE = re.compile(
    rf"\b(?:\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}\s+(?:{_MON})\s+\d{{4}}"
    rf"|(?:{_MON})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*\d{{4}})?|(?:{_MON})\s+\d{{4}})\b"
)
_NON_ENTITY = STOPWORDS | set(_MONTHS) | {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "yes", "no", "thanks",
    "thank", "wow", "sure", "great", "awesome", "sounds", "absolutely", "here", "please", "hello", "good",
    "nice", "cool", "glad", "congrats", "congratulations", "haha", "lol", "omg", "user", "assistant", "today",
    "yesterday", "tomorrow", "first", "next", "last", "also", "however", "additionally", "finally", "overall",
}


def _identity(name: str) -> str:
    return name.lower().replace(" ", "_").replace("'", "")


def _node_name(name: str) -> str:
    return name.lower().replace("'", "")


def _label(speaker: str) -> str:
    return speaker.capitalize() if speaker.islower() else speaker


def _proper_speaker(speaker: str) -> bool:
    return bool(speaker) and speaker[0].isupper() and speaker.lower() not in ("user", "assistant")


def _spot(sentence: str) -> list[str]:
    found: list[str] = []
    for m in _DATE.finditer(sentence):
        found.append(m.group(0))
    for m in _CAP_SPAN.finditer(sentence):
        toks = m.group(0).split()
        while toks and toks[0].lower().strip("'") in _NON_ENTITY:
            toks = toks[1:]
        while toks and toks[-1].lower() in ("of", "the", "de", "van", "von", "and"):
            toks = toks[:-1]
        if not toks:
            continue
        span = " ".join(toks)
        if all(t.lower().strip("'") in _NON_ENTITY for t in toks) or span == "I" or len(span) < 2:
            continue
        if span.lower().endswith("'s"):
            span = span[:-2]
        found.append(span)
    return found


class Cognee(System):
    name = "cognee"
    family = "graph-engine"
    faithful_notes = (
        "Reproduces Cognee's default HYBRID_COMPLETION retriever with its published BEAM-100K lane budgets "
        "(chunks 20, entities 20, 10 edges per entity, 5 facts): one turn pair per chunk with Cognee's BEAM "
        "'[Session s, Turn t, date D]' prefix; chunk lane = RRF (k=60, 0-based ranks) of a dense chunk ranking "
        "(2*CK candidates) and a second view of the same chunk (Cognee: LLM TextSummary vectors; here BM25), "
        "neutral importance factor; entity lane = dense entity-name hits with one-hop edge bullets ranked by the "
        "fact search; fact lane = dense search over distinct edge texts, deduped against bullets, >= 3 words. "
        "LLM entity/relation extraction is approximated by regex entities (capitalised spans, dates, proper-name "
        "speaker) and sentence co-occurrence edges whose text is the sentence. No time, recency or validity logic, "
        "as in Cognee. Items are Cognee's context sections in order: passages, entity blocks, fact lines."
    )

    def __init__(self, mode: str = "hybrid", lanes: str = "beam_100k"):
        assert mode in ("hybrid", "graph_completion")
        self.mode = mode
        if lanes == "search_default":
            self.ck, self.ek, self.max_edges, self.facts_k = 10, 10, 10, 10
        else:
            self.ck, self.ek, self.max_edges, self.facts_k = CK, EK, MAX_EDGES, FACTS_K

    # ------------------------------------------------------------------ build
    def build(self, corpus, enc):
        self.enc = enc
        sess_no = {sid: i + 1 for i, sid in enumerate(corpus.session_order)}
        by_sess: dict[str, list] = defaultdict(list)
        for t in corpus.turns:
            by_sess[t.session_id].append(t)

        # units: user message + following reply (one Cognee chunk each)
        self.units: list[dict] = []
        for sid in corpus.session_order:
            ts = sorted(by_sess.get(sid, []), key=lambda x: x.idx)
            i, n = 0, 0
            while i < len(ts):
                group = [ts[i]]
                if i + 1 < len(ts) and ts[i + 1].speaker != ts[i].speaker:
                    group.append(ts[i + 1])
                i += len(group)
                n += 1
                prefix = f"[Session {sess_no[sid]}, Turn {n}, date {group[0].ts.strftime('%Y-%m-%d')}]"
                body = "\n".join(f"{_label(t.speaker)}: {t.text}" for t in group)
                self.units.append({"turns": group, "text": f"{prefix}\n{body}", "id": f"{sid}#{n}"})

        # graph: entities, contains edges, co-occurrence edges
        self.ent_name: dict[str, str] = {}          # identity -> stored (normalised) name
        self.ent_edges: dict[str, list[int]] = defaultdict(list)  # identity -> edge indices (insertion order)
        self.edges: list[dict] = []                 # {src, tgt, rel, text, unit}
        edge_seen: set[tuple] = set()
        for u_idx, u in enumerate(self.units):
            first_desc: dict[str, str] = {}
            for t in u["turns"]:
                for s in sentences(t.text):
                    ents = []
                    if _proper_speaker(t.speaker):
                        ents.append(t.speaker)
                    ents += _spot(s)
                    keys = []
                    for e in ents:
                        k = _identity(e)
                        if not k.strip("_"):
                            continue
                        self.ent_name.setdefault(k, _node_name(e))
                        if k not in keys:
                            keys.append(k)
                        first_desc.setdefault(k, s)
                    for a in range(len(keys)):
                        for b in range(a + 1, len(keys)):
                            key = (keys[a], keys[b], s)
                            if key in edge_seen:
                                continue
                            edge_seen.add(key)
                            self._add_edge(keys[a], keys[b], "related_to", s, u_idx)
            for k, desc in first_desc.items():
                text = f"{CONTAINS_PREFIX}{self.ent_name[k]}: {desc}"
                self._add_edge(f"unit:{u_idx}", k, "contains", text, u_idx)

        # vector collections
        self.unit_texts = [u["text"] for u in self.units]
        self.U = enc.embed_many(self.unit_texts)
        self.bm25 = enc.bm25(self.unit_texts)
        self.ent_keys = list(self.ent_name)
        self.E = enc.embed_many([self.ent_name[k] for k in self.ent_keys])
        self.fact_texts: list[str] = []
        self.fact_units: dict[str, set[int]] = defaultdict(set)
        for e in self.edges:
            if e["text"] not in self.fact_units:
                self.fact_texts.append(e["text"])
            self.fact_units[e["text"]].add(e["unit"])
        self.F = enc.embed_many(self.fact_texts)

    def _add_edge(self, src, tgt, rel, text, unit):
        idx = len(self.edges)
        self.edges.append({"src": src, "tgt": tgt, "rel": rel, "text": text, "unit": unit})
        for node in (src, tgt):
            if not node.startswith("unit:"):
                self.ent_edges[node].append(idx)

    def _turn_ids(self, unit_ids) -> list[str]:
        out = []
        for u in sorted(unit_ids):
            out += [t.id for t in self.units[u]["turns"]]
        return list(dict.fromkeys(out))

    @staticmethod
    def _top(sims: np.ndarray, n: int) -> list[int]:
        if n <= 0 or sims.size == 0:
            return []
        n = min(n, sims.size)
        idx = np.argpartition(-sims, n - 1)[:n]
        return sorted(idx.tolist(), key=lambda i: (-sims[i], i))

    # --------------------------------------------------------------- retrieve
    def retrieve(self, q, k=30):
        qv = self.enc.embed(q.text)
        if self.mode == "graph_completion":
            return self._graph_completion(q, qv, k)
        return self._hybrid(q, qv, k)

    def _hybrid(self, q, qv, k):
        items: list[Item] = []
        # chunk lane
        ck = self.ck
        vec = self._top(self.U @ qv, 2 * ck)
        bm = self.bm25.score(q.text)
        summ = [i for i in self._top(bm, ck) if bm[i] > 0]
        rrf_k = max(30, min(60, 20 + 2 * ck))
        ranks: dict[int, list[int]] = defaultdict(list)
        for r, u in enumerate(vec):
            ranks[u].append(r)
        for r, u in enumerate(summ):
            ranks[u].append(r)
        imp = 0.75 + 0.5 * IMPORTANCE
        scored = []
        for u, rs in ranks.items():
            rrf = sum(1.0 / (rrf_k + r + 1) for r in rs)
            scored.append((rrf * imp, rrf, min(rs), self.units[u]["id"], u))
        scored.sort(key=lambda x: (-x[0], -x[1], x[2], x[3]))
        for s, _, _, _, u in scored[:ck]:
            items.append(Item(self._turn_ids([u]), self.units[u]["text"], float(s), {"section": "passages"}))

        # entity + fact lanes
        ent_hits = self._top(self.E @ qv, self.ek) if self.ent_keys else []
        fact_sims = self.F @ qv if self.fact_texts else np.zeros(0)
        fact_hits = self._top(fact_sims, self.ek * self.max_edges + self.facts_k)
        fact_rank = {self.fact_texts[i]: r for r, i in enumerate(fact_hits)}
        bullet_texts: set[str] = set()
        ent_sims = self.E @ qv if self.ent_keys else np.zeros(0)
        for r, ei in enumerate(ent_hits):
            key = self.ent_keys[ei]
            seen, bullets = set(), []
            for pos, idx in enumerate(self.ent_edges[key]):
                e = self.edges[idx]
                dk = (e["src"], e["rel"], e["tgt"], e["text"])
                if dk in seen:
                    continue
                seen.add(dk)
                rank = fact_rank.get(e["text"])
                bullets.append(((1, rank) if rank is not None else (2, pos), e))
            bullets.sort(key=lambda b: b[0])
            bullets = [e for _, e in bullets[: self.max_edges]]
            if not bullets:
                continue
            bullet_texts.update(e["text"] for e in bullets)
            text = f"### {self.ent_name[key]}\n" + "\n".join(f"- {e['text']}" for e in bullets)
            items.append(Item(self._turn_ids({e["unit"] for e in bullets}), text, float(ent_sims[ei]),
                              {"section": "entities", "rank": r}))
        budget = self.facts_k if ent_hits else self.ek * self.max_edges
        n_facts = 0
        for i in fact_hits:
            if n_facts >= budget:
                break
            t = self.fact_texts[i]
            if t in bullet_texts or len(t.split()) < MIN_FACT_WORD_COUNT:
                continue
            bullet_texts.add(t)
            shown = t
            if shown.startswith(CONTAINS_PREFIX):
                shown = shown[len(CONTAINS_PREFIX):]
                shown = shown[:1].upper() + shown[1:]
            items.append(Item(self._turn_ids(self.fact_units[t]), f"- {shown}", float(fact_sims[i]),
                              {"section": "facts"}))
            n_facts += 1
        return items[:k]

    def _graph_completion(self, q, qv, k):
        w = WIDE_SEARCH_TOP_K
        us, es, fs = self.U @ qv, self.E @ qv, self.F @ qv
        node_d: dict[str, float] = {}
        for i in self._top(us, w):
            node_d[f"unit:{i}"] = 1.0 - float(us[i])
        for i in self._top(es, w):
            node_d[self.ent_keys[i]] = 1.0 - float(es[i])
        edge_d = {self.fact_texts[i]: 1.0 - float(fs[i]) for i in self._top(fs, w)}
        seeds = set(node_d)
        cand = [e for e in self.edges if e["src"] in seeds or e["tgt"] in seeds]
        mult = 2 - IMPORTANCE

        def score(e):
            return (mult * node_d.get(e["src"], TRIPLET_PENALTY) + mult * node_d.get(e["tgt"], TRIPLET_PENALTY)
                    + mult * edge_d.get(e["text"], TRIPLET_PENALTY))

        top = sorted(cand, key=score)[: min(GRAPH_TOP_K, k)]
        items = []
        for e in top:
            def name(n):
                if n.startswith("unit:"):
                    words = self.units[int(n[5:])]["text"].split()
                    return " ".join(words[:7]) + "..."
                return self.ent_name[n]
            line = f"{name(e['src'])} --[{e['rel']}]--> {name(e['tgt'])}  ({e['text']})"
            items.append(Item(self._turn_ids([e["unit"]]), line, -score(e), {"section": "connections"}))
        return items
