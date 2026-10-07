"""MCP reference "memory" server (modelcontextprotocol/servers src/memory, HEAD 5abed86, v0.6.3) LLM-free baseline.

Implements section 13 of /home/claude/mp/reports/mcp-memory.md.

The real server has no extraction, ranking, embeddings or time. The client LLM writes entities, relations and
observations through CRUD tools. The server then answers ``search_nodes(query)``, which keeps every entity whose
name, entityType or any observation contains the whole lowercased query as a substring (index.ts:314-339).
``open_nodes(names)`` is an exact-name lookup, and ``read_graph()`` dumps everything. Results come back in file
(creation) order, unranked and unlimited. A matched entity carries ALL its observations, plus every relation
with either endpoint in the hit set.

MODES
- Mode A (DEFAULT, ``mode="A"``): agent-like probing. Emulates what a typical agent does with this server:
  several short keyword and name probes to search_nodes, a union of the hits, then open_nodes on the 1-hop
  neighbours of the best hits. If nothing matches at all, it falls back to read_graph, the README system
  prompt's "retrieve all relevant information from your knowledge graph".
- Mode F (``mode="F"``): a literal single search_nodes(q.text) call, with the exact server semantics. It
  usually returns nothing for natural-language questions (the '?' alone defeats the substring). That is the
  honest floor. When it matches nothing, it returns [] (fewer by design).

WRITE PATH (approximates the LLM-authored graph; recipe steps 1-5)
- Person entity per speaker (entityType "person"). One observation per turn: ``"[YYYY-MM-DD] {text}"``, deduped
  as exact strings, the same as add_observations (index.ts:253).
- Event entity per session ``"session_{session_id}"``, with one observation ``"[date] session with A, B"``.
  Relation ``(speaker, session, "participated_in")`` per speaker, deduped on the triple (index.ts:230-238).
- "thing" entities from the proper-noun regex ``\\b([A-Z][a-zA-Z'\\-]+(?:\\s+[A-Z][a-zA-Z'\\-]+){0,3})\\b``,
  applied per sentence (split ``(?<=[.!?])\\s+``).
  - Drop: speaker names, spans shorter than 3 chars, single-token spans that are the sentence's first word and
    a stopword. Keep a candidate only if it appears in >= 2 distinct sentences corpus-wide.
  - Observations ``"[date] {sentence}"``. Relation ``(speaker, thing, "mentions")``.
  - Entity name = casing of the first occurrence. Later matches compare case-sensitively, as the server keys
    on exact names.
- Creation order = position of first appearance in the corpus (session entity at session start, speaker at
  first turn, thing at first mention). This is the only order the server has.

READ PATH, Mode A (recipe steps 7-11)
- Probes are lowercased, deduped (keep first), in this order, capped at MAX_PROBES = 8:
  - (a) full query, weight 2;
  - (b) entity names occurring in the query, longest first, weight 3;
  - (c) capitalised spans from the query (same regex/filters), weight 3;
  - (d) query tokens ``[a-z0-9']+`` of length >= 4, not stopwords, first 6, weight 1;
  - (e) 4-digit years and month names, weight 1.
- Every probe runs the exact search_nodes matcher (substring on name, entityType or any observation).
  - s(e) = sum of weights of the probes matching e.
  - Rank by (-s, creation index). Entities with s = 0 are not returned.
- 1-hop open_nodes: for the top 3 entities, every relation endpoint not yet present is added with
  score 0.5 * s(parent). Then re-sort.
- Flattening into harness items (the server returns whole entities):
  - per entity in rank order, first the observations containing >= 1 probe (by #probes contained desc, then
    chronological), then the rest of that entity's observations chronologically;
  - an observation whose source turn was already emitted is skipped (turn-id dedupe, keep first);
  - observations with no source turn (session-entity summaries) are skipped, since they cover no turns;
  - stop at k.

ITEM TEXT
- ``"{entity name} ({entityType}): {observation}"``, e.g. ``"Caroline (person): [2023-05-08] I went to ..."``.
  This is the per-observation rendering of the JSON entity the server returns (name, entityType, the
  observation string with the date prefix the "LLM" wrote).
- Item.turn_ids = the single source turn of that observation.
- The relations array the server also returns has no source turns. It is not emitted as items, so its
  (small) token cost is not charged.

PARAMETERS
- MAX_PROBES = 8; weights name = 3, full query = 2, keyword/date = 1; keyword len >= 4, max 6.
- HOP_TOP = 3, HOP_FACTOR = 0.5; thing min sentences = 2; k = harness k (the server has no limit).
- No BM25 and no dense encoder are used: the server has neither. The optional BM25 tie-break in recipe step 9
  is off by default and is not implemented, to keep fidelity. No decay or recency, and q.time is unused: the
  server has no notion of time.

TUNABLE KNOBS
The real server exposes NO retrieval knobs. Its only configuration is MEMORY_FILE_PATH (src/memory/index.ts:30-38).
search_nodes takes just ``query`` (max 2048 chars, index.ts:625-630) and has no limit, threshold, ranking or
expansion option (index.ts:314-339). So every knob below is an agent-usage convention: how an LLM client drives
the server's tools. They come from report section 13, steps 7 and 10. TUNABLE is a grid of 3 x 2 x 3 = 18.
- ``max_probes`` [4, 8, 12], default 8: how many search_nodes calls the agent makes per question (recipe step 7).
- ``kw_max`` [3, 6], default 6: how many content keywords the agent probes with (recipe step 7d).
- ``hop_top`` [0, 3, 6], default 3: how many top hits the agent follows with open_nodes on their neighbours.
  0 turns expansion off. The server's either-endpoint relation return makes neighbours visible
  (index.ts:327-331, 350-356; recipe step 10).
- ``hop_factor`` (0.5) and ``mode`` ("A"/"F") are constructor options but are not in the grid. Mode F is the
  literal floor, not an agent usage pattern.

DELIBERATE INTERPRETATIONS / DEVIATIONS
- Probe (b) matches entity names case-sensitively on word boundaries in the original query, not as raw lowercase
  substrings. An agent recognises names as names; this stops "Art" firing inside "start" and stops LME entities
  such as "Take" firing on the verb "take".
- A single-token span must be seen capitalised at least once in a non-sentence-initial position. Without this,
  interjections ("Wow", "Thanks", "Now") become entities with dozens of observations, which no LLM would write.
  The recipe's stopword-only filter is too weak to approximate LLM entity naming.
- Proper-noun spans have leading/trailing stopword tokens trimmed ("Hey Caroline" -> "Caroline") and a trailing
  "'s" removed. This approximates an LLM naming entities cleanly.
- The read_graph fallback on zero hits (Mode A only) is agent emulation, not a server feature.
"""
from __future__ import annotations

