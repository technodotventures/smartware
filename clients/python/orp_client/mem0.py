"""A mem0-compatible ``Memory`` class backed by ORP (adapter level L2).

The call shapes follow mem0's open-source ``Memory`` (add, search, get, get_all, update, delete, delete_all,
history) and its documented ``{"results": [...]}`` return form.

Identity: ``user_id``, ``agent_id`` and ``run_id`` are scope and filters, never the principal. They become scope
segments ``<scope_root>/user/<id>/agent/<id>/run/<id>``. The principal comes from the client's API key, and new
memories are readable only by that principal unless the shim is built with other readers. mem0's own REST API checks
the API key but not the ``user_id`` a caller passes, so any key holder can read any user's memories by naming them;
this shim does not repeat that: naming someone else's ``user_id`` with your key finds nothing of theirs.

What it cannot do, and says so (through the client's ``on_degraded`` hook, which logs a warning by default):

* ``infer=True`` needs an LLM to extract facts. The shim has none, so messages are stored as they are
  (``mem0:infer_not_supported``). ``prompt`` and ``memory_type`` are refused.
* ``history`` lists the add, update and delete events with their times, but not the old text of an update: ORP's
  upsert replaces content in place, and the shim keeps no shadow copies of overwritten text
  (``mem0:history_without_old_text``).
* Searches whose ids cannot be expressed as a scope prefix (for example ``agent_id`` without ``user_id``) recall
  over the wider scope and filter afterwards, so they may miss results (``mem0:filter_applied_after_recall``).
* ``reset`` is refused.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .client import OrpClient, _UNSET, utc_now
from .errors import NotFoundError, UnsupportedParameterError, VersionConflictError
from .types import Memory as OrpMemory

LEVEL = "L2"
ROLES = ("user", "assistant", "system", "tool")
_ID_ORDER = ("user", "agent", "run")


def _seg(s: str) -> str:
    return urllib.parse.quote(str(s), safe="").replace("~", "%7E")


def _utc(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)).isoformat()


class Memory:
    """mem0-compatible memory on ORP. Build it from an ``OrpClient`` (or ``Memory.from_config``)."""

    level = LEVEL

    def __init__(self, client: OrpClient, *, readers: Any = _UNSET, audience: Optional[str] = None,
                 scope_root: str = "mem0", budget_tokens: int = 4000, delete_mode: str = "retract") -> None:
        if not isinstance(client, OrpClient):
            raise TypeError("client must be an OrpClient")
        self.client = client
        self._kw: Dict[str, Any] = {}
        if readers is not _UNSET:
            self._kw["readers"] = readers
        if audience is not None:
            self._kw["audience"] = audience
        self.scope_root = scope_root.strip("/")
        self.budget_tokens = budget_tokens
        self.delete_mode = delete_mode

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "Memory":
        """Build from ``{"orp": {"url": ..., "token_env": "ORP_TOKEN", "tenant": ...}}``.

        A token itself is never read from a config dict (config files get committed); name an environment variable.
        """
        c = dict((config or {}).get("orp") or {})
        if "token" in c or "api_key" in c:
            raise ValueError("put the token in an environment variable and pass its name as token_env")
        client = OrpClient(c["url"], token_env=c.get("token_env"), tenant=c.get("tenant"),
                           timeout=c.get("timeout", 30), retries=c.get("retries", 3))
        return cls(client, scope_root=c.get("scope_root", "mem0"), budget_tokens=c.get("budget_tokens", 4000))

    def capabilities(self) -> Dict[str, Any]:
        return {"adapter": "mem0", "level": LEVEL, "losses": [
            "infer: no LLM fact extraction; messages are stored verbatim",
            "history: no old text for updates",
            "search: ids that are not a scope prefix are filtered after recall",
            "reset: refused"]}

    # ------------------------------------------------------------------ mapping
    def _ids(self, user_id: Optional[str], agent_id: Optional[str], run_id: Optional[str]) -> Dict[str, str]:
        ids = {k: v for k, v in (("user", user_id), ("agent", agent_id), ("run", run_id)) if v is not None}
        for k, v in ids.items():
            if not isinstance(v, str) or not v:
                raise ValueError(f"{k}_id must be a non-empty string")
        if not ids:
            raise ValueError("At least one of 'user_id', 'agent_id', or 'run_id' must be provided.")
        return ids

    def _scope(self, ids: Dict[str, str], prefix_only: bool = False) -> Tuple[str, Dict[str, str]]:
        """The scope path for these ids and the ids left over to filter on (those after a gap in user/agent/run)."""
        segs = [self.scope_root] if self.scope_root else []
        rest: Dict[str, str] = {}
        gap = False
        for k in _ID_ORDER:
            if k not in ids:
                gap = True
                continue
            if gap and prefix_only:
                rest[k] = ids[k]
            else:
                segs += [k, _seg(ids[k])]
        return "/".join(segs), rest

    @staticmethod
    def _owned(m: OrpMemory) -> Optional[Dict[str, Any]]:
        x = m.meta.get("mem0")
        return x if isinstance(x, dict) else None

    def _result(self, m: OrpMemory, score: Optional[float] = None) -> Dict[str, Any]:
        x = self._owned(m) or {}
        out: Dict[str, Any] = {"id": m.id, "memory": m.content, "hash": hashlib.md5(m.content.encode("utf-8")).hexdigest(),
                               "metadata": x.get("metadata"), "created_at": _utc(m.observed_at),
                               "updated_at": _utc(x.get("updated_at"))}
        if score is not None:
            out["score"] = score
        for k in ("user_id", "agent_id", "run_id", "actor_id", "role"):
            if x.get(k) is not None:
                out[k] = x[k]
        return out

    @staticmethod
    def _keep(m: OrpMemory, x: Dict[str, Any], rest: Dict[str, str], filters: Optional[Dict[str, Any]]) -> bool:
        if m.status != "active":
            return False
        for k, v in rest.items():
            if x.get(f"{k}_id") != v:
                return False
        md = x.get("metadata") or {}
        for k, v in (filters or {}).items():
            if k in ("user_id", "agent_id", "run_id", "actor_id", "role"):
                if x.get(k) != v:
                    return False
            elif md.get(k) != v:
                return False
        return True

    # ------------------------------------------------------------------ mem0 surface
    def add(self, messages: Union[str, Dict[str, Any], Sequence[Dict[str, Any]]], *, user_id: Optional[str] = None,
            agent_id: Optional[str] = None, run_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
            infer: bool = True, memory_type: Optional[str] = None, prompt: Optional[str] = None) -> Dict[str, Any]:
        if memory_type is not None:
            raise UnsupportedParameterError("unsupported_parameter", "memory_type is not supported by this shim",
                                            None, {"parameters": ["memory_type"], "where": "mem0"})
        if prompt is not None:
            raise UnsupportedParameterError("unsupported_parameter", "prompt needs LLM extraction, which this shim "
                                            "does not do", None, {"parameters": ["prompt"], "where": "mem0"})
        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError("metadata must be a dict")
        ids = self._ids(user_id, agent_id, run_id)
        scope, _ = self._scope(ids)
        if isinstance(messages, str):
            msgs: List[Dict[str, Any]] = [{"role": "user", "content": messages}]
        elif isinstance(messages, dict):
            msgs = [messages]
        else:
            msgs = list(messages)
        if infer:
            self.client._degraded("mem0.add", ["mem0:infer_not_supported:messages_stored_verbatim"])
        now = utc_now()
        mems: List[Dict[str, Any]] = []
        for msg in msgs:
            if not isinstance(msg, dict):
                raise TypeError("messages are strings or {'role', 'content'} dicts")
            role, content = msg.get("role", "user"), msg.get("content")
            if role == "system" or not isinstance(content, str) or not content.strip():
                continue  # as mem0 does without inference: system messages are not memories
            mid = str(uuid.uuid4())
            mems.append({"id": mid, "kind": "event", "content": content, "role": role if role in ROLES else "user",
                         "scope": scope, "thread": f"mem0:{mid}",
                         "meta": {"mem0": {"user_id": user_id, "agent_id": agent_id, "run_id": run_id,
                                           "actor_id": msg.get("name"), "role": role,
                                           "metadata": dict(metadata or {}), "updated_at": None, "created": now}}})
        if not mems:
            return {"results": []}
        r = self.client.remember(mems, idempotency_key=f"mem0:{uuid.uuid4().hex}", **self._kw)
        results = []
        for i, (m, got) in enumerate(zip(mems, r.ids)):
            if i in r.deduplicated:
                continue  # an identical memory already exists; mem0 reports no event for it
            results.append({"id": got, "memory": m["content"], "event": "ADD"})
        return {"results": results}

    def search(self, query: str, *, user_id: Optional[str] = None, agent_id: Optional[str] = None,
               run_id: Optional[str] = None, limit: int = 100, filters: Optional[Dict[str, Any]] = None,
               threshold: Optional[float] = None) -> Dict[str, Any]:
        ids = self._ids(user_id, agent_id, run_id)
        scope, rest = self._scope(ids, prefix_only=True)
        post = bool(rest or filters)
        if post:
            self.client._degraded("mem0.search", ["mem0:filter_applied_after_recall"])
        pack = self.client.recall(query, scope=scope, kinds=["event"], budget_tokens=self.budget_tokens,
                                  budget_items=200 if post else max(1, min(200, limit)), granularity="unit")
        out: List[Dict[str, Any]] = []
        seen = set()
        for it in pack.items:
            if it.id in seen:
                continue
            seen.add(it.id)
            if threshold is not None and it.score < threshold:
                continue
            try:
                m = self.client.advanced.get(it.id)
            except NotFoundError:
                continue
            x = self._owned(m)
            if x is None or not self._keep(m, x, rest, filters):
                continue
            out.append(self._result(m, it.score))
            if len(out) >= limit:
                break
        return {"results": out}

    def get(self, memory_id: str) -> Optional[Dict[str, Any]]:
        try:
            m = self.client.advanced.get(memory_id)
        except NotFoundError:
            return None
        if self._owned(m) is None or m.status != "active":
            return None
        return self._result(m)

    def _all(self, ids: Dict[str, str], filters: Optional[Dict[str, Any]], limit: Optional[int]) -> List[OrpMemory]:
        scope, rest = self._scope(ids, prefix_only=True)
        out: List[OrpMemory] = []
        for m in self.client.advanced.iter_list(scope=scope, kinds=["event"]):  # complete: filtering here cuts nothing
            x = self._owned(m)
            if x is None or not self._keep(m, x, rest, filters):
                continue
            out.append(m)
            if limit is not None and len(out) >= limit:
                break
        return out

    def get_all(self, *, user_id: Optional[str] = None, agent_id: Optional[str] = None, run_id: Optional[str] = None,
                filters: Optional[Dict[str, Any]] = None, limit: int = 100) -> Dict[str, Any]:
        return {"results": [self._result(m) for m in self._all(self._ids(user_id, agent_id, run_id), filters, limit)]}

    def update(self, memory_id: str, data: str) -> Dict[str, Any]:
        if not isinstance(data, str) or not data.strip():
            raise ValueError("data must be non-empty text")
        for attempt in range(3):
            m = self.client.advanced.get(memory_id)
            x = self._owned(m)
            if x is None or m.status != "active":
                raise NotFoundError("not_found", memory_id)
            inp = m.to_input()
            inp["content"] = data
            inp["meta"] = dict(inp["meta"], mem0=dict(x, updated_at=utc_now()))
            kw = {"audience": "tenant"} if m.readers is None else {}
            try:
                self.client.remember(inp, mode="upsert", if_version=m.version,
                                     idempotency_key=f"mem0:{uuid.uuid4().hex}", **kw)
            except VersionConflictError:
                if attempt < 2:
                    continue
                raise
            return {"message": "Memory updated successfully!"}
        raise AssertionError("unreachable")

    def delete(self, memory_id: str) -> Dict[str, Any]:
        m = self.client.advanced.get(memory_id)
        if self._owned(m) is None or m.status != "active":
            raise NotFoundError("not_found", memory_id)
        self.client.forget(memory_id, mode=self.delete_mode, allow_reingest=True, reason="mem0 delete")
        return {"message": "Memory deleted successfully!"}

    def delete_all(self, *, user_id: Optional[str] = None, agent_id: Optional[str] = None,
                   run_id: Optional[str] = None) -> Dict[str, Any]:
        ms = self._all(self._ids(user_id, agent_id, run_id), None, None)
        for i in range(0, len(ms), 100):
            self.client.forget([m.id for m in ms[i:i + 100]], mode=self.delete_mode, allow_reingest=True,
                               reason="mem0 delete_all")
        return {"message": "Memories deleted successfully!"}

    def history(self, memory_id: str) -> List[Dict[str, Any]]:
        m = self.client.advanced.get(memory_id)  # raises NotFoundError when you cannot read it
        x = self._owned(m)
        if x is None:
            raise NotFoundError("not_found", memory_id)
        self.client._degraded("mem0.history", ["mem0:history_without_old_text"])
        events: List[Dict[str, Any]] = []
        for ch in self.client.advanced.changes():
            if ch.get("id") != memory_id:
                continue
            op = str(ch.get("op", ""))
            if op == "remember":
                ev = "ADD" if not events else "UPDATE"
            elif op.startswith("forget"):
                ev = "DELETE"
            else:
                continue
            events.append({"id": f"{memory_id}:{ch.get('cursor')}", "memory_id": memory_id, "old_memory": None,
                           "new_memory": None, "event": ev, "created_at": _utc(ch.get("at")), "updated_at": None,
                           "is_deleted": ev == "DELETE", "actor_id": x.get("actor_id"), "role": x.get("role")})
        last = [e for e in events if e["event"] != "DELETE"]
        if last and m.status == "active":
            last[-1]["new_memory"] = m.content  # the current text is the only text the shim can vouch for
        return events

    def reset(self) -> None:
        raise NotImplementedError("reset is refused: it would wipe every memory this key can reach. Use "
                                  "delete_all(user_id=...) for one user, or the store's own admin tools.")


__all__ = ["Memory", "LEVEL"]
