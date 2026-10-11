"""The ORP client: five everyday verbs, with everything else under ``client.advanced``.

Safe by construction:

* Identity is bound, never passed. A client is built from credentials; no method takes a principal, tenant, actor
  or reader-of-the-request argument. The tenant and principal come from the server (``whoami``).
* Writes fail closed. ``remember`` stores records readable only by you unless you name readers or say
  ``audience="tenant"``. Widening later is an explicit ``share()``.
* ``kind="procedure"`` (a standing rule) defaults to ``mode="propose"``, so it waits for approval.
* ``profile`` requires a budget, and every ``recall`` states one.
* ``run()`` pins ``as_of`` once per agent run and derives idempotency keys from the run and step, so a retried or
  replayed run never writes twice.
* Nothing is swallowed: unknown parameters are hard errors, and ``degraded[]`` on any response is reported through
  ``on_degraded`` (by default a warning on the ``orp_client`` logger).
"""
from __future__ import annotations

import calendar
import copy
import datetime as _dt
import logging
import re
import threading
import urllib.parse
import uuid
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Union

from ._http import Transport
from .errors import InvalidParameterError, NotFoundError, OrpError, UnsupportedParameterError, VersionConflictError
from .types import ContextPack, ForgetResult, Json, Memory, Profile, ProfileItem, RememberResult, Whoami

log = logging.getLogger("orp_client")

#: Fields of MemoryInput in spec 0.1 (schema/orp-0.1/MemoryInput.schema.json). Anything else is refused locally
#: with the same error the server would send, before any content leaves the process.
MEMORY_INPUT_KEYS = frozenset([
    "id", "kind", "content", "claim", "scope", "readers", "writers", "actor", "role", "origin", "thread", "seq",
    "event_time", "valid_from", "valid_to", "expires_at", "sources", "derived_from", "labels", "load", "confidence",
    "support", "meta", "supersedes"])
REMEMBER_MODES = ("append", "upsert", "supersede", "propose")
FORGET_MODES = ("retract", "redact", "purge")
AUDIENCES = ("self", "tenant")
DEFAULT_RECALL_BUDGET = 2000
MCP_PROTOCOL = "2025-06-18"

_UNSET: Any = object()
TimeLike = Union[str, _dt.datetime, None]
DegradedHook = Callable[[str, List[str]], None]


def _iso(t: TimeLike) -> Optional[str]:
    """ISO 8601 in UTC. A naive datetime is taken to be UTC already."""
    if t is None:
        return None
    if isinstance(t, _dt.datetime):
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.timezone.utc)
        return t.astimezone(_dt.timezone.utc).isoformat()
    if isinstance(t, str) and t.strip():
        return t
    raise TypeError("times are ISO 8601 strings or datetime objects")


