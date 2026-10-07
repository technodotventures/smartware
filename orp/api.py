"""ORP service layer: the verbs of spec §4 with request validation, principal binding,
concurrency control, governance (§6) and lifecycle (§7) on top of the recall engine.

Transports (orp/http_server.py, orp/mcp_server.py) call Service methods with a principal
resolved from credentials, never from request bodies. Every response carries `request_id`
and `degraded` (spec §4).
"""
from __future__ import annotations

import functools
import hashlib
import json
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime

from . import __version__
from ._text import count_tokens, sentences, terms
from .engine import Engine, Params
from .model import KINDS, ORIGINS, SYSTEM, Claim, Memory, Principal, RecallRequest, Source

SPEC_VERSION = "0.1"
ROLES = ("user", "assistant", "system", "tool", "human", "connector")
LOADS = ("always", "on_scope", "on_query")
WAITS = ("accepted", "stored", "indexed")
DEFAULT_RECALL_BUDGET = 2000


class ORPError(Exception):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def to_json(self):
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


def _check_keys(body: dict, allowed: set[str], where: str):
    if not isinstance(body, dict):
        raise ORPError("invalid_parameter", f"{where}: expected an object")
    extra = set(body) - allowed
    if extra:
        raise ORPError("unsupported_parameter", f"{where}: unsupported parameter(s) {sorted(extra)}",
                       parameters=sorted(extra))


def _enum(v, allowed, name):
    if v not in allowed:
        raise ORPError("unsupported_parameter", f"{name} {v!r} not in {list(allowed)}")
    return v


def _ts(v):
    if v is None or isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise ORPError("invalid_parameter", f"bad timestamp {v!r}")


def _enveloped(fn):
    @functools.wraps(fn)
    def wrap(self, *a, **kw):
        out = fn(self, *a, **kw)
        if isinstance(out, dict):
            out.setdefault("degraded", [])
            out["request_id"] = "req_" + uuid.uuid4().hex[:16]
        return out
    return wrap


_REMEMBER_KEYS = {"memories", "mode", "idempotency_key", "if_version", "wait"}
_MEM_KEYS = {"id", "kind", "content", "claim", "scope", "readers", "writers", "actor", "role", "origin", "thread", "seq",
             "event_time", "valid_from", "valid_to", "expires_at", "sources", "derived_from", "labels", "load",
             "confidence", "support", "meta", "supersedes"}
_RECALL_KEYS = {"query", "queries", "as_of", "known_at", "scope", "kinds", "budget", "granularity", "include", "time",
                "mode", "min_coverage", "cursor"}
_FORGET_KEYS = {"ids", "mode", "cascade", "dry_run", "reason", "allow_reingest"}


