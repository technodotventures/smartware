"""ORP service layer: the verbs of spec §4 with request validation, principal binding,
concurrency control, governance (§6) and lifecycle (§7) on top of the recall engine.

Transports (orp/http_server.py, orp/mcp_server.py) call Service methods with a principal
resolved from credentials, never from request bodies. Every response carries `request_id`
and `degraded` (spec §4).

Identifiers: engine-assigned ids (`mem_<hex>`) are globally unique. Client-supplied ids are
unique per tenant (M02): they are stored internally as `<id>~<tenant>` and every response
shows the client's own id, so one tenant's ids never collide with, or reveal, another's.
"""
from __future__ import annotations

import functools
import hashlib
import json
import pickle
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime

from . import __version__
from ._text import count_tokens, sentences, terms
from .engine import Engine, Params
from .model import KINDS, ORIGINS, SYSTEM, Claim, Memory, Principal, RecallRequest, Source, new_id
from . import clock as _clock

SPEC_VERSION = "0.1"
ROLES = ("user", "assistant", "system", "tool", "human", "connector")
LOADS = ("always", "on_scope", "on_query")
WAITS = ("accepted", "stored", "indexed")
DEFAULT_RECALL_BUDGET = 2000
MAX_ITEMS, MAX_BUDGET = 200, 32000
_ENGINE_ID = re.compile(r"^mem_[0-9a-f]{24}$")


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


_TS_RX = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}(:\d{2}(:\d{2}([.,]\d{1,9})?)?)?)?\s*(Z|[+-]\d{2}(:?\d{2})?)?$")


def _ts(v):
    """ISO 8601 / RFC 3339 timestamps only (YYYY-MM-DD[THH[:MM[:SS[.f]]]][Z|±HH[:MM]]); numbers are rejected rather
    than guessed at (M18)."""
    if v is None or isinstance(v, datetime):
        return v
    if not isinstance(v, str) or not _TS_RX.match(v.strip()):
        raise ORPError("invalid_parameter", f"bad timestamp {v!r}: use ISO 8601, e.g. 2026-05-12T09:30:00Z")
    try:
        return _clock.parse(v)  # offsets are converted to UTC, never just dropped
    except ValueError:
        raise ORPError("invalid_parameter", f"bad timestamp {v!r}")


def _strs(v, name, nullable=False):
    if v is None and nullable:
        return None
    if not isinstance(v, (list, tuple)) or not all(isinstance(x, str) for x in v):
        raise ORPError("invalid_parameter", f"{name} must be a list of strings")
    return list(v)