import re

from bench.system import Item, System
from bench.text import STOPWORDS

MAX_PROBES = 8
W_NAME, W_FULL, W_KEYWORD = 3.0, 2.0, 1.0
KW_MIN_LEN, KW_MAX = 4, 6
HOP_TOP, HOP_FACTOR = 3, 0.5
THING_MIN_SENTENCES = 2

_SENT = re.compile(r"(?<=[.!?])\s+")
_PROPER = re.compile(r"\b([A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+){0,3})\b")
_KW = re.compile(r"[a-z0-9']+")
_YEAR = re.compile(r"\b(1[89]\d\d|20\d\d)\b")
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december")


def _clean_span(span: str) -> str:
    toks = span.split()
    while toks and toks[0].lower() in STOPWORDS:
        toks = toks[1:]
    while toks and toks[-1].lower() in STOPWORDS:
        toks = toks[:-1]
    if toks and toks[-1].endswith("'s"):
        toks[-1] = toks[-1][:-2]
    return " ".join(t for t in toks if t)


def _proper_spans(sentence: str, with_initial: bool = False):
    """Proper-noun spans of a sentence. With with_initial=True returns (span, is_sentence_initial_single) pairs."""
    out = []
    first = sentence.lstrip().split(" ", 1)[0] if sentence.strip() else ""
    for m in _PROPER.finditer(sentence):
        raw = m.group(1)
        if " " not in raw and raw == first and raw.lower() in STOPWORDS:
            continue
        s = _clean_span(raw)
        if len(s) >= 3:
            initial = " " not in s and m.start() == len(sentence) - len(sentence.lstrip())
            out.append((s, initial) if with_initial else s)
    return out


class _Entity:
    __slots__ = ("name", "etype", "obs", "obs_turn", "obs_lower", "blob", "order")

    def __init__(self, name: str, etype: str, order: int):
        self.name, self.etype, self.order = name, etype, order
        self.obs: list[str] = []
        self.obs_turn: list[str | None] = []
        self.obs_lower: list[str] = []
        self.blob = ""

    def add_obs(self, text: str, turn_id: str | None):
        if text in self.obs:  # add_observations exact-string dedupe
            return
        self.obs.append(text)
        self.obs_turn.append(turn_id)

    def finalise(self):
        self.obs_lower = [o.lower() for o in self.obs]
        self.blob = "\x00".join([self.name.lower(), self.etype.lower(), *self.obs_lower])

    def matches(self, probe: str) -> bool:  # search_nodes semantics (index.ts:318-322)
        return probe in self.blob


