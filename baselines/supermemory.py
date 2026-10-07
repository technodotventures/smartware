"""Supermemory (supermemoryai/supermemory @3535ff7): LLM-free retrieval baseline.

This implements section 13 of /home/claude/mp/reports/supermemory.md.

The engine is closed. The public repo has only clients, the MCP proxy, zod schemas and docs. So this baseline
reproduces:
  - the documented API behaviour;
  - the persisted data model (versioned memories, profile, chunks);
  - the open-source client-side context assembly in @supermemory/tools.
Server internals are approximated.

FAITHFUL (with sources)
- Two layers in one hybrid ranked list, v5 default ``searchMode: "hybrid"``:
  - memories (atomic facts) and document chunks (verbatim source);
  - the chunk header is embedded with the chunk, i.e. contextual chunking (``Chunk.embeddedContent``).
- v5 default similarity threshold 0.30, applied to dense cosine.
  - Lexical FTS is a documented second channel ("Vector, FTS, and graph built in"; "hybrid keyword search still
    finds rare tokens"), so BM25 top hits bypass the threshold.
- Retrieval only sees latest, non-forgotten memories (``isLatest``, ``isForgotten``).
- Version history is attached to each hit as parents (``version -1, -2``) and a later child. This is
  ``include.related``: parents, children and siblings.
- ``updates`` supersession follows ingestion order (docs: "how supermemory determines what came first"):
  - the old node gets ``is_latest=False``;
  - the new node gets ``version+1`` and ``parent=old``.
- Restating an existing fact increments ``sourceCount`` instead of creating a duplicate.
- Query-independent profile (``static`` + ``dynamic``), one per namespace:
  - dynamic lines carry a ``[YYYY-MM-DD]`` prefix.
- Context assembly mirrors packages/tools ``buildMemoriesText`` in mode "full":
  - "## Static Profile", then "## Dynamic Profile", then "Search results for user's recent message:";
  - dedupe priority static > dynamic > search, keyed by ``normalizeMemoryFact``: trim, strip leading "[recent] "
    and "[YYYY-MM-DD] ", collapse whitespace, lowercase.

APPROXIMATED (no LLM; server closed)
- LLM fact extraction -> sentence units.
  - Units are ``"[date] speaker: sentence"``, keeping sentences of >= 4 words; shorter fragments are merged into
    the previous sentence.
  - BM25 indexes the full unit text (date tokens included).
  - The dense embedding uses ``"speaker: sentence"``: the date is metadata on a supermemory memory, not part of the
    embedded fact.
- Chunking -> session windows of 6 turns, stride 3, with header ``"[date] session <id>"``.
- ``updates`` detection (no LLM) works on the 20 nearest earlier same-speaker units. A unit is marked as updating
  the most similar latest candidate p when all of these hold:
  - cos >= 0.85;
  - it is later in time and in a different turn;
  - the content tokens differ (a number, a capitalised token, or a non-stopword; the shared bench STOPWORDS list
    stands in for the recipe's 200-word stoplist).
  If instead cos >= 0.95 and the content tokens are identical, it is reinforcement: ``source_count += 1`` and the
  new unit leaves the primary pool.
- ``extends`` -> siblings: the adjacent units in the same session.
- ``derives`` is omitted.
- Fusion is not published. Within each layer it is RRF(k=60) over dense top 50/30 and BM25 top 50/30 (units/chunks):
  - normalise by the layer max;
  - add ``+0.02 * min(source_count, 5) / 5`` for reinforcement;
  - merge both layers into one list. Recency is off.
- Profile synthesis -> heuristics over latest non-assistant units:
  - static = units with ``source_count >= 2`` or matching first-person stable patterns, cap 12;
  - dynamic = the 8 most recent, excluding static.
- Context budget: 800 tokens (README "~720 tokens" at Recall@15), applied to the search section. Pruning order:
  - attached siblings, from the lowest rank up;
  - chunks ranked below 10;
  - the lowest-ranked results.

PARAMETERS
- limit K=15. The profile is prepended, and the total is capped at the harness k.
- Window W=6 / stride 3.
- RRF k=60.
- Threshold 0.30, with a BM25 top-10 bypass.
- Supersession 0.85; reinforcement 0.95.
- Parents: 2 hops.
- Static cap 12, dynamic cap 8.
- Search budget 800 tokens.

DELIBERATE DEVIATIONS / INTERPRETATIONS (all documented, none tuned)
- One profile per namespace, built from every non-assistant speaker. This matches the API ("each namespace gets
  its own profile") rather than the recipe's per-speaker profile.
- The 800-token budget covers only the search section: README's ~720 tokens is a search-only number.
- A unit is dropped as contained in a chunk only if that chunk ranks above it, so no evidence is lost by reordering.
- When choosing which unit u updates, only current chain heads are eligible.
- Thresholds on the shared embedder, checked on DEV corpora only:
  - 0.30 keeps about 15% of query-unit pairs on LoCoMo and about 1.5% on LME, so it is neither near-empty nor
    near-all, and it is kept;
  - 0.85 and 0.95 are kept as is: they select about 0.2% and 0.02% of same-speaker pairs, and the content-token
    condition gates them further.
- No "now" is used: recency is off, and dynamic is relative to the corpus.
- Options:
  - ``mode="query"`` (search only) and ``mode="profile"`` exist but are not the default;
  - ``budget=None`` disables pruning;
  - ``related=False`` disables attachments.
"""
from __future__ import annotations

