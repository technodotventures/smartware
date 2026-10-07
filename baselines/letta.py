"""Letta (letta-ai/letta archive @56ba9c2 + letta-ai/letta-code @4b028fa) LLM-free retrieval baseline.

Implements section 13 of /home/claude/mp/reports/letta.md. The default is the Letta Cloud hybrid configuration,
Letta's strongest shipped search: recall over the message log via Turbopuffer vector ANN + BM25 fused by RRF.
That is what ``conversation_search`` (archive) and the letta-code recall subagent's ``letta messages search``
hit on Cloud. This is followed by the recall subagent's "needle + expand" step and the in-context tail.

FAITHFUL
- Unit = one message (one turn). No chunking. Messages are never split (message_manager.py:477-603).
- Indexed text = ``_extract_message_text``. A plain user or assistant message is stored as
  ``json.dumps({"content": text})``, with Python's default ensure_ascii=True, exactly as Letta stores it in
  Turbopuffer. Both BM25 and the embedder see that string. The query is embedded raw.
- Hybrid search (tpuf_client.py:791-907, 1489-1560):
  - Vector channel: top N by cosine (ANN, cosine_distance).
  - Lexical channel: top N by BM25, matching docs only (score > 0).
  - Both use N = limit, the final k (Letta passes top_k=limit to each sub-query).
  - Filters (date range) are applied INSIDE each channel, i.e. before ranking.
  - RRF with k = 60 and weights 0.5 / 0.5: ``score = 0.5/(60+r_vec) + 0.5/(60+r_fts)``. Ranks start at 1, and a
    missing list contributes 0. Sort descending and keep the top `limit`.
- limit = 10: the letta-code ``messages search`` default (cli/subcommands/messages.ts:432), which is what the
  Cloud recall subagent runs. Pass limit=5 to mimic the server tool default RETRIEVAL_QUERY_DEFAULT_PAGE_SIZE.
- Inclusive day bounds on date filters: a date-only start is 00:00:00, and a date-only end is 23:59:59.999999.
- Needle + expand (agent/prompts/recall_subagent.md, "Strategy 1"):
  - Take the top-1 hit.
  - ``messages list --before <id> --limit 10``: the 10 messages before it, by global sequence.
  - ``messages list --after <id> --order asc --limit 10``: the 10 messages after it.
  - The window crosses session boundaries, because Letta lists by agent-wide sequence_id.
- No recency decay, no reranker, no graph, no truncation of search tool returns. The archive never clips
  conversation_search output (letta_agent_v3.py:1878).
- Item.text = the conversation_search result dict as JSON:
  - ``timestamp``: ISO in the agent timezone, default "UTC".
  - ``time_ago``: "Ns/m/h/d ago".
  - ``role``.
  - ``content``: the parsed ``{"content": ...}``.
  - ``relevance`` is included for search hits, holding rrf_score, vector_rank and fts_rank (keys that are None
    are dropped). These are the fields Letta puts in the tool return (core_tool_executor.py:186-273).
- "now" for time_ago: Letta uses wall-clock now. We use q.time, the benchmark's now.

APPROXIMATED (no LLM)
- LLM-authored core-memory blocks, archival passages and the compaction summary cannot be reproduced and are
  omitted. Only recall (message-log) retrieval is modelled.
- The LLM's query formulation is replaced by the raw question.
- The LLM-chosen start_date / end_date are replaced by regex-parsed ABSOLUTE dates in the question:
  - YYYY-MM-DD, "D Month YYYY", "Month D, YYYY" give a day window.
  - "Month YYYY" gives a month window.
  - Several dates give the window min..max.
  - If the filtered pool has fewer than `limit` messages, the filter is dropped, as an agent would retry.
  - Relative expressions ("last week") are ignored.
- Turbopuffer BM25 (word tokenizer, default no stemming) is approximated by the shared bench BM25 (k1=1.2,
  b=0.75, stemmed, stopwords removed). OpenAI text-embedding-3-small is replaced by the shared dense encoder.
  No cosine thresholds exist in this path, so there is no threshold conversion.
- Roles: an LME turn keeps its user/assistant role. LoCoMo has two human speakers and no agent, so both are
  mapped to role "user" (they would be user messages sent to the agent).
- Small deviation: a ``name`` field with the speaker is added when the speaker is not user/assistant. Letta
  stores this in Message.name; it is not part of the indexed text. Without it LoCoMo results would not say
  who spoke.
- In-context tail: the last T = 20 turns of the most recent session (the corpus's last session by time) stand
  in for the messages still in the context window after sliding-window compaction (which keeps about 70% of
  the window).
- "Search echo" demotion (x0.1, from letta-code local BM25): applied to a message whose text equals the
  question. This is rarely triggered on these corpora.

TUNABLE KNOBS (dev-split tuning only; defaults unchanged)
- ``limit`` in {5, 10, 20, 50}: the result count per search, which is also the per-channel candidate pool.
  Letta exposes it as:
  - ``conversation_search(limit=...)`` (vendor/letta-ai_letta-archive/letta/functions/function_sets/base.py:91),
    default 5 (constants.py:458)
  - ``POST /v1/messages/search`` ``limit`` 1..100 (letta/schemas/message.py:2688)
  - ``letta messages search --limit``, default 10 (vendor/letta-ai_letta-code/src/cli/subcommands/messages.ts:67,432)
- ``search_mode`` in {"hybrid", "vector", "fts"}: the request parameter ``search_mode`` (letta/schemas/message.py:2682)
  and the CLI ``--mode`` (messages.ts:64,427-429). Single modes rank one channel and return combined_score
  1/rank (tpuf_client.py:1214-1225).
- ``expand`` in {0, 10}: the recall subagent's ``letta messages list --before/--after --limit N`` expansion
  around the top hit. It is set in vendor/letta-ai_letta-code/src/agent/prompts/recall_subagent.md
  ("Strategy 1: Needle + Expand", --limit 10). 0 means search results only (report section 13, step 8).
Total grid = 4 x 3 x 2 = 24.

ASSEMBLY / PARAMETERS
- Output order, deduplicated by turn id, first occurrence kept:
  1. Fused hits in RRF order.
  2. The ±10 expansion window around the top-1 hit, in chronological order.
  3. The tail, in chronological order.
- Returns up to k items. Defaults: limit=10, N=limit, rrf_k=60, weights 0.5/0.5, expand=10 before and 10
  after, tail=20, echo_factor=0.1, timezone UTC.
- Option ``variant="oss"`` gives self-hosted Letta without Turbopuffer, for reference only (not the default):
  - lowercase whole-question substring match (ILIKE '%q%'), newest first, top 5.
  - date filters ignored.
  - no expansion.
"""
from __future__ import annotations

