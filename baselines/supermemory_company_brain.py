"""Supermemory Company Brain (supermemoryai/company-brain @ef8a45e): LLM-free retrieval baseline.

This implements section 13 of /home/claude/mp/reports/supermemory-company-brain.md. Company Brain stores all memory
in the hosted supermemory API. The repo decides only:
  - what is written (curated per-batch documents, each tagged and prefixed with ``DOCUMENT_DATE``);
  - which containers are read;
  - how per-container searches are merged;
  - how results are formatted for the model.
The server-side extraction and the hybrid fusion are opaque, so they are approximated below.

FAITHFUL (with code citations from the project)
- Search call: ``search_company_brain`` -> ``searchBrain`` (src/brain/memory/search-brain.ts:24-127):
  - per container: limit=40, threshold=0.3 (BRAIN_RELEVANCE_THRESHOLD), searchMode="hybrid";
  - rerank, rewriteQuery and aggregate are all off;
  - include.chunks=true, so memories and document chunks come back in one list;
  - merge across containers by id, keeping the max similarity; sort descending; keep the top 40;
  - the model's natural-language question is the query (here: the raw question).
- Single container. Benchmark data is one tenant/surface, so the read set is {sm_org_shared} and the fan-out and
  merge are trivially a single search.
- Output formatting: ``formatBrainResults`` (src/brain/search-brain-format.ts):
  - each line is ``"{n}. Memory|Chunk ({pct}%) {id}: {text}"``;
  - whitespace in the text is collapsed;
  - results with similarity < 0.3 are dropped.
  Ids are synthetic 22-char ids, so the token charge matches supermemory ids.
- Write documents mirror channel-observe batching (src/brain/slack/channel-observe.ts):
  - per session, windows of <= 60 messages (MAX_NEW_MESSAGES) and <= 8000 chars (MAX_CONVO_CHARS) of
    ``"speaker: text"`` lines;
  - prefixed ``DOCUMENT_DATE: <date of last message>`` (writeback.ts:152-158; channel-observe.ts:999-1001).
- Tag recall: ``recall_tagged_memories`` -> ``buildBrainProfileContext`` (src/brain/memory/profile-recall.ts:143-218):
  - registry tags are ordered by use_count desc;
  - person tags come from people named in the question;
  - then add tags whose ``key + label`` haystack contains any query token of >= 3 chars (substring test), up to 10
    tags (FLOOR_TAG_KEYS);
  - fetch memories carrying any selected tag, newest-updated first, cap 500 (FLOOR_CANDIDATE_LIMIT);
  - dedupe by lowercased, whitespace-collapsed text;
  - pack to 20,000 tokens, costed as chars/4 + 2 per memory (FLOOR_TOKEN_BUDGET);
  - each memory is rendered as ``"- {memory}"``.
- No recency decay, no reranker, and no graph expansion in search: the project has none client-side.

APPROXIMATED (no LLM; supermemory server is closed)
- Server fact extraction is replaced by memory units:
  - one unit per turn: ``"DOCUMENT_DATE: <turn date>\\n<speaker>: <text>"``;
  - for turns over 400 chars, one extra unit per sentence, with the same header.
- Server chunking: documents are cut into ~1000-char chunks on line boundaries, overlapping by one line when that
  line is <= 100 chars.
  - Each chunk is indexed with its document's ``DOCUMENT_DATE`` header (contextual chunking).
  - The chunk is returned without the header.
- Server hybrid similarity: ``sim = 0.7 * dense_cos + 0.3 * bm25 / max_bm25``.
  - It is computed per layer (memories / chunks) over the union of the BM25 top-200 and the dense top-200.
  - The negative cosine part is clipped to 0.
  - The 0.7/0.3 weights are an assumption; supermemory does not publish its fusion.
- Threshold 0.3: kept on the fused scale and checked on a DEV corpus only.
  - It neither drops nearly everything nor keeps nearly everything.
  - The BM25 part guarantees that lexical top hits survive, which mirrors a hybrid engine.
- Tags: LLM topic/project tags and the topic tree are not reproducible. Units carry deterministic tags:
  - ``person_<speaker>``, labelled with the speaker name;
  - ``channel_<session>``, labelled with the session id.
- Ambient profile: injected every turn, but it consists of bucketed person/channel memories and tagged non-person
  "static" memories. With no buckets and every unit person-tagged, it is faithfully empty, so it is omitted.
- Supersession (server ``isLatest``/"updates"): optional and off by default (``supersede=False``).
  - When on, it keeps the newer of two same-speaker units with cos >= 0.95.
  - It is not calibrated to our embedder.

ASSEMBLY / PARAMETERS
- Final list: search results (<= 40, similarity order), then tag-recall memories not already present (recency order).
- Truncated to k. Deduped by unit/chunk id.
- Parameters:
  - search: per_container_limit=40, threshold=0.3, w_dense=0.7, w_bm25=0.3, cand=200;
  - documents: window_msgs=60, window_chars=8000, chunk_chars=1000, chunk_overlap_max=100;
  - units: long_turn=400;
  - tag recall: tag_keys=10, tag_fetch=500, tag_budget=20000.
- q.time is not used: the project's retrieval has no "now".

DELIBERATE DEVIATIONS
- None beyond the approximations above.
- The wrapper text of the tag-recall block (``<brain_memory_context>``, the topic-map header and the truncation
  notice) is not charged. Only the memory lines are returned.
"""
from __future__ import annotations