import re
from collections import defaultdict

import numpy as np

from bench.system import Item, System
from bench.text import STOPWORDS, count_tokens, sentences, words

_STATIC_PAT = re.compile(
    r"\b(i am|i'm|my name|i live|i work|i prefer|i like|i love|i hate|i'm allergic|"
    r"my (wife|husband|partner|son|daughter|job|birthday))\b"
)
_NORM_RECENT = re.compile(r"^\[recent\]\s*", re.I)
_NORM_DATE = re.compile(r"^\[\d{4}-\d{2}-\d{2}\]\s*")
_RAW_TOK = re.compile(r"[A-Za-z0-9]+")


def normalize_memory_fact(s: str) -> str:
    """packages/tools/src/tools-shared.ts:197-205."""
    s = s.strip()
    s = _NORM_RECENT.sub("", s)
    s = _NORM_DATE.sub("", s)
    return re.sub(r"\s+", " ", s.strip()).lower()


def _content_tokens(s: str) -> frozenset[str]:
    out = set()
    for w in _RAW_TOK.findall(s):
        if w.isdigit() or w[0].isupper() or w.lower() not in STOPWORDS:
            out.add(w.lower())
    return frozenset(out)


class Supermemory(System):
    name = "supermemory"
    family = "memory-sdk"
    faithful_notes = (
        "Supermemory's engine is closed source, so this rebuilds what its API documents. Retrieval is hybrid: "
        "versioned sentence-level 'memories' and contextual session-window chunks come back in one ranked list. "
        "Search keeps the v5 default cosine threshold 0.30 and lets BM25 (FTS) top hits bypass it. Retrieval sees only "
        "the latest version of each memory, with its 'updates' history (two parents) and adjacent sentences attached "
        "as include.related. Supersession follows ingestion order. Restated facts reinforce sourceCount. A "
        "query-independent static/dynamic profile is prepended. Output is assembled and deduplicated the way "
        "@supermemory/tools does it in 'full' mode. Approximations: LLM extraction becomes sentence units, LLM "
        "relation typing becomes a cosine >= 0.85 plus changed-content-token rule, and 'derives' is omitted. Fusion is "
        "unpublished; RRF k=60 per layer with max normalisation is assumed. The search section is capped at an "
        "800-token budget, matching README's ~720 tokens at Recall@15."
    )

    def __init__(self, mode: str = "full", limit: int = 15, budget: int | None = 800, related: bool = True,
                 threshold: float = 0.30, rrf_k: int = 60, window: int = 6, stride: int = 3,
                 sup_cos: float = 0.85, reinf_cos: float = 0.95, static_cap: int = 12, dynamic_cap: int = 8):
        self.mode, self.limit, self.budget, self.related = mode, limit, budget, related
        self.threshold, self.rrf_k, self.window, self.stride = threshold, rrf_k, window, stride
        self.sup_cos, self.reinf_cos = sup_cos, reinf_cos
        self.static_cap, self.dynamic_cap = static_cap, dynamic_cap

    # ------------------------------------------------------------------ build
    def build(self, corpus, enc):
        self.enc = enc
        sorder = {s: i for i, s in enumerate(corpus.session_order)}
        turns = sorted(corpus.turns, key=lambda t: (t.ts, sorder.get(t.session_id, 0), t.idx))
        self.turns = turns
        roles = {t.speaker: t.role for t in turns}

        # 1-3: memory units (sentence level)
        U = []  # dicts
        for t in turns:
            d = t.ts.strftime("%Y-%m-%d")
            sents, buf = [], ""
            for s in sentences(t.text):
                if len(words(s)) >= 4:
                    sents.append(s)
                elif sents:
                    sents[-1] = sents[-1] + " " + s
                else:
                    buf = (buf + " " + s).strip()
            if buf and sents:
                sents[0] = buf + " " + sents[0]
            for s in sents:
                U.append(dict(text=f"[{d}] {t.speaker}: {s}", emb_text=f"{t.speaker}: {s}", fact=s,
                              speaker=t.speaker, role=roles.get(t.speaker, t.role), sid=t.session_id,
                              tid=t.id, ts=t.ts, version=1, latest=True, parent=None, children=[],
                              source_count=1, dup=False, content=_content_tokens(s)))
        self.units = U
        n = len(U)
        self.UE = enc.embed_many([u["emb_text"] for u in U]) if n else np.zeros((0, 1), np.float32)

        # 5: supersession / reinforcement in ingestion order
        spk = np.array([u["speaker"] for u in U], dtype=object)
        tids = np.array([u["tid"] for u in U], dtype=object)
        B = 512
        for b0 in range(0, n, B):
            S = self.UE[b0:b0 + B] @ self.UE[:b0 + B].T
            for r in range(S.shape[0]):
                i = b0 + r
                if i == 0:
                    continue
                row = S[r, :i].copy()
                row[spk[:i] != spk[i]] = -np.inf
                row[tids[:i] == tids[i]] = -np.inf
                m = min(20, i)
                nb = np.argpartition(-row, m - 1)[:m]
                nb = nb[np.argsort(-row[nb])]
                u = U[i]
                for j in nb:
                    c = float(row[j])
                    if not np.isfinite(c) or c < self.sup_cos:
                        break
                    p = U[j]
                    if not p["latest"] or p["dup"] or not (u["ts"] > p["ts"]):
                        continue
                    if u["content"] == p["content"]:
                        if c >= self.reinf_cos:
                            p["source_count"] += 1
                            u["dup"] = True
                            break
                        continue
                    p["latest"] = False
                    u["version"] = p["version"] + 1
                    u["parent"] = j
                    p["children"].append(i)
                    break
        self.pool = np.array([i for i, u in enumerate(U) if u["latest"] and not u["dup"]], dtype=np.int64)
        self.unit_bm25 = enc.bm25([U[i]["text"] for i in self.pool]) if len(self.pool) else None
        self.pool_E = self.UE[self.pool] if len(self.pool) else None

        # siblings: adjacent units in the same session
        self.prev_sib, self.next_sib = {}, {}
        by_sess = defaultdict(list)
        for i, u in enumerate(U):
            by_sess[u["sid"]].append(i)
        for ids in by_sess.values():
            for a, b in zip(ids, ids[1:]):
                self.next_sib[a] = b
                self.prev_sib[b] = a

        # 2: chunks (session windows, contextual header embedded)
        C = []
        sess_turns = defaultdict(list)
        for t in turns:
            sess_turns[t.session_id].append(t)
        for sid, ts_ in sess_turns.items():
            starts = list(range(0, max(1, len(ts_) - self.window + self.stride), self.stride)) or [0]
            for s0 in starts:
                w = ts_[s0:s0 + self.window]
                if not w:
                    continue
                d = w[0].ts.strftime("%Y-%m-%d")
                body = "\n".join(f"{t.speaker}: {t.text}" for t in w)
                C.append(dict(text=f"[{d}] session {sid}\n{body}", sid=sid, tids=[t.id for t in w]))
                if s0 + self.window >= len(ts_):
                    break
        self.chunks = C
        self.CE = enc.embed_many([c["text"] for c in C])
        self.chunk_bm25 = enc.bm25([c["text"] for c in C]) if C else None
        self.chunk_of_tid = defaultdict(set)
        for ci, c in enumerate(C):
            for tid in c["tids"]:
                self.chunk_of_tid[tid].add(ci)

        # 6: profile (one per namespace, non-assistant speakers)
        cand = [i for i in self.pool if U[i]["role"] != "assistant" and U[i]["speaker"] != "assistant"]
        stat = [i for i in cand if U[i]["source_count"] >= 2 or _STATIC_PAT.search(U[i]["fact"].lower())]
        stat.sort(key=lambda i: (-U[i]["source_count"], -U[i]["ts"].timestamp(), -i))
        self.static = stat[: self.static_cap]
        ss = set(self.static)
        dyn = [i for i in cand if i not in ss]
        dyn.sort(key=lambda i: (-U[i]["ts"].timestamp(), -i))
        self.dynamic = dyn[: self.dynamic_cap]

    # ------------------------------------------------------------- helpers
    def _layer(self, qv, qtext, E, bm, dense_top, bm_top):
        n = 0 if E is None else E.shape[0]
        if n == 0:
            return {}, None
        cos = E @ qv
        bs = bm.score(qtext)
        d_rank = np.argsort(-cos)[:dense_top]
        b_order = np.argsort(-bs)
        b_rank = [i for i in b_order[:bm_top] if bs[i] > 0]
        fused = defaultdict(float)
        for r, i in enumerate(d_rank, 1):
            fused[int(i)] += 1.0 / (self.rrf_k + r)
        for r, i in enumerate(b_rank, 1):
            fused[int(i)] += 1.0 / (self.rrf_k + r)
        bypass = set(int(i) for i in b_rank[:10])
        kept = {i: s for i, s in fused.items() if cos[i] >= self.threshold or i in bypass}
        if kept:
            mx = max(kept.values())
            kept = {i: s / mx for i, s in kept.items()}
        return kept, cos

    # ------------------------------------------------------------ retrieve
    def retrieve(self, q, k=30):
        U = self.units
        seen: set[str] = set()
        out: list[Item] = []

        def unit_text(i):
            return U[i]["text"]

        # profile (static > dynamic)
        if self.mode in ("full", "profile"):
            first = True
            for i in self.static:
                key = normalize_memory_fact(f"{U[i]['speaker']}: {U[i]['fact']}")
                if key in seen:
                    continue
                seen.add(key)
                txt = f"{U[i]['speaker']}: {U[i]['fact']}"
                out.append(Item([U[i]["tid"]], ("## Static Profile\n- " if first else "- ") + txt, 1.0,
                                {"kind": "static", "unit": int(i)}))
                first = False
            first = True
            for i in self.dynamic:
                key = normalize_memory_fact(unit_text(i))
                if key in seen:
                    continue
                seen.add(key)
                out.append(Item([U[i]["tid"]], ("## Dynamic Profile\n- " if first else "- ") + unit_text(i), 1.0,
                                {"kind": "dynamic", "unit": int(i)}))
                first = False
        if self.mode == "profile":
            return out[:k]

        # 7-9: hybrid search
        qv = self.enc.embed(q.text)
        ul, ucos = self._layer(qv, q.text, self.pool_E, self.unit_bm25, 50, 50)
        cl, ccos = self._layer(qv, q.text, self.CE, self.chunk_bm25, 30, 30)
        cands = []
        for pi, s in ul.items():
            ui = int(self.pool[pi])
            s = s + 0.02 * min(U[ui]["source_count"], 5) / 5
            cands.append((s, "u", ui, float(ucos[pi])))
        for ci, s in cl.items():
            cands.append((s, "c", ci, float(ccos[ci])))
        cands.sort(key=lambda x: -x[0])

        # 10 + 12: cut and dedupe
        res = []
        chunk_rank_sids = set()
        selected_chunks = []
        for s, kind, idx, cs in cands:
            if len(res) >= self.limit:
                break
            if kind == "u":
                key = normalize_memory_fact(unit_text(idx))
                if key in seen:
                    continue
                if self.chunk_of_tid.get(U[idx]["tid"], set()) & set(selected_chunks):
                    continue  # contained in a higher-ranked selected chunk
                seen.add(key)
            else:
                key = normalize_memory_fact(self.chunks[idx]["text"])
                if key in seen:
                    continue
                seen.add(key)
                selected_chunks.append(idx)
            res.append(dict(kind=kind, idx=idx, score=s, sim=cs, parents=[], children=[], sibs=[]))

        # 11: related attachments
        if self.related:
            for r in res:
                if r["kind"] != "u":
                    continue
                i = r["idx"]
                p, hops = U[i]["parent"], 0
                while p is not None and hops < 2:
                    kk = normalize_memory_fact(unit_text(p))
                    if kk not in seen:
                        seen.add(kk)
                        r["parents"].append(p)
                    p, hops = U[p]["parent"], hops + 1
                ch = U[i]["children"]
                if ch:
                    c = ch[-1]
                    while U[c]["children"]:
                        c = U[c]["children"][-1]
                    kk = normalize_memory_fact(unit_text(c))
                    if kk not in seen:
                        seen.add(kk)
                        r["children"].append(c)
                for sb in (self.prev_sib.get(i), self.next_sib.get(i)):
                    if sb is None:
                        continue
                    kk = normalize_memory_fact(unit_text(sb))
                    if kk not in seen:
                        seen.add(kk)
                        r["sibs"].append(sb)

        def render(r, first):
            head = "Search results for user's recent message: \n" if first else ""
            if r["kind"] == "c":
                c = self.chunks[r["idx"]]
                return head + c["text"], list(c["tids"])
            i = r["idx"]
            lines = [f"- {unit_text(i)}"]
            tids = [U[i]["tid"]]
            for p in r["parents"]:
                lines.append(f"  (earlier: {unit_text(p)})")
                tids.append(U[p]["tid"])
            for c in r["children"]:
                lines.append(f"  (later: {unit_text(c)})")
                tids.append(U[c]["tid"])
            for sb in r["sibs"]:
                lines.append(f"  (context: {unit_text(sb)})")
                tids.append(U[sb]["tid"])
            return head + "\n".join(lines), list(dict.fromkeys(tids))

        def total():
            return sum(count_tokens(render(r, j == 0)[0]) for j, r in enumerate(res))

        # 14: budget pruning (search section)
        if self.budget is not None and res and total() > self.budget:
            for r in reversed(res):
                if r["sibs"]:
                    r["sibs"] = []
                    if total() <= self.budget:
                        break
            while total() > self.budget:
                drop = next((j for j in range(len(res) - 1, 9, -1) if res[j]["kind"] == "c"), None)
                if drop is None:
                    break
                res.pop(drop)
            while total() > self.budget and len(res) > 1:
                res.pop()

        for j, r in enumerate(res):
            txt, tids = render(r, j == 0)
            out.append(Item(tids, txt, float(r["score"]),
                            {"kind": "memory" if r["kind"] == "u" else "chunk", "similarity": r["sim"]}))
        return out[:k]