def _int(v, name, lo=None, hi=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
        raise ORPError("invalid_parameter", f"{name} must be an integer")
    v = int(v)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ORPError("invalid_parameter", f"{name} must be within [{lo}, {hi}]")
    return v


def _num(v, name, lo=None, hi=None):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ORPError("invalid_parameter", f"{name} must be a number")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ORPError("invalid_parameter", f"{name} must be within [{lo}, {hi}]")
    return float(v)


def _enveloped(fn):
    @functools.wraps(fn)
    def wrap(self, *a, **kw):
        out = fn(self, *a, **kw)
        if isinstance(out, dict):
            out.setdefault("degraded", [])
            out["request_id"] = "req_" + uuid.uuid4().hex[:16]
        return out
    return wrap


_REMEMBER_KEYS = {"memories", "mode", "idempotency_key", "if_version", "wait", "allow_reingest"}
_MEM_KEYS = {"id", "kind", "content", "claim", "scope", "readers", "writers", "actor", "role", "origin", "thread", "seq",
             "event_time", "valid_from", "valid_to", "expires_at", "sources", "derived_from", "labels", "load",
             "confidence", "support", "meta", "supersedes"}
_RECALL_KEYS = {"query", "queries", "as_of", "known_at", "scope", "kinds", "budget", "granularity", "include", "time",
                "mode", "min_coverage", "cursor"}
_FORGET_KEYS = {"ids", "mode", "cascade", "dry_run", "reason", "allow_reingest"}


_DERIVED_META = {"entities", "time_refs"}  # computed by the engine from content; never taken from a client
META_MAX_DEPTH = 32  # client meta may nest this deep (it is stored, exported and serialised recursively)


def _json_depth(v, d):
    """Depth of a JSON value, stopping as soon as it passes the limit (so a hostile value costs little)."""
    if d > META_MAX_DEPTH or not isinstance(v, (dict, list)):
        return d
    m = d + 1
    for x in (v.values() if isinstance(v, dict) else v):
        m = max(m, _json_depth(x, d + 1))
        if m > META_MAX_DEPTH:
            break
    return m


def is_admin(pr: Principal) -> bool:
    return "*" in pr.groups or "admin" in pr.groups


class Service:
    def __init__(self, embedder, params: Params | None = None, tokenizer: str = "orp-approx-1"):
        self.engine = Engine(embedder, params)
        self.tokenizer = tokenizer
        self.encoder_name = getattr(embedder, "name", "")
        self.ranker_notes: list[str] = []
        self._keys: dict[tuple, list[str]] = {}
        self._jobs: dict[str, dict] = {}
        self._feedback: list[dict] = []
        self._edges: dict[str, dict] = {}
        self._tombstones: dict[str, dict] = {}
        self._blocked: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._changes: dict[str, list[dict]] = defaultdict(list)

    # ------------------------------------------------------------ ids
    def _in(self, tenant: str, cid) -> str:
        if not isinstance(cid, str):
            raise ORPError("invalid_parameter", "ids are strings")
        q = f"{cid}~{tenant}"
        return q if q in self.engine.mem else cid

    @staticmethod
    def _out(tenant: str, iid):
        if isinstance(iid, str) and iid.endswith("~" + tenant):
            return iid[: -len(tenant) - 1]
        return iid

    def _outl(self, tenant, ids):
        return [self._out(tenant, i) for i in ids]

    def _readable_id(self, i, pr: Principal) -> bool:
        m = self.engine.mem.get(i)
        return m is not None and self._can_read(m, pr)

    # ------------------------------------------------------------ helpers
    def _log(self, op, mem_id, principal, **kw):
        m = self.engine.mem.get(mem_id)
        tenant = principal.tenant if m is None else m.tenant
        feed = self._changes[tenant]
        feed.append({"cursor": len(feed) + 1, "at": _clock.now().isoformat(), "op": op,
                     "id": self._out(tenant, mem_id), "_iid": mem_id, "tenant": tenant, "by": principal.id,
                     "version": m.version if m else None, "status": m.status if m else None, **kw})

    def _can_read(self, m: Memory, pr: Principal, labels: tuple = ()) -> bool:
        return self.engine.readable(m, pr, labels)

    def _can_write(self, m: Memory, pr: Principal) -> bool:
        return self.engine.can_write(m, pr)

    def _view(self, pr: Principal, as_of=None, labels: tuple = ()) -> set[str]:
        """Ids admissible for `pr` under the same rules as recall (§5.1, §5.2)."""
        req = RecallRequest(query="", principal=pr, tenant=pr.tenant, as_of=as_of or _clock.now(),
                            filters={"labels": labels})
        return set(self.engine.admissible(req))

    def _record(self, m: Memory, pr: Principal, internal: bool = False) -> dict:
        """Wire form of a record, with client-facing ids. Links to records the caller cannot read are dropped."""
        t = m.tenant
        d = m.to_json()
        d["id"] = self._out(t, m.id)
        keep = (lambda i: True) if internal else (lambda i: self._readable_id(i, pr))
        d["supersedes"] = [self._out(t, i) for i in m.supersedes if keep(i)]
        d["superseded_by"] = self._out(t, m.superseded_by) if m.superseded_by and keep(m.superseded_by) else None
        d["derived_from"] = [self._out(t, i) for i in m.derived_from if internal or keep(i)]
        if internal:
            meta = dict(d["meta"])
            if "_supersedable_by" in meta:
                meta["_supersedable_by"] = [self._out(t, i) for i in meta["_supersedable_by"]]
            d["meta"] = meta
        else:
            d["written_by"] = m.meta.get("_creator")
            d["meta"] = {k: v for k, v in d["meta"].items() if not k.startswith("_")}
        return d

    # ------------------------------------------------------------ capabilities
    @_enveloped
    def capabilities(self, principal: Principal = SYSTEM) -> dict:
        p = self.engine.p
        learned = p.ranker in ("linear", "gbdt")
        inactive = set(p.drop_features) | {"rr_qexp"} | ({"rr_sub"} if p.w_sub <= 0 else set())
        return {
            "spec": SPEC_VERSION, "engine": f"orp-reference/{__version__}",
            "conformance": {"claimed": ["L1", "L2", "L3", "L4"], "suite": "tests/conformance (see spec/MUSTS.md)"},
            "kinds": list(KINDS), "origins": list(ORIGINS), "roles": list(ROLES),
            "tokens": ["principal:<id>", "public", "group:<g>", "channel:<c>", "role:<r>", "label:<l>", "team:<t>",
                       "org:<o>"],
            "channels": ["lexical:bm25(k1=1.2,b=0.75, admissible-set statistics)",
                         "lexical:window(unit+admissible neighbours)", "lexical:segment-maxpool", "dense:unit",
                         "dense:window", "dense:segment-maxpool", "thread(admissible units)", "entity", "temporal",
                         "prf:rm3"],
            "fusion": f"weighted-rrf(k={p.rrf_k})",
            "encoder": self.encoder_name, "notes": list(self.ranker_notes),
            "ranker": {"name": f"orp-{p.ranker}-1",
                       "features": [x for x in Engine.FEATURES if x not in inactive] if learned else
                       ["coverage", "actor", "role", "order", "current", "prefer"],
                       "post_ranker_multipliers": ["prefer"] + ([] if learned else ["order"]),
                       "fitted_on": "public dev splits (LoCoMo-10 conv 0-2; LME-R dev split)"},
            "score_scale": "probability-like 0..1 from the learned ranker" if learned else
            "fused RRF normalised to top=1, times feature multipliers",
            "coverage_method": "top item's ranker probability" if learned else "top score / feature ceiling",
            "recall_parameters": sorted(_RECALL_KEYS - {"queries", "cursor", "time"}),
            "rejected_parameters": ["queries", "cursor", "time"],
            "granularity": ["unit", "span", "thread", "auto"], "tokenizer": self.tokenizer,
            "granularity_auto": "units; a short reply carries the prompt it answers"
            + ("; questions carry their reply" if p.expand_next else ""),
            "kinds_quota": "a map restricts recall to the listed kinds and caps each; a list only restricts",
            "modes": {"fast": "no pseudo-relevance feedback, rrf ranker", "balanced": "full pipeline",
                      "deep": "same as balanced in this engine (no model-based reranking)"},
            "dedup": {"idempotency_key": "per (tenant, writer, key), unbounded window",
                      "content": "per (tenant, writer, readers, labels, thread, actor, content), unbounded window"},
            "defaults": {"budget.tokens": DEFAULT_RECALL_BUDGET, "budget.items": 50},
            "limits": {"max_items": MAX_ITEMS, "max_budget_tokens": MAX_BUDGET},
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

    def _build_memory(self, raw: dict, principal: Principal, mode: str) -> Memory:
        _check_keys(raw, _MEM_KEYS, "memory")
        t = principal.tenant
        if not isinstance(raw.get("content"), str) or not raw["content"]:
            raise ORPError("invalid_parameter", "content must be a non-empty string")
        kind = raw.get("kind", "event")
        if kind not in KINDS and not str(kind).startswith("x-"):
            raise ORPError("unsupported_parameter", f"kind {kind!r}")
        origin = _enum(raw.get("origin", "observed" if kind in ("event", "document") else "human"), ORIGINS,
                       "origin")
        if raw.get("role") is not None:
            _enum(raw["role"], ROLES, "role")
        load = _enum(raw.get("load", "on_query"), LOADS, "load")
        for f in ("scope", "thread", "actor"):
            if f in raw and not isinstance(raw[f], str):
                raise ORPError("invalid_parameter", f"{f} must be a string")
        if "meta" in raw and not isinstance(raw["meta"], dict):
            raise ORPError("invalid_parameter", "meta must be an object")
        if "meta" in raw and _json_depth(raw["meta"], 0) > META_MAX_DEPTH:
            raise ORPError("invalid_parameter", f"meta nests deeper than {META_MAX_DEPTH} levels")
        if any(str(k).startswith("_") or k in _DERIVED_META for k in raw.get("meta", {})):
            raise ORPError("invalid_parameter", "meta keys starting with '_', and the engine-derived keys "
                           f"{sorted(_DERIVED_META)}, are reserved for the engine")
        readers = _strs(raw.get("readers"), "readers", nullable=True)
        writers = _strs(raw.get("writers"), "writers", nullable=True)
        labels = _strs(raw.get("labels", []), "labels")
        derived = [self._in(t, s) for s in _strs(raw.get("derived_from", []), "derived_from")]
        if origin in ("extracted", "inferred") and not derived:
            raise ORPError("invalid_parameter", "extracted/inferred memories require derived_from")
        for s in derived:
            src = self.engine.mem.get(s)
            if src is None or not self._can_read(src, principal, ("secret",)):
                raise ORPError("forbidden", f"cannot derive from {self._out(t, s)}")
        claim = None
        if raw.get("claim") is not None:
            c = raw["claim"]
            _check_keys(c, {"subject", "predicate", "object", "cardinality"}, "claim")
            if not all(isinstance(c.get(k), str) and c.get(k) for k in ("subject", "predicate", "object")):
                raise ORPError("invalid_parameter", "claim needs string subject, predicate and object")
            _enum(c.get("cardinality", "one"), ("one", "many"), "claim.cardinality")
            claim = Claim(**c)
        sources = []
        for s in raw.get("sources", []):
            _check_keys(s, {"uri", "span", "hash", "observed_at"}, "source")
            if not isinstance(s.get("uri"), str):
                raise ORPError("invalid_parameter", "source.uri is required")
            sources.append(Source(**{k: str(v) for k, v in s.items()}))
        m_kw = {"meta": dict(raw.get("meta", {}))}
        self._parse_time(raw, m_kw)
        if origin != "observed" and not sources:
            sources = [Source(uri=f"orp://{t}/mem/{self._out(t, s)}") for s in derived] or \
                      [Source(uri=f"orp://{t}/principal/{principal.id}")]
        m = Memory(
            content=raw["content"], kind=kind, tenant=t, scope=raw.get("scope", ""),
            readers=frozenset(readers) if readers is not None else None,
            writers=frozenset(writers) if writers is not None else None,
            actor=raw.get("actor", principal.id), role=raw.get("role", ""), origin=origin,
            thread=raw.get("thread", ""), seq=_int(raw.get("seq", 0), "seq"), observed_at=_clock.now(),
            event_time=m_kw["event_time"], valid_from=_ts(raw.get("valid_from")),
            valid_to=_ts(raw.get("valid_to")), expires_at=_ts(raw.get("expires_at")),
            claim=claim, sources=sources, derived_from=derived, labels=labels, load=load,
            confidence=_num(raw.get("confidence", 1.0), "confidence", 0, 1),
            support=_int(raw.get("support", 1), "support", 0), meta=m_kw["meta"],
        )
        if raw.get("id") is not None:
            cid = raw["id"]
            if not isinstance(cid, str) or not cid or "~" in cid:
                raise ORPError("invalid_parameter", "client ids are non-empty strings without '~'")
            if _ENGINE_ID.match(cid):  # an engine id may only name an existing record of this tenant (upsert)
                ex = self.engine.mem.get(cid)
                if ex is None or ex.tenant != t:
                    raise ORPError("invalid_parameter", "ids of the form mem_<24 hex> are engine-assigned")
                m.id = cid
            else:
                m.id = f"{cid}~{t}"
        m.meta["_creator"] = principal.id
        if m.actor != principal.id:
            m.meta["_actor_asserted"] = True  # `actor` is client-asserted; `written_by` is authenticated
        if mode == "propose":
            m.status = "draft"
        return m

    @_enveloped
    def remember(self, body: dict, principal: Principal) -> dict:
        _check_keys(body, _REMEMBER_KEYS, "remember")
        mode = _enum(body.get("mode", "append"), ("append", "upsert", "supersede", "propose"), "mode")
        _enum(body.get("wait", "indexed"), WAITS, "wait")
        if not isinstance(body.get("memories", []), list):
            raise ORPError("invalid_parameter", "memories must be a list")
        t = principal.tenant
        key = body.get("idempotency_key")
        kk = (t, principal.id, key)
        if key and kk in self._keys:
            return {"ids": self._outl(t, self._keys[kk]), "replayed": True, "stage": "indexed"}
        raws = body.get("memories", [])
        mems = [self._build_memory(raw, principal, mode) for raw in raws]  # validate all first
        # every check runs before any change, so a batch is all-or-nothing
        seen = set()
        for m in mems:
            if m.id in seen:
                raise ORPError("invalid_parameter", f"{self._out(t, m.id)} appears twice in one batch")
            seen.add(m.id)
        olds = []
        for raw, m in zip(raws, mems):
            if not (body.get("allow_reingest") or m.meta.get("allow_reingest")):
                # blocked only if the writer could read the retracted record: never reveals others' content
                for bid in self._blocked.get((t, m.content_hash()), []):
                    bm = self.engine.mem.get(bid)
                    if bm is not None and (self._can_read(bm, principal, ("secret",)) or
                                           bm.meta.get("_creator") == principal.id):
                        raise ORPError("forbidden", "content was retracted; re-ingest blocked")
            existing = self.engine.mem.get(m.id)
            if existing is not None:
                if mode != "upsert":
                    raise ORPError("version_conflict", f"{self._out(t, m.id)} exists; use mode=upsert with if_version")
                if not self._can_write(existing, principal):
                    raise ORPError("forbidden", f"no write access to {self._out(t, m.id)}")
                if body.get("if_version") is None:
                    raise ORPError("invalid_parameter", "upsert of an existing record requires if_version")
                if existing.status == "retracted" and not body.get("allow_reingest"):
                    raise ORPError("forbidden", "record was retracted; upsert would restore it (set allow_reingest)")
                if _int(body["if_version"], "if_version") != existing.version:
                    raise ORPError("version_conflict", "version mismatch", current=existing.version)
            sup = []
            if mode == "supersede":
                if m.kind in ("event", "document"):
                    raise ORPError("invalid_parameter", "evidence (event/document) is never superseded")
                for old in _strs(raw.get("supersedes", []), "supersedes"):
                    oi = self._in(t, old)
                    om = self.engine.mem.get(oi)
                    if om is None or not self._can_read(om, principal, ("secret",)) or \
                            not self._can_write(om, principal):
                        raise ORPError("forbidden", f"cannot supersede {old}")
                    if om.kind in ("event", "document"):
                        raise ORPError("invalid_parameter", "evidence (event/document) is never superseded")
                    sup.append(oi)
            olds.append((existing, sup))
        out = []
        for raw, (existing, sup), m in zip(raws, olds, mems):
            stale = []
            if existing is not None:
                m.version = existing.version + 1
                m.observed_at = existing.observed_at
                m.meta["_creator"] = existing.meta.get("_creator", principal.id)
                for k in ("_supersedable_by", "_wseq"):
                    if k in existing.meta:
                        m.meta[k] = existing.meta[k]
                for hk in [hk for hk, v in self.engine._hash.items() if v == m.id]:
                    del self.engine._hash[hk]
                stale = self.engine.unchain(m.id)  # the new version may state another claim, or none
                del self.engine.mem[m.id]
                self.engine.order.remove(m.id)
                self.engine.tcount[existing.tenant] -= 1
            for oi in sup:
                om = self.engine.mem[oi]
                om.status, om.superseded_by = "superseded", m.id
                om.valid_to = m.valid_from or m.event_time or _clock.now()
                om.meta["_explicit_superseded"] = True
                om.version += 1
                m.supersedes.append(oi)
                self._log("supersede", oi, principal)
            ids = self.engine.remember(m, principal, dedup_content=raw.get("id") is None)
            if ids != [m.id] and existing is not None:
                raise RuntimeError("upsert must store the record under its own id")
            if existing is not None:
                for k in stale:
                    self.engine.resolve_chain(k)
                self.engine.reclose(m.id)
            out += ids
            self._log("remember", ids[0], principal)
        if key:
            self._keys[kk] = out
        return {"ids": self._outl(t, out), "replayed": False, "stage": "indexed"}

    # ------------------------------------------------------------ recall
    @_enveloped
    def recall(self, body: dict, principal: Principal) -> dict:
        _check_keys(body, _RECALL_KEYS, "recall")
        if not isinstance(body.get("query"), str) or not body["query"].strip():
            raise ORPError("invalid_parameter", "query must be a non-empty string (an empty query never means 'everything')")
        rejected = [k for k in ("queries", "cursor", "time") if k in body]
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
        if not isinstance(scope.get("within", ""), str):
            raise ORPError("invalid_parameter", "scope.within must be a string")
        prefer = _strs(scope.get("prefer", []), "scope.prefer")
        mode = _enum(body.get("mode", "balanced"), ("fast", "balanced", "deep"), "mode")
        gran = _enum(body.get("granularity", "auto"), ("unit", "span", "thread", "auto"), "granularity")
        kinds, quotas = body.get("kinds"), {}
        if isinstance(kinds, dict):
            quotas = {k: _int(v, f"kinds.{k}", 0) for k, v in kinds.items()}
            kinds = tuple(kinds)
        elif kinds is not None:
            kinds = tuple(_strs(kinds, "kinds"))
        for k in kinds or ():
            if k not in KINDS and not str(k).startswith("x-"):
                raise ORPError("unsupported_parameter", f"kind {k!r}")
        tokens = _int(budget.get("tokens", DEFAULT_RECALL_BUDGET), "budget.tokens", hi=MAX_BUDGET)
        if tokens < 50:
            raise ORPError("budget_too_small", "budget.tokens must be >= 50")
        items_max = _int(budget.get("items", 50), "budget.items", 1, MAX_ITEMS)
        nbh = inc.get("neighbours", 0)
        if nbh not in (0, None, "auto") and (isinstance(nbh, bool) or not isinstance(nbh, int) or not 0 <= nbh <= 10):
            raise ORPError("invalid_parameter", "include.neighbours must be an integer 0..10 or 'auto'")
        min_cov = body.get("min_coverage")
        if min_cov is not None:
            min_cov = _num(min_cov, "min_coverage", 0, 1)
        labels = tuple(_strs(inc.get("labels", []), "include.labels"))
        filters = {"labels": labels, "drafts": bool(inc.get("drafts")), "granularity": gran,
                   "neighbours": nbh if isinstance(nbh, int) else 0, "quotas": quotas, "prefer": prefer}
        req = RecallRequest(query=body["query"], principal=principal, tenant=principal.tenant,
                            as_of=_ts(body.get("as_of")) or _clock.now(), known_at=_ts(body.get("known_at")),
                            scope=scope.get("within", ""), kinds=kinds, budget_tokens=tokens,
                            max_items=items_max, include_history=bool(inc.get("history")), filters=filters)
        p = self.engine.p
        if mode == "fast":  # declared: no second pass, no learned ranker
            self.engine.p = replace(p, w_prf=0.0, ranker="rrf")
        try:
            pack = self.engine.recall(req)
        finally:
            self.engine.p = p
        t = principal.tenant
        view_sup = dict(self.engine._view_sup)
        items, used = [], 0
        for it in pack.items:
            if used + it.tokens > tokens:  # engine already packs within budget; defensive
                continue
            used += it.tokens
            m0 = self.engine.mem.get(it.memory_ids[0])
            sup = []
            for i in it.memory_ids:
                mi = self.engine.mem.get(i)
                nxt = view_sup.get(i) if mi is not None and self.engine.in_chain(mi) else (mi.superseded_by if mi
                                                                                          else None)
                if nxt and self._readable_id(nxt, principal):
                    sup.append(self._out(t, nxt))
            items.append({
                "ref": f"orp://{t}/mem/{self._out(t, it.memory_ids[0])}",
                "memory_ids": self._outl(t, it.memory_ids),
                "kind": m0.kind if m0 else "event", "scope": m0.scope if m0 else "", "text": it.text,
                "tokens": it.tokens, "score": it.score, "why": it.why if inc.get("explain", True) else {},
                "validity": it.valid, "citations": [self._out(t, c) for c in it.citations], "superseded": sup,
            })
        cov = pack.coverage
        abstain = min_cov is not None and cov < min_cov
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
        b = body.get("budget")
        if not isinstance(b, dict) or not b.get("tokens"):
            raise ORPError("invalid_parameter", "profile requires budget.tokens (no unbounded ambient context)")
        _check_keys(b, {"tokens"}, "budget")
        tokens = _int(b["tokens"], "budget.tokens", 1, MAX_BUDGET)
        as_of = _ts(body.get("as_of")) or _clock.now()
        subj = body.get("subject")
        if subj is not None and not isinstance(subj, str):
            raise ORPError("invalid_parameter", "subject must be a string")
        view = self._view(principal, as_of)  # same admissibility as recall: ACL, labels, validity, expiry, claims
        prio = {"procedure": 0, "preference": 1, "profile": 2, "fact": 3}
        cands = [self.engine.mem[i] for i in view if self.engine.mem[i].load == "always"]
        cands = [m for m in cands if not (subj and m.claim and m.claim.subject.lower() != subj.lower()
                                          and m.actor != subj)]
        cands.sort(key=lambda m: (prio.get(m.kind, 9), -_clock.epoch(m.valid_from or m.observed_at), m.id))
        lines, used = [], 0
        for m in cands:
            n = count_tokens(m.content) + 2
            if used + n > tokens:
                continue
            used += n
            lines.append({"id": self._out(m.tenant, m.id), "kind": m.kind, "text": m.content})
        return {"items": lines, "tokens": used, "budget": {"tokens": tokens}}

    # ------------------------------------------------------------ get / neighbours / history
    @_enveloped
    def get(self, ids: list[str], principal: Principal, labels: tuple = ()) -> dict:
        """Records by id. Secret-labelled records need include.labels=['secret'] and label:secret, as in recall."""
        out, missing = [], []
        for cid in _strs(ids, "ids"):
            m = self.engine.mem.get(self._in(principal.tenant, cid))
            if m is None or not self._can_read(m, principal, tuple(labels)):  # forbidden and absent look the same
                missing.append(cid)
                continue
            out.append(self._record(m, principal))
        return {"memories": out, "not_found": missing}

    @_enveloped
    def neighbours(self, mem_id: str, before: int, after: int, principal: Principal) -> dict:
        before, after = _int(before, "before", 0, 50), _int(after, "after", 0, 50)
        iid = self._in(principal.tenant, mem_id)
        view = self._view(principal)
        m = self.engine.mem.get(iid)
        if m is None or iid not in view:
            raise ORPError("not_found", mem_id)
        if not m.thread:
            same = [m]  # an unthreaded memory has no neighbours
        else:
            same = sorted((self.engine.mem[i] for i in view if self.engine.mem[i].thread == m.thread
                           and self.engine.mem[i].tenant == m.tenant),
                          key=lambda x: (x.seq, x.observed_at or datetime.min, x.id))
        idx = [x.id for x in same].index(iid)
        sel = same[max(0, idx - before): idx + after + 1]
        return {"memories": [{"id": self._out(x.tenant, x.id), "actor": x.actor, "content": x.content, "seq": x.seq}
                             for x in sel]}

    @_enveloped
    def thread(self, thread: str, principal: Principal) -> dict:
        """Admissible units of one thread in order (spec §8.2 /threads/{thread}/neighbours)."""
        if not isinstance(thread, str) or not thread:
            raise ORPError("invalid_parameter", "thread is a non-empty string")
        view = self._view(principal)
        ms = sorted((self.engine.mem[i] for i in view if self.engine.mem[i].thread == thread),
                    key=lambda x: (x.seq, x.observed_at or datetime.min, x.id))
        return {"memories": [{"id": self._out(x.tenant, x.id), "actor": x.actor, "content": x.content, "seq": x.seq}
                             for x in ms]}

    @_enveloped
    def history(self, subject: str, predicate: str, principal: Principal) -> dict:
        if not isinstance(subject, str) or not isinstance(predicate, str):
            raise ORPError("invalid_parameter", "subject and predicate are strings")
        chain = [m for m in self.engine.history(subject, predicate, principal.tenant) if self._can_read(m, principal)]
        t = principal.tenant
        return {"versions": [{"id": self._out(t, m.id), "object": m.claim.object, "status": m.status,
                              "version": m.version,
                              "valid_from": m.valid_from.isoformat() if m.valid_from else None,
                              "valid_to": m.valid_to.isoformat() if m.valid_to else None,
                              "superseded_by": self._out(t, m.superseded_by)
                              if m.superseded_by and self._readable_id(m.superseded_by, principal) else None}
                             for m in chain]}

    # ------------------------------------------------------------ link
    @_enveloped
    def link(self, body: dict, principal: Principal) -> dict:
        _check_keys(body, {"type", "from", "to", "valid_from", "valid_to", "sources"}, "link")
        _enum(body.get("type"), ("about", "mentions", "part_of", "follows", "causes", "supports", "contradicts",
                                 "supersedes", "derived_from", "relates"), "type") \
            if not str(body.get("type", "")).startswith("x-") else None
        e = dict(body)
        for end in ("from", "to"):
            iid = self._in(principal.tenant, body.get(end))
            m = self.engine.mem.get(iid)
            if m is None or not self._can_read(m, principal):
                raise ORPError("not_found", str(body.get(end)))
            e[end] = iid
        eid = "edge_" + uuid.uuid4().hex[:16]
        self._edges[eid] = dict(e, id=eid, tenant=principal.tenant)
        return {"id": eid}

    def _edge_visible(self, e: dict, principal: Principal, labels: tuple = ()) -> bool:
        a, b = self.engine.mem.get(e["from"]), self.engine.mem.get(e["to"])
        return bool(e.get("tenant") == principal.tenant and a and b and self._can_read(a, principal, labels)
                    and self._can_read(b, principal, labels))

    def edges(self, mem_id: str, principal: Principal) -> list[dict]:
        iid = self._in(principal.tenant, mem_id)
        t = principal.tenant
        return [dict(e, **{"from": self._out(t, e["from"]), "to": self._out(t, e["to"])})
                for e in self._edges.values() if iid in (e["from"], e["to"]) and self._edge_visible(e, principal)]

    # ------------------------------------------------------------ forget
    @_enveloped
    def forget(self, body: dict, principal: Principal) -> dict:
        _check_keys(body, _FORGET_KEYS, "forget")
        mode = _enum(body.get("mode", "retract"), ("retract", "redact", "purge"), "mode")
        cascade = body.get("cascade", True)
        if not isinstance(cascade, bool):
            raise ORPError("invalid_parameter", "cascade must be a boolean")
        t = principal.tenant
        ids = []
        for cid in _strs(body.get("ids"), "ids"):
            i = self._in(t, cid)
            m = self.engine.mem.get(i)
            if m is None or not self._can_read(m, principal, ("secret",)):
                raise ORPError("not_found", cid)
            if not self._can_write(m, principal):
                raise ORPError("forbidden", f"no write access to {cid}")
            ids.append(i)
        affected = self.engine.cascade_set(ids, cascade)
        # the caller sees only what it may read; cascaded records it cannot read are still forgotten (M28)
        visible = sorted(self._out(t, i) for i in affected if self._readable_id(i, principal) or i in ids)
        if body.get("dry_run"):
            return {"dry_run": True, "affected": visible}
        if not body.get("allow_reingest"):
            for i in affected:
                m = self.engine.mem.get(i)
                if m is not None:
                    self._blocked[(m.tenant, m.content_hash())].append(i)
        done = []
        for i in ids:
            if i in self.engine.mem:
                done += self.engine.forget(i, mode=mode, cascade=cascade, principal=principal,
                                           reason=str(body.get("reason", "")))
        for i in dict.fromkeys(done):
            if mode == "purge":
                self._tombstones[i] = {"id": self._out(t, i), "_iid": i, "tenant": t, "at": _clock.now().isoformat()}
            self._log(f"forget:{mode}", i, principal, reason=str(body.get("reason", "")))
        return {"dry_run": False, "affected": visible}

    # ------------------------------------------------------------ feedback / approval
    @_enveloped
    def feedback(self, body: dict, principal: Principal) -> dict:
        _check_keys(body, {"target", "signal", "draft", "final", "note"}, "feedback")
        _enum(body.get("signal"), ("used", "helpful", "wrong", "outdated", "draft_final"), "signal")
        t = principal.tenant
        m = self.engine.mem.get(self._in(t, body.get("target")))
        if m is None or not self._can_read(m, principal):
            raise ORPError("not_found", str(body.get("target")))
        self._feedback.append(dict(body, target=m.id, actor=principal.id, at=_clock.now().isoformat()))
        out = {"recorded": True}
        if body["signal"] == "draft_final" and body.get("draft") and body.get("final"):
            rule = _rule_from_diff(str(body["draft"]), str(body["final"]))
            if rule:
                r = self.remember({"memories": [{"kind": "procedure", "content": rule, "origin": "inferred",
                                                 "derived_from": [self._out(t, m.id)], "load": "on_scope",
                                                 "scope": m.scope}],
                                   "mode": "propose"}, principal)
                out["rule_candidate"] = r["ids"][0]
        return out

    @_enveloped
    def approve(self, mem_id: str, principal: Principal) -> dict:
        iid = self._in(principal.tenant, mem_id)
        m = self.engine.mem.get(iid)
        if m is None or m.status != "draft" or not self._can_read(m, principal, ("secret",)):
            raise ORPError("not_found", mem_id)
        if not (self._can_write(m, principal) or "approver" in principal.groups):
            raise ORPError("forbidden", "approval requires write access or the approver role")
        m.status = "active"
        m.version += 1
        if m.claim is not None and m.kind not in ("event", "document"):
            self.engine._supersede(m, principal)  # an approved claim supersedes like any other (M48)
        self.engine.touch(m.tenant)
        self._log("approve", iid, principal)
        return {"id": mem_id, "status": "active", "version": m.version}

    # ------------------------------------------------------------ reflect (deterministic consolidation)
    @_enveloped
    def reflect(self, body: dict, principal: Principal) -> dict:
        _check_keys(body, {"scope", "threads", "kinds"}, "reflect")
        kinds = _strs(body.get("kinds", ["summary"]), "kinds")
        if set(kinds) != {"summary"}:
            raise ORPError("unsupported_parameter", "this engine's reflect produces summaries only", parameters=["kinds"])
        sc = body.get("scope", "")
        if not isinstance(sc, str):
            raise ORPError("invalid_parameter", "scope must be a string")
        sc = sc.rstrip("/")
        job = "job_" + uuid.uuid4().hex[:12]
        made = []
        threads = defaultdict(list)
        want = set(_strs(body["threads"], "threads")) if body.get("threads") else None
        for m in self.engine.mem.values():
            if m.kind in ("event", "document") and m.status == "active" and self._can_read(m, principal):
                if want is not None and m.thread not in want:
                    continue
                if sc and not (m.scope == sc or m.scope.startswith(sc + "/")):
                    continue
                threads[m.thread].append(m)
        sysp = Principal("system", frozenset({"*"}), tenant=principal.tenant)
        for th, ms in sorted(threads.items()):
            ms.sort(key=lambda m: (m.seq, m.id))
            key = "reflect:" + hashlib.sha1((th + "|" + ",".join(m.id for m in ms)).encode()).hexdigest()[:16]
            if (principal.tenant, "system", key) in self._keys:
                continue
            acl = None
            for m in ms:
                if m.readers is not None:
                    acl = set(m.readers) if acl is None else acl & set(m.readers)
            readers = None if acl is None else frozenset(acl)
            # a new summary replaces only summaries with exactly its audience (never one the asker cannot read)
            prev = [x.id for x in self.engine.mem.values() if x.kind == "summary" and x.thread == th
                    and x.status == "active" and x.tenant == principal.tenant and x.readers == readers]
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
    def job(self, job_id: str, principal: Principal) -> dict:
        j = self._jobs.get(job_id)
        if j is None or j["tenant"] != principal.tenant:
            raise ORPError("not_found", str(job_id))
        return dict(j)

    # ------------------------------------------------------------ changes / audit
    @_enveloped
    def changes(self, cursor: int, principal: Principal, limit: int = 500) -> dict:
        cursor, limit = _int(cursor, "cursor", 0), _int(limit, "limit", 1, 100_000)
        feed = self._changes.get(principal.tenant, [])
        out = []
        auditor = "auditor" in principal.groups
        for c in feed[cursor: cursor + limit]:
            m = self.engine.mem.get(c["_iid"])
            if not auditor and (m is None or not self._can_read(m, principal)):
                continue
            out.append({k: v for k, v in c.items() if not k.startswith("_")})
        return {"changes": out, "cursor": min(len(feed), cursor + limit)}  # per-tenant cursor

    @_enveloped
    def audit_log(self, principal: Principal) -> dict:
        if "auditor" not in principal.groups:
            raise ORPError("forbidden", "audit requires the auditor role")
        t = principal.tenant
        ids = {i for i, m in self.engine.mem.items() if m.tenant == t} | \
              {x["_iid"] for x in self._tombstones.values() if x["tenant"] == t}
        return {"entries": [dict(a, id=self._out(t, a["id"])) for a in self.engine.audit if a["id"] in ids]}

    # ------------------------------------------------------------ export / import (§8.3)
    @_enveloped
    def export(self, principal: Principal) -> dict:
        t = principal.tenant
        # secret-labelled records are exported only to principals holding label:secret (or admins)
        mems = [m for m in self.engine.mem.values() if m.tenant == t and self._can_read(m, principal, ("secret",))]
        lines = [json.dumps(self._record(m, principal, internal=True), sort_keys=True, default=str)
                 for m in sorted(mems, key=lambda m: self._out(t, m.id))]
        edges = [json.dumps({k: v for k, v in dict(e, **{"from": self._out(t, e["from"]), "to": self._out(t, e["to"])}).items()
                             if not k.startswith("_")}, sort_keys=True)
                 for e in sorted(self._edges.values(), key=lambda e: e["id"]) if self._edge_visible(e, principal, ("secret",))]
        manifest = {"spec": SPEC_VERSION, "tenant": t, "count": len(lines),
                    "sha256": hashlib.sha256("\n".join(lines).encode()).hexdigest(),
                    "tombstones": [{k: x[k] for k in sorted(x) if not k.startswith("_")}
                                   for x in sorted(self._tombstones.values(), key=lambda x: x["id"]) if x["tenant"] == t]}
        return {"manifest": manifest, "memories.jsonl": "\n".join(lines), "edges.jsonl": "\n".join(edges)}

    @_enveloped
    def import_(self, bundle: dict, principal: Principal) -> dict:
        """Lossless import (M62). Restricted to tenant administrators, because it carries engine-set fields such as
        observed_at. Every record and edge must belong to the importing tenant; closure, labels, de-duplication and
        claim chains are re-applied; ids that collide with another tenant's engine ids are remapped (id_map)."""
        if not is_admin(principal):
            raise ORPError("forbidden", "import requires the tenant admin role")
        t = principal.tenant
        if not isinstance(bundle, dict) or not isinstance(bundle.get("memories.jsonl", ""), str):
            raise ORPError("invalid_parameter", "bundle needs memories.jsonl")
        man = bundle.get("manifest", {})
        if man.get("tenant", t) != t:
            raise ORPError("forbidden", "bundle tenant mismatch")
        try:
            rows = [json.loads(l) for l in bundle["memories.jsonl"].split("\n") if l.strip()]
            edge_rows = [json.loads(l) for l in str(bundle.get("edges.jsonl", "")).split("\n") if l.strip()]
        except ValueError as e:
            raise ORPError("invalid_parameter", f"bad bundle line: {e}")
        if not all(isinstance(d, dict) and isinstance(d.get("id"), str) for d in rows):
            raise ORPError("invalid_parameter", "every memories.jsonl line is a record with a string id")
        if not all(isinstance(e, dict) and isinstance(e.get("from"), str) and isinstance(e.get("to"), str)
                   and isinstance(e.get("id", ""), str) for e in edge_rows):
            raise ORPError("invalid_parameter", "every edges.jsonl line is an edge with string from and to")
        tomb_rows = man.get("tombstones", [])
        if not isinstance(tomb_rows, list) or not all(isinstance(x, dict) and isinstance(x.get("id"), str) for x in tomb_rows):
            raise ORPError("invalid_parameter", "manifest.tombstones is a list of objects with a string id")
        id_map: dict[str, str] = {}

        def conv(x):
            if x in id_map:
                return id_map[x]
            if _ENGINE_ID.match(x):
                o = self.engine.mem.get(x)
                return x if o is None or o.tenant == t else id_map.setdefault(x, new_id())
            return f"{x}~{t}"
        for d in rows:
            if d.get("tenant", t) != t:
                raise ORPError("forbidden", "bundle tenant mismatch")
        for e in edge_rows:
            if e.get("tenant", t) != t:
                raise ORPError("forbidden", "edge tenant mismatch")
        n, keys = 0, set()
        built = []
        for d in rows:
            d = dict(d)
            d.pop("written_by", None)
            d["id"] = conv(d["id"])
            d["supersedes"] = [conv(x) for x in d.get("supersedes", [])]
            d["superseded_by"] = conv(d["superseded_by"]) if d.get("superseded_by") else None
            d["derived_from"] = [conv(x) for x in d.get("derived_from", [])]
            meta = dict(d.get("meta", {}))
            if "_supersedable_by" in meta:
                meta["_supersedable_by"] = [conv(x) for x in meta["_supersedable_by"]]
            d["meta"] = meta
            try:
                built.append(Memory.from_json(d))
            except (TypeError, ValueError, KeyError) as e:
                raise ORPError("invalid_parameter", f"bad record: {e}")
        # check edges before changing anything: each endpoint is in the bundle or already in this tenant
        incoming = {m.id for m in built}
        for m in built:
            if m.tenant != t:
                raise ORPError("forbidden", "bundle tenant mismatch")
        for e in edge_rows:
            for end in (conv(e["from"]), conv(e["to"])):
                o = self.engine.mem.get(end)
                if end not in incoming and not (o and o.tenant == t):
                    raise ORPError("invalid_parameter", "edge endpoints must exist in this tenant")
        for m in built:
            if m.id in self.engine.mem:
                continue
            got = self.engine.remember(m, Principal(m.meta.get("_creator", "system"), tenant=t), chain_only=True)
            if got and got[0] == m.id:
                n += 1
                if m.claim is not None:
                    keys.add((t, m.claim.key()))
        for k in keys:
            self.engine.resolve_chain(k)
        for e in edge_rows:
            e = dict(e)
            e["from"], e["to"] = conv(e["from"]), conv(e["to"])
            a, b = self.engine.mem.get(e["from"]), self.engine.mem.get(e["to"])
            if not (a and b and a.tenant == t and b.tenant == t):
                raise ORPError("invalid_parameter", "edge endpoints must exist in this tenant")
            eid = e.get("id") or "edge_" + uuid.uuid4().hex[:16]
            if eid in self._edges and self._edges[eid].get("tenant") != t:
                eid = "edge_" + uuid.uuid4().hex[:16]
            self._edges[eid] = dict(e, id=eid, tenant=t)
        for x in tomb_rows:
            iid = conv(x["id"])
            if iid in self._tombstones and self._tombstones[iid].get("tenant") != t:
                iid = id_map.setdefault(x["id"], new_id())
            self._tombstones[iid] = dict(x, _iid=iid, tenant=t)
        self.engine.touch(t)
        return {"imported": n, "id_map": {k: self._out(t, v) for k, v in id_map.items()}}

    # ------------------------------------------------------------ persistence (reference servers)
    _STATE = ("_keys", "_jobs", "_feedback", "_edges", "_tombstones", "_blocked", "_changes")
    _ENGINE_STATE = ("mem", "order", "_hash", "_claims", "audit", "mutations", "tmut", "tcount", "_epoch", "_wseq",
                     "_secret", "_has_expiry")

    def save(self, path: str):
        """Full server state (records, idempotency keys, re-ingest blocks, change feeds, audit log) in a SQLite file
        (orp/store.py, schema orp-store-1), written in one transaction."""
        from . import store
        store.save(self, path)

    def load(self, path: str):
        from . import store
        if store.is_sqlite(path):
            store.load(self, path)
            return
        # legacy pickle stores (v0.1.3 and earlier): only on explicit opt-in, since loading a pickle can run code
        import os
        if os.environ.get("ORP_ALLOW_PICKLE") != "1":
            raise ValueError(f"{path} is not an ORP SQLite store. If it is a legacy pickle store you created, set "
                             "ORP_ALLOW_PICKLE=1 once to migrate it; it is rewritten as SQLite on the next save.")
        with open(path, "rb") as f:
            state = pickle.load(f)
        for k, v in state["service"].items():
            setattr(self, k, v)
        for k, v in state["engine"].items():
            setattr(self.engine, k, v)
        self.engine._dirty = True


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
