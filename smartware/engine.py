"""Smartware reference engine (v0.1.4).

A single-process implementation of the Smartware protocol (spec/Smartware-0.1.md):
bi-temporal memory records, explicit supersession, provenance-closed access control,
and a budget-aware multi-channel recall pipeline returning a ContextPack.

Admissibility (§5.1) is computed first, and everything after it is computed over the
admissible set only: lexical collection statistics (N, df, average length), window and
thread aggregates, neighbour features, entity hub damping, pseudo-relevance feedback and
dialogue expansion. An inadmissible memory therefore cannot change what or how an asker
recalls (no side channel). Threads never cross tenants.

Recall pipeline:
  0. Admissibility mask: tenant, ACL tokens, labels, status, validity at as_of, known_at, scope path, kinds.
  1. Query analysis: content terms, named entities, temporal intent, role intent, parts.
  2. Channels (each nominates its own top `depth` candidates): BM25(unit), window (unit + admissible
     neighbours), BM25 over 2-sentence segments max-pooled to units, dense(unit), dense(window),
     dense(segments), thread relevance, entity match, temporal window, RM3 pseudo-relevance feedback,
     optional query decomposition.
  3. Weighted reciprocal-rank fusion -> candidate pool.
  4. Ranking: multiplicative features (ranker="rrf") or a learned model over declared features
     (ranker="gbdt" | "linear").
  5. Thread interleaving for aggregate questions.
  6. Packing into the token budget per granularity, with once-per-thread headers and resolved dates.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import numpy as np

from . import timeparse
from ._text import STOPWORDS, count_tokens, sentences, stem, terms, words
from .index import BM25
from .model import SYSTEM, ContextItem, ContextPack, Memory, Principal, RecallRequest
from . import clock as _clock


class Embedder(Protocol):
    def embed(self, text: str) -> np.ndarray: ...

    def embed_many(self, texts: list[str]) -> np.ndarray: ...


@dataclass
class Params:
    # channel weights (weighted RRF)
    w_bm25: float = 1.0
    w_bm25_win: float = 0.6
    w_dense: float = 0.6
    w_dense_win: float = 0.4
    w_thread: float = 0.5
    w_entity: float = 0.4
    w_time: float = 0.5
    w_prf: float = 0.6
    w_sub: float = 0.0  # optional channel, off by default
    w_qexp: float = 0.0  # optional channel, off by default
    qexp_weight: float = 0.4
    qexp_min: float = 0.45
    w_bm25_seg: float = 0.6
    w_dense_seg: float = 0.4
    seg_sentences: int = 2
    rrf_k: float = 60.0
    depth: int = 200
    pool: int = 300
    # rrf-ranker features (multiplicative)
    f_cover: float = 0.6
    f_actor: float = 0.25
    f_role: float = 0.25
    f_order: float = 0.15
    prefer_boost: float = 0.25
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
    render: str = "clean"  # since v0.1.3 (blind QA, results/qa5test). v012: "## date · thread" headers | clean: every line carries its own date, no thread labels
    resolve_dates: bool = True
    count_div: bool = True
    # learned rankers
    ranker: str = "rrf"  # rrf | linear | gbdt
    lin_w: tuple = ()
    lin_b: float = 0.0
    lin_mu: tuple = ()
    lin_sd: tuple = ()
    gbdt: dict | None = None
    drop_features: tuple = ()  # feature names forced to 0 (ablations)
    admissible_stats: bool = True  # False only for experiments: global statistics (side-channel baseline)
    exact_dot: bool = False  # True: dense scores accumulate in float64 and round once, so a vector's score never
    # depends on its row position in the matrix (BLAS sgemv rounds rows differently by position). The product engine
    # always scores this way; the benchmark results were produced with False.


class Engine:
    FEATURES = ("rr_bm25", "rr_bm25_win", "rr_dense", "rr_dense_win", "rr_bm25_seg", "rr_dense_seg", "rr_thread",
                "rr_entity", "rr_time", "rr_prf", "fused", "bm25_norm", "bm25_seg_norm", "cos", "cos_seg", "cover",
                "cover_unit", "cover_next", "actor_match", "role_match", "in_window", "order_pos", "log_tokens",
                "is_question", "prev_is_question", "next_reply", "when_q", "count_q", "nb_bm25", "has_time",
                "has_number", "when_x_time", "count_x_number", "where_x_place", "ent_overlap", "rr_thread_x_cover",
                "rr_sub", "n_parts", "rr_qexp")

    def __init__(self, embedder: Embedder, params: Params | None = None):
        self.emb = embedder
        self.p = params or Params()
        self.mem: dict[str, Memory] = {}
        self.order: list[str] = []
        self._hash: dict[str, str] = {}
        self._dirty = True
        self._claims: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.audit: list[dict] = []
        self.collect = False
        self.last = None
        self.mutations = 0
        self.tmut: Counter = Counter()  # per-tenant mutation counters (snapshots never reveal other tenants)
        self.tcount: Counter = Counter()
        self._epoch = 0
        self._wseq = 0
        self._secret = os.urandom(16)
        self._view_sup: dict[str, str] = {}
        self._has_expiry = False

    # ------------------------------------------------------------ write
    def touch(self, tenant: str | None = None):
        """Mark the index stale. With a tenant, only that tenant's snapshot changes; without one (maintenance)
        every tenant's snapshot changes."""
        self.mutations += 1
        if tenant is None:
            self._epoch += 1
        else:
            self.tmut[tenant] += 1
        self._dirty = True

    def remember(self, items: Memory | list[Memory], principal: Principal = SYSTEM,
                 chain_only: bool = False, dedup_content: bool = True) -> list[str]:
        """Store memories. chain_only (import): register claim chains without adding supersession permissions,
        which the bundle carries; the caller resolves chains afterwards. dedup_content=False (a record with a
        client-supplied id): the id is the record's identity, so identical content elsewhere never absorbs it."""
        if isinstance(items, Memory):
            items = [items]
        ids, touched = [], set()
        for m in items:
            writer = m.meta.setdefault("_creator", principal.id)
            if m.derived_from:  # provenance-closed access (§6.3) and label inheritance, before de-duplication
                acl, labels = self._closure(m)
                if acl is not None:
                    m.readers = frozenset(acl) if m.readers is None else frozenset(set(m.readers) & acl)
                m.labels = sorted(set(m.labels) | labels)
            # de-duplication is scoped to tenant, writer and audience (M17): never resolves to another
            # principal's record, never crosses tenants
            ik = m.meta.get("_idempotency_key")  # engine-reserved; client meta cannot set "_" keys
            h = ("key", m.tenant, writer, ik) if ik else (m.dedup_key(writer) if dedup_content else None)
            if h is not None and h in self._hash and self._hash[h] in self.mem:
                ids.append(self._hash[h])
                continue
            if m.observed_at is None:
                m.observed_at = _clock.now()
            if m.valid_from is None:
                m.valid_from = m.event_time or m.observed_at
            m.meta.setdefault("_valid_to", m.valid_to.isoformat() if m.valid_to else None)
            self._wseq += 1
            m.meta.setdefault("_wseq", self._wseq)
            if self.p.resolve_dates and m.observed_at and m.kind in ("event", "document"):
                refs = timeparse.resolve_relative(m.content, m.event_time or m.observed_at)
                if refs:
                    m.meta["time_refs"] = refs
            m.meta.setdefault("entities", _entities(m.content))
            self.mem[m.id] = m
            self.order.append(m.id)
            self.tcount[m.tenant] += 1
            self._has_expiry |= m.expires_at is not None
            if h is not None:
                self._hash[h] = m.id
            if ik or h is None:
                self._hash.setdefault(m.dedup_key(writer), m.id)
            if m.claim is not None and m.kind not in ("event", "document"):
                if chain_only:
                    ch = self._claims[(m.tenant, m.claim.key())]
                    if m.id not in ch:
                        ch.append(m.id)
                else:
                    self._supersede(m, principal)
            self.audit.append({"op": "remember", "id": m.id, "by": principal.id, "at": _clock.now().isoformat()})
            ids.append(m.id)
            touched.add(m.tenant)
        for t in touched:
            self.touch(t)
        return ids

    SENSITIVE = ("secret", "pii", "health", "legal")

    def _closure(self, d: Memory) -> tuple[set | None, set]:
        """Intersection of the sources' readers (None = unrestricted) and the union of their sensitivity labels."""
        acl, labels = None, set()
        for s in d.derived_from:
            sm = self.mem.get(s)
            if sm is None:
                continue
            if sm.readers is not None:
                acl = set(sm.readers) if acl is None else acl & set(sm.readers)
            labels |= {l for l in sm.labels if l in self.SENSITIVE}
        return acl, labels

    # ---- supersession (§5.6)
    def _sup_order(self, i: str):
        m = self.mem[i]
        return (m.valid_from or datetime.min, m.observed_at or datetime.min, m.meta.get("_wseq", 0))

    @staticmethod
    def _covers(new: frozenset | None, old: frozenset | None) -> bool:
        if new is None:
            return True
        return old is not None and set(new) >= set(old)

    @staticmethod
    def in_chain(m: Memory) -> bool:
        """Claim records whose current version is decided by the claim-key rule (not explicit supersession)."""
        return (m.claim is not None and m.claim.cardinality == "one" and m.kind not in ("event", "document")
                and not m.meta.get("_explicit_superseded"))

    def _authored_valid_to(self, m: Memory):
        v = m.meta.get("_valid_to")
        return datetime.fromisoformat(v) if isinstance(v, str) else v

    def _supersede(self, m: Memory, principal: Principal = SYSTEM):
        """Register m in its claim chain. A newer claim may supersede an older one only if whoever wrote the later
        of the two records may write the record being superseded; such pairs are recorded on the older record
        (`_supersedable_by`). Recall applies them over the asker's admissible set (so as_of, known_at and ACLs all
        apply: an asker who can read both versions sees the newer one). The STORED status (what get/history show
        and what every reader inherits) changes only when, in addition, every reader of the older version can read
        the newer one, so no reader ever loses a fact without seeing its replacement."""
        key = (m.tenant, m.claim.key())
        chain = self._claims[key]
        if m.id not in chain:
            chain.append(m.id)
        if m.claim.cardinality != "one":
            return
        for oid in chain:
            o = self.mem.get(oid)
            if oid == m.id or o is None or not self.in_chain(o):
                continue
            old, new = (o, m) if self._sup_order(oid) < self._sup_order(m.id) else (m, o)
            if old is o and not self.can_write(o, principal):
                continue
            lst = old.meta.setdefault("_supersedable_by", [])
            if new.id not in lst:
                lst.append(new.id)
        self.resolve_chain(key)

    def resolve_chain(self, key):
        chain = sorted((i for i in self._claims.get(key, []) if i in self.mem), key=self._sup_order)
        live = [i for i in chain if self.mem[i].status in ("active", "superseded") and self.in_chain(self.mem[i])]
        for r, i in enumerate(live):
            x = self.mem[i]
            ok = set(x.meta.get("_supersedable_by", ()))
            obj = x.claim.object.strip().lower()
            succ = next((j for j in live[r + 1:] if j in ok and self.mem[j].claim.object.strip().lower() != obj
                         and self._covers(self.mem[j].readers, x.readers)), None)
            if succ is not None:
                if x.status != "superseded" or x.superseded_by != succ:
                    x.status, x.superseded_by, x.valid_to = "superseded", succ, self.mem[succ].valid_from
                    x.version += 1
            elif x.status != "active" or x.superseded_by is not None:
                x.status, x.superseded_by, x.valid_to = "active", None, self._authored_valid_to(x)
                x.version += 1
        for i in live:  # supersedes links mirror superseded_by inside the chain
            y = self.mem[i]
            y.supersedes = [j for j in y.supersedes if j not in chain or self.mem[j].superseded_by == i]
            for j in live:
                if self.mem[j].superseded_by == i and j not in y.supersedes:
                    y.supersedes.append(j)

    def reclose(self, mem_id: str) -> list[str]:
        """Re-apply provenance-closed ACL and sensitivity labels to everything derived (transitively) from mem_id."""
        changed, frontier, seen = [], [mem_id], set()
        while frontier:
            cur = frontier.pop()
            for d in self.mem.values():
                if cur not in d.derived_from or d.id in seen:
                    continue
                seen.add(d.id)
                acl, labels = self._closure(d)
                new = d.readers if acl is None else frozenset(acl if d.readers is None else set(d.readers) & acl)
                new_labels = sorted(set(d.labels) | labels)
                if new != d.readers or new_labels != sorted(d.labels):
                    d.readers, d.labels = new, new_labels
                    d.version += 1
                    changed.append(d.id)
                frontier.append(d.id)
        m = self.mem.get(mem_id)
        self.touch(m.tenant if m else None)
        return changed

    def forget(self, mem_id: str, mode: str = "retract", cascade: bool = True, principal: Principal = SYSTEM,
               reason: str = "") -> list[str]:
        """retract: hidden from recall, kept for audit. redact: content and derived metadata erased.
        purge: record removed. cascade: derived memories are retracted/redacted too (no laundering)."""
        hit = self.cascade_set([mem_id], cascade)
        tenant = self.mem[mem_id].tenant if mem_id in self.mem else None
        chain_keys = {(self.mem[i].tenant, self.mem[i].claim.key()) for i in hit
                      if i in self.mem and self.mem[i].claim is not None}  # before a purge removes them
        for i in hit:
            m = self.mem.get(i)
            if not m:
                continue
            if mode == "purge":
                del self.mem[i]
                self.order.remove(i)
                self.tcount[m.tenant] -= 1
                for k in [k for k, v in self._hash.items() if v == i]:
                    del self._hash[k]
                if mode == "purge":
                    self._forget_cached_text(m.content)
            else:
                m.status = "retracted"
                m.version += 1
                if mode == "redact":
                    self._forget_cached_text(m.content)
                    m.content = "[redacted]"
                    m.meta.pop("time_refs", None)
                    m.meta["entities"] = []  # what the engine derives from "[redacted]", so an export round-trips
                    if m.claim is not None:
                        m.claim.subject = m.claim.predicate = m.claim.object = "[redacted]"
                    for s in m.sources:
                        s.span = s.hash = ""
            self.audit.append({"op": f"forget:{mode}", "id": i, "by": principal.id, "reason": reason,
                               "at": _clock.now().isoformat()})
        for key in chain_keys:
            self.resolve_chain(key)  # a retracted superseder restores the version it replaced
        self.touch(tenant)
        return hit

    def cascade_set(self, ids: list[str], cascade: bool = True) -> list[str]:
        """Memories affected by forgetting `ids`: with cascade, everything derived (transitively) from them; without,
        derived memories left with no remaining source (M28: a derived memory never keeps facts its sources no longer
        support). Only records with a source in the affected set are considered, and never across tenants (M03)."""
        hit = list(dict.fromkeys(ids))
        tenants = {self.mem[i].tenant for i in hit if i in self.mem}
        changed = True
        while changed:
            changed = False
            hs = set(hit)
            for m in self.mem.values():
                if not m.derived_from or m.id in hs or m.tenant not in tenants:
                    continue
                if not any(s in hs for s in m.derived_from):
                    continue
                if cascade or all(s in hs or s not in self.mem or self.mem[s].status == "retracted"
                                  for s in m.derived_from):
                    hit.append(m.id)
                    changed = True
        return hit

    def _forget_cached_text(self, content: str):
        cache = getattr(self.emb, "_cache", None)
        if not isinstance(cache, dict) or not content:
            return
        for key in list(cache):
            body = key.split(": ", 1)[-1]
            if content in key or (len(body) > 12 and body in content):
                del cache[key]

    def history(self, subject: str, predicate: str, tenant: str = "default") -> list[Memory]:
        key = (tenant, f"{subject.lower().strip()}|{predicate.lower().strip()}")
        return [self.mem[i] for i in self._claims.get(key, []) if i in self.mem and self.mem[i].claim is not None
                and self.mem[i].claim.key() == key[1]]

    def unchain(self, mem_id: str):
        """Remove a record from every claim chain (an upsert that changed or dropped its claim) and re-resolve them."""
        keys = [k for k, ids in self._claims.items() if mem_id in ids]
        for k in keys:
            self._claims[k] = [i for i in self._claims[k] if i != mem_id]
            for i in self._claims[k]:
                o = self.mem.get(i)
                if o is not None and mem_id in o.meta.get("_supersedable_by", []):
                    o.meta["_supersedable_by"] = [x for x in o.meta["_supersedable_by"] if x != mem_id]
        return keys

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
        self.prev = np.full(n, -1)
        self.next = np.full(n, -1)
        by_thread = defaultdict(list)
        for k, m in enumerate(ms):
            by_thread[(m.tenant, m.thread)].append(k)  # threads never cross tenants
        for key, ks in by_thread.items():
            ks.sort(key=lambda k: (ms[k].seq, ms[k].observed_at or datetime.min))
            if not key[1]:
                continue  # unthreaded memories have no neighbours
            for a, b in zip(ks, ks[1:]):
                self.next[a] = b
                self.prev[b] = a
        self.threads = list(by_thread)
        self.thread_units = [np.array(by_thread[t]) for t in self.threads]
        self.thread_named = [bool(t[1]) for t in self.threads]
        self.thread_of = np.zeros(n, dtype=int)
        for ti, ks in enumerate(self.thread_units):
            self.thread_of[ks] = ti
        seg_txt, seg_unit = [], []
        for k, m in enumerate(ms):
            ss = sentences(m.content)
            w = self.p.seg_sentences
            if len(ss) <= w:
                seg_txt.append(unit_txt[k])
                seg_unit.append(k)
                continue
            for i in range(0, len(ss) - w + 1):
                seg_txt.append(f"{m.actor}: " + " ".join(ss[i:i + w]))
                seg_unit.append(k)
        self.seg_unit = np.array(seg_unit, dtype=int)
        self.bm_seg = BM25(seg_txt)
        self.ES = self.emb.embed_many(seg_txt) if seg_txt else np.zeros((0, 1))
        self.bm = BM25(unit_txt)
        self.E = self.emb.embed_many([f"{m.actor}: {m.content}" for m in ms]) if n else np.zeros((0, 1))
        self.d_kp = np.zeros(n)
        self.d_kn = np.zeros(n)
        self.d_pn = np.zeros(n)
        for k in range(n):
            p_, n_ = self.prev[k], self.next[k]
            if p_ >= 0:
                self.d_kp[k] = float(self.E[k] @ self.E[p_])
            if n_ >= 0:
                self.d_kn[k] = float(self.E[k] @ self.E[n_])
            if p_ >= 0 and n_ >= 0:
                self.d_pn[k] = float(self.E[p_] @ self.E[n_])
        self.ent = defaultdict(set)
        for k, m in enumerate(ms):
            for e in m.meta.get("entities", []):
                self.ent[e.lower()].add(k)
            if m.actor:
                self.ent[m.actor.lower()].add(k)
        self.ts = np.array([_clock.epoch(((m.event_time or m.observed_at) or datetime(1970, 1, 1))) for m in ms])
        self.ntok = np.array([count_tokens(t) for t in unit_txt])
        self.is_assistant = np.array([m.role == "assistant" for m in ms], dtype=bool)
        self.has_roles = bool(self.is_assistant.any())
        self.unit_terms = [set(terms(t)) for t in unit_txt]
        _time_rx = re.compile(r"\b(yesterday|today|tomorrow|tonight|last|next|ago|week|weekend|month|year|"
                              r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|january|february|march|"
                              r"april|may|june|july|august|september|october|november|december|\d{4}|"
                              r"\d{1,2}(st|nd|rd|th))\b", re.I)
        self.has_time = np.array([bool(m.meta.get("time_refs")) or bool(_time_rx.search(m.content)) for m in ms])
        self.has_num = np.array([bool(re.search(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
                                                r"twice|once|several|dozen)\b", m.content, re.I)) for m in ms])
        self.has_place = np.array([bool(m.meta.get("entities")) for m in ms])
        self.is_q = np.array([m.content.rstrip().endswith("?") for m in ms])
        self.actor_l = [m.actor.lower() for m in ms]
        self._dirty = False

    # ------------------------------------------------------------ access
    def readable(self, m: Memory, pr: Principal, labels: tuple = ()) -> bool:
        if m.tenant != pr.tenant:
            return False
        star = "*" in pr.groups
        if not star and m.readers is not None and not (m.readers & pr.tokens()):
            return False
        if "secret" in m.labels and not ("secret" in labels and (star or "label:secret" in pr.tokens())):
            return False
        return True

    def can_write(self, m: Memory, pr: Principal) -> bool:
        if m.tenant != pr.tenant:
            return False
        if "*" in pr.groups:
            return True
        if m.writers is None:
            return m.meta.get("_creator") == pr.id
        return bool(m.writers & pr.tokens())

    @staticmethod
    def valid_at(m: Memory, t: datetime) -> bool:
        if m.valid_from and m.valid_from > t:
            return False
        if m.valid_to and t >= m.valid_to:
            return False
        return True

    def _mask(self, req: RecallRequest) -> np.ndarray:
        """Admissible set (§5.1, §5.2). Everything here depends only on the memory itself and the request, except
        the claim view at the end, which is computed over admissible memories only."""
        as_of = req.as_of or _clock.now()
        now = _clock.now()  # retention is real time, not the as_of view (§7)
        labels = tuple(req.filters.get("labels", ()))
        drafts = bool(req.filters.get("drafts"))
        sc = req.scope.rstrip("/")
        ok = np.zeros(len(self.ids), dtype=bool)
        chain_ks = []
        for k, i in enumerate(self.ids):
            m = self.mem[i]
            if not self.readable(m, req.principal, labels):
                continue
            if m.status in ("retracted", "expired"):
                continue
            if m.status == "draft" and not (drafts and self.can_write(m, req.principal)):
                continue
            if req.known_at and m.observed_at and m.observed_at > req.known_at:
                continue
            if m.expires_at and m.expires_at <= now:
                continue
            chain = self.in_chain(m)
            if m.valid_from and m.valid_from > as_of:
                continue  # nothing from after as_of, with or without history
            vt = self._authored_valid_to(m) if chain else m.valid_to
            if vt and as_of >= vt and not req.include_history:
                continue
            if sc and not (m.scope == sc or m.scope.startswith(sc + "/")):
                continue
            if req.kinds and m.kind not in req.kinds:
                continue
            ok[k] = True
            if chain:
                chain_ks.append(k)
        # claim view: an admissible version is current unless a newer admissible version that may supersede it
        # (recorded at write time, §5.6) states a different object
        self._view_sup = {}
        by = defaultdict(list)
        for k in chain_ks:
            m = self.mem[self.ids[k]]
            by[(m.tenant, m.claim.key())].append(self.ids[k])
        for ids in by.values():
            if len(ids) < 2:
                continue
            ids.sort(key=self._sup_order)
            for r, i in enumerate(ids):
                x = self.mem[i]
                okset = set(x.meta.get("_supersedable_by", ()))
                obj = x.claim.object.strip().lower()
                succ = next((j for j in ids[r + 1:] if j in okset and
                             self.mem[j].claim.object.strip().lower() != obj), None)
                if succ is not None:
                    self._view_sup[i] = succ
                    if not req.include_history:
                        ok[self.pos[i]] = False
        return ok

    def admissible(self, req: RecallRequest) -> list[str]:
        """Ids admissible for the request (the same set every recall channel draws from)."""
        if self._dirty:
            self._build()
        if not self.ids:
            return []
        mask = self._mask(req)
        return [self.ids[k] for k in np.flatnonzero(mask)]

    # ------------------------------------------------------------ recall
    def recall(self, req: RecallRequest) -> ContextPack:
        if self._dirty:
            self._build()
        n = len(self.ids)
        if self.tcount.get(req.principal.tenant, 0) <= 0:
            return ContextPack([], 0, req.budget_tokens, 0.0, self._snapshot(req.principal.tenant))
        p = self.p
        now = req.as_of or _clock.now()
        mask = self._mask(req)
        if not mask.any():
            return ContextPack([], 0, req.budget_tokens, 0.0, self._snapshot(req.principal.tenant),
                               ["no admissible memories"])
        notes: list[str] = []
        q = req.query
        self._qtext = q
        ti = timeparse.parse_query(q, now)
        qset = set(terms(q))
        try:
            qvec = np.asarray(self.emb.embed(q), dtype=np.float32)
        except Exception:  # spec §5.8: report, never fail silently
            qvec = np.zeros(self.E.shape[1] if self.E.ndim == 2 else 1, np.float32)
        if not np.any(qvec):
            notes.append("degraded:dense_unavailable")
        NEG = -1e9
        apm, anm = self._admissible_neighbours(mask)
        if p.admissible_stats:
            pm, nm = apm, anm
        else:  # experiment only: neighbour features see inadmissible units (as many systems do)
            pm, nm = self.prev.copy(), self.next.copy()
        self._pm, self._nm, self._apm, self._anm = pm, nm, apm, anm
        smask = mask if p.admissible_stats else None

        adm = np.flatnonzero(mask)

        def topranks(score: np.ndarray, depth: int = p.depth) -> dict[int, int]:
            # deterministic: order admissible units by (-score, position); ties never depend on other units
            sc = score[adm]
            order = adm[np.lexsort((adm, -sc))][:depth]
            if sc.max() > 0:
                order = order[score[order] > 0]
            return {int(i): r for r, i in enumerate(order)}

        def nb(arr: np.ndarray, ix: np.ndarray) -> np.ndarray:
            return np.where(ix >= 0, arr[np.maximum(ix, 0)], 0.0)

        chans: dict[str, tuple[float, dict[int, int]]] = {}
        s_bm_all = self.bm.score(q, mask=smask, exact=True)
        s_bm = s_bm_all if smask is None else np.where(mask, s_bm_all, 0.0)
        chans["bm25"] = (p.w_bm25, topranks(s_bm))
        chans["bm25_win"] = (p.w_bm25_win, topranks(s_bm + 0.5 * (nb(s_bm, pm) + nb(s_bm, nm))))
        s_dense = self._dot(self.E, qvec)
        chans["dense"] = (p.w_dense, topranks(s_dense + 1.0))
        hp, hn = (pm >= 0).astype(float), (nm >= 0).astype(float)
        Ep, En = self.E[np.maximum(pm, 0)], self.E[np.maximum(nm, 0)]
        d_kp = (self.E * Ep).sum(1) * hp
        d_kn = (self.E * En).sum(1) * hn
        d_pn = (Ep * En).sum(1) * hp * hn
        wnorm = np.sqrt(np.maximum(1e-9, 1 + 0.25 * hp + 0.25 * hn + d_kp + d_kn + 0.5 * d_pn))
        s_win = (s_dense + 0.5 * nb(s_dense, pm) + 0.5 * nb(s_dense, nm)) / wnorm
        chans["dense_win"] = (p.w_dense_win, topranks(s_win + 1.0))
        segmask = mask[self.seg_unit]
        ub = np.zeros(n)
        np.maximum.at(ub, self.seg_unit, np.where(segmask, self.bm_seg.score(
            q, mask=segmask if p.admissible_stats else None, exact=True), 0.0))
        ud = np.full(n, -1.0)
        np.maximum.at(ud, self.seg_unit, self._dot(self.ES, qvec))
        chans["bm25_seg"] = (p.w_bm25_seg, topranks(ub))
        chans["dense_seg"] = (p.w_dense_seg, topranks(ud + 1.0))
        th_b = np.zeros(len(self.threads))
        th_d = np.full(len(self.threads), -1.0)
        for t, ks in enumerate(self.thread_units):
            ks = ks[mask[ks]] if p.admissible_stats else ks
            if len(ks):
                th_b[t] = float(np.sort(s_bm[ks])[-3:].sum())
                th_d[t] = float(s_dense[ks].mean())
        live = np.array([bool(mask[ks].any()) if p.admissible_stats else True for ks in self.thread_units])
        lt = np.flatnonzero(live)
        # ties between threads break on each thread's first ADMISSIBLE position (M68), never on hidden units
        first = np.array([int(ks[mask[ks]].min()) if mask[ks].any() else n for ks in self.thread_units])
        th_rank = np.zeros(len(self.threads))
        th_rank[lt] = _rrf([np.lexsort((first[lt], -th_b[lt])), np.lexsort((first[lt], -th_d[lt]))], len(lt), k=10)
        chans["thread"] = (p.w_thread, topranks(th_rank[self.thread_of] + 1e-6))
        qents = set()
        for w in re.findall(r"\b[A-Z][a-zA-Z'\-]+\b", q):
            wl = w.lower().removesuffix("'s")
            if wl in self.ent and wl not in STOPWORDS and any(mask[k] for k in self.ent[wl]):
                qents.add(wl)
        if qents:
            es = np.zeros(n)
            for e in qents:
                ks = [k for k in self.ent[e] if mask[k]]
                for k in ks:
                    es[k] += 1.0 / math.log(2 + len(ks))
            if es[mask].max() > 0:
                chans["entity"] = (p.w_entity, topranks(es * (1 + 0.01 * (s_bm > 0)) + 1e-9 * s_bm))
        if ti.window:
            lo, hi = _clock.epoch(ti.window[0]), _clock.epoch(ti.window[1])
            inwin = (self.ts >= lo) & (self.ts <= hi)
            if (inwin & mask).any():
                chans["time"] = (p.w_time, topranks(inwin * (s_bm + 0.1 * (s_dense + 1)) + 1e-6 * inwin))
        parts = _split_query(q)
        self._n_parts = len(parts)
        if len(parts) >= 2 and p.w_sub > 0:
            sub = np.zeros(n)
            for pt in parts:
                for k, r in topranks(self.bm.score(pt, mask=mask, exact=True), 60).items():
                    sub[k] = max(sub[k], 1.0 / (10 + r))
            chans["sub"] = (p.w_sub, topranks(sub))
        fused0 = self._fuse(chans, n)
        if p.w_prf > 0:
            tf = Counter()
            for k in adm[np.lexsort((adm, -fused0[adm]))][: p.prf_docs]:
                if fused0[k] <= 0 or not mask[k]:
                    continue
                c = Counter(terms(self.unit_txt[k]))
                tot = sum(c.values()) or 1
                for t, v in c.items():
                    tf[t] += v / tot
            nadm = max(1, int(mask.sum()))
            exp = {t: v for t, v in tf.most_common(p.prf_terms * 3) if t not in qset and t in self.bm.vocab}

            def _idf(t):
                d = self.bm.df(t, mask)
                return math.log(1 + (nadm - d + 0.5) / (d + 0.5))
            exp = dict(sorted(exp.items(), key=lambda kv: -kv[1] * _idf(kv[0]))[: p.prf_terms])
            if exp:
                mx = max(exp.values())
                wq = {t: 1.0 for t in qset}
                for t, v in exp.items():
                    wq[t] = wq.get(t, 0) + p.prf_mix * v / mx
                chans["prf"] = (p.w_prf, topranks(self.bm.score("", weights=wq, mask=mask, exact=True)))
        fused = self._fuse(chans, n)
        cand = adm[np.lexsort((adm, -fused[adm]))][: max(req.max_items * 4, p.pool)]
        cand = [int(k) for k in cand if fused[k] > 0]
        first_person = bool(re.search(r"\b(i|my|me|mine|i've|i'm|i'd)\b", q.lower()))
        self._has_roles = bool(self.is_assistant[mask].any())
        asks_assistant = bool(re.search(r"\b(you|your)\b", q.lower())) and self._has_roles
        self._asks_assistant = asks_assistant
        final: dict[int, float] = {}
        self._mult: dict[int, dict] = {}
        top_fused = fused[cand[0]] if cand else 1.0
        qn = max(1, len(qset))
        for k in cand:
            sc = fused[k] / top_fused
            mul = {}
            ctx = self.unit_terms[k] | (self.unit_terms[pm[k]] if pm[k] >= 0 else set())
            mul["coverage"] = 1 + p.f_cover * len(qset & ctx) / qn
            if qents and self.actor_l[k] in qents:
                mul["actor"] = 1 + p.f_actor
            if self._has_roles:
                role = self.mem[self.ids[k]].role
                if (asks_assistant and role == "assistant") or (first_person and not asks_assistant and
                                                                role == "user"):
                    mul["role"] = 1 + p.f_role
            for v in mul.values():
                sc *= v
            final[k] = sc
            self._mult[k] = mul
        feats = None
        if p.ranker in ("linear", "gbdt") or self.collect:
            feats = self._featurize(cand, chans, fused, top_fused, qset, qents, ti, first_person, asks_assistant,
                                    s_bm, s_dense, ub, ud)
            if self.collect:
                self.last = (cand, feats)
            if p.ranker == "linear" and p.lin_w and len(cand):
                z = ((feats - np.array(p.lin_mu)) / np.array(p.lin_sd)) @ np.array(p.lin_w) + p.lin_b
                final = {k: float(1 / (1 + math.exp(-z[i]))) for i, k in enumerate(cand)}
            elif p.ranker == "gbdt" and p.gbdt and len(cand):
                z = _gbdt_predict(p.gbdt, feats)
                final = {k: float(1 / (1 + math.exp(-z[i]))) for i, k in enumerate(cand)}
        if ti.order and final and p.ranker == "rrf":
            strong = [k for k, v in final.items() if v >= 0.5 * max(final.values())][:30]
            if len(strong) > 1:
                tsv = sorted(self.ts[k] for k in strong)
                lo, hi = tsv[0], tsv[-1]
                for k in strong:
                    pos = (self.ts[k] - lo) / (hi - lo + 1e-9)
                    f_ = 1 + p.f_order * (pos if ti.order == "latest" else 1 - pos)
                    final[k] *= f_
                    self._mult.setdefault(k, {})["order"] = f_
        if req.filters.get("prefer"):
            pref = [s.rstrip("/") for s in req.filters["prefer"]]
            for k in final:
                sc_ = self.mem[self.ids[k]].scope
                if any(sc_ == s or sc_.startswith(s + "/") for s in pref):
                    final[k] *= 1 + p.prefer_boost
                    self._mult.setdefault(k, {})["prefer"] = 1 + p.prefer_boost
        ranked = sorted(final, key=lambda k: -final[k])
        if p.count_div and (ti.count or ti.span) and ranked:
            ranked = _interleave_threads(ranked[:60], self.thread_of) + ranked[60:]
        fidx = {k: i for i, k in enumerate(cand)}
        items = self._pack(ranked, final, chans, req, list(qset), mask, feats, fidx, now)
        if p.ranker in ("linear", "gbdt"):
            cov = final[ranked[0]] if ranked else 0.0
        else:
            cov = min(1.0, (final[ranked[0]] if ranked else 0) / (1 + p.f_cover + p.f_actor + p.f_role))
        if ti.window:
            notes.append(f"time:{ti.window[0].date()}..{ti.window[1].date()}")
        return ContextPack(items, sum(i.tokens for i in items), req.budget_tokens, round(float(cov), 3),
                           self._snapshot(req.principal.tenant), notes)

    def _dot(self, M: np.ndarray, v: np.ndarray) -> np.ndarray:
        if not self.p.exact_dot:
            return M @ v
        return (M.astype(np.float64) @ v.astype(np.float64)).astype(np.float32)

    def _admissible_neighbours(self, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Previous/next admissible unit in thread order (skipping inadmissible ones, so gaps are invisible)."""
        n = len(mask)
        pm, nm = np.full(n, -1), np.full(n, -1)
        for ks, named in zip(self.thread_units, self.thread_named):
            if not named:
                continue
            ka = ks[mask[ks]]
            if len(ka) > 1:
                pm[ka[1:]] = ka[:-1]
                nm[ka[:-1]] = ka[1:]
        return pm, nm

    def _featurize(self, cand, chans, fused, top_fused, qset, qents, ti, first_person, asks_assistant, s_bm, s_dense,
                   ub, ud):
        X = np.zeros((len(cand), len(self.FEATURES)), np.float32)
        if not cand:
            return X
        pm, nm = self._pm, self._nm
        names = ["bm25", "bm25_win", "dense", "dense_win", "bm25_seg", "dense_seg", "thread", "entity", "time", "prf"]
        bmx = max(float(s_bm[cand].max()), 1e-6)
        ubx = max(float(ub[cand].max()), 1e-6)
        qn = max(1, len(qset))
        win = (_clock.epoch(ti.window[0]), _clock.epoch(ti.window[1])) if ti.window else None
        tsv = self.ts[cand]
        lo, hi = float(tsv.min()), float(tsv.max())
        where_q = bool(re.search(r"\bwhere\b|\bwhich (city|country|place|state|town)\b", self._qtext.lower()))
        qents_all = {w.lower().removesuffix("'s") for w in re.findall(r"\b[A-Z][a-zA-Z'\-]+\b", self._qtext)}
        for i, k in enumerate(cand):
            f = X[i]
            for j, c in enumerate(names):
                r = chans.get(c, (0, {}))[1].get(k)
                f[j] = 0.0 if r is None else 1.0 / (1 + r)
            f[10] = fused[k] / top_fused
            f[11] = s_bm[k] / bmx
            f[12] = ub[k] / ubx
            f[13] = s_dense[k]
            f[14] = ud[k]
            pk, nk = pm[k], nm[k]
            ut = self.unit_terms[k]
            pt = self.unit_terms[pk] if pk >= 0 else set()
            nt = self.unit_terms[nk] if nk >= 0 else set()
            f[15] = len(qset & (ut | pt)) / qn
            f[16] = len(qset & ut) / qn
            f[17] = len(qset & nt) / qn
            m = self.mem[self.ids[k]]
            f[18] = float(bool(qents) and self.actor_l[k] in qents)
            if self._has_roles:
                f[19] = float((asks_assistant and m.role == "assistant") or
                              (first_person and not asks_assistant and m.role == "user"))
            if win:
                f[20] = float(win[0] <= self.ts[k] <= win[1])
            if ti.order:
                pos = (self.ts[k] - lo) / (hi - lo + 1e-9)
                f[21] = pos if ti.order == "latest" else 1 - pos
            f[22] = math.log1p(self.ntok[k])
            f[23] = float(self.is_q[k])
            f[24] = float(pk >= 0 and self.is_q[pk])
            f[25] = float(nk >= 0 and self.actor_l[nk] != self.actor_l[k])
            f[26] = float(ti.when)
            f[27] = float(ti.count or ti.span)
            f[28] = max(s_bm[pk] if pk >= 0 else 0.0, s_bm[nk] if nk >= 0 else 0.0) / bmx
            f[29] = float(self.has_time[k])
            f[30] = float(self.has_num[k])
            f[31] = f[26] * f[29]
            f[32] = f[27] * f[30]
            f[33] = float(where_q) * float(self.has_place[k])
            ents = {e.lower() for e in m.meta.get("entities", [])}
            f[34] = len(ents & qents_all) / max(1, len(qents_all))
            f[35] = f[6] * f[15]
            r = chans.get("sub", (0, {}))[1].get(k)
            f[36] = 0.0 if r is None else 1.0 / (1 + r)
            f[37] = float(self._n_parts)
            r = chans.get("qexp", (0, {}))[1].get(k)
            f[38] = 0.0 if r is None else 1.0 / (1 + r)
        for name in self.p.drop_features:
            X[:, self.FEATURES.index(name)] = 0.0
        return X

    def _fuse(self, chans, n):
        out = np.zeros(n)
        for name, (w, ranks) in chans.items():
            if w <= 0:
                continue
            for k, r in ranks.items():
                out[k] += w / (self.p.rrf_k + r + 1)
        return out

    def _pack(self, ranked, final, chans, req, qterms, mask, feats, fidx, as_of) -> list[ContextItem]:
        p = self.p
        pm, nm = self._apm, self._anm  # nearest admissible neighbours only
        gran = req.filters.get("granularity", "auto")
        nbh = int(req.filters.get("neighbours", 0) or 0)
        quotas = req.filters.get("quotas") or {}
        kcount: Counter = Counter()
        items: list[ContextItem] = []
        used = 0
        seen_threads: set = set()
        emitted: set[int] = set()
        for k in ranked:
            if len(items) >= req.max_items:
                break
            if k in emitted:
                continue
            m = self.mem[self.ids[k]]
            if quotas and m.kind in quotas and kcount[m.kind] >= int(quotas[m.kind]):
                continue
            if gran == "thread" and self.thread_named[self.thread_of[k]]:
                members = [int(x) for x in self.thread_units[self.thread_of[k]] if mask[x] and x not in emitted]
            elif gran == "thread":
                members = [k]  # an unthreaded memory is its own thread
            else:
                members = [k]
                pk, nk = pm[k], nm[k]
                if gran != "unit":
                    expand_prev = gran == "span" or p.expand_prev_q
                    expand_next = gran == "span" or p.expand_next
                    if pk >= 0 and pk not in emitted and self.actor_l[pk] != self.actor_l[k] and (
                            len(m.content.split()) <= p.prompt_merge_words or (expand_prev and self.is_q[pk])):
                        members = [int(pk), k]
                    if expand_next and nk >= 0 and nk not in emitted and self.actor_l[nk] != self.actor_l[k] and (
                            m.content.rstrip().endswith("?") or "?" in m.content[-60:]) and \
                            self.ntok[nk] <= p.long_unit_tokens and \
                            (self.mem[self.ids[nk]].role != "assistant" or self._asks_assistant):
                        members.append(int(nk))
                for _ in range(nbh):
                    a, b = pm[members[0]], nm[members[-1]]
                    if a >= 0 and a not in emitted:
                        members.insert(0, int(a))
                    if b >= 0 and b not in emitted:
                        members.append(int(b))
            lines, claimed = [], []
            for j in members:
                mj = self.mem[self.ids[j]]
                body, frac = (mj.content, 1.0) if gran == "thread" else self._body(j, qterms)
                if p.resolve_dates and mj.meta.get("time_refs"):
                    body += " (" + "; ".join(f"{a} = {b}" for a, b in mj.meta["time_refs"][:2]) + ")"
                line = f"{mj.actor}: {body}" if mj.actor else body
                if p.render == "clean":
                    dj = mj.event_time or mj.observed_at
                    line = (f"[{dj.day} {dj.strftime('%b %Y')}] " if dj else "") + line
                lines.append(line)
                if frac >= p.claim_fraction:
                    claimed.append(j)
            head = ""
            tkey = (m.tenant, m.thread)
            if p.headers and p.render != "clean" and tkey not in seen_threads:
                d = m.event_time or m.observed_at
                ds = d.strftime("%Y-%m-%d (%a)") if d else ""
                lab = m.meta.get("thread_label") or (m.thread.split(":")[-1] if m.thread else "")
                head = f"## {ds} · {lab}\n" if lab else f"## {ds}\n"
            if self.in_chain(m):
                label = "superseded" if m.id in self._view_sup else "current"
            else:
                label = "current" if self.valid_at(m, as_of) else ("superseded" if m.status == "superseded"
                                                                   else "historical")
            text = head + "\n".join(lines)
            if label != "current":
                text = f"[{label}] " + text
            tok = count_tokens(text)
            if req.budget_tokens and used + tok > req.budget_tokens:
                continue  # greedy: skip what does not fit, keep filling (never exceed the budget, M43)
            if head:
                seen_threads.add(tkey)
            emitted.update(members)
            kcount[m.kind] += 1
            used += tok
            why = {c: round(w / (p.rrf_k + r[k] + 1), 5) for c, (w, r) in chans.items() if k in r and w > 0}
            if feats is not None and k in fidx and p.ranker != "rrf":
                fv = feats[fidx[k]]
                why["features"] = {nme: round(float(v), 4) for nme, v in zip(self.FEATURES, fv)
                                   if v and nme not in p.drop_features}
                for kk, vv in self._mult.get(k, {}).items():  # post-ranker multipliers are features too (M41)
                    if kk in ("prefer", "order"):
                        why["features"][kk] = round(float(vv), 4)
            else:
                why["features"] = {kk: round(float(vv), 4) for kk, vv in self._mult.get(k, {}).items()}
            why["ranker"] = p.ranker
            mids = [self.ids[j] for j in members]
            items.append(ContextItem(memory_ids=mids, text=text, score=round(float(final.get(k, 0)), 5), why=why,
                                     tokens=tok, citations=[s.uri for j in members for s in
                                                            self.mem[self.ids[j]].sources] or mids,
                                     valid=label,
                                     turn_ids=[t for j in claimed for t in (self.mem[self.ids[j]].meta.get("turn_ids") or
                                                                           [self.mem[self.ids[j]].meta.get("turn_id",
                                                                                                           self.ids[j])])]))
        return items

    def _body(self, k: int, qterms: list[str]) -> tuple[str, float]:
        m = self.mem[self.ids[k]]
        if self.ntok[k] <= self.p.long_unit_tokens:
            return m.content, 1.0
        sents = sentences(m.content)
        if len(sents) <= self.p.snippet_sentences:
            return m.content, 1.0
        qs = set(qterms)
        scored = [(len(set(terms(s)) & qs) + (0.5 if i == 0 else 0), i) for i, s in enumerate(sents)]
        keep = sorted(i for _, i in sorted(scored, reverse=True)[: self.p.snippet_sentences])
        out, last = (["…"] if keep[0] > 0 else []), -1
        for i in keep:
            if last >= 0 and i != last + 1:
                out.append("…")
            out.append(sents[i])
            last = i
        if keep[-1] < len(sents) - 1:
            out.append("…")
        txt = " ".join(out)
        return txt, len(txt) / max(1, len(m.content))

    def _snapshot(self, tenant: str = "default") -> str:
        """Per-tenant, keyed: changes on every write, forget, approval or ACL change in this tenant (and on
        retention expiry), reveals nothing about other tenants (M52)."""
        now = _clock.now()
        exp = sum(1 for m in self.mem.values() if m.tenant == tenant and m.expires_at and m.expires_at <= now) \
            if self._has_expiry else 0
        msg = f"{tenant}|{self.tmut[tenant]}|{self._epoch}|{self.tcount[tenant]}|{exp}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()[:16]


# ---------------------------------------------------------------- helpers
_GBDT_CACHE: dict[int, tuple] = {}


def _gbdt_predict(model: dict, X: np.ndarray) -> np.ndarray:
    """Evaluate an exported gradient-boosted tree ensemble (feature <= threshold goes left)."""
    hit = _GBDT_CACHE.get(id(model))
    if hit is not None and hit[0] is model:
        trees = hit[1]
    else:
        trees = [tuple(np.asarray(t[k]) for k in ("f", "t", "l", "r", "v", "leaf")) for t in model["trees"]]
        _GBDT_CACHE[id(model)] = (model, trees)
    out = np.full(len(X), float(model["base"]))
    rows = np.arange(len(X))
    for f, t, l, r, v, leaf in trees:
        node = np.zeros(len(X), dtype=np.int64)
        for _ in range(64):
            active = ~leaf[node].astype(bool)
            if not active.any():
                break
            nd = node[active]
            go_left = X[rows[active], f[nd]] <= t[nd]
            node[active] = np.where(go_left, l[nd], r[nd])
        out += v[node]
    return out


_SPLIT = re.compile(r"\bbetween\b|\band\b|\bor\b|\bversus\b|\bvs\.?\b|,|;|\bafter\b|\bbefore\b|\bthan\b|"
                    r"\bsince\b|\bwhen\b|\bwhile\b")


def _split_query(q: str) -> list[str]:
    parts = [x.strip() for x in _SPLIT.split(q) if x and x.strip()]
    good = [x for x in parts if len(terms(x)) >= 2]
    return good if len(good) >= 2 else []


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
class SmartwareSystem:
    """Adapter so the harness can run the reference engine like any baseline."""

    name = "smartware"
    family = "protocol-reference"

    def __init__(self, params: Params | None = None, **kw):
        p = params or Params()
        for k, v in kw.items():
            if k.startswith("_"):
                continue
            if isinstance(v, list) and k in ("lin_w", "lin_mu", "lin_sd", "drop_features"):
                v = tuple(v)
            setattr(p, k, v)
        self.params = p

    def build(self, corpus, enc):
        self.eng = Engine(enc.dense, self.params)
        label = {s: f"S{i + 1}" for i, s in enumerate(corpus.session_order)}  # neutral, dataset-free labels
        mems = [Memory(content=t.text, kind="event", actor=t.speaker, role=t.role, thread=t.session_id, seq=t.idx,
                       observed_at=t.ts, meta={"turn_id": t.id, "_idempotency_key": t.id,
                                                "thread_label": label.get(t.session_id, "")})
                for t in corpus.turns]
        self.eng.remember(mems)
        self.eng._build()

    def retrieve(self, q, k=30):
        from bench.system import Item
        pack = self.eng.recall(RecallRequest(query=q.text, as_of=q.time, budget_tokens=None, max_items=k))
        return [Item(it.turn_ids, it.text, it.score) for it in pack.items]