def as_of_time(t: TimeLike) -> Optional[str]:
    """A point in time for ``as_of`` / ``known_at``. A coarse date means "as of the end of that period": ``"2024"``
    is the end of 2024, ``"2024-05"`` the end of May 2024 and ``"2024-05-12"`` the end of that day, so events later
    in the stated period are not hidden by a midnight cut-off. Full timestamps and datetimes are sent unchanged."""
    if isinstance(t, _dt.date) and not isinstance(t, _dt.datetime):
        t = t.isoformat()
    if isinstance(t, str):
        s = t.strip()
        if re.fullmatch(r"\d{4}", s):
            return f"{s}-12-31T23:59:59.999999Z"
        m = re.fullmatch(r"(\d{4})-(\d{2})", s)
        if m:
            y, mo = int(m.group(1)), int(m.group(2))
            if not 1 <= mo <= 12:
                raise ValueError(f"bad month in {s!r}")
            last = calendar.monthrange(y, mo)[1]
            return f"{s}-{last:02d}T23:59:59.999999Z"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return f"{s}T23:59:59.999999Z"
    return _iso(t)


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _seg(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")


def _default_degraded(verb: str, degraded: List[str]) -> None:
    log.warning("orp %s: degraded %s", verb, ", ".join(degraded))


class OrpClient:
    """A client bound to one credential.

    ``token``, ``token_env`` (the *name* of an environment variable) or ``get_token`` (a callable returning a fresh
    token, for example from a secret manager): pass at most one. With none, requests carry no credentials, which only
    a ``--dev`` server on localhost accepts.

    ``tenant`` is optional: when left out the client asks the server which tenant the credential belongs to.
    ``on_degraded(verb, degraded)`` is called whenever a response reports ``degraded[]``.
    """

    def __init__(self, url: str, token: Optional[str] = None, token_env: Optional[str] = None,
                 get_token: Optional[Callable[[], str]] = None, tenant: Optional[str] = None, timeout: float = 30,
                 retries: int = 3, on_degraded: Optional[DegradedHook] = None, *, allow_insecure_http: bool = False,
                 _transport: Optional[Transport] = None) -> None:
        self._t = _transport or Transport(url, token=token, token_env=token_env, get_token=get_token,
                                          timeout=timeout, retries=retries, allow_insecure_http=allow_insecure_http)
        if tenant is not None and (not isinstance(tenant, str) or not tenant):
            raise TypeError("tenant must be a non-empty string")
        self._tenant = tenant
        self._who: Optional[Whoami] = None
        self._who_lock = threading.Lock()
        self._on_degraded: DegradedHook = on_degraded or _default_degraded
        self._as_of: Optional[str] = None
        self._run_id: Optional[str] = None
        self._step = 0
        self._step_lock = threading.Lock()
        self.advanced = Advanced(self)

    def __repr__(self) -> str:
        run = f", run={self._run_id!r}, as_of={self._as_of!r}" if self._run_id else ""
        return f"OrpClient({self._t.base!r}, tenant={self._tenant!r}{run})"

    # ------------------------------------------------------------------ identity (from the server, never arguments)
    @property
    def tenant(self) -> str:
        if self._tenant is None:
            self._tenant = self._discover_tenant()
        return self._tenant

    @property
    def principal(self) -> str:
        """Your principal id, as the server resolved it from the credential."""
        return self._whoami().id

    @property
    def run_id(self) -> Optional[str]:
        return self._run_id

    @property
    def as_of(self) -> Optional[str]:
        """The pinned ``as_of`` of a run-scoped client (``None`` outside a run)."""
        return self._as_of

    def _whoami(self, refresh: bool = False) -> Whoami:
        with self._who_lock:
            if self._who is None or refresh:
                d = self._call("GET", ["whoami"], "whoami", retry=True)
                self._who = Whoami(id=d["id"], tenant=d["tenant"], groups=list(d.get("groups") or []),
                                   tokens=list(d.get("tokens") or []))
            return self._who

    def _discover_tenant(self) -> str:
        """Find the credential's tenant without being told it.

        The HTTP binding's ``whoami`` sits under ``/v1/{tenant}/``, so it cannot answer this. The MCP binding on the
        same server can: ``resources/list`` names ``orp://<tenant>/capabilities``. All three calls are reads.
        """
        hdr = {"accept": "application/json, text/event-stream", "mcp-protocol-version": MCP_PROTOCOL}
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": MCP_PROTOCOL, "capabilities": {}, "clientInfo": {"name": "orp-client-python",
                                                                               "version": "0.1"}}}
        body, headers = self._t.request("POST", "/mcp", verb="discover-tenant", body=init, retry=True, headers=hdr)
        sid = headers.get("mcp-session-id")
        if not sid:
            raise OrpError("internal", "tenant discovery failed: no MCP session; pass tenant= explicitly")
        try:
            h2 = dict(hdr, **{"mcp-session-id": sid})
            res, _ = self._t.request("POST", "/mcp", verb="discover-tenant", retry=True, headers=h2,
                                     body={"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {}})
            for r in ((res or {}).get("result") or {}).get("resources") or []:
                uri = r.get("uri", "")
                if uri.startswith("orp://") and uri.endswith("/capabilities"):
                    return uri[len("orp://"):-len("/capabilities")]
            raise OrpError("internal", "tenant discovery failed; pass tenant= explicitly")
        finally:
            try:
                self._t.request("DELETE", "/mcp", verb="discover-tenant", headers=dict(hdr, **{"mcp-session-id": sid}))
            except OrpError:
                pass

    # ------------------------------------------------------------------ plumbing
    def _call(self, method: str, segs: Sequence[str], verb: str, *, body: Any = None,
              query: Optional[Dict[str, Any]] = None, retry: bool = False,
              headers: Optional[Dict[str, str]] = None) -> Json:
        path = "/v1/" + _seg(self.tenant) + "/" + "/".join(_seg(s) for s in segs)
        out, _ = self._t.request(method, path, verb=verb, body=body, query=query, retry=retry, headers=headers)
        if isinstance(out, dict):
            deg = out.get("degraded")
            if isinstance(deg, list) and deg:
                self._degraded(verb, [str(x) for x in deg])
        return out

    def _degraded(self, verb: str, degraded: List[str]) -> None:
        try:
            self._on_degraded(verb, degraded)
        except Exception:  # noqa: BLE001 - a broken hook must not turn a good response into a failure
            log.exception("orp on_degraded hook raised")

    def _next_key(self, step: Optional[str]) -> Optional[str]:
        if self._run_id is None:
            return None
        if step is None:
            with self._step_lock:
                self._step += 1
                step = str(self._step)
        return f"run:{self._run_id}:{step}"

    # ------------------------------------------------------------------ everyday verbs
    def recall(self, query: str, *, as_of: TimeLike = None, budget_tokens: Optional[int] = None,
               kinds: Union[Sequence[str], Dict[str, int], None] = None, include_history: bool = False,
               mode: Optional[str] = None, min_coverage: Optional[float] = None, scope: Optional[str] = None,
               budget_items: Optional[int] = None, granularity: Optional[str] = None,
               known_at: TimeLike = None) -> ContextPack:
        """Recall a budgeted, cited context pack for ``query``.

        In a run, ``as_of`` defaults to the run's pinned time. ``scope`` limits recall to a scope path and the paths
        under it. ``kinds`` is a list (restrict) or a map of kind to item quota.
        """
        if not isinstance(query, str) or not query.strip():
            raise InvalidParameterError("invalid_parameter", "query is required (an empty query never means "
                                                             "'everything')")
        budget: Json = {"tokens": DEFAULT_RECALL_BUDGET if budget_tokens is None else budget_tokens}
        if budget_items is not None:
            budget["items"] = budget_items
        body: Json = {"query": query, "budget": budget}
        at = as_of_time(as_of) if as_of is not None else self._as_of
        if at is not None:
            body["as_of"] = at
        if known_at is not None:
            body["known_at"] = as_of_time(known_at)
        if kinds is not None:
            body["kinds"] = dict(kinds) if isinstance(kinds, dict) else list(kinds)
        if include_history:
            body["include"] = {"history": True}
        if mode is not None:
            body["mode"] = mode
        if min_coverage is not None:
            body["min_coverage"] = min_coverage
        if scope is not None:
            body["scope"] = {"within": scope}
        if granularity is not None:
            body["granularity"] = granularity
        return ContextPack.from_json(self._call("POST", ["recall"], "recall", body=body, retry=True))

    def remember(self, content_or_memory_or_list: Union[str, Json, Sequence[Union[str, Json]]], *,
                 kind: Optional[str] = None, readers: Any = _UNSET, audience: Optional[str] = None,
                 claim: Optional[Json] = None, thread: Optional[str] = None, idempotency_key: Optional[str] = None,
                 mode: Optional[str] = None, scope: Optional[str] = None, meta: Optional[Json] = None,
                 if_version: Optional[int] = None, allow_reingest: bool = False,
                 step: Optional[str] = None) -> RememberResult:
        """Store one record, a memory dict (``MemoryInput``), or a list of either.

        Readers default to you alone (``["principal:<you>"]``). Pass ``readers=[...]`` to name access tokens, or
        ``audience="tenant"`` to make the records readable by everyone in the tenant. ``readers=None`` is refused
        because it silently means "everyone".

        Keyword values fill in fields a memory dict does not set itself. In a run, ``idempotency_key`` defaults to
        ``run:<run_id>:<step>``; with a key, retries are safe and a replay returns the first write's ids.
        """
        items = self._items(content_or_memory_or_list)
        if claim is not None and len(items) != 1:
            raise ValueError("claim= applies to a single memory; put claims inside each memory dict instead")
        if audience is not None and audience not in AUDIENCES:
            raise ValueError(f"audience must be one of {AUDIENCES}")
        if readers is not _UNSET:
            if readers is None:
                raise ValueError("readers=None means everyone in the tenant; say audience='tenant' if you mean it")
            if audience is not None:
                raise ValueError("pass readers or audience, not both")
            if isinstance(readers, str) or not all(isinstance(r, str) for r in readers):
                raise TypeError("readers is a list of access tokens such as 'principal:bob' or 'group:sales'")
            readers = list(readers)
        mems: List[Json] = []
        for raw in items:
            m = dict(raw)
            extra = sorted(set(m) - MEMORY_INPUT_KEYS)
            if extra:
                raise UnsupportedParameterError("unsupported_parameter", f"memory: unsupported parameter(s) {extra}",
                                                None, {"parameters": extra, "where": "client"})
            cid = m.get("id")
            if cid is not None and (not isinstance(cid, str) or not cid or "~" in cid):
                raise InvalidParameterError("invalid_parameter", "client ids are non-empty strings without '~'")
            for k, v in (("kind", kind), ("claim", claim), ("thread", thread), ("scope", scope), ("meta", meta)):
                if v is not None and k not in m:
                    m[k] = copy.deepcopy(v)
            if "readers" in m:
                if m["readers"] is None and audience != "tenant":
                    raise ValueError("a memory with readers=None is readable by everyone in the tenant; "
                                     "pass audience='tenant' to confirm")
            elif readers is not _UNSET:
                m["readers"] = list(readers)
            elif audience == "tenant":
                m["readers"] = None
            else:
                m["readers"] = [f"principal:{self.principal}"]
            mems.append(m)
        procedures = sum(1 for m in mems if m.get("kind") == "procedure")
        if mode is None and procedures:
            if procedures != len(mems):
                raise ValueError("a batch mixing procedures with other kinds needs an explicit mode; procedures "
                                 "default to mode='propose' (they wait for approval)")
            mode = "propose"
        if mode is not None and mode not in REMEMBER_MODES:
            raise UnsupportedParameterError("unsupported_parameter", f"mode {mode!r} not in {list(REMEMBER_MODES)}",
                                            None, {"parameters": ["mode"], "where": "client"})
        key = idempotency_key if idempotency_key is not None else self._next_key(step)
        body: Json = {"memories": mems}
        if mode is not None:
            body["mode"] = mode
        if key is not None:
            body["idempotency_key"] = key
        if if_version is not None:
            body["if_version"] = if_version
        if allow_reingest:
            body["allow_reingest"] = True
        out = self._call("POST", ["remember"], "remember", body=body, retry=key is not None)
        ids = list(out.get("ids") or [])
        dedup = [i for i, m in enumerate(mems) if m.get("id") is not None and i < len(ids) and ids[i] != m["id"]]
        return RememberResult(ids=ids, replayed=bool(out.get("replayed")), stage=out.get("stage", ""),
                              degraded=list(out.get("degraded") or []), request_id=out.get("request_id", ""),
                              deduplicated=dedup, raw=out)

    @staticmethod
    def _items(x: Any) -> List[Json]:
        if isinstance(x, str):
            return [{"content": x}]
        if isinstance(x, dict):
            return [x]
        if isinstance(x, (list, tuple)):
            out: List[Json] = []
            for i in x:
                if isinstance(i, str):
                    out.append({"content": i})
                elif isinstance(i, dict):
                    out.append(i)
                else:
                    raise TypeError("remember takes strings, memory dicts, or a list of them")
            if not out:
                raise ValueError("nothing to remember")
            return out
        raise TypeError("remember takes strings, memory dicts, or a list of them")

    def forget(self, ids: Union[str, Sequence[str]], *, mode: str = "retract", dry_run: bool = False,
               reason: Optional[str] = None, cascade: Optional[bool] = None,
               allow_reingest: bool = False) -> ForgetResult:
        """Forget records (and, by default, what was derived from them).

        ``retract`` hides, ``redact`` also overwrites the text, ``purge`` deletes. Unless ``allow_reingest`` is set,
        writing the same content again is then refused. A forget is not retried on failure (only a dry run is).
        """
        id_list = [ids] if isinstance(ids, str) else list(ids)
        if not id_list:
            raise ValueError("ids is empty")
        if mode not in FORGET_MODES:
            raise UnsupportedParameterError("unsupported_parameter", f"mode {mode!r} not in {list(FORGET_MODES)}",
                                            None, {"parameters": ["mode"], "where": "client"})
        body: Json = {"ids": id_list, "mode": mode}
        if dry_run:
            body["dry_run"] = True
        if reason is not None:
            body["reason"] = reason
        if cascade is not None:
            body["cascade"] = cascade
        if allow_reingest:
            body["allow_reingest"] = True
        out = self._call("POST", ["forget"], "forget", body=body, retry=dry_run)
        return ForgetResult(dry_run=bool(out.get("dry_run")), affected=list(out.get("affected") or []),
                            degraded=list(out.get("degraded") or []), request_id=out.get("request_id", ""), raw=out)

    def profile(self, budget_tokens: int, subject: Optional[str] = None) -> Profile:
        """The always-loaded records (``load: "always"``) that fit ``budget_tokens``. A budget is required."""
        if not isinstance(budget_tokens, int) or isinstance(budget_tokens, bool) or budget_tokens < 1:
            raise InvalidParameterError("invalid_parameter", "profile requires a positive integer budget_tokens")
        body: Json = {"budget": {"tokens": budget_tokens}}
        if subject is not None:
            body["subject"] = subject
        if self._as_of is not None:
            body["as_of"] = self._as_of
        out = self._call("POST", ["profile"], "profile", body=body, retry=True)
        return Profile(items=[ProfileItem(id=i.get("id", ""), kind=i.get("kind", ""), text=i.get("text", ""))
                              for i in out.get("items") or []],
                       tokens=int(out.get("tokens", 0)), budget=dict(out.get("budget") or {}),
                       degraded=list(out.get("degraded") or []), request_id=out.get("request_id", ""), raw=out)

    def share(self, id: str, readers: Union[Sequence[str], str]) -> Memory:
        """Widen who can read a record: add access tokens to its readers, or pass ``"tenant"`` for everyone.

        This reads the record and writes it back with ``if_version``, so a concurrent change is never overwritten
        (it re-reads and tries again, up to three times). You need write access to the record.
        """
        if readers == "tenant":
            add: Optional[List[str]] = None
        elif isinstance(readers, str):
            raise TypeError("readers is a list of access tokens, or the string 'tenant'")
        else:
            add = list(readers)
            if not add or not all(isinstance(r, str) and r for r in add):
                raise ValueError("readers must name at least one access token")
        for attempt in range(3):
            cur = self.advanced.get(id)
            if cur.status == "retracted":
                raise NotFoundError("not_found", id)
            if cur.status != "active":
                # an upsert stores an active record, so sharing a draft would approve it by the back door
                raise InvalidParameterError("invalid_parameter", f"cannot share a {cur.status} record; only active "
                                                                 "records can be widened (approve drafts first)")
            if cur.readers is None:
                return cur  # already readable by the whole tenant: nothing to widen
            new = None if add is None else sorted(set(cur.readers) | set(add))
            if new is not None and new == sorted(cur.readers):
                return cur
            inp = cur.to_input()
            inp["readers"] = new
            try:
                # a fresh key per attempt: transport retries of this one upsert are safe, and a replay is a no-op
                self._call("POST", ["remember"], "share", retry=True,
                           body={"memories": [inp], "mode": "upsert", "if_version": cur.version,
                                 "idempotency_key": f"share:{uuid.uuid4().hex}"})
            except VersionConflictError:
                if attempt == 2:
                    raise
                continue
            return self.advanced.get(id)
        raise AssertionError("unreachable")

    def history(self, subject: str, predicate: str) -> List[Json]:
        """Every version of the claim ``(subject, predicate)`` you can read, oldest first."""
        out = self._call("GET", ["history"], "history", query={"subject": subject, "predicate": predicate}, retry=True)
        return list(out.get("versions") or [])

    # ------------------------------------------------------------------ scoped clients
    def run(self, run_id: Optional[str] = None, as_of: TimeLike = None) -> "OrpClient":
        """A run-scoped client: ``as_of`` is pinned once (now, unless given) for every recall and profile, and
        writes get idempotency keys ``run:<run_id>:<step>`` (step counts up from 1, or pass ``step=``).

        Replaying a run with the same ``run_id`` replays its writes instead of duplicating them. Records written
        after the pinned time, including the run's own writes, are not visible to the run's recalls: that is the
        point of one consistent view.
        """
        if self._run_id is not None:
            raise ValueError("already a run-scoped client")
        rid = run_id if run_id is not None else uuid.uuid4().hex
        if not isinstance(rid, str) or not rid:
            raise TypeError("run_id must be a non-empty string")
        c = copy.copy(self)
        c._as_of = as_of_time(as_of) if as_of is not None else utc_now()
        c._run_id = rid
        c._step = 0
        c._step_lock = threading.Lock()
        c._who_lock = threading.Lock()
        c.advanced = Advanced(c)
        return c

    def for_user(self, assertion_token: str) -> "OrpClient":
        """A client acting as one end user, from a signed assertion issued by your identity provider.

        The server verifies the assertion (OIDC/JWT) and maps its claims to that user's principal, groups and
        tenant. The new client carries only the assertion, never this client's service credential, so it cannot
        read more than the user can.
        """
        if not isinstance(assertion_token, str) or not assertion_token:
            raise TypeError("assertion_token must be a non-empty string")
        c = OrpClient(self._t.base, on_degraded=self._on_degraded, _transport=self._t.clone_with_token(assertion_token))
        return c


class ChangeFeed:
    """Iterator over the change feed, following the cursor until the end of the feed.

    ``cursor`` holds the position to resume from next time (store it and pass it back).
    """

    def __init__(self, client: OrpClient, cursor: int, limit: int) -> None:
        self._c = client
        self.cursor = int(cursor)
        self._limit = int(limit)

    def __iter__(self) -> Iterator[Json]:
        while True:
            out = self._c._call("GET", ["changes"], "changes", query={"cursor": self.cursor, "limit": self._limit},
                                retry=True)
            nxt = int(out.get("cursor", self.cursor))
            for ch in out.get("changes") or []:
                yield ch
            if nxt <= self.cursor:
                return
            self.cursor = nxt


class Advanced:
    """Everything beyond the everyday verbs. Same rules: identity comes from the credential only."""

    def __init__(self, client: OrpClient) -> None:
        self._c = client

    def whoami(self) -> Whoami:
        return self._c._whoami(refresh=True)

    def capabilities(self) -> Json:
        return self._c._call("GET", ["capabilities"], "capabilities", retry=True)

    def get(self, id: str) -> Memory:
        """One record. Raises ``NotFoundError`` when it is absent or you cannot read it (the two look the same)."""
        return Memory.from_json(self._c._call("GET", ["memories", id], "get", retry=True))

    def get_many(self, ids: Iterable[str]) -> List[Memory]:
        """The readable records among ``ids``, in order; missing ones are skipped."""
        out: List[Memory] = []
        for i in ids:
            try:
                out.append(self.get(i))
            except NotFoundError:
                pass
        return out

    def list(self, *, kinds: Optional[Sequence[str]] = None, thread: Optional[str] = None, scope: Optional[str] = None,
             limit: int = 100, cursor: Optional[int] = None) -> Json:
        """One page of the records you can read, newest first: ``{"memories": [Memory], "cursor": next or None}``.

        Uses the same admissibility as recall, so it never shows what recall would hide.
        """
        q: Json = {"limit": limit}
        if kinds:
            q["kind"] = ",".join(kinds)
        if thread is not None:
            q["thread"] = thread
        if scope is not None:
            q["scope"] = scope
        if cursor:
            q["cursor"] = cursor
        out = self._c._call("GET", ["memories"], "list", query=q, retry=True)
        return {"memories": [Memory.from_json(m) for m in out.get("memories") or []], "cursor": out.get("cursor")}

    def iter_list(self, *, kinds: Optional[Sequence[str]] = None, thread: Optional[str] = None,
                  scope: Optional[str] = None, page_size: int = 100) -> Iterator[Memory]:
        """Every record you can read (filtered), across pages."""
        cursor: Optional[int] = None
        while True:
            page = self.list(kinds=kinds, thread=thread, scope=scope, limit=page_size, cursor=cursor)
            yield from page["memories"]
            cursor = page["cursor"]
            if cursor is None:
                return

    def neighbours(self, id: str, before: int = 2, after: int = 2) -> List[Json]:
        out = self._c._call("GET", ["memories", id, "neighbours"], "neighbours",
                            query={"before": before, "after": after}, retry=True)
        return list(out.get("memories") or [])

    def thread(self, thread: str) -> List[Json]:
        out = self._c._call("GET", ["threads", thread, "neighbours"], "thread", retry=True)
        return list(out.get("memories") or [])

    def link(self, type: str, from_id: str, to_id: str, *, valid_from: TimeLike = None, valid_to: TimeLike = None,
             sources: Optional[List[Json]] = None) -> str:
        """Add an edge; returns its id. Not retried (links carry no idempotency key)."""
        body: Json = {"type": type, "from": from_id, "to": to_id}
        if valid_from is not None:
            body["valid_from"] = _iso(valid_from)
        if valid_to is not None:
            body["valid_to"] = _iso(valid_to)
        if sources is not None:
            body["sources"] = sources
        return self._c._call("POST", ["link"], "link", body=body)["id"]

    def unlink(self, edge_id: str) -> Json:
        return self._c._call("DELETE", ["edges", edge_id], "unlink")

    def edges(self, id: str) -> List[Json]:
        return list(self._c._call("GET", ["memories", id, "edges"], "edges", retry=True).get("edges") or [])

    def feedback(self, target: str, signal: str, *, draft: Optional[str] = None, final: Optional[str] = None,
                 note: Optional[str] = None) -> Json:
        body: Json = {"target": target, "signal": signal}
        for k, v in (("draft", draft), ("final", final), ("note", note)):
            if v is not None:
                body[k] = v
        return self._c._call("POST", ["feedback"], "feedback", body=body)

    def approve(self, id: str) -> Json:
        return self._c._call("POST", ["memories", id, "approve"], "approve")

    def reflect(self, *, scope: Optional[str] = None, threads: Optional[Sequence[str]] = None,
                kinds: Optional[Sequence[str]] = None) -> Json:
        body: Json = {}
        if scope is not None:
            body["scope"] = scope
        if threads is not None:
            body["threads"] = list(threads)
        if kinds is not None:
            body["kinds"] = list(kinds)
        return self._c._call("POST", ["reflect"], "reflect", body=body)

    def job(self, job_id: str) -> Json:
        return self._c._call("GET", ["jobs", job_id], "job", retry=True)

    def changes(self, cursor: int = 0, limit: int = 500) -> ChangeFeed:
        """Iterate the change feed from ``cursor`` to its end (see ``ChangeFeed.cursor`` to resume)."""
        return ChangeFeed(self._c, cursor, limit)

    def export(self) -> Json:
        """A lossless bundle of every record you can read: ``manifest``, ``memories.jsonl``, ``edges.jsonl``."""
        return self._c._call("GET", ["export"], "export", retry=True)

    def import_(self, bundle: Json) -> Json:
        """Import a bundle (tenant administrators only). Records are data: nothing in a bundle is evaluated."""
        if not isinstance(bundle, dict) or not isinstance(bundle.get("memories.jsonl", ""), str):
            raise InvalidParameterError("invalid_parameter", "bundle needs memories.jsonl")
        body = {k: v for k, v in bundle.items() if k in ("manifest", "memories.jsonl", "edges.jsonl")}
        return self._c._call("POST", ["import"], "import", body=body)


__all__ = ["OrpClient", "Advanced", "ChangeFeed", "MEMORY_INPUT_KEYS", "utc_now"]