class Service:
    def __init__(self, embedder, params: Params | None = None, tokenizer: str = "orp-approx-1"):
        self.engine = Engine(embedder, params)
        self.tokenizer = tokenizer
        self._keys: dict[tuple[str, str], list[str]] = {}
        self._jobs: dict[str, dict] = {}
        self._feedback: list[dict] = []
        self._edges: dict[str, dict] = {}
        self._tombstones: dict[str, dict] = {}
        self._blocked: set[str] = set()
        self._changes: list[dict] = []

    # ------------------------------------------------------------ helpers
    def _log(self, op, mem_id, principal, **kw):
        m = self.engine.mem.get(mem_id)
        self._changes.append({"cursor": len(self._changes) + 1, "at": datetime.now().isoformat(), "op": op,
                              "id": mem_id, "tenant": principal.tenant if m is None else m.tenant,
                              "by": principal.id, "version": m.version if m else None, "status": m.status if m else None,
                              **kw})

    def _can_read(self, m: Memory, pr: Principal, secret_ok: bool = True) -> bool:
        return self.engine.readable(m, pr, ("secret",) if secret_ok else ())

    def _can_write(self, m: Memory, pr: Principal) -> bool:
        return self.engine.can_write(m, pr)

    # ------------------------------------------------------------ capabilities
    @_enveloped
    def capabilities(self, principal: Principal = SYSTEM) -> dict:
        p = self.engine.p
        learned = p.ranker in ("linear", "gbdt")
        return {
            "spec": SPEC_VERSION, "engine": f"orp-reference/{__version__}",
            "conformance": {"claimed": ["L1", "L2", "L3", "L4"], "suite": "tests/conformance (see spec/MUSTS.md)"},
            "kinds": list(KINDS), "origins": list(ORIGINS), "roles": list(ROLES),
            "channels": ["lexical:bm25(k1=1.2,b=0.75, admissible-set statistics)",
                         "lexical:window(unit+admissible neighbours)", "lexical:segment-maxpool", "dense:unit",
                         "dense:window", "dense:segment-maxpool", "thread(admissible units)", "entity", "temporal",
                         "prf:rm3"],
            "fusion": f"weighted-rrf(k={p.rrf_k})",
            "ranker": {"name": f"orp-{p.ranker}-1", "features": [x for x in Engine.FEATURES if x not in p.drop_features] if learned else
                       ["coverage", "actor", "role", "order", "current", "prefer"],
                       "fitted_on": "public dev splits (LoCoMo-10 conv 0-2; LME-R dev split)"},
            "score_scale": "probability-like 0..1 from the learned ranker" if learned else
            "fused RRF normalised to top=1, times feature multipliers",
            "coverage_method": "top item's ranker probability" if learned else "top score / feature ceiling",
            "recall_parameters": sorted(_RECALL_KEYS - {"queries", "cursor", "time"}),
            "rejected_parameters": ["queries", "cursor", "time"],
            "granularity": ["unit", "span", "thread", "auto"], "tokenizer": self.tokenizer,
            "granularity_auto": "units; a short reply carries the prompt it answers"
            + ("; questions carry their reply" if p.expand_next else ""),
            "modes": {"fast": "no pseudo-relevance feedback, rrf ranker", "balanced": "full pipeline",
                      "deep": "same as balanced in this engine (no model-based reranking)"},
            "defaults": {"budget.tokens": DEFAULT_RECALL_BUDGET, "budget.items": 50},
            "limits": {"max_items": 200, "max_budget_tokens": 32000},
        }

    # ------------------------------------------------------------ remember
    @staticmethod
    def _parse_time(raw, m_kw):
        et = raw.get("event_time")
        if isinstance(et, dict):
            _check_keys(et, {"start", "end", "granularity"}, "event_time")
            m_kw["event_time"] = _ts(et.get("start"))
            m_kw["meta"]["event_end"] = et.get("end")
            m_kw["meta"]["event_granularity"] = et.get("granularity")
        else:
            m_kw["event_time"] = _ts(et)

    @_enveloped
    def remember(self, body: dict, principal: Principal) -> dict:
        _check_keys(body, _REMEMBER_KEYS, "remember")
        mode = _enum(body.get("mode", "append"), ("append", "upsert", "supersede", "propose"), "mode")
        wait = _enum(body.get("wait", "indexed"), WAITS, "wait")
        key = body.get("idempotency_key")
        if key and (principal.tenant, key) in self._keys:
            return {"ids": self._keys[(principal.tenant, key)], "replayed": True, "stage": "indexed"}
        out = []
        for raw in body.get("memories", []):
            _check_keys(raw, _MEM_KEYS, "memory")
            kind = raw.get("kind", "event")
            if kind not in KINDS and not str(kind).startswith("x-"):
                raise ORPError("unsupported_parameter", f"kind {kind!r}")
            origin = _enum(raw.get("origin", "observed" if kind in ("event", "document") else "human"), ORIGINS,
                           "origin")
            if raw.get("role") is not None:
                _enum(raw["role"], ROLES, "role")
            load = _enum(raw.get("load", "on_query"), LOADS, "load")
            if origin in ("extracted", "inferred") and not raw.get("derived_from"):
                raise ORPError("invalid_parameter", "extracted/inferred memories require derived_from")
            for s in raw.get("derived_from", []):
                src = self.engine.mem.get(s)
                if src is None or not self._can_read(src, principal):
                    raise ORPError("forbidden", f"cannot derive from {s}")
            m_kw = {"meta": dict(raw.get("meta", {}))}
            self._parse_time(raw, m_kw)
            sources = [s if isinstance(s, Source) else Source(**s) for s in raw.get("sources", [])]
            if origin != "observed" and not sources:
                sources = [Source(uri=f"orp://{principal.tenant}/mem/{s}") for s in raw.get("derived_from", [])] or \
                          [Source(uri=f"orp://{principal.tenant}/principal/{principal.id}")]
            m = Memory(
                content=raw["content"], kind=kind, tenant=principal.tenant, scope=raw.get("scope", ""),
                readers=frozenset(raw["readers"]) if raw.get("readers") is not None else None,
                writers=frozenset(raw["writers"]) if raw.get("writers") is not None else None,
                actor=raw.get("actor", principal.id), role=raw.get("role", ""), origin=origin,
                thread=raw.get("thread", ""), seq=int(raw.get("seq", 0)), observed_at=datetime.now(),
                event_time=m_kw["event_time"], valid_from=_ts(raw.get("valid_from")),
                valid_to=_ts(raw.get("valid_to")), expires_at=_ts(raw.get("expires_at")),
                claim=Claim(**raw["claim"]) if raw.get("claim") else None, sources=sources,
                derived_from=list(raw.get("derived_from", [])), labels=list(raw.get("labels", [])), load=load,
                confidence=float(raw.get("confidence", 1.0)), support=int(raw.get("support", 1)), meta=m_kw["meta"],
            )
            if raw.get("id"):
                m.id = raw["id"]
            m.meta["_creator"] = principal.id
            if mode == "propose":
                m.status = "draft"
            if m.content_hash() in self._blocked and not m.meta.get("allow_reingest"):
                raise ORPError("forbidden", "content was retracted; re-ingest blocked")
            existing = self.engine.mem.get(m.id)
            if existing is not None and existing.tenant != principal.tenant:
                raise ORPError("invalid_parameter", "id unavailable")
            if existing is not None:
                if mode != "upsert":
                    raise ORPError("version_conflict", f"{m.id} exists; use mode=upsert with if_version")
                if not self._can_write(existing, principal):
                    raise ORPError("forbidden", f"no write access to {m.id}")
                if body.get("if_version") is None:
                    raise ORPError("invalid_parameter", "upsert of an existing record requires if_version")
                if int(body["if_version"]) != existing.version:
                    raise ORPError("version_conflict", "version mismatch", current=existing.version)
                m.version = existing.version + 1
                m.observed_at = existing.observed_at
                m.meta["_creator"] = existing.meta.get("_creator", principal.id)
                for hk in [hk for hk, v in self.engine._hash.items() if v == m.id]:
                    del self.engine._hash[hk]
                del self.engine.mem[m.id]
                self.engine.order.remove(m.id)
            if mode == "supersede":
                if kind in ("event", "document"):
                    raise ORPError("invalid_parameter", "evidence (event/document) is never superseded")
                for old in raw.get("supersedes", []):
                    om = self.engine.mem.get(old)
                    if om is None or not self._can_write(om, principal):
                        raise ORPError("forbidden", f"cannot supersede {old}")
                    if om.kind in ("event", "document"):
                        raise ORPError("invalid_parameter", "evidence (event/document) is never superseded")
                    om.status, om.superseded_by = "superseded", m.id
                    om.valid_to = m.valid_from or m.event_time or datetime.now()
                    om.version += 1
                    m.supersedes.append(old)
                    self._log("supersede", old, principal)
            ids = self.engine.remember(m, principal)
            if existing is not None:
                self.engine.reclose(m.id)
            out += ids
            self._log("remember", ids[0], principal)
        if key:
            self._keys[(principal.tenant, key)] = out
        return {"ids": out, "replayed": False, "stage": "indexed"}

    # ------------------------------------------------------------ recall
    @_enveloped
    def recall(self, body: dict, principal: Principal) -> dict:
        _check_keys(body, _RECALL_KEYS, "recall")
        if "query" not in body or not str(body["query"]).strip():
            raise ORPError("invalid_parameter", "query is required (an empty query never means 'everything')")
        rejected = [k for k in ("queries", "cursor", "time") if body.get(k)]
        if rejected:  # honoured-or-rejected (P5)
            raise ORPError("unsupported_parameter", f"not supported by this engine: {rejected}", parameters=rejected)
        budget = body.get("budget", {"tokens": DEFAULT_RECALL_BUDGET})
        _check_keys(budget, {"tokens", "items"}, "budget")
        inc = body.get("include", {})
        _check_keys(inc, {"history", "drafts", "neighbours", "explain", "labels"}, "include")
        scope = body.get("scope", {})
        if isinstance(scope, str):
            scope = {"within": scope}
        _check_keys(scope, {"within", "prefer"}, "scope")
        mode = _enum(body.get("mode", "balanced"), ("fast", "balanced", "deep"), "mode")
        gran = _enum(body.get("granularity", "auto"), ("unit", "span", "thread", "auto"), "granularity")
        kinds, quotas = body.get("kinds"), {}
        if isinstance(kinds, dict):
            quotas = {k: int(v) for k, v in kinds.items()}
            kinds = tuple(kinds)
        elif kinds is not None:
            kinds = tuple(kinds)
        for k in kinds or ():
            if k not in KINDS and not str(k).startswith("x-"):
                raise ORPError("unsupported_parameter", f"kind {k!r}")
        tokens = int(budget.get("tokens", DEFAULT_RECALL_BUDGET))
        if tokens < 50:
            raise ORPError("budget_too_small", "budget.tokens must be >= 50")
        nbh = inc.get("neighbours", 0)
        if nbh not in (0, None, "auto") and not isinstance(nbh, int):
            raise ORPError("invalid_parameter", "include.neighbours must be an integer or 'auto'")
        filters = {"labels": tuple(inc.get("labels", ())), "drafts": bool(inc.get("drafts")), "granularity": gran,
                   "neighbours": nbh if isinstance(nbh, int) else 0, "quotas": quotas,
                   "prefer": list(scope.get("prefer", []))}
        req = RecallRequest(query=body["query"], principal=principal, tenant=principal.tenant,
                            as_of=_ts(body.get("as_of")) or datetime.now(), known_at=_ts(body.get("known_at")),
                            scope=scope.get("within", ""), kinds=kinds, budget_tokens=tokens,
                            max_items=int(budget.get("items", 50)), include_history=bool(inc.get("history")),
                            filters=filters)
        p = self.engine.p
        if mode == "fast":  # declared: no second pass, no learned ranker
            self.engine.p = replace(p, w_prf=0.0, ranker="rrf")
        try:
            pack = self.engine.recall(req)
        finally:
            self.engine.p = p
        items, used = [], 0
        for it in pack.items:
            if used + it.tokens > tokens:
                break
            used += it.tokens
            m0 = self.engine.mem.get(it.memory_ids[0])
            items.append({
                "ref": f"orp://{principal.tenant}/mem/{it.memory_ids[0]}", "memory_ids": it.memory_ids,
                "kind": m0.kind if m0 else "event", "scope": m0.scope if m0 else "", "text": it.text,
                "tokens": it.tokens, "score": it.score, "why": it.why if inc.get("explain", True) else {},
                "validity": it.valid, "citations": it.citations,
                "superseded": [self.engine.mem[i].superseded_by for i in it.memory_ids
                               if i in self.engine.mem and self.engine.mem[i].superseded_by],
            })
        cov = pack.coverage
        abstain = body.get("min_coverage") is not None and cov < float(body["min_coverage"])
        if abstain:
            items, used = [], 0
        degraded = [n.split(":", 1)[1] for n in pack.notes if n.startswith("degraded:")]
        return {"items": items, "tokens": used, "budget": {"tokens": tokens}, "coverage": cov, "abstain_hint": abstain,
                "snapshot": pack.snapshot, "degraded": degraded,
                "notes": [n for n in pack.notes if not n.startswith("degraded:")], "tokenizer": self.tokenizer}

    # ------------------------------------------------------------ profile (load: always)
    @_enveloped
    def profile(self, body: dict, principal: Principal) -> dict:
        _check_keys(body, {"subject", "budget", "as_of"}, "profile")
        if not body.get("budget", {}).get("tokens"):
            raise ORPError("invalid_parameter", "profile requires budget.tokens (no unbounded ambient context)")
        tokens = int(body["budget"]["tokens"])
        as_of = _ts(body.get("as_of")) or datetime.now()
        subj = body.get("subject")
        prio = {"procedure": 0, "preference": 1, "profile": 2, "fact": 3}
        cands = [m for m in self.engine.mem.values()
                 if m.load == "always" and self._can_read(m, principal, secret_ok=False) and m.status == "active"
                 and self.engine.valid_at(m, as_of)
                 and not (subj and m.claim and m.claim.subject.lower() != subj.lower() and m.actor != subj)]
        cands.sort(key=lambda m: (prio.get(m.kind, 9), -(m.valid_from or m.observed_at).timestamp()))
        lines, used = [], 0
        for m in cands:
            t = count_tokens(m.content) + 2
            if used + t > tokens:
                break
            used += t
            lines.append({"id": m.id, "kind": m.kind, "text": m.content})
        return {"items": lines, "tokens": used, "budget": {"tokens": tokens}}

    # ------------------------------------------------------------ get / neighbours / history
    @_enveloped
    def get(self, ids: list[str], principal: Principal) -> dict:
        out, missing = [], []
        for i in ids:
            m = self.engine.mem.get(i)
            if m is None or not self._can_read(m, principal):  # forbidden and absent look the same
                missing.append(i)
                continue
            d = m.to_json()
            d["meta"] = {k: v for k, v in d["meta"].items() if not k.startswith("_")}
            out.append(d)
        return {"memories": out, "not_found": missing}

    @_enveloped
    def neighbours(self, mem_id: str, before: int, after: int, principal: Principal) -> dict:
        m = self.engine.mem.get(mem_id)
        if m is None or not self._can_read(m, principal):
            raise ORPError("not_found", mem_id)
        same = sorted((x for x in self.engine.mem.values() if x.thread == m.thread and x.tenant == m.tenant
                       and self._can_read(x, principal) and x.status not in ("retracted", "draft")),
                      key=lambda x: (x.seq, x.observed_at))
        idx = [x.id for x in same].index(mem_id)
        sel = same[max(0, idx - before): idx + after + 1]
        return {"memories": [{"id": x.id, "actor": x.actor, "content": x.content, "seq": x.seq} for x in sel]}

    @_enveloped
    def history(self, subject: str, predicate: str, principal: Principal) -> dict:
        chain = [m for m in self.engine.history(subject, predicate, principal.tenant) if self._can_read(m, principal)]
        return {"versions": [{"id": m.id, "object": m.claim.object, "status": m.status, "version": m.version,
                              "valid_from": m.valid_from.isoformat() if m.valid_from else None,
                              "valid_to": m.valid_to.isoformat() if m.valid_to else None,
                              "superseded_by": m.superseded_by} for m in chain]}

    # ------------------------------------------------------------ link
    @_enveloped
    def link(self, body: dict, principal: Principal) -> dict:
        _check_keys(body, {"type", "from", "to", "valid_from", "valid_to", "sources"}, "link")
        for end in ("from", "to"):
            m = self.engine.mem.get(body[end])
            if m is None or not self._can_read(m, principal):
                raise ORPError("not_found", body[end])
        eid = "edge_" + uuid.uuid4().hex[:16]
        self._edges[eid] = dict(body, id=eid, tenant=principal.tenant)
        return {"id": eid}

    def _edge_visible(self, e: dict, principal: Principal) -> bool:
        a, b = self.engine.mem.get(e["from"]), self.engine.mem.get(e["to"])
        return bool(a and b and self._can_read(a, principal) and self._can_read(b, principal))

    def edges(self, mem_id: str, principal: Principal) -> list[dict]:
        return [e for e in self._edges.values() if mem_id in (e["from"], e["to"]) and self._edge_visible(e, principal)]

    # ------------------------------------------------------------ forget
    @_enveloped
    def forget(self, body: dict, principal: Principal) -> dict:
        _check_keys(body, _FORGET_KEYS, "forget")
        mode = _enum(body.get("mode", "retract"), ("retract", "redact", "purge"), "mode")
        affected = []
        for i in body["ids"]:
            m = self.engine.mem.get(i)
            if m is None or not self._can_read(m, principal):
                raise ORPError("not_found", i)
            if not self._can_write(m, principal):
                raise ORPError("forbidden", f"no write access to {i}")
            affected.append(i)
            if body.get("cascade", True):
                frontier = [i]
                while frontier:
                    cur = frontier.pop()
                    for d in self.engine.mem.values():
                        if cur in d.derived_from and d.id not in affected:
                            affected.append(d.id)
                            frontier.append(d.id)
        if body.get("dry_run"):
            return {"dry_run": True, "affected": affected}
        for i in affected:
            m = self.engine.mem.get(i)
            if m and not body.get("allow_reingest"):
                self._blocked.add(m.content_hash())
        done = []
        for i in body["ids"]:
            done += self.engine.forget(i, mode=mode, cascade=body.get("cascade", True), principal=principal,
                                       reason=body.get("reason", ""))
        for i in set(done):
            if mode == "purge":
                self._tombstones[i] = {"id": i, "tenant": principal.tenant, "at": datetime.now().isoformat()}
            self._log(f"forget:{mode}", i, principal, reason=body.get("reason", ""))
        return {"dry_run": False, "affected": sorted(set(done))}

    # ------------------------------------------------------------ feedback / approval
    @_enveloped
    def feedback(self, body: dict, principal: Principal) -> dict:
        _check_keys(body, {"target", "signal", "draft", "final", "note"}, "feedback")
        _enum(body["signal"], ("used", "helpful", "wrong", "outdated", "draft_final"), "signal")
        m = self.engine.mem.get(body["target"])
        if m is None or not self._can_read(m, principal):
            raise ORPError("not_found", body["target"])
        self._feedback.append(dict(body, actor=principal.id, at=datetime.now().isoformat()))
        out = {"recorded": True}
        if body["signal"] == "draft_final" and body.get("draft") and body.get("final"):
            rule = _rule_from_diff(body["draft"], body["final"])
            if rule:
                r = self.remember({"memories": [{"kind": "procedure", "content": rule, "origin": "inferred",
                                                 "derived_from": [m.id], "load": "on_scope", "scope": m.scope}],
                                   "mode": "propose"}, principal)
                out["rule_candidate"] = r["ids"][0]
        return out

    @_enveloped
    def approve(self, mem_id: str, principal: Principal) -> dict:
        m = self.engine.mem.get(mem_id)
        if m is None or m.status != "draft" or m.tenant != principal.tenant:
            raise ORPError("not_found", mem_id)
        if not (self._can_write(m, principal) or "approver" in principal.groups):
            raise ORPError("forbidden", "approval requires write access")
        m.status = "active"
        m.version += 1
        self.engine.touch()
        self._log("approve", mem_id, principal)
        return {"id": mem_id, "status": "active", "version": m.version}

    # ------------------------------------------------------------ reflect (deterministic consolidation)
    @_enveloped
    def reflect(self, body: dict, principal: Principal) -> dict:
        _check_keys(body, {"scope", "threads", "kinds"}, "reflect")
        job = "job_" + uuid.uuid4().hex[:12]
        made = []
        threads = defaultdict(list)
        for m in self.engine.mem.values():
            if m.kind in ("event", "document") and m.status == "active" and self._can_read(m, principal, False):
                if body.get("threads") and m.thread not in body["threads"]:
                    continue
                threads[m.thread].append(m)
        sysp = Principal("system", frozenset({"*"}), tenant=principal.tenant)
        for th, ms in threads.items():
            ms.sort(key=lambda m: m.seq)
            key = "reflect:" + hashlib.sha1((th + "|" + ",".join(m.id for m in ms)).encode()).hexdigest()[:16]
            if (principal.tenant, key) in self._keys:
                continue
            prev = [x.id for x in self.engine.mem.values() if x.kind == "summary" and x.thread == th
                    and x.status == "active" and x.tenant == principal.tenant]
            r = self.remember({"memories": [{"kind": "summary", "content": _extractive_summary(ms),
                                             "origin": "extracted", "derived_from": [m.id for m in ms], "thread": th,
                                             "supersedes": prev,
                                             "sources": [{"uri": f"orp://{principal.tenant}/thread/{th}"}],
                                             "event_time": (ms[-1].event_time or ms[-1].observed_at).isoformat()}],
                               "mode": "supersede" if prev else "append", "idempotency_key": key}, sysp)
            made += r["ids"]
        self._jobs[job] = {"status": "completed", "created": made, "tenant": principal.tenant}
        return {"job": job, "status": "completed", "created": made}

    @_enveloped
    def job(self, job_id: str, principal: Principal = SYSTEM) -> dict:
        j = self._jobs.get(job_id)
        if j is None or j["tenant"] != principal.tenant:
            raise ORPError("not_found", job_id)
        return dict(j)

    # ------------------------------------------------------------ changes / audit
    @_enveloped
    def changes(self, cursor: int, principal: Principal, limit: int = 500) -> dict:
        out = []
        auditor = "auditor" in principal.groups
        for c in self._changes[cursor: cursor + limit]:
            if c["tenant"] != principal.tenant:
                continue
            m = self.engine.mem.get(c["id"])
            if m is not None and not self._can_read(m, principal) and not auditor:
                continue
            if m is None and not auditor:
                continue
            out.append(c)
        return {"changes": out, "cursor": min(len(self._changes), cursor + limit)}

    @_enveloped
    def audit_log(self, principal: Principal) -> dict:
        if "auditor" not in principal.groups:
            raise ORPError("forbidden", "audit requires the auditor role")
        ids = {i for i, m in self.engine.mem.items() if m.tenant == principal.tenant} | \
              {t["id"] for t in self._tombstones.values() if t["tenant"] == principal.tenant}
        return {"entries": [a for a in self.engine.audit if a["id"] in ids]}

    # ------------------------------------------------------------ export / import
    @_enveloped
    def export(self, principal: Principal) -> dict:
        mems = [m for m in self.engine.mem.values() if m.tenant == principal.tenant and self._can_read(m, principal)]
        lines = []
        for m in sorted(mems, key=lambda m: m.id):
            d = m.to_json()
            lines.append(json.dumps(d, sort_keys=True))
        edges = [json.dumps(e, sort_keys=True) for e in self._edges.values()
                 if e["tenant"] == principal.tenant and self._edge_visible(e, principal)]
        manifest = {"spec": SPEC_VERSION, "tenant": principal.tenant, "count": len(lines),
                    "sha256": hashlib.sha256("\n".join(lines).encode()).hexdigest(),
                    "tombstones": [t for t in self._tombstones.values() if t["tenant"] == principal.tenant]}
        return {"manifest": manifest, "memories.jsonl": "\n".join(lines), "edges.jsonl": "\n".join(edges)}

    @_enveloped
    def import_(self, bundle: dict, principal: Principal) -> dict:
        lines = [l for l in bundle["memories.jsonl"].split("\n") if l.strip()]
        n = 0
        for l in lines:
            m = Memory.from_json(json.loads(l))
            if m.tenant != principal.tenant:
                raise ORPError("forbidden", "bundle tenant mismatch")
            if m.id in self.engine.mem:
                continue
            self.engine.mem[m.id] = m
            self.engine.order.append(m.id)
            self.engine._hash[m.meta.get("idempotency_key") or m.content_hash()] = m.id
            if m.claim is not None:
                self.engine._claims[(m.tenant, m.claim.key())].append(m.id)
            n += 1
        for l in [l for l in bundle.get("edges.jsonl", "").split("\n") if l.strip()]:
            e = json.loads(l)
            self._edges[e["id"]] = e
        for t in bundle.get("manifest", {}).get("tombstones", []):
            self._tombstones[t["id"]] = t
        self.engine.touch()
        return {"imported": n}