import calendar
import json
import re
from datetime import datetime, timedelta

import numpy as np

from bench.system import Item, System

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MON = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?"
_RE_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_RE_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MON},?\s+(\d{{4}})\b", re.I)
_RE_MDY = re.compile(rf"\b{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I)
_RE_MY = re.compile(rf"\b{_MON}\s+(?:of\s+)?(\d{{4}})\b", re.I)


def _month(s: str) -> int | None:
    s = s.lower().rstrip(".")
    if s == "sept":
        s = "sep"
    return _MONTHS.get(s)


def _day_window(y: int, m: int, d: int):
    try:
        start = datetime(y, m, d)
    except ValueError:
        return None
    return start, start.replace(hour=23, minute=59, second=59, microsecond=999999)


def parse_date_window(q: str):
    """Absolute dates in the question -> (start, end) inclusive, else None."""
    wins = []
    taken = []

    def free(span):
        return all(span[1] <= a or span[0] >= b for a, b in taken)

    for m in _RE_ISO.finditer(q):
        w = _day_window(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if w:
            wins.append(w)
            taken.append(m.span())
    for m in _RE_DMY.finditer(q):
        mo = _month(m.group(2))
        if mo and free(m.span()):
            w = _day_window(int(m.group(3)), mo, int(m.group(1)))
            if w:
                wins.append(w)
                taken.append(m.span())
    for m in _RE_MDY.finditer(q):
        mo = _month(m.group(1))
        if mo and free(m.span()):
            w = _day_window(int(m.group(3)), mo, int(m.group(2)))
            if w:
                wins.append(w)
                taken.append(m.span())
    for m in _RE_MY.finditer(q):
        mo = _month(m.group(1))
        if mo and free(m.span()):
            y = int(m.group(2))
            last = calendar.monthrange(y, mo)[1]
            wins.append((datetime(y, mo, 1), datetime(y, mo, last, 23, 59, 59, 999999)))
            taken.append(m.span())
    if not wins:
        return None
    return min(w[0] for w in wins), max(w[1] for w in wins)


def _time_ago(now: datetime, ts: datetime) -> str:
    total = int((now - ts).total_seconds())
    if total < 60:
        return f"{total}s ago"
    if total < 3600:
        return f"{total // 60}m ago"
    if total < 86400:
        return f"{total // 3600}h ago"
    return f"{total // 86400}d ago"


class Letta(System):
    name = "letta"
    family = "memory-os"
    faithful_notes = (
        "Letta Cloud recall path, as reached by conversation_search or the letta-code recall subagent's "
        "`letta messages search` (default hybrid, limit 10). Units are whole messages, indexed as Letta's "
        "json.dumps({'content': text}). The vector top-10 and BM25 top-10 (matching docs only) are fused with "
        "RRF k=60 and weights 0.5/0.5. Date filters are applied pre-ranking with inclusive day bounds; the "
        "LLM's date arguments are approximated by regex-parsed absolute dates, and the filter is dropped if it "
        "leaves fewer than 10 messages. The recall subagent's needle-and-expand step is reproduced as ±10 "
        "messages around the top-1 hit across sessions, plus a 20-turn in-context tail of the latest session. "
        "Items are rendered as conversation_search result dicts with timestamp, time_ago against q.time, role, "
        "content and relevance. LLM-written core blocks, archival passages and compaction summaries are "
        "omitted. Turbopuffer BM25 and OpenAI embeddings are replaced by the shared encoders. No decay, no "
        "reranker."
    )

    TUNABLE: dict[str, list] = {
        "limit": [5, 10, 20, 50],
        "search_mode": ["hybrid", "vector", "fts"],
        "expand": [0, 10],
    }

    def __init__(self, variant: str = "cloud", limit: int = 10, rrf_k: int = 60, vector_weight: float = 0.5,
                 fts_weight: float = 0.5, expand: int = 10, tail: int = 20, echo_factor: float = 0.1,
                 search_mode: str = "hybrid"):
        if search_mode not in ("hybrid", "vector", "fts"):
            raise ValueError(f"Invalid search_mode: {search_mode}")
        self.variant = variant
        self.search_mode = search_mode
        self.limit = limit if variant == "cloud" else 5
        self.rrf_k, self.wv, self.wf = rrf_k, vector_weight, fts_weight
        self.expand, self.tail_n, self.echo_factor = expand, tail, echo_factor

    # ------------------------------------------------------------------ build
    def build(self, corpus, enc):
        self.enc = enc
        order = {s: i for i, s in enumerate(corpus.session_order)}
        turns = sorted(corpus.turns, key=lambda t: (t.ts, order.get(t.session_id, 0), t.idx))
        turns = [t for t in turns if t.text and t.text.strip()]
        self.turns = turns
        self.n = len(turns)
        self.ts = np.array([t.ts.timestamp() for t in turns], dtype=np.float64)
        # _extract_message_text: plain (non-JSON) content is wrapped as {"content": ...}, ensure_ascii default.
        self.indexed = [json.dumps({"content": t.text}) for t in turns]
        self.bm25 = enc.bm25(self.indexed)
        self.E = enc.embed_many(self.indexed)
        self.lower = [t.text.lower() for t in turns]
        self.norm_text = [" ".join(t.text.lower().split()) for t in turns]
        last_sess = turns[-1].session_id if turns else None
        sess_pos = [i for i, t in enumerate(turns) if t.session_id == last_sess]
        self.tail_pos = sess_pos[-self.tail_n:] if self.tail_n > 0 else []

    # ------------------------------------------------------------------ render
    def _render(self, i: int, now: datetime, relevance: dict | None = None) -> str:
        t = self.turns[i]
        role = "assistant" if t.role == "assistant" else "user"
        d = {"timestamp": t.ts.replace(microsecond=0).isoformat() + "+00:00", "time_ago": _time_ago(now, t.ts),
             "role": role}
        if t.speaker not in ("user", "assistant"):
            d["name"] = t.speaker
        if relevance:
            rel = {k: v for k, v in relevance.items() if v is not None}
            if rel:
                d["relevance"] = rel
        d["content"] = t.text
        return json.dumps(d, ensure_ascii=False)

    # ------------------------------------------------------------------ search
    def _hybrid(self, q) -> list[tuple[int, float, dict]]:
        K = self.limit
        mask = np.ones(self.n, dtype=bool)
        win = parse_date_window(q.text)
        if win is not None:
            lo, hi = win[0].timestamp(), win[1].timestamp()
            m = (self.ts >= lo) & (self.ts <= hi)
            if int(m.sum()) >= K:
                mask = m
        idx = np.nonzero(mask)[0]
        if idx.size == 0:
            return []
        # vector channel: ANN top-K by cosine
        vs = self.E[idx] @ self.enc.embed(q.text)
        vorder = idx[np.argsort(-vs, kind="stable")[:K]]
        # fts channel: BM25 top-K, matching docs only
        bs = self.bm25.score(q.text)[idx]
        pos = np.nonzero(bs > 0)[0]
        forder = idx[pos[np.argsort(-bs[pos], kind="stable")[:K]]]
        qn = " ".join(q.text.lower().split())
        if self.search_mode != "hybrid":
            # single-mode: rank one channel, combined_score = 1/rank (tpuf_client.py:1214-1225)
            order = vorder if self.search_mode == "vector" else forder
            key = f"{self.search_mode}_rank"
            single = []
            for r, d in enumerate(order):
                d = int(d)
                s = 1.0 / (r + 1)
                if self.norm_text[d] == qn:
                    s *= self.echo_factor
                single.append((d, s, {"rrf_score": s, key: r + 1}))
            single.sort(key=lambda x: (-x[1], -x[0]))
            return single[:K]
        vr = {int(d): r + 1 for r, d in enumerate(vorder)}
        fr = {int(d): r + 1 for r, d in enumerate(forder)}
        out = []
        for d in set(vr) | set(fr):
            s = 0.0
            if d in vr:
                s += self.wv / (self.rrf_k + vr[d])
            if d in fr:
                s += self.wf / (self.rrf_k + fr[d])
            if self.norm_text[d] == qn:
                s *= self.echo_factor
            out.append((d, s, {"rrf_score": s, "vector_rank": vr.get(d), "fts_rank": fr.get(d)}))
        out.sort(key=lambda x: (-x[1], -x[0]))  # tie -> newer first
        return out[:K]

    def _oss(self, q) -> list[tuple[int, float, dict]]:
        ql = q.text.lower()
        hits = [i for i in range(self.n - 1, -1, -1) if ql in self.lower[i]]
        return [(i, 0.0, {}) for i in hits[: self.limit]]

    def retrieve(self, q, k=30):
        now = q.time
        hits = self._hybrid(q) if self.variant == "cloud" else self._oss(q)
        items: list[Item] = []
        seen: set[int] = set()

        def add(i, score, rel=None, kind=""):
            if i in seen or len(items) >= k:
                return
            seen.add(i)
            items.append(Item([self.turns[i].id], self._render(i, now, rel), float(score), {"kind": kind}))

        for i, s, rel in hits:
            add(i, s, rel, "search")
        if self.variant == "cloud" and hits and self.expand > 0:
            top = hits[0][0]
            for i in range(max(0, top - self.expand), min(self.n, top + self.expand + 1)):
                add(i, 0.0, None, "expand")
        if self.variant == "cloud":
            for i in self.tail_pos:
                add(i, 0.0, None, "in_context")
        if not items and self.tail_pos:  # nothing found: the in-context tail is still visible
            for i in self.tail_pos:
                add(i, 0.0, None, "in_context")
        return items
