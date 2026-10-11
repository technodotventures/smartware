"""A LangGraph ``BaseStore``-compatible store backed by ORP (adapter level L2).

Mapping:

* a namespace tuple becomes a scope path under ``scope_root`` (``("users", "u1")`` is ``langgraph/users/u1``), so
  a search on a namespace prefix is a recall within that scope, filtered before ranking by the server;
* a key becomes part of a client id, and the value round-trips through ``meta`` unchanged;
* ``put`` is an append or an upsert guarded by ``if_version``; ``delete`` is a forget.

What the LangGraph interface cannot carry, and so comes from constructor config instead: ``as_of`` (or the pinned
time of a run-scoped client), the recall budget, and who may read new items. The principal is never configurable:
it comes from the client's credential.

This module does not import LangGraph. When ``langgraph`` is importable the store subclasses its ``BaseStore`` and
returns its ``Item`` and ``SearchItem``; otherwise it defines compatible stand-ins with the same fields.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import urllib.parse
import uuid
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple, Union

from .client import OrpClient, _UNSET, utc_now
from .errors import NotFoundError, VersionConflictError
from .types import Memory

LEVEL = "L2"
#: what this adapter cannot do through the LangGraph interface, reported by ``capabilities()``
DECLARED_LOSSES = (
    "as_of: per-call time travel has no place in the interface; set as_of in the constructor or use a run client",
    "budget: per-call token budgets have no place in the interface; set budget_tokens in the constructor",
    "ttl: not supported (supports_ttl is False); records can still expire through expires_at set by other writers",
    "get: reads the current record; the HTTP binding has no as_of on get",
)

try:  # pragma: no cover - exercised only where langgraph is installed
    from langgraph.store.base import (BaseStore as _Base, GetOp, Item, ListNamespacesOp,  # type: ignore
                                      MatchCondition, PutOp, SearchItem, SearchOp)
    try:
        from langgraph.store.base import NOT_PROVIDED  # type: ignore
    except ImportError:
        NOT_PROVIDED = None  # type: ignore
    HAVE_LANGGRAPH = True
except ImportError:
    HAVE_LANGGRAPH = False
    _Base = object  # type: ignore
    NOT_PROVIDED = None  # type: ignore

    class Item:  # type: ignore[no-redef]
        """Stand-in for ``langgraph.store.base.Item``."""
        __slots__ = ("value", "key", "namespace", "created_at", "updated_at")

        def __init__(self, *, value: Dict[str, Any], key: str, namespace: Tuple[str, ...],
                     created_at: _dt.datetime, updated_at: _dt.datetime) -> None:
            self.value = value
            self.key = key
            self.namespace = tuple(namespace)
            self.created_at = created_at
            self.updated_at = updated_at

        def dict(self) -> Dict[str, Any]:
            return {"namespace": list(self.namespace), "key": self.key, "value": self.value,
                    "created_at": self.created_at.isoformat(), "updated_at": self.updated_at.isoformat()}

        def __eq__(self, other: object) -> bool:
            return isinstance(other, Item) and self.dict() == other.dict()

        def __hash__(self) -> int:
            return hash((self.namespace, self.key))

        def __repr__(self) -> str:
            return f"{type(self).__name__}(namespace={self.namespace!r}, key={self.key!r})"

    class SearchItem(Item):  # type: ignore[no-redef]
        """Stand-in for ``langgraph.store.base.SearchItem``."""
        __slots__ = ("score",)

        def __init__(self, namespace: Tuple[str, ...], key: str, value: Dict[str, Any], created_at: _dt.datetime,
                     updated_at: _dt.datetime, score: Optional[float] = None) -> None:
            super().__init__(value=value, key=key, namespace=namespace, created_at=created_at, updated_at=updated_at)
            self.score = score

        def dict(self) -> Dict[str, Any]:
            d = super().dict()
            d["score"] = self.score
            return d

    class GetOp(NamedTuple):  # type: ignore[no-redef]
        namespace: Tuple[str, ...]
        key: str
        refresh_ttl: bool = True

    class SearchOp(NamedTuple):  # type: ignore[no-redef]
        namespace_prefix: Tuple[str, ...]
        filter: Optional[Dict[str, Any]] = None
        limit: int = 10
        offset: int = 0
        query: Optional[str] = None
        refresh_ttl: bool = True

    class PutOp(NamedTuple):  # type: ignore[no-redef]
        namespace: Tuple[str, ...]
        key: str
        value: Optional[Dict[str, Any]]
        index: Union[None, bool, List[str]] = None
        ttl: Optional[float] = None

    class MatchCondition(NamedTuple):  # type: ignore[no-redef]
        match_type: str  # "prefix" or "suffix"
        path: Tuple[str, ...]

    class ListNamespacesOp(NamedTuple):  # type: ignore[no-redef]
        match_conditions: Optional[Tuple[MatchCondition, ...]] = None
        max_depth: Optional[int] = None
        limit: int = 100
        offset: int = 0


def _enc(s: str) -> str:
    # percent-encode a namespace label or key so "/" never splits a segment and "~" (reserved by ORP ids) never
    # appears; quote() leaves "~" alone, so encode it by hand
    return urllib.parse.quote(s, safe="").replace("~", "%7E")


def _ts(s: Optional[str]) -> _dt.datetime:
    if not s:
        return _dt.datetime.now(_dt.timezone.utc)
    d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)


def _text(value: Any, fields: Optional[Sequence[str]]) -> str:
    """The text recall indexes for a value: the named fields, or the whole value, as plain ``key: value`` lines."""
    def flat(v: Any, prefix: str = "") -> List[str]:
        if isinstance(v, dict):
            out: List[str] = []
            for k, x in v.items():
                out += flat(x, f"{prefix}{k}: " if not isinstance(x, (dict, list)) else f"{prefix}{k} ")
            return out
        if isinstance(v, list):
            return [s for x in v for s in flat(x, prefix)]
        return [f"{prefix}{v}"] if v is not None and v != "" else []

    if fields:
        parts: List[str] = []
        for f in fields:
            cur: Any = value
            for p in f.split("."):
                cur = cur.get(p) if isinstance(cur, dict) else None
            parts += flat(cur)
        return "\n".join(parts)
    return "\n".join(flat(value))


_OPS = {"$eq": lambda a, b: a == b, "$ne": lambda a, b: a != b,
        "$gt": lambda a, b: a is not None and a > b, "$gte": lambda a, b: a is not None and a >= b,
        "$lt": lambda a, b: a is not None and a < b, "$lte": lambda a, b: a is not None and a <= b}


def _matches(value: Dict[str, Any], flt: Optional[Dict[str, Any]]) -> bool:
    for k, want in (flt or {}).items():
        cur: Any = value
        for p in k.split("."):
            cur = cur.get(p) if isinstance(cur, dict) else None
        if isinstance(want, dict) and want and all(o in _OPS for o in want):
            try:
                if not all(_OPS[o](cur, w) for o, w in want.items()):
                    return False
            except TypeError:
                return False
        elif cur != want:
            return False
    return True


def _ns_match(ns: Tuple[str, ...], pattern: Tuple[str, ...], suffix: bool) -> bool:
    if len(pattern) > len(ns):
        return False
    part = ns[len(ns) - len(pattern):] if suffix else ns[:len(pattern)]
    return all(p == "*" or p == n for p, n in zip(pattern, part))


class OrpStore(_Base):  # type: ignore[misc, valid-type]
    """LangGraph store on ORP.

    ``client``: an ``OrpClient`` (a run client pins ``as_of`` for searches). ``scope_root``: the scope every
    namespace sits under. ``budget_tokens`` and ``as_of``: recall settings the interface has no place for.
    ``readers`` / ``audience``: who can read new items (default: only you). ``kind``: the record kind items are
    stored as. ``index_fields``: value fields to index for search (default: the whole value). ``per_principal_keys``:
    give each principal its own key space (default ``True``; client ids are unique per tenant, so without it two
    users writing the same namespace and key would collide).
    """

    supports_ttl = False
    level = LEVEL

    def __init__(self, client: OrpClient, *, scope_root: str = "langgraph", budget_tokens: int = 2000,
                 as_of: Optional[str] = None, recall_mode: Optional[str] = None, readers: Any = _UNSET,
                 audience: Optional[str] = None, kind: str = "document", index_fields: Optional[Sequence[str]] = None,
                 per_principal_keys: bool = True, delete_mode: str = "retract") -> None:
        if not isinstance(client, OrpClient):
            raise TypeError("client must be an OrpClient")
        if not scope_root or "~" in scope_root:
            raise ValueError("scope_root must be a non-empty path without '~'")
        self.client = client
        self.scope_root = scope_root.strip("/")
        self.budget_tokens = budget_tokens
        self.as_of = as_of
        self.recall_mode = recall_mode
        self._readers = readers
        self._audience = audience
        self.kind = kind
        self.index_fields = list(index_fields) if index_fields else None
        self.per_principal_keys = per_principal_keys
        self.delete_mode = delete_mode

    # ------------------------------------------------------------------ declared level
    def capabilities(self) -> Dict[str, Any]:
        return {"adapter": "langgraph", "level": LEVEL, "losses": list(DECLARED_LOSSES),
                "langgraph_installed": HAVE_LANGGRAPH}

    # ------------------------------------------------------------------ mapping
    def _scope(self, namespace: Sequence[str]) -> str:
        return "/".join([self.scope_root] + [_enc(s) for s in namespace])

    def _id(self, namespace: Tuple[str, ...], key: str) -> str:
        base = f"{self._scope(namespace)}#{_enc(key)}"
        return f"lg:{_enc(self.client.principal)}:{base}" if self.per_principal_keys else f"lg:{base}"

    @staticmethod
    def _check_ns(namespace: Any, allow_empty: bool = False) -> Tuple[str, ...]:
        if isinstance(namespace, str) or not isinstance(namespace, (tuple, list)):
            raise TypeError("namespace is a tuple of strings")
        ns = tuple(namespace)
        if not ns and not allow_empty:
            raise ValueError("namespace cannot be empty")
        if not all(isinstance(s, str) and s for s in ns):
            raise ValueError("namespace labels are non-empty strings")
        return ns

    def _item(self, m: Memory, score: Optional[float] = None, search: bool = False) -> Optional[Item]:
        lg = m.meta.get("lg")
        if not isinstance(lg, dict) or "key" not in lg or "namespace" not in lg:
            return None
        created = _ts(m.observed_at)
        updated = _ts(lg.get("updated_at")) if lg.get("updated_at") else created
        ns, key, value = tuple(lg["namespace"]), lg["key"], lg.get("value") or {}
        if search:
            return SearchItem(namespace=ns, key=key, value=value, created_at=created, updated_at=updated, score=score)
        return Item(value=value, key=key, namespace=ns, created_at=created, updated_at=updated)

    # ------------------------------------------------------------------ operations
    def _get(self, op: Any) -> Optional[Item]:
        ns = self._check_ns(op.namespace)
        try:
            m = self.client.advanced.get(self._id(ns, op.key))
        except NotFoundError:
            return None
        if m.status != "active":
            return None
        it = self._item(m)
        return it if it is not None and it.namespace == ns and it.key == op.key else None

    def _put(self, op: Any) -> None:
        ns = self._check_ns(op.namespace)
        if not isinstance(op.key, str) or not op.key:
            raise ValueError("key is a non-empty string")
        ttl = getattr(op, "ttl", None)
        if ttl is not None and ttl is not NOT_PROVIDED:
            raise NotImplementedError("OrpStore does not support ttl (supports_ttl is False)")
        mid = self._id(ns, op.key)
        if op.value is None:  # delete
            try:
                self.client.forget(mid, mode=self.delete_mode, allow_reingest=True, reason="langgraph delete")
            except NotFoundError:
                pass
            return
        if not isinstance(op.value, dict):
            raise TypeError("value must be a dict")
        index = getattr(op, "index", None)
        fields = list(index) if isinstance(index, (list, tuple)) else self.index_fields
        text = _text(op.value, fields) if index is not False else ""
        mem: Dict[str, Any] = {
            "id": mid, "kind": self.kind, "content": text.strip() or op.key, "scope": self._scope(ns),
            # one thread per item: ORP de-duplicates identical content per thread, and two items may hold the same value
            "thread": mid,
            "meta": {"lg": {"namespace": list(ns), "key": op.key, "value": op.value, "indexed": index is not False,
                            "updated_at": utc_now()}},
        }
        kw: Dict[str, Any] = {}
        if self._readers is not _UNSET:
            kw["readers"] = self._readers
        if self._audience is not None:
            kw["audience"] = self._audience
        for attempt in range(3):
            try:
                cur = self.client.advanced.get(mid)
            except NotFoundError:
                cur = None
            try:
                key = f"lg:{uuid.uuid4().hex}"  # one logical write: transport retries are safe
                if cur is None:
                    self.client.remember(mem, idempotency_key=key, **kw)
                else:
                    m2, kw2 = dict(mem), dict(kw)
                    if not kw:  # no store-wide audience configured: an update keeps the item's readers
                        if cur.readers is None:
                            kw2["audience"] = "tenant"
                        else:
                            m2["readers"] = list(cur.readers)
                    self.client.remember(m2, mode="upsert", if_version=cur.version, idempotency_key=key,
                                         allow_reingest=cur.status == "retracted", **kw2)
                return
            except VersionConflictError:
                if attempt == 2:
                    raise

    def _search(self, op: Any) -> List[SearchItem]:
        ns = self._check_ns(op.namespace_prefix, allow_empty=True)
        scope = self._scope(ns)
        limit, offset = int(op.limit), int(op.offset)
        flt = op.filter or None
        out: List[SearchItem] = []
        if op.query:
            want = 200 if flt else min(200, max(1, offset + limit))
            pack = self.client.recall(op.query, scope=scope, kinds=[self.kind], budget_tokens=self.budget_tokens,
                                      budget_items=want, granularity="unit", as_of=self.as_of, mode=self.recall_mode)
            if flt:
                self.client._degraded("langgraph.search", ["langgraph:filter_applied_after_recall"])
            seen = set()
            for ci in pack.items:
                if ci.id in seen:
                    continue
                seen.add(ci.id)
                try:
                    m = self.client.advanced.get(ci.id)
                except NotFoundError:
                    continue
                it = self._item(m, ci.score, search=True)
                if it is None or not (m.meta.get("lg") or {}).get("indexed", True) or not _matches(it.value, flt):
                    continue
                out.append(it)  # type: ignore[arg-type]
            return out[offset:offset + limit]
        for m in self.client.advanced.iter_list(scope=scope, kinds=[self.kind]):
            it = self._item(m, None, search=True)
            if it is None or not _matches(it.value, flt):
                continue
            out.append(it)  # type: ignore[arg-type]
            if len(out) >= offset + limit:
                break
        return out[offset:offset + limit]

    def _list_namespaces(self, op: Any) -> List[Tuple[str, ...]]:
        found = set()
        for m in self.client.advanced.iter_list(scope=self.scope_root, kinds=[self.kind]):
            lg = m.meta.get("lg")
            if isinstance(lg, dict) and isinstance(lg.get("namespace"), list):
                found.add(tuple(lg["namespace"]))
        res = []
        for ns in found:
            ok = True
            for c in op.match_conditions or ():
                if not _ns_match(ns, tuple(c.path), c.match_type == "suffix"):
                    ok = False
                    break
            if ok:
                res.append(ns[:op.max_depth] if op.max_depth is not None else ns)
        res = sorted(set(res))
        return res[op.offset:op.offset + op.limit]

    def batch(self, ops: Iterable[Any]) -> List[Any]:
        out: List[Any] = []
        for op in ops:
            if hasattr(op, "match_conditions"):
                out.append(self._list_namespaces(op))
            elif hasattr(op, "namespace_prefix"):
                out.append(self._search(op))
            elif hasattr(op, "value"):
                out.append(self._put(op))
            elif hasattr(op, "namespace") and hasattr(op, "key"):
                out.append(self._get(op))
            else:
                raise TypeError(f"unknown store operation {type(op).__name__}")
        return out

    async def abatch(self, ops: Iterable[Any]) -> List[Any]:
        return await asyncio.to_thread(self.batch, list(ops))

    # ------------------------------------------------------------------ the BaseStore convenience surface
    def get(self, namespace: Tuple[str, ...], key: str, *, refresh_ttl: Optional[bool] = None) -> Optional[Item]:
        return self.batch([GetOp(tuple(namespace), str(key))])[0]

    def search(self, namespace_prefix: Tuple[str, ...], /, *, query: Optional[str] = None,
               filter: Optional[Dict[str, Any]] = None, limit: int = 10, offset: int = 0,
               refresh_ttl: Optional[bool] = None) -> List[SearchItem]:
        return self.batch([SearchOp(tuple(namespace_prefix), filter, limit, offset, query)])[0]

    def put(self, namespace: Tuple[str, ...], key: str, value: Dict[str, Any],
            index: Union[None, bool, List[str]] = None, *, ttl: Any = NOT_PROVIDED) -> None:
        if ttl is not None and ttl is not NOT_PROVIDED:
            raise NotImplementedError("OrpStore does not support ttl (supports_ttl is False)")
        self.batch([PutOp(tuple(namespace), str(key), value, index)])

    def delete(self, namespace: Tuple[str, ...], key: str) -> None:
        self.batch([PutOp(tuple(namespace), str(key), None)])

    def list_namespaces(self, *, prefix: Optional[Sequence[str]] = None, suffix: Optional[Sequence[str]] = None,
                        max_depth: Optional[int] = None, limit: int = 100, offset: int = 0) -> List[Tuple[str, ...]]:
        conds = []
        if prefix:
            conds.append(MatchCondition(match_type="prefix", path=tuple(prefix)))
        if suffix:
            conds.append(MatchCondition(match_type="suffix", path=tuple(suffix)))
        return self.batch([ListNamespacesOp(tuple(conds) or None, max_depth, limit, offset)])[0]

    async def aget(self, namespace: Tuple[str, ...], key: str, *, refresh_ttl: Optional[bool] = None) -> Optional[Item]:
        return (await self.abatch([GetOp(tuple(namespace), str(key))]))[0]

    async def asearch(self, namespace_prefix: Tuple[str, ...], /, *, query: Optional[str] = None,
                      filter: Optional[Dict[str, Any]] = None, limit: int = 10, offset: int = 0,
                      refresh_ttl: Optional[bool] = None) -> List[SearchItem]:
        return (await self.abatch([SearchOp(tuple(namespace_prefix), filter, limit, offset, query)]))[0]

    async def aput(self, namespace: Tuple[str, ...], key: str, value: Dict[str, Any],
                   index: Union[None, bool, List[str]] = None, *, ttl: Any = NOT_PROVIDED) -> None:
        if ttl is not None and ttl is not NOT_PROVIDED:
            raise NotImplementedError("OrpStore does not support ttl (supports_ttl is False)")
        await self.abatch([PutOp(tuple(namespace), str(key), value, index)])

    async def adelete(self, namespace: Tuple[str, ...], key: str) -> None:
        await self.abatch([PutOp(tuple(namespace), str(key), None)])

    async def alist_namespaces(self, *, prefix: Optional[Sequence[str]] = None,
                               suffix: Optional[Sequence[str]] = None, max_depth: Optional[int] = None,
                               limit: int = 100, offset: int = 0) -> List[Tuple[str, ...]]:
        return await asyncio.to_thread(self.list_namespaces, prefix=prefix, suffix=suffix, max_depth=max_depth,
                                       limit=limit, offset=offset)


__all__ = ["OrpStore", "Item", "SearchItem", "GetOp", "SearchOp", "PutOp", "ListNamespacesOp", "MatchCondition",
           "HAVE_LANGGRAPH", "LEVEL"]
