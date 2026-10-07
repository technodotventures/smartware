"""Graphiti (getzep/graphiti v0.30.2) LLM-free retrieval baseline.

Implements section 13 of /home/claude/mp/reports/graphiti.md.

Default mode reproduces ``Graphiti.search(query, num_results=k)`` (graphiti.py:1653), the path used by
the MCP tool ``search_memory_facts`` and REST ``/search``: recipe ``EDGE_HYBRID_SEARCH_RRF`` =
edge (fact) BM25 + edge cosine, fused by RRF, top ``limit`` facts. Graphiti's ``search_`` default
(``COMBINED_HYBRID_SEARCH_CROSS_ENCODER``) needs an LLM / paid reranker, so it is not used.

FAITHFUL
- Candidate depth 2*limit per channel (search.py:286, 297); limit = k (``search_config.limit = num_results``).
- Cosine gate: Graphiti keeps ``vector.similarity.cosine(...) > 0.6`` on Neo4j, i.e. (1+cos)/2 > 0.6,
  i.e. raw cosine > 0.2 (DEFAULT_MIN_SCORE, search_utils.py:65). Applied here as raw cos > 0.2.
- RRF with rank constant 1 and 0-based ranks: score = sum 1/(rank+1) (search_utils.py:1775); ties keep
  first-seen order (BM25 list is iterated first, Python stable sort over dict insertion order).
- reranker_min_score = 0, so nothing is dropped after fusion.
- BM25 channel disabled when the query has >= 128 words (+1 group id) (search_utils.py:108).
- No recency/decay, no validity filtering (superseded facts are not filtered by default), no token budget.
  q.time is never used: Graphiti's search has no "now".
- Item.text = one entry of Graphiti's <FACTS> context block (search_helpers.py):
  json.dumps({"fact", "valid_at": str(valid_at), "invalid_at": "Present"}).
- Exact-duplicate facts (same normalised text) are merged and accumulate source episodes, like the
  resolve_extracted_edge fast path (edge_operations.py:706); Item.turn_ids = the fact's episodes.
- Entity resolution ladder (used only by the optional modes): exact normalised name, then entropy-gated
  (len >= 6 or >= 2 tokens, char entropy >= 1.5) 3-gram Jaccard >= 0.9 (dedup_helpers.py). Jaccard is
  computed exactly via a shingle inverted index instead of MinHash/LSH (same threshold, no LSH misses).
- Entity summary = append-only concatenation of its facts while <= 2*MAX_SUMMARY_CHARS = 2000 chars
  (node_operations.py:906, the no-LLM path).

APPROXIMATED (no LLM)
- LLM fact extraction -> sentence units per turn (bench.text.sentences), fragments < 4 words merged into
  the previous sentence; first-person pronouns rewritten to the speaker name (I/me/my/mine/myself, I'm,
  I've, I'll, I'd); sentences with no detected entity are prefixed "<speaker>: ". No relation-type name
  is indexed (Graphiti's fulltext index covers e.name + e.fact).
- valid_at = the source turn's timestamp (Graphiti: LLM-extracted, = episode time for ongoing facts).
  invalid_at always "Present": no contradiction detection/invalidation without an LLM.
- Entities = speaker + capitalised spans (regex) + possessive kinship ("Nisha's dad").
- Shared BM25 (k1=1.2, b=0.75, stemmed, stopwords removed) stands in for Lucene/RediSearch BM25.
- Cosine threshold kept at the project's value on our embedder (no reliable percentile mapping for
  text-embedding-3-small). DEV check: raw cos > 0.2 passes ~15% (LoCoMo) / ~5% (LME) of units per query,
  so it neither removes everything nor nothing; it rarely binds within the top 2*limit.

OPTIONAL MODES (not default)
- mode="combined": COMBINED_HYBRID_SEARCH_RRF scopes minus communities (edges, entity nodes, episodes),
  per-scope limit ceil(k/3), concatenated in context-string order FACTS, ENTITIES, EPISODES.
  (Deviation: Graphiti returns `limit` per scope; the harness caps total items at k.)
- reranker="episode_mentions": stable-sort RRF result by number of source episodes (search.py:450).
- bfs=True: add the edge-BFS channel (depth 3, directed from each unit's first entity, seeded by the
  first-stage hits, BFS order, 2*limit) as in COMBINED_HYBRID_SEARCH_CROSS_ENCODER's edge methods.

PARAMETERS: limit=k, candidates 2k/channel, RRF const 1, cos gate 0.2 raw (0.6 normalised), BFS depth 3,
fuzzy Jaccard 0.9, entropy 1.5, min name length 6 / 2 tokens, summary cap 2000 chars, BM25 query cap 128 words.
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import timezone

import numpy as np

from bench.system import Item, System
from bench.text import STOPWORDS, sentences, words

DEFAULT_MIN_SCORE = 0.6           # Graphiti, on (1+cos)/2
RAW_COS_MIN = 2 * DEFAULT_MIN_SCORE - 1  # = 0.2 raw cosine
RRF_RANK_CONST = 1
MAX_QUERY_LENGTH = 128
MAX_SEARCH_DEPTH = 3
SUMMARY_APPEND_CAP = 2000         # 2 * MAX_SUMMARY_CHARS
FUZZY_JACCARD = 0.9
ENTROPY_MIN = 1.5
MIN_NAME_LEN = 6
MIN_TOKENS = 2

_FIRST_PERSON = [
    (re.compile(r"\bI'm\b"), "{s} is"), (re.compile(r"\bI've\b"), "{s} has"),
    (re.compile(r"\bI'll\b"), "{s} will"), (re.compile(r"\bI'd\b"), "{s} would"),
    (re.compile(r"\bI\b"), "{s}"), (re.compile(r"\b(?:me|myself)\b", re.I), "{s}"),
    (re.compile(r"\b(?:my|mine)\b", re.I), "{s}'s"),
]
_CAP_SPAN = re.compile(r"\b([A-Z][\w'-]*(?:\s+[A-Z][\w'-]*)*)")
_KIN = re.compile(r"\b([A-Z][\w-]*)'s\s+(dad|mom|mother|father|sister|brother|husband|wife|spouse|son|daughter|"
                  r"uncle|aunt|cousin|grandma|grandpa|friend|boss|teacher|neighbor|roommate|dog|cat|pet|puppy|kitten)\b")
_NON_ENTITY = STOPWORDS | {
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "yes", "thanks", "thank", "wow", "sure", "great", "awesome", "sounds", "absolutely", "here", "please",
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def _fuzzy(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", " ", _norm(s))).strip()


def _shingles(n: str) -> set[str]:
    c = n.replace(" ", "")
    if len(c) < 2:
        return {c} if c else set()
    return {c[i:i + 3] for i in range(len(c) - 2)}


def _high_entropy(n: str) -> bool:
    if len(n) < MIN_NAME_LEN and len(n.split()) < MIN_TOKENS:
        return False
    chars = n.replace(" ", "")
    if not chars:
        return False
    cnt = defaultdict(int)
    for ch in chars:
        cnt[ch] += 1
    ent = -sum(v / len(chars) * math.log2(v / len(chars)) for v in cnt.values())
    return ent >= ENTROPY_MIN


def _rrf(lists: list[list[int]]) -> tuple[list[int], list[float]]:
    scores: dict[int, float] = defaultdict(float)
    for lst in lists:
        for r, u in enumerate(lst):
            scores[u] += 1.0 / (r + RRF_RANK_CONST)
    order = sorted(scores, key=lambda u: scores[u], reverse=True)  # stable: ties keep first-seen order
    return order, [scores[u] for u in order]


class Graphiti(System):
    name = "graphiti"
    family = "graph-engine"
    faithful_notes = (
        "Reproduces Graphiti.search() (EDGE_HYBRID_SEARCH_RRF, used by MCP search_memory_facts and REST /search): "
        "fact-level BM25 + cosine (raw cos > 0.2, i.e. Neo4j's normalised score > 0.6), 2*k candidates each, RRF with "
        "rank constant 1, top k facts, no recency, no validity filtering, text = Graphiti's <FACTS> JSON entry. LLM fact "
        "extraction is approximated by sentence units with first-person pronouns rewritten to the speaker; valid_at is "
        "the turn timestamp and invalid_at is always 'Present' because contradiction-based invalidation needs an LLM. "
        "Exact-duplicate facts merge their source turns. Optional modes (combined scopes, episode_mentions reranker, "
        "BFS channel) mirror other shipped recipes but are off by default."
    )

    def __init__(self, mode: str = "edges", reranker: str = "rrf", bfs: bool = False):
        self.mode, self.reranker, self.bfs = mode, reranker, bfs

    # ------------------------------------------------------------------ build
    def build(self, corpus, enc):
        self.enc = enc
        self.turns = corpus.turns
        speakers = {t.speaker for t in corpus.turns}
        self.ent_names: list[str] = []
        self.ent_by_exact: dict[str, list[int]] = defaultdict(list)
        self.ent_shingles: list[set[str]] = []
        self.shingle_index: dict[str, list[int]] = defaultdict(list)
        self.ent_units: list[list[int]] = []
        self.ent_summary: list[str] = []

        units: list[dict] = []
        by_text: dict[str, int] = {}
        for t in corpus.turns:
            for s in self._split(t.text):
                s2 = s
                for pat, rep in _FIRST_PERSON:
                    s2 = pat.sub(rep.format(s=t.speaker), s2)
                ents = self._entities(s2, t.speaker, speakers)
                if not ents:
                    s2 = f"{t.speaker}: {s2}"
                    ents = [self._resolve(t.speaker)]
                key = _norm(s2)
                if key in by_text:  # exact-duplicate fact: append episode
                    u = units[by_text[key]]
                    if t.id not in u["episodes"]:
                        u["episodes"].append(t.id)
                    continue
                by_text[key] = len(units)
                units.append({"text": s2, "episodes": [t.id], "ts": t.ts, "ents": ents})
        self.units = units
        for ui, u in enumerate(units):
            for e in dict.fromkeys(u["ents"]):
                self.ent_units[e].append(ui)
                cur = self.ent_summary[e]
                nxt = f"{cur}\n{u['text']}".strip()
                if len(nxt) <= SUMMARY_APPEND_CAP:
                    self.ent_summary[e] = nxt
        texts = [u["text"] for u in units]
        self.bm25 = enc.bm25(texts)
        self.E = enc.embed_many(texts)
        if self.mode == "combined":
            self.node_bm25 = enc.bm25([f"{n} {s}" for n, s in zip(self.ent_names, self.ent_summary)])
            self.node_E = enc.embed_many(self.ent_names)
            self.ep_texts = [f"{t.speaker}: {t.text}" for t in corpus.turns]
            self.ep_bm25 = enc.bm25(self.ep_texts)

    @staticmethod
    def _split(text: str) -> list[str]:
        out: list[str] = []
        for s in sentences(text):
            if out and len(words(s)) < 4:
                out[-1] = f"{out[-1]} {s}"
            else:
                out.append(s)
        return out or [text]

    def _entities(self, s: str, speaker: str, speakers: set[str]) -> list[int]:
        names: list[str] = []
        for sp in speakers:
            if re.search(rf"\b{re.escape(sp)}\b", s):
                names.append(sp)
        for m in _KIN.finditer(s):
            names.append(f"{m.group(1)}'s {m.group(2)}")
        for m in _CAP_SPAN.finditer(s):
            span = m.group(1).strip()
            toks = span.split()
            while toks and toks[0].lower() in _NON_ENTITY:
                toks = toks[1:]
            span = " ".join(toks)
            if span and span.lower() not in _NON_ENTITY and span not in names and len(span) > 1:
                names.append(span)
        return [self._resolve(n) for n in names]

    def _resolve(self, name: str) -> int:
        ex = _norm(name)
        hits = self.ent_by_exact.get(ex, [])
        if len(hits) == 1:
            return hits[0]
        fz = _fuzzy(name)
        if not hits and _high_entropy(fz):
            sh = _shingles(fz)
            overlap: dict[int, int] = defaultdict(int)
            for g in sh:
                for e in self.shingle_index.get(g, ()):
                    overlap[e] += 1
            best, best_j = -1, 0.0
            for e, inter in overlap.items():
                j = inter / (len(sh) + len(self.ent_shingles[e]) - inter)
                if j > best_j:
                    best, best_j = e, j
            if best >= 0 and best_j >= FUZZY_JACCARD:
                return best
        # new entity (ambiguous exact hits would go to the LLM in Graphiti; here they stay separate)
        i = len(self.ent_names)
        self.ent_names.append(name)
        self.ent_by_exact[ex].append(i)
        sh = _shingles(fz)
        self.ent_shingles.append(sh)
        for g in sh:
            self.shingle_index[g].append(i)
        self.ent_units.append([])
        self.ent_summary.append("")
        return i

    # --------------------------------------------------------------- retrieve
    @staticmethod
    def _top(scores: np.ndarray, n: int, mask: np.ndarray) -> list[int]:
        idx = np.flatnonzero(mask)
        if idx.size == 0:
            return []
        order = idx[np.argsort(-scores[idx], kind="stable")][:n]
        return order.tolist()

    def _hybrid(self, bm25, E, query: str, qv: np.ndarray, limit: int) -> list[list[int]]:
        lists = []
        if len(query.split(" ")) + 1 < MAX_QUERY_LENGTH:
            b = bm25.score(query)
            lists.append(self._top(b, 2 * limit, b > 0))
        c = E @ qv
        lists.append(self._top(c, 2 * limit, c > RAW_COS_MIN))
        return lists

    def _edge_bfs(self, origin_units: list[int], limit: int) -> list[int]:
        seen_u: list[int] = []
        seen = set()
        frontier = list(dict.fromkeys(self.units[u]["ents"][0] for u in origin_units))
        visited = set(frontier)
        for _ in range(MAX_SEARCH_DEPTH):
            nxt = []
            for e in frontier:
                for u in self.ent_units[e]:
                    if self.units[u]["ents"][0] != e:  # directed: e is the fact's source
                        continue
                    if u not in seen:
                        seen.add(u)
                        seen_u.append(u)
                        if len(seen_u) >= 2 * limit:
                            return seen_u
                    for t in self.units[u]["ents"][1:]:
                        if t not in visited:
                            visited.add(t)
                            nxt.append(t)
            frontier = nxt
        return seen_u

    def _fact_item(self, u: int, score: float) -> Item:
        unit = self.units[u]
        va = unit["ts"].replace(tzinfo=timezone.utc) if unit["ts"].tzinfo is None else unit["ts"]
        text = json.dumps({"fact": unit["text"], "valid_at": str(va), "invalid_at": "Present"}, ensure_ascii=False)
        return Item(list(unit["episodes"]), text, float(score), {"type": "fact"})

    def _edge_search(self, query: str, qv: np.ndarray, limit: int) -> list[Item]:
        lists = self._hybrid(self.bm25, self.E, query, qv, limit)
        if self.bfs:
            origins = [u for lst in lists for u in lst]
            lists.append(self._edge_bfs(origins, limit) if origins else [])
        order, scores = _rrf(lists)
        if self.reranker == "episode_mentions":
            pairs = sorted(zip(order, scores), key=lambda p: len(self.units[p[0]]["episodes"]), reverse=True)
            order, scores = [p[0] for p in pairs], [p[1] for p in pairs]
        return [self._fact_item(u, s) for u, s in zip(order[:limit], scores[:limit])]

    def retrieve(self, q, k=30):
        qv = self.enc.embed(q.text.replace("\n", " "))
        if not q.text.strip():
            return []
        if self.mode != "combined":
            return self._edge_search(q.text, qv, k)
        lim = math.ceil(k / 3)
        facts = self._edge_search(q.text, qv, lim)
        order, scores = _rrf(self._hybrid(self.node_bm25, self.node_E, q.text, qv, lim))
        nodes = []
        for e, s in zip(order[:lim], scores[:lim]):
            tids = list(dict.fromkeys(t for u in self.ent_units[e] if self.units[u]["text"] in self.ent_summary[e]
                                      for t in self.units[u]["episodes"]))
            if tids:
                text = json.dumps({"entity_name": self.ent_names[e], "summary": self.ent_summary[e]}, ensure_ascii=False)
                nodes.append(Item(tids, text, float(s), {"type": "entity"}))
        eps = []
        if len(q.text.split(" ")) + 1 < MAX_QUERY_LENGTH:
            b = self.ep_bm25.score(q.text)
            for i in self._top(b, lim, b > 0):
                text = json.dumps({"source_description": "", "content": self.ep_texts[i]}, ensure_ascii=False)
                eps.append(Item([self.turns[i].id], text, 1.0 / (len(eps) + 1), {"type": "episode"}))
        return (facts + nodes + eps)[:k]
