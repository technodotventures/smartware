"""LangMem (langchain-ai/langmem 0.0.30, commit 48e3c11) LLM-free retrieval baseline.

Implements section 13 of /home/claude/mp/reports/langmem.md. LangMem has no ranking code of its own. Every read
is LangGraph ``BaseStore.search(namespace_prefix, query=..., filter=None, limit=..., offset=0)``, called by the
``search_memory`` tool (langmem/knowledge/tools.py:436-474), ``MemoryStoreManager.search`` or the documented
hot-path prompt pattern. With the documented index config (``fields=["$"]``, cosine), that search is:
pre-filter by namespace prefix, embed the query, score each item by cosine against the embedding of
``json.dumps(value, sort_keys=True, ensure_ascii=False)``, sort descending, then slice [offset:offset+limit].

FAITHFUL
- Single namespace per corpus, ``("memories", <corpus id>)``. This is the default template
  ``("memories", "{langgraph_user_id}")`` filled from config. It acts as a pre-ranking filter (a no-op here,
  since one corpus = one namespace).
- Value envelope written by MemoryStoreManager (extraction.py:1114, 1121):
  ``{"kind": "Memory", "content": {"content": <memory text>}}``. Key = UUID (made deterministic with uuid5 of
  the source turn ids, for reproducibility).
- Index text = ``json.dumps(value, sort_keys=True, ensure_ascii=False)``. This is LangGraph's
  ``get_text_at_path(value, "$")`` for the default ``fields=["$"]``. The JSON scaffolding ("content", "kind",
  "Memory") is therefore embedded, exactly as in LangMem deployments. One vector per item, so max-pooling over
  fields is a no-op.
- Score = cosine(query, item) (PostgresStore: ``1 - (embedding <=> q)``; InMemoryStore: cosine). Dense only:
  no BM25, no fusion, no recency, no decay, no importance, no reranker, no graph, no token budgeting. Ties keep
  insertion order (InMemoryStore iterates its dict in insertion order; sorting is stable).
- Query = the raw question string. The hot-path quickstart uses ``query=state["messages"][-1].content``. LLM
  query generation (``query_model`` / ``create_memory_searcher``) is optional in LangMem and needs an LLM.
- limit = k (the caller passes ``limit``; BaseStore default 10 if omitted), offset = 0, filter = None.
- Item.text = one element of the ``search_memory`` tool's output: ``orjson.dumps([m.dict() for m in
  memories])``, compact JSON per SearchItem with keys namespace, key, value, created_at, updated_at, score
  (tools.py:452-454). That is what LangMem itself places in the agent's context, scaffolding included. Use
  ``context_format="content"`` for just the memory string (not the default).
- q.time is not used. LangMem/LangGraph have no "now" in ranking.

APPROXIMATED (no LLM)
- LLM extraction (MemoryManager + trustcall insert/patch/delete over the 5 nearest neighbours of the last
  message) is replaced by turn-level units:
  - One unit per maximal run of consecutive same-speaker turns within a session. This mirrors LangChain
    ``merge_message_runs``, which joins contents with "\n".
  - Unit memory text = ``"<speaker>: <text>"``.
  - Real LangMem stores LLM-written facts ("User prefers dark mode"). Raw turns keep more detail but none of
    the synthesis.
- created_at = updated_at = the timestamp of the unit's last turn, as if the background manager processed each
  exchange live. LangMem itself stamps wall-clock write time and never uses the conversation timestamps.
- Write-time consolidation (the LLM patch/delete loop) is OFF by default because it cannot be reproduced
  faithfully. ``dedupe=True`` enables a crude stand-in: before inserting a unit, find the
  ``query_limit=5`` nearest stored units by the cosine of the unit's own text (the default dilated window is
  N = 5//4 = 1, i.e. the last message only). If the best one has the same speaker and cosine >=
  ``dedupe_sim``, overwrite it in place (key kept, value replaced, turn ids replaced, as an LLM "patch" that
  rewrites the memory would do). ``enable_deletes`` defaults to False, so nothing is deleted.

THRESHOLDS
- ``dedupe_sim = 0.95`` (option only; not used by default). It is an approximation constant, not a LangMem
  constant; LangMem has no similarity thresholds anywhere. On text-embedding-3-small, 0.95 is "near
  paraphrase". DEV check on this embedder (unit index texts, all pairs): LoCoMo conv-26 0.000% of 87,571 pairs
  reach 0.95 (p99 = 0.744); LME gpt4_76048e76 0.016% (p99 = 0.669). It merges almost nothing, which matches
  its intent (only near-identical restatements) and the faithful default of no deterministic dedupe. Kept as
  is rather than converted to a percentile, because the option exists only to show the effect of
  consolidation, not to be tuned.

PARAMETERS: limit = k, offset = 0, dense cosine only, index field "$" (whole JSON value), namespace
("memories", corpus_id), units = same-speaker runs; options: context_format ("json" | "content"),
index_mode ("json" | "text"), dedupe (False), dedupe_sim (0.95), query_limit (5).

TUNABLE KNOBS (dev-split tuning only; the defaults above are unchanged)
- ``limit``: the ``limit`` request parameter of ``search_memory`` / ``MemoryStoreManager.search`` /
  ``BaseStore.search``. Defaults are 10 in vendor/langchain-ai_langmem/src/langmem/knowledge/tools.py:439 and
  459 and extraction.py:1358 and 1518. None (the default here) means "pass the harness k through". Candidates:
  None, 10, 20, 30.
- ``index_mode``: the store's index field paths. ``"json"`` is ``fields: ["$"]`` (langgraph.json:15, the
  README/docs default). ``"text"`` is ``index=["content.content"]``, i.e. embedding only the memory string; the
  per-put ``index=[...]`` option is documented at extraction.py:1434-1440 and 1461. Report section 3.
- ``context_format``: how the caller renders results into context. ``"json"`` is the ``search_memory`` tool's
  orjson output (tools.py:452-454). ``"content"`` is the memory string only, the developer-formatted pattern in
  docs/docs/guides/extract_episodic_memories.md:120-131 (``item.value["content"]``). Report section 5.

DELIBERATE DEVIATIONS: none in the default path beyond the no-LLM unitisation and the live-ingestion
timestamps described above.
"""
from __future__ import annotations