class MCPMemory(System):
    name = "mcp-memory"
    family = "reference-server"
    faithful_notes = (
        "Reproduces the MCP reference memory server (src/memory, v0.6.3) over an LLM-free approximation of the "
        "graph an agent would write. That graph has a person entity per speaker with one '[date] text' "
        "observation per turn, a session event entity linked by participated_in, and regex proper-noun 'thing' "
        "entities (>=2 sentences; single tokens must occur capitalised mid-sentence) whose observations are the dated sentences mentioning them, linked by mentions. "
        "Matching is the server's exact search_nodes rule: case-insensitive whole-probe substring on name, "
        "entityType or any observation, unranked, in creation order. Default Mode A emulates agent use: up to 8 "
        "probes (full query w2, entity names and capitalised spans w3, <=6 keywords of len>=4 and years/months "
        "w1), summed per entity, then 1-hop open_nodes of the top 3 at 0.5x, then flattening of whole entities "
        "(probe-matching observations first, then the rest chronologically, turn-deduped), with a read_graph "
        "fallback when nothing matches. Mode F is a single literal search_nodes(q.text). No BM25, embeddings, "
        "decay or time are used, because the server has none. Item text is 'name (type): observation'; "
        "relation rows are not charged."
    )

    # Agent-usage conventions, not server settings: the server exposes no retrieval knobs (see docstring).
    TUNABLE: dict[str, list] = {
        "max_probes": [4, 8, 12],
        "kw_max": [3, 6],
        "hop_top": [0, 3, 6],
    }

    def __init__(self, mode: str = "A", max_probes: int = MAX_PROBES, kw_max: int = KW_MAX,
                 hop_top: int = HOP_TOP, hop_factor: float = HOP_FACTOR):
        if mode not in ("A", "F"):
            raise ValueError("mode must be 'A' or 'F'")
        self.mode = mode
        self.max_probes, self.kw_max = int(max_probes), int(kw_max)
        self.hop_top, self.hop_factor = int(hop_top), float(hop_factor)

    def __repr__(self):
        return (f"MCPMemory(mode={self.mode!r}, max_probes={self.max_probes}, kw_max={self.kw_max}, "
                f"hop_top={self.hop_top}, hop_factor={self.hop_factor})")

    # ---------------------------------------------------------------- build (write path)
    def build(self, corpus, enc):
        speakers = set(corpus.speakers) | {t.speaker for t in corpus.turns}
        ents: dict[str, _Entity] = {}
        rels: list[tuple[str, str, str]] = []
        rel_set: set[tuple[str, str, str]] = set()
        pos = 0

        def ent(name, etype):
            nonlocal pos
            e = ents.get(name)
            if e is None:
                e = ents[name] = _Entity(name, etype, pos)
                pos += 1
            return e

        def rel(a, b, rt):
            r = (a, b, rt)
            if r not in rel_set:
                rel_set.add(r)
                rels.append(r)

        # pass 1: count distinct sentences per proper-noun candidate
        sent_rows = []  # (turn, date, sentence, spans)
        cand_count: dict[str, int] = {}
        mid_sentence: set[str] = set()  # single-token spans seen capitalised somewhere other than sentence start
        for t in corpus.turns:
            date = t.ts.strftime("%Y-%m-%d")
            for s in _SENT.split(t.text.strip()):
                s = s.strip()
                if not s:
                    continue
                pairs = _proper_spans(s, with_initial=True)
                for x, initial in pairs:
                    if not initial:
                        mid_sentence.add(x)
                spans = [x for x in dict.fromkeys(x for x, _ in pairs) if x not in speakers]
                sent_rows.append((t, date, s, spans))
                for x in spans:
                    cand_count[x] = cand_count.get(x, 0) + 1
        keep = {x for x, c in cand_count.items()
                if c >= THING_MIN_SENTENCES and (" " in x or x in mid_sentence)}

        by_turn: dict[str, list] = {}
        for row in sent_rows:
            by_turn.setdefault(row[0].id, []).append(row)

        by_sess: dict[str, list] = {}
        for t in corpus.turns:
            by_sess.setdefault(t.session_id, []).append(t)

        for sid in corpus.session_order:
            turns = by_sess.get(sid, [])
            if not turns:
                continue
            sname = f"session_{sid}"
            sp = list(dict.fromkeys(t.speaker for t in turns))
            se = ent(sname, "event")
            se.add_obs(f"[{corpus.session_ts[sid].strftime('%Y-%m-%d')}] session with {', '.join(sp)}", None)
            for t in turns:
                date = t.ts.strftime("%Y-%m-%d")
                pe = ent(t.speaker, "person")
                rel(t.speaker, sname, "participated_in")
                pe.add_obs(f"[{date}] {t.text}", t.id)
                for (_, d, s, spans) in by_turn.get(t.id, []):
                    for x in spans:
                        if x in keep:
                            ent(x, "thing").add_obs(f"[{d}] {s}", t.id)
                            rel(t.speaker, x, "mentions")

        self.entities = sorted(ents.values(), key=lambda e: e.order)
        for e in self.entities:
            e.finalise()
        self.by_name = {e.name: e for e in self.entities}
        self.adj: dict[str, list[str]] = {}
        for a, b, _ in rels:
            self.adj.setdefault(a, []).append(b)
            self.adj.setdefault(b, []).append(a)
        # names for probe (b), longest first, word-boundary patterns
        self._name_pats = [(e.name.lower(), re.compile(r"(?<![A-Za-z0-9])" + re.escape(e.name) + r"(?![A-Za-z0-9])"))
                           for e in sorted(self.entities, key=lambda e: -len(e.name))]

    # ---------------------------------------------------------------- probes
    def _probes(self, query: str) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        seen: set[str] = set()

        def add(p, w):
            p = p.lower()
            if p and p not in seen and len(out) < self.max_probes:
                seen.add(p)
                out.append((p, w))

        ql = query.lower()
        add(ql, W_FULL)                                              # (a)
        for n, pat in self._name_pats:                               # (b)
            if pat.search(query):
                add(n, W_NAME)
        for s in _proper_spans(query):                               # (c)
            add(s, W_NAME)
        kws = [w for w in _KW.findall(ql) if len(w) >= KW_MIN_LEN and w not in STOPWORDS]
        for w in list(dict.fromkeys(kws))[:self.kw_max]:             # (d)
            add(w, W_KEYWORD)
        for y in _YEAR.findall(ql):                                  # (e)
            add(y, W_KEYWORD)
        for m in _MONTHS:
            if re.search(r"\b" + m + r"\b", ql):
                add(m, W_KEYWORD)
        return out

    # ---------------------------------------------------------------- flatten
    def _flatten(self, ranked: list[tuple[_Entity, float]], probes: list[str], k: int) -> list[Item]:
        items: list[Item] = []
        emitted: set[str] = set()
        for e, sc in ranked:
            idx = list(range(len(e.obs)))
            if probes:
                hits = {i: sum(1 for p in probes if p in e.obs_lower[i]) for i in idx}
                first = sorted([i for i in idx if hits[i] > 0], key=lambda i: (-hits[i], i))
                rest = [i for i in idx if hits[i] == 0]
                idx = first + rest
            for i in idx:
                tid = e.obs_turn[i]
                if tid is None or tid in emitted:
                    continue
                emitted.add(tid)
                items.append(Item([tid], f"{e.name} ({e.etype}): {e.obs[i]}", float(sc),
                                  {"entity": e.name, "mode": self.mode}))
                if len(items) >= k:
                    return items
        return items

    # ---------------------------------------------------------------- retrieve
    def retrieve(self, q, k=30):
        if self.mode == "F":
            p = q.text.lower()
            hits = [(e, 1.0) for e in self.entities if e.matches(p)]  # creation order, whole entities
            return self._flatten(hits, [], k)

        probes = self._probes(q.text)
        score: dict[str, float] = {}
        for p, w in probes:
            for e in self.entities:
                if e.matches(p):
                    score[e.name] = score.get(e.name, 0.0) + w
        if not score:  # agent falls back to read_graph
            return self._flatten([(e, 0.0) for e in self.entities], [], k)

        ranked = sorted(score, key=lambda n: (-score[n], self.by_name[n].order))
        for n in ranked[:self.hop_top]:  # open_nodes on 1-hop neighbours
            for m in self.adj.get(n, []):
                if m not in score:
                    score[m] = self.hop_factor * score[n]
        ranked = sorted(score, key=lambda n: (-score[n], self.by_name[n].order))
        return self._flatten([(self.by_name[n], score[n]) for n in ranked], [p for p, _ in probes], k)
