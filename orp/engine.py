"""ORP reference engine.

A single-process, dependency-light implementation of the Open Recall Protocol
(spec/ORP-0.1.md): bi-temporal memory records, provenance-closed access control
enforced before ranking, claim supersession, and a budget-aware multi-channel
recall pipeline that returns a ContextPack.

Recall pipeline (all deterministic; LLM hooks optional):
  0. Admissibility mask (tenant, ACL tokens, status/validity at as_of, known_at, scope, kinds) -- before ranking.
  1. Query analysis: content terms, named entities, temporal intent (window/order/current/when/count), role intent.
  2. Candidate channels over memory units: BM25(unit), BM25(window), dense(unit), dense(window),
     thread prior (BM25+dense over whole threads), entity match, temporal window, RM3 pseudo-relevance feedback.
  3. Weighted reciprocal-rank fusion.
  4. Feature rerank: query-term coverage, actor/role match, temporal fit, order/current intents, thread prior.
  5. Diversification for aggregate questions (count/list/multi-thread).
  6. Packing into a token budget: thread headers emitted once, relative dates resolved inline,
     short replies carry their prompt, long units are snippeted (and only claimed when mostly included).
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Protocol

import numpy as np

from . import timeparse
from ._text import STOPWORDS, count_tokens, sentences, stem, terms, words
from .index import BM25
from .model import (SYSTEM, Claim, ContextItem, ContextPack, Memory, Principal, RecallRequest)


class Embedder(Protocol):
    def embed(self, text: str) -> np.ndarray: ...

    def embed_many(self, texts: list[str]) -> np.ndarray: ...


@dataclass
class Params:
    # channel weights for weighted RRF
    w_bm25: float = 1.0
    w_bm25_win: float = 0.6
    w_dense: float = 0.6
    w_dense_win: float = 0.4
    w_thread: float = 0.5
    w_entity: float = 0.4
    w_time: float = 0.5
    w_prf: float = 0.6
    w_bm25_seg: float = 0.6
    w_dense_seg: float = 0.4
    seg_sentences: int = 2
    rrf_k: float = 60.0
    depth: int = 200  # candidates per channel
    # rerank features (multiplicative on fused score)
    f_cover: float = 0.6
    f_actor: float = 0.25
    f_role: float = 0.25
    f_order: float = 0.15
    f_thread_div: float = 0.0
    prf_docs: int = 8
    prf_terms: int = 12
    prf_mix: float = 0.5
    # packing
    long_unit_tokens: int = 220
    snippet_sentences: int = 4
    claim_fraction: float = 0.0
    prompt_merge_words: int = 14
    expand_next: bool = True
    expand_prev_q: bool = True
    headers: bool = True
    resolve_dates: bool = True
    count_div: bool = True


_Q_STRIP = re.compile(r"^(what|when|where|who|whom|which|why|how|did|does|do|is|are|was|were|can|could|would|should|"
                      r"has|have|had)\b", re.I)


class Engine:
    def __init__(self, embedder: Embedder, params: Params | None = None):
        self.emb = embedder
        self.p = params or Params()
        self.mem: dict[str, Memory] = {}
        self.order: list[str] = []
        self._hash: dict[str, str] = {}
        self._dirty = True
        self._claims: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.audit: list[dict] = []

    # ------------------------------------------------------------ write
    def remember(self, items: Memory | list[Memory], principal: Principal = SYSTEM) -> list[str]:
        if isinstance(items, Memory):
            items = [items]
        ids = []
        for m in items:
            h = m.meta.get("idempotency_key") or m.content_hash()
            if h in self._hash:  # idempotent replay
                ids.append(self._hash[h])
                continue
            if m.observed_at is None:
                m.observed_at = datetime.now()
            if m.valid_from is None:
                m.valid_from = m.event_time or m.observed_at
            # provenance-closed access: derived memories may be read only by principals who can read every source
            if m.derived_from:
                src = [self.mem[s] for s in m.derived_from if s in self.mem]
                acl = None
                for s in src:
                    if s.readers is not None:
                        acl = set(s.readers) if acl is None else acl & set(s.readers)
                if acl is not None:
                    m.readers = frozenset(acl) if m.readers is None else frozenset(set(m.readers) & acl)
            # deterministic enrichment
            if self.p.resolve_dates and m.observed_at:
                refs = timeparse.resolve_relative(m.content, m.event_time or m.observed_at)
                if refs:
                    m.meta["time_refs"] = refs
            m.meta.setdefault("entities", _entities(m.content))
            self.mem[m.id] = m
            self.order.append(m.id)
            self._hash[h] = m.id
            if m.claim is not None:
                self._supersede(m)
            self.audit.append({"op": "remember", "id": m.id, "by": principal.id})
            ids.append(m.id)
        self._dirty = True
        return ids

    def _supersede(self, m: Memory):
        key = (m.tenant, m.claim.key())
        chain = self._claims[key]
        chain.append(m.id)
        if m.claim.cardinality != "one":
            return
        # order by world validity, then by transaction time; the latest-valid assertion is current
        chain.sort(key=lambda i: (self.mem[i].valid_from or datetime.min, self.mem[i].observed_at or datetime.min))
        live = [i for i in chain if self.mem[i].status in ("active", "superseded")]
        for a, b in zip(live, live[1:]):
            ma, mb = self.mem[a], self.mem[b]
            if ma.claim.object.strip().lower() == mb.claim.object.strip().lower():
                continue
            ma.status = "superseded"
            ma.superseded_by = b
            ma.valid_to = mb.valid_from
            if a not in mb.supersedes:
                mb.supersedes.append(a)
        if live:
            last = self.mem[live[-1]]
            last.status = "active"
            last.superseded_by = None
            last.valid_to = None

    def reclose(self, mem_id: str) -> list[str]:
        """Re-apply provenance-closed ACL to everything derived (transitively) from mem_id after its ACL changed."""
        changed, frontier = [], [mem_id]
        while frontier:
            cur = frontier.pop()
            for d in self.mem.values():
                if cur not in d.derived_from:
                    continue
                acl = None
                for s in d.derived_from:
                    sm = self.mem.get(s)
                    if sm is not None and sm.readers is not None:
                        acl = set(sm.readers) if acl is None else acl & set(sm.readers)
                new = d.readers if acl is None else frozenset(acl if d.readers is None else set(d.readers) & acl)
                if new != d.readers:
                    d.readers = new
                    changed.append(d.id)
                frontier.append(d.id)
        self._dirty = True
        return changed

    def forget(self, mem_id: str, mode: str = "retract", cascade: bool = True, principal: Principal = SYSTEM,
               reason: str = "") -> list[str]:
        """retract: hidden from recall, kept for audit. redact: content erased. purge: record removed.
        cascade: derived memories built from it are retracted too (no laundering through summaries)."""
        hit = [mem_id]
        if cascade:
            frontier = [mem_id]
            while frontier:
                cur = frontier.pop()
                for m in self.mem.values():
                    if cur in m.derived_from and m.id not in hit:
                        hit.append(m.id)
                        frontier.append(m.id)
        for i in hit:
            m = self.mem.get(i)
            if not m:
                continue
            if mode == "purge":
                del self.mem[i]
                self.order.remove(i)
            else:
                m.status = "retracted"
                if mode == "redact":
                    m.content = "[redacted]"
                    m.meta.pop("time_refs", None)
            self.audit.append({"op": f"forget:{mode}", "id": i, "by": principal.id, "reason": reason})
        self._dirty = True
        return hit

    def history(self, subject: str, predicate: str, tenant: str = "default") -> list[Memory]:
        key = (tenant, f"{subject.lower().strip()}|{predicate.lower().strip()}")
        return [self.mem[i] for i in self._claims.get(key, []) if i in self.mem]

    # ------------------------------------------------------------ index
    def _build(self):
        ids = [i for i in self.order if i in self.mem]
        self.ids = ids
        ms = [self.mem[i] for i in ids]
        n = len(ms)
        self.pos = {i: k for k, i in enumerate(ids)}
        unit_txt = []
        for m in ms:
            extra = " ".join(r for _, r in m.meta.get("time_refs", []))
            unit_txt.append(f"{m.actor}: {m.content} {extra}".strip())
        self.unit_txt = unit_txt
        # neighbours within thread
        self.prev = np.full(n, -1)
        self.next = np.full(n, -1)
        by_thread = defaultdict(list)
        for k, m in enumerate(ms):
            by_thread[m.thread].append(k)
        for th, ks in by_thread.items():
            ks.sort(key=lambda k: (ms[k].seq, ms[k].observed_at or datetime.min))
            for a, b in zip(ks, ks[1:]):
                self.next[a] = b
                self.prev[b] = a
        self.by_thread = by_thread
        win_txt = []
        for k in range(n):
            parts = [unit_txt[self.prev[k]]] if self.prev[k] >= 0 else []
            parts.append(unit_txt[k])
            if self.next[k] >= 0:
                parts.append(unit_txt[self.next[k]])
            win_txt.append(" ".join(parts))
        seg_txt, seg_unit = [], []
        for k, m in enumerate(ms):
            ss = sentences(m.content)
            if len(ss) <= self.p.seg_sentences:
                seg_txt.append(unit_txt[k])
                seg_unit.append(k)
                continue
            w = self.p.seg_sentences
            for i in range(0, len(ss) - w + 1):
                seg_txt.append(f"{m.actor}: " + " ".join(ss[i:i + w]))
                seg_unit.append(k)
        self.seg_unit = np.array(seg_unit, dtype=int)
        self.bm_seg = BM25(seg_txt)
        self.ES = self.emb.embed_many(seg_txt) if seg_txt else np.zeros((0, 1))
        self.bm = BM25(unit_txt)
        self.bm_win = BM25(win_txt)
        self.E = self.emb.embed_many([f"{m.actor}: {m.content}" for m in ms]) if n else np.zeros((0, 1))
        W = np.zeros_like(self.E)
        for k in range(n):
            v = self.E[k].copy()
            if self.prev[k] >= 0:
                v += 0.5 * self.E[self.prev[k]]
            if self.next[k] >= 0:
                v += 0.5 * self.E[self.next[k]]
            W[k] = v / (np.linalg.norm(v) + 1e-9)
        self.W = W
        # threads
        self.threads = list(by_thread)
        self.thread_of = np.zeros(n, dtype=int)
        th_txt, th_vec = [], []
        for ti, th in enumerate(self.threads):
            ks = by_thread[th]
            self.thread_of[ks] = ti
            th_txt.append(" ".join(unit_txt[k] for k in ks))
            v = self.E[ks].mean(0)
            th_vec.append(v / (np.linalg.norm(v) + 1e-9))
        self.bm_th = BM25(th_txt)
        self.TV = np.stack(th_vec) if th_vec else np.zeros((0, 1))
        self.thread_start = {th: min((ms[k].observed_at or datetime.min) for k in ks) for th, ks in by_thread.items()}
        # entities and actors
        self.ent = defaultdict(set)
        self.actors = Counter(m.actor for m in ms)
        for k, m in enumerate(ms):
            for e in m.meta.get("entities", []):
                self.ent[e.lower()].add(k)
            if m.actor:
                self.ent[m.actor.lower()].add(k)
        self.ts = np.array([((m.event_time or m.observed_at) or datetime(1970, 1, 1)).timestamp() for m in ms])
        self.ntok = np.array([count_tokens(t) for t in unit_txt])
        roles = [m.role for m in ms]
        self.is_assistant = np.array([r == "assistant" for r in roles])
        self.has_roles = bool(self.is_assistant.any())
        self.unit_terms = [set(terms(t)) for t in unit_txt]
        self._dirty = False

    # ------------------------------------------------------------ admissibility (before ranking)
    def _mask(self, req: RecallRequest) -> np.ndarray:
        tok = req.principal.tokens()
        star = "*" in req.principal.groups
        as_of = req.as_of
        ok = np.zeros(len(self.ids), dtype=bool)
        for k, i in enumerate(self.ids):
            m = self.mem[i]
            if m.tenant != req.tenant:
                continue
            if not star and m.readers is not None and not (m.readers & tok):
                continue
            if m.status in ("retracted", "draft"):
                continue
            if m.status == "superseded" and not req.include_history:
                if not (as_of and m.valid_to and as_of < m.valid_to):  # as_of time travel: it was current then
                    continue
            if req.known_at and m.observed_at and m.observed_at > req.known_at:
                continue
            if as_of and m.valid_from and m.valid_from > as_of + timedelta(days=1) and m.kind != "event":
                continue
            if m.expires_at and as_of and m.expires_at <= as_of:
                continue
            if req.scope and not m.scope.startswith(req.scope):
                continue
            if req.kinds and m.kind not in req.kinds:
                continue
            ok[k] = True
        return ok

    # ------------------------------------------------------------ recall
    def recall(self, req: RecallRequest) -> ContextPack:
        if self._dirty:
            self._build()
        n = len(self.ids)
        if n == 0:
            return ContextPack([], 0, req.budget_tokens, 0.0, self._snapshot())
        p = self.p
        now = req.as_of or datetime.now()
        mask = self._mask(req)
        if not mask.any():
            return ContextPack([], 0, req.budget_tokens, 0.0, self._snapshot(), ["no admissible memories"])
        q = req.query
        qcore = _Q_STRIP.sub("", q).strip()
        ti = timeparse.parse_query(q, now)
        qterms = [t for t in terms(q)]
        qset = set(qterms)
        qvec = self.emb.embed(q)
        NEG = -1e9

        def topranks(score: np.ndarray, depth: int = p.depth) -> dict[int, int]:
            s = np.where(mask, score, NEG)
            idx = np.argpartition(-s, min(depth, n - 1))[:depth] if n > depth else np.arange(n)
            idx = idx[s[idx] > NEG / 2]
            idx = idx[np.argsort(-s[idx], kind="stable")]
            idx = [i for i in idx if score[i] > 0] if score.max() > 0 else list(idx)
            return {int(i): r for r, i in enumerate(idx)}

        chans: dict[str, tuple[float, dict[int, int]]] = {}
        s_bm = self.bm.score(q)
        chans["bm25"] = (p.w_bm25, topranks(s_bm))
        chans["bm25_win"] = (p.w_bm25_win, topranks(self.bm_win.score(q)))
        s_dense = self.E @ qvec
        chans["dense"] = (p.w_dense, topranks(s_dense + 1.0))
        chans["dense_win"] = (p.w_dense_win, topranks(self.W @ qvec + 1.0))
        if p.w_bm25_seg > 0 or p.w_dense_seg > 0:
            ub = np.zeros(n)
            np.maximum.at(ub, self.seg_unit, self.bm_seg.score(q))
            chans["bm25_seg"] = (p.w_bm25_seg, topranks(ub))
            ud = np.full(n, -1.0)
            np.maximum.at(ud, self.seg_unit, self.ES @ qvec)
            chans["dense_seg"] = (p.w_dense_seg, topranks(ud + 1.0))
        # thread prior
        th_s = self.bm_th.score(q)
        th_d = self.TV @ qvec
        th_rank = _rrf([np.argsort(-th_s), np.argsort(-th_d)], len(self.threads), k=10)
        unit_th = th_rank[self.thread_of]
        chans["thread"] = (p.w_thread, topranks(unit_th + 1e-6))
        # entities named in the query (actors and capitalised names seen in memory)
        qents = set()
        for w in re.findall(r"\b[A-Z][a-zA-Z'\-]+\b", q):
            wl = w.lower().removesuffix("'s")
            if wl in self.ent and wl not in STOPWORDS:
                qents.add(wl)
        if qents:
            es = np.zeros(n)
            for e in qents:
                for k in self.ent[e]:
                    es[k] += 1.0 / math.log(2 + len(self.ent[e]))
            chans["entity"] = (p.w_entity, topranks(es * (1 + 0.01 * (s_bm > 0)) + 1e-9 * s_bm))
        # temporal window
        if ti.window:
            lo, hi = ti.window[0].timestamp(), ti.window[1].timestamp()
            inwin = (self.ts >= lo) & (self.ts <= hi)
            if inwin[mask].any():
                chans["time"] = (p.w_time, topranks(inwin * (s_bm + 0.1 * (s_dense + 1)) + 1e-6 * inwin))
        # first-pass fusion for pseudo-relevance feedback (RM3-style)
        fused0 = self._fuse(chans, n)
        if p.w_prf > 0:
            top = np.argsort(-fused0)[: p.prf_docs]
            tf = Counter()
            for k in top:
                if fused0[k] <= 0:
                    continue
                c = Counter(terms(self.unit_txt[k]))
                tot = sum(c.values()) or 1
                for t, v in c.items():
                    tf[t] += v / tot
            for t in list(tf):
                if t in qset or t not in self.bm.vocab:
                    continue
            exp = {t: v for t, v in tf.most_common(p.prf_terms * 3) if t not in qset and t in self.bm.vocab}
            exp = dict(sorted(exp.items(), key=lambda kv: -kv[1] * self.bm.idf[self.bm.vocab[kv[0]]])[: p.prf_terms])
            if exp:
                mx = max(exp.values())
                wq = {t: 1.0 for t in qset}
                for t, v in exp.items():
                    wq[t] = wq.get(t, 0) + p.prf_mix * v / mx
                chans["prf"] = (p.w_prf, topranks(self.bm.score("", weights=wq)))
        fused = self._fuse(chans, n)
        cand = np.argsort(-fused)[: max(req.max_items * 4, 200)]
        cand = [int(k) for k in cand if fused[k] > 0 and mask[k]]
        # ---------------- feature rerank
        first_person = bool(re.search(r"\b(i|my|me|mine|i've|i'm|i'd)\b", q.lower()))
        asks_assistant = bool(re.search(r"\b(you|your)\b", q.lower())) and self.has_roles
        self._asks_assistant = asks_assistant
        final = {}
        top_fused = fused[cand[0]] if cand else 1.0
        qn = max(1, len(qset))
        for k in cand:
            sc = fused[k] / top_fused
            ctx_terms = self.unit_terms[k] | (self.unit_terms[self.prev[k]] if self.prev[k] >= 0 else set())
            cover = len(qset & ctx_terms) / qn
            sc *= 1 + p.f_cover * cover
            m = self.mem[self.ids[k]]
            if qents and m.actor.lower() in qents:
                sc *= 1 + p.f_actor
            if self.has_roles:
                if asks_assistant and m.role == "assistant":
                    sc *= 1 + p.f_role
                elif first_person and not asks_assistant and m.role == "user":
                    sc *= 1 + p.f_role
            final[k] = sc
        # order / current-state intents: among strong candidates prefer earliest/latest
        if ti.order and final:
            strong = [k for k, v in final.items() if v >= 0.5 * max(final.values())][:30]
            if len(strong) > 1:
                tsv = sorted(self.ts[k] for k in strong)
                lo, hi = tsv[0], tsv[-1]
                for k in strong:
                    pos = (self.ts[k] - lo) / (hi - lo + 1e-9)
                    final[k] *= 1 + p.f_order * (pos if ti.order == "latest" else 1 - pos)
        ranked = sorted(final, key=lambda k: -final[k])
        # diversification for aggregate questions: interleave threads
        if p.count_div and (ti.count or ti.span) and ranked:
            ranked = _interleave_threads(ranked[:60], self.thread_of) + ranked[60:]
        # ---------------- packing
        items = self._pack(ranked[: req.max_items], final, chans, req, qterms)
        cov = min(1.0, (final[ranked[0]] if ranked else 0) / (1 + p.f_cover + p.f_actor + p.f_role))
        return ContextPack(items, sum(i.tokens for i in items), req.budget_tokens, round(float(cov), 3),
                           self._snapshot(), [f"time:{ti.window[0].date()}..{ti.window[1].date()}"] if ti.window else [])

    def _fuse(self, chans, n):
        out = np.zeros(n)
        for name, (w, ranks) in chans.items():
            if w <= 0:
                continue
            for k, r in ranks.items():
                out[k] += w / (self.p.rrf_k + r + 1)
        return out

    def _pack(self, ranked, final, chans, req, qterms) -> list[ContextItem]:
        p = self.p
        items: list[ContextItem] = []
        used = 0
        seen_threads: set[str] = set()
        emitted: set[int] = set()
        for k in ranked:
            if k in emitted:
                continue
            m = self.mem[self.ids[k]]
            members = [k]
            # dialogue-structure expansion: a question needs its answer, a short reply needs its prompt
            pk, nk = self.prev[k], self.next[k]
            if pk >= 0 and pk not in emitted and self.mem[self.ids[pk]].actor != m.actor and (
                    len(m.content.split()) <= p.prompt_merge_words or
                    (p.expand_prev_q and self.mem[self.ids[pk]].content.rstrip().endswith("?"))):
                members = [pk, k]
            if p.expand_next and nk >= 0 and nk not in emitted and self.mem[self.ids[nk]].actor != m.actor and (
                    m.content.rstrip().endswith("?") or "?" in m.content[-60:]) and \
                    self.ntok[nk] <= p.long_unit_tokens and \
                    (self.mem[self.ids[nk]].role != "assistant" or self._asks_assistant):
                members.append(nk)
            lines, claimed = [], []
            for j in members:
                mj = self.mem[self.ids[j]]
                body, frac = self._body(j, qterms)
                if p.resolve_dates and mj.meta.get("time_refs"):
                    body += " (" + "; ".join(f"{a} = {b}" for a, b in mj.meta["time_refs"][:2]) + ")"
                lines.append(f"{mj.actor}: {body}" if mj.actor else body)
                if frac >= p.claim_fraction:
                    claimed.append(j)
            head = ""
            if p.headers and m.thread not in seen_threads:
                d = (m.event_time or m.observed_at)
                ds = d.strftime("%Y-%m-%d (%a)") if d else ""
                lab = m.thread.split(":")[-1] if m.thread else ""
                head = f"## {ds} · {lab}\n" if lab else f"## {ds}\n"
                seen_threads.add(m.thread)
            text = head + "\n".join(lines)
            if m.status == "superseded":
                text = "[superseded] " + text
            emitted.update(members)
            tok = count_tokens(text)
            used += tok
            why = {c: round(w / (p.rrf_k + r[k] + 1), 5) for c, (w, r) in chans.items() if k in r}
            mids = [self.ids[j] for j in members]
            items.append(ContextItem(memory_ids=mids, text=text, score=round(float(final[k]), 5), why=why, tokens=tok,
                                     citations=[s.uri for j in members for s in self.mem[self.ids[j]].sources] or mids,
                                     valid="superseded" if m.status == "superseded" else "current",
                                     turn_ids=[self.mem[self.ids[j]].meta.get("turn_id", self.ids[j]) for j in claimed]))
            if req.budget_tokens and used >= req.budget_tokens:
                break
        return items

    def _body(self, k: int, qterms: list[str]) -> tuple[str, float]:
        """Unit text, snippeted to the most query-relevant sentences if long. Returns (text, fraction kept)."""
        m = self.mem[self.ids[k]]
        if self.ntok[k] <= self.p.long_unit_tokens:
            return m.content, 1.0
        sents = sentences(m.content)
        if len(sents) <= self.p.snippet_sentences:
            return m.content, 1.0
        qs = set(qterms)
        scored = []
        for i, s in enumerate(sents):
            st = set(terms(s))
            scored.append((len(st & qs) + (0.5 if i == 0 else 0), i))
        keep = sorted(i for _, i in sorted(scored, reverse=True)[: self.p.snippet_sentences])
        out, last = [], -1
        for i in keep:
            if last >= 0 and i != last + 1:
                out.append("…")
            out.append(sents[i])
            last = i
        txt = " ".join(out)
        return txt, len(txt) / max(1, len(m.content))

    def _snapshot(self) -> str:
        return hashlib.sha1(("|".join(self.order[-50:]) + str(len(self.order))).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- helpers
def _rrf(rankings: list[np.ndarray], n: int, k: float = 60) -> np.ndarray:
    out = np.zeros(n)
    for order in rankings:
        for r, i in enumerate(order):
            out[i] += 1.0 / (k + r + 1)
    return out


def _interleave_threads(ranked: list[int], thread_of: np.ndarray) -> list[int]:
    buckets: dict[int, list[int]] = {}
    for k in ranked:
        buckets.setdefault(int(thread_of[k]), []).append(k)
    out = []
    while buckets:
        for t in list(buckets):
            out.append(buckets[t].pop(0))
            if not buckets[t]:
                del buckets[t]
    return out


_ENT = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b")


def _entities(text: str) -> list[str]:
    out = []
    for mt in _ENT.finditer(text):
        e = mt.group(1)
        if e.lower() in STOPWORDS or len(e) < 3:
            continue
        out.append(e)
    return list(dict.fromkeys(out))[:12]


# ---------------------------------------------------------------- benchmark adapter
class ORPSystem:
    """Adapter so the harness can run the reference engine like any baseline."""

    name = "orp"
    family = "protocol-reference"

    def __init__(self, params: Params | None = None, **kw):
        p = params or Params()
        for k, v in kw.items():
            if k.startswith("_"):
                continue
            setattr(p, k, v)
        self.params = p

    def build(self, corpus, enc):
        self.eng = Engine(enc.dense, self.params)
        mems = []
        for t in corpus.turns:
            mems.append(Memory(content=t.text, kind="event", actor=t.speaker, role=t.role, thread=t.session_id,
                               seq=t.idx, observed_at=t.ts, meta={"turn_id": t.id}))
        self.eng.remember(mems)
        self.eng._build()

    def retrieve(self, q, k=30):
        from bench.system import Item
        pack = self.eng.recall(RecallRequest(query=q.text, as_of=q.time, budget_tokens=None, max_items=k))
        return [Item(it.turn_ids, it.text, it.score) for it in pack.items]