import json
import uuid

import numpy as np

from bench.system import Item, System

NAMESPACE_ROOT = "memories"          # default namespace ("memories", "{langgraph_user_id}")
DEFAULT_LIMIT = 10                   # BaseStore.search / search_memory tool default (used only if k is None)
QUERY_LIMIT = 5                      # create_memory_store_manager(query_limit=5)
DEDUPE_SIM = 0.95                    # approximation constant for the optional dedupe stand-in


def _iso(ts) -> str:
    # LangGraph stores timezone-aware UTC datetimes; Item.dict() serialises them with isoformat().
    return ts.isoformat() + "+00:00" if ts.tzinfo is None else ts.isoformat()


class LangMem(System):
    name = "langmem"
    family = "memory-sdk"
    faithful_notes = (
        "LangMem delegates all recall to LangGraph BaseStore.search: namespace-prefix pre-filter, cosine between "
        "the raw query embedding and one embedding per item of json.dumps(value) (index field '$'), sorted "
        "descending, sliced to limit. This baseline reproduces that exactly with the shared dense encoder: dense "
        "only, no BM25, no recency/decay, no rerank, ties in insertion order, limit = k. LLM fact extraction "
        "and patch/delete consolidation are replaced by turn-level units (same-speaker runs, 'speaker: text') "
        "stored in LangMem's {'kind':'Memory','content':{'content':...}} envelope. created_at/updated_at are the "
        "unit's turn time, as if ingested live. Item.text is the search_memory tool's compact JSON per "
        "SearchItem (namespace, key, value, timestamps, score), which is what LangMem puts in context. "
        "Optional (off) dedupe stand-in at cosine 0.95."
    )

    TUNABLE: dict[str, list] = {
        "limit": [None, 10, 20, 30],          # search_memory / BaseStore.search limit (None = harness k)
        "index_mode": ["json", "text"],       # index fields ["$"] vs ["content.content"]
        "context_format": ["json", "content"],  # tool JSON vs memory string only
    }

    def __init__(self, context_format: str = "json", index_mode: str = "json", dedupe: bool = False,
                 dedupe_sim: float = DEDUPE_SIM, query_limit: int = QUERY_LIMIT, limit: int | None = None):
        assert context_format in ("json", "content")
        assert index_mode in ("json", "text")
        assert limit is None or limit > 0
        self.limit = limit
        self.context_format = context_format
        self.index_mode = index_mode
        self.dedupe = dedupe
        self.dedupe_sim = dedupe_sim
        self.query_limit = query_limit

    # ------------------------------------------------------------------ write path
    def _units(self, corpus):
        by_sess: dict[str, list] = {}
        for t in corpus.turns:
            by_sess.setdefault(t.session_id, []).append(t)
        order = list(corpus.session_order) + [s for s in by_sess if s not in corpus.session_order]
        units = []
        for sid in order:
            run: list = []
            for t in sorted(by_sess.get(sid, []), key=lambda x: x.idx):
                if run and run[-1].speaker != t.speaker:
                    units.append(run)
                    run = []
                run.append(t)
            if run:
                units.append(run)
        return units

    def _index_text(self, value: dict, text: str) -> str:
        if self.index_mode == "text":
            return text
        return json.dumps(value, sort_keys=True, ensure_ascii=False)

    def build(self, corpus, enc):
        self.enc = enc
        self.namespace = [NAMESPACE_ROOT, corpus.id]
        self.records: list[dict] = []  # insertion-ordered store items
        vecs: list[np.ndarray] = []
        for run in self._units(corpus):
            text = f"{run[0].speaker}: " + "\n".join(t.text for t in run)
            value = {"kind": "Memory", "content": {"content": text}}
            ts = _iso(run[-1].ts)
            v = enc.embed(self._index_text(value, text))
            rec = dict(key=str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(t.id for t in run))), value=value,
                       created_at=ts, updated_at=ts, turn_ids=[t.id for t in run], speaker=run[0].speaker)
            if self.dedupe and vecs:
                # Stand-in for the reconcile loop: neighbours of the new message, top query_limit.
                sims = np.stack(vecs) @ v
                nb = np.argsort(-sims, kind="stable")[: self.query_limit]
                j = int(nb[0])
                if sims[j] >= self.dedupe_sim and self.records[j]["speaker"] == rec["speaker"]:
                    # In-place overwrite (LangMem patch): key kept; InMemoryStore resets both timestamps on put.
                    self.records[j].update(value=value, created_at=ts, updated_at=ts, turn_ids=rec["turn_ids"])
                    vecs[j] = v
                    continue
            self.records.append(rec)
            vecs.append(v)
        self.E = np.stack(vecs) if vecs else np.zeros((0, enc.dense.dim), np.float32)

    # ------------------------------------------------------------------ read path
    def _render(self, rec: dict, score: float) -> str:
        if self.context_format == "content":
            return rec["value"]["content"]["content"]
        item = {
            "namespace": self.namespace,
            "key": rec["key"],
            "value": rec["value"],
            "created_at": rec["created_at"],
            "updated_at": rec["updated_at"],
            "score": score,
        }
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"))

    def retrieve(self, q, k=30):
        if self.limit is not None:
            limit = self.limit if k is None else min(self.limit, k)
        else:
            limit = DEFAULT_LIMIT if k is None else k
        offset = 0
        if not len(self.records):
            return []
        qv = self.enc.embed(q.text)
        sims = self.E @ qv
        order = np.argsort(-sims, kind="stable")[offset: offset + limit]
        out = []
        for i in order:
            rec = self.records[int(i)]
            s = float(sims[i])
            out.append(Item(list(rec["turn_ids"]), self._render(rec, s), s,
                            meta={"key": rec["key"], "kind": "Memory"}))
        return out