# ---------------------------------------------------------------- deterministic helpers
def _extractive_summary(ms: list[Memory], k: int = 5) -> str:
    sents = []
    for m in ms:
        for s in sentences(m.content):
            if len(s.split()) >= 5:
                sents.append((m.seq, f"{m.actor}: {s}" if m.actor else s))
    if not sents:
        return " ".join(m.content for m in ms)[:400]
    df = Counter(t for _, s in sents for t in set(terms(s)))
    cent = Counter()
    for _, s in sents:
        cent.update(terms(s))
    scored = []
    for i, (seq, s) in enumerate(sents):
        ts = set(terms(s))
        scored.append((sum(cent[t] / (1 + df[t]) for t in ts) / (1 + len(ts) ** 0.5), i))
    keep = sorted(i for _, i in sorted(scored, reverse=True)[:k])
    return " ".join(sents[i][1] for i in keep)


def _rule_from_diff(draft: str, final: str) -> str | None:
    d, f = set(re.findall(r"\w+", draft.lower())), set(re.findall(r"\w+", final.lower()))
    removed, added = sorted(d - f)[:8], sorted(f - d)[:8]
    if not removed and not added:
        return None
    parts = []
    if removed:
        parts.append("avoid: " + ", ".join(removed))
    if added:
        parts.append("prefer: " + ", ".join(added))
    ratio = len(final) / max(1, len(draft))
    if ratio < 0.8:
        parts.append(f"keep it about {int(ratio * 100)}% of the draft length")
    return "When drafting similar content, " + "; ".join(parts) + "."