import hashlib
import re

import numpy as np

from bench.system import Item, System
from bench.text import sentences

_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _sm_id(key: str) -> str:
    n = int(hashlib.sha256(key.encode()).hexdigest(), 16)
    out = []
    for _ in range(22):
        n, r = divmod(n, 62)
        out.append(_B62[r])
    return "".join(out)


def _norm_tag(key: str) -> str:
    """normalizeBrainTagKey (src/brain/memory/tags.ts:90-101)."""
    k = key.strip().lower()
    k = re.sub(r"[^a-z0-9/]+", "_", k)
    k = re.sub(r"_*/_*", "/", k)
    k = re.sub(r"/{2,}", "/", k)
    k = re.sub(r"^[/_]+|[/_]+$", "", k)[:128]
    return k or "other"


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


class CompanyBrain(System):
    name = "supermemory-company-brain"
    family = "company-brain"
    faithful_notes = (
        "search_company_brain: a hybrid search (supermemory memories + chunks) per container with limit 40 and "
        "threshold 0.3, no rerank and no query rewrite. Results are merged by max similarity, the top 40 are kept, "
        "and they are formatted as 'n. Memory|Chunk (pct%) id: text'. Next come recall_tagged_memories results "
        "(<= 10 tags matched by person name or query-token substring, <= 500 newest memories, deduped, packed to 20k "
        "tokens). Approximations: the server's LLM extraction becomes per-turn units (plus sentence units for long "
        "turns) with a DOCUMENT_DATE prefix; documents are channel-observe-style windows (60 msgs / 8000 chars) "
        "chunked at about 1000 chars; the unpublished hybrid fusion is assumed to be 0.7*cosine + 0.3*max-normalised "
        "BM25; LLM topic tags become person_/channel_ tags. The ambient profile is empty without buckets, and "
        "supersession is off."
    )

    def __init__(self, per_container_limit: int = 40, threshold: float = 0.3, w_dense: float = 0.7,
                 w_bm25: float = 0.3, cand: int = 200, window_msgs: int = 60, window_chars: int = 8000,
                 chunk_chars: int = 1000, chunk_overlap_max: int = 100, long_turn: int = 400,
                 tag_keys: int = 10, tag_fetch: int = 500, tag_budget: int = 20_000, supersede: bool = False):
        self.limit, self.threshold = per_container_limit, threshold
        self.w_dense, self.w_bm25, self.cand = w_dense, w_bm25, cand
        self.window_msgs, self.window_chars = window_msgs, window_chars
        self.chunk_chars, self.chunk_overlap_max, self.long_turn = chunk_chars, chunk_overlap_max, long_turn
        self.tag_keys, self.tag_fetch, self.tag_budget = tag_keys, tag_fetch, tag_budget
        self.supersede = supersede

    # ------------------------------------------------------------------ build
    def build(self, corpus, enc):
        self.enc = enc
        by_sess: dict[str, list] = {}
        for t in corpus.turns:
            by_sess.setdefault(t.session_id, []).append(t)
        for v in by_sess.values():
            v.sort(key=lambda t: (t.ts, t.idx))

        # memory units (approximate server-extracted memories)
        units = []  # dict(id, text, turns, ts, tags, speaker)
        for t in corpus.turns:
            hdr = f"DOCUMENT_DATE: {t.ts.strftime('%Y-%m-%d')}\n"
            ptag, ctag = _norm_tag(f"person_{t.speaker}"), _norm_tag(f"channel_{t.session_id}")
            base = {"turns": [t.id], "ts": t.ts, "tags": (ptag, ctag), "speaker": t.speaker}
            units.append({**base, "id": _sm_id(f"m:{t.id}"), "text": f"{hdr}{t.speaker}: {t.text}"})
            if len(t.text) > self.long_turn:
                for si, s in enumerate(sentences(t.text)):
                    units.append({**base, "id": _sm_id(f"m:{t.id}:{si}"), "text": f"{hdr}{t.speaker}: {s}"})

        uE = enc.embed_many([u["text"] for u in units])
        if self.supersede:  # optional isLatest emulation; off by default
            keep = np.ones(len(units), bool)
            order = sorted(range(len(units)), key=lambda i: units[i]["ts"])
            for a_pos, i in enumerate(order):
                for j in order[a_pos + 1:]:
                    if units[i]["speaker"] == units[j]["speaker"] and units[i]["turns"] != units[j]["turns"] \
                            and float(uE[i] @ uE[j]) >= 0.95:
                        keep[i] = False
                        break
            units = [u for u, k in zip(units, keep) if k]
            uE = uE[keep]
        self.units, self.uE = units, uE
        self.uBM = enc.bm25([u["text"] for u in units])

        # documents (channel-observe batches) -> chunks
        chunks = []  # dict(id, text, index_text, turns)
        for sid in corpus.session_order:
            turns = by_sess.get(sid, [])
            i = 0
            while i < len(turns):
                batch, size = [], 0
                while i < len(turns) and len(batch) < self.window_msgs:
                    line = f"{turns[i].speaker}: {turns[i].text}"
                    add = len(line) + (1 if batch else 0)
                    if batch and size + add > self.window_chars:
                        break
                    batch.append((turns[i], line))
                    size += add
                    i += 1
                hdr = f"DOCUMENT_DATE: {batch[-1][0].ts.strftime('%Y-%m-%d')}"
                chunks += self._chunk(sid, len(chunks), hdr, batch)
        self.chunks = chunks
        self.cE = enc.embed_many([c["index_text"] for c in chunks])
        self.cBM = enc.bm25([c["index_text"] for c in chunks])

        # tag registry: use_count = number of memories carrying the tag
        reg: dict[str, dict] = {}
        for u in units:
            for tag in u["tags"]:
                r = reg.setdefault(tag, {"key": tag, "count": 0, "updated": u["ts"],
                                         "label": u["speaker"] if tag.startswith("person_") else tag[len("channel_"):]})
                r["count"] += 1
                r["updated"] = max(r["updated"], u["ts"])
        self.tags = sorted(reg.values(), key=lambda r: (-r["count"], -r["updated"].timestamp()))
        self.speakers = sorted({u["speaker"] for u in units})

    def _chunk(self, sid, base, hdr, batch):
        out, cur, cur_len = [], [], 0
        carried = False  # cur holds only the overlap line carried from the previous chunk
        for t, ln in batch:
            add = len(ln) + (1 if cur else 0)
            if cur and not carried and cur_len + add > self.chunk_chars:
                out.append(cur)
                last = cur[-1]
                cur = [last] if len(last[1]) <= self.chunk_overlap_max else []
                cur_len = len(last[1]) if cur else 0
                carried = bool(cur)
                add = len(ln) + (1 if cur else 0)
            cur.append((t, ln))
            cur_len += add
            carried = False
        if cur:
            out.append(cur)
        res = []
        for n, c in enumerate(out):
            body = "\n".join(ln for _, ln in c)
            text = f"{hdr}\n{body}" if n == 0 else body  # header lives at the top of the document only
            res.append({"id": _sm_id(f"c:{sid}:{base + n}"), "text": text, "index_text": f"{hdr}\n{body}",
                        "turns": list(dict.fromkeys(t.id for t, _ in c))})
        return res

    # --------------------------------------------------------------- retrieve
    def _layer(self, q, qv, E, BM):
        n = E.shape[0]
        if n == 0:
            return []
        dense = np.clip(E @ qv, 0.0, None)
        bm = BM.score(q)
        c = min(self.cand, n)
        cand = set(np.argpartition(-dense, c - 1)[:c].tolist())
        bpos = np.flatnonzero(bm > 0)
        if len(bpos):
            cb = min(self.cand, len(bpos))
            cand |= set(bpos[np.argpartition(-bm[bpos], cb - 1)[:cb]].tolist())
        cand = np.fromiter(cand, int)
        mx = float(bm[cand].max()) if len(cand) else 0.0
        bmn = bm[cand] / mx if mx > 0 else np.zeros(len(cand))
        sim = self.w_dense * dense[cand] + self.w_bm25 * bmn
        return [(int(i), float(s)) for i, s in zip(cand, sim)]

    def search(self, q: str):
        qv = self.enc.embed(q)
        res = []
        for kind, E, BM, store in (("Memory", self.uE, self.uBM, self.units),
                                   ("Chunk", self.cE, self.cBM, self.chunks)):
            for i, s in self._layer(q, qv, E, BM):
                if s >= self.threshold:
                    res.append((s, kind, store[i]))
        res.sort(key=lambda x: -x[0])
        res = res[: self.limit]  # per-container top `limit`
        merged: dict[str, tuple] = {}  # merge across containers by id, max similarity (one container here)
        for r in res:
            if r[2]["id"] not in merged or r[0] > merged[r[2]["id"]][0]:
                merged[r[2]["id"]] = r
        return sorted(merged.values(), key=lambda x: -x[0])[:40]

    def tag_recall(self, q: str):
        ql = q.lower()
        keys: list[str] = []
        for sp in self.speakers:  # person tags for people named in the question
            if re.search(rf"\b{re.escape(sp.lower())}\b", ql):
                keys.append(_norm_tag(f"person_{sp}"))
        qtok = {t for t in re.split(r"[^a-z0-9]+", ql) if len(t) >= 3}
        for tag in self.tags:
            if len(keys) >= self.tag_keys:
                break
            if tag["key"] in keys:
                continue
            hay = f"{tag['key']} {tag['label']}".lower()
            if any(t in hay for t in qtok):
                keys.append(tag["key"])
        keys = keys[: self.tag_keys]
        if not keys:
            return []
        ks = set(keys)
        rows = [u for u in self.units if ks & set(u["tags"])]
        rows.sort(key=lambda u: u["ts"], reverse=True)
        rows = rows[: self.tag_fetch]
        out, seen, used = [], set(), 0
        for u in rows:
            key = _ws(u["text"]).lower()
            if key in seen:
                continue
            seen.add(key)
            cost = -(-len(u["text"].strip()) // 4) + 2
            if used + cost > self.tag_budget:
                break
            used += cost
            out.append(u)
        return out

    def retrieve(self, q, k=30):
        items, seen = [], set()
        for n, (s, kind, x) in enumerate(self.search(q.text), 1):
            text = f"{n}. {kind} ({s * 100:.0f}%) {x['id']}: {_ws(x['text'])}"
            items.append(Item(list(x["turns"]), text, s, {"channel": "search", "type": kind.lower()}))
            seen.add(x["id"])
        for u in self.tag_recall(q.text):
            if len(items) >= k:
                break
            if u["id"] in seen:
                continue
            seen.add(u["id"])
            items.append(Item(list(u["turns"]), f"- {u['text'].strip()}", 0.0, {"channel": "tag_recall"}))
        return items[:k]
