"""Response types for the Smartware HTTP binding (spec 0.1).

Each type keeps the fields the schema pack names and also the full response in ``raw``, so a newer server's extra
fields are never lost. ``degraded`` is a typed field on every response: the client also reports it through the
``on_degraded`` hook, so nothing is swallowed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

Json = Dict[str, Any]

#: meta keys the engine derives from content on write
ENGINE_DERIVED_META = frozenset({"entities", "time_refs"})


@dataclass
class ContextItem:
    memory_ids: List[str]
    text: str
    tokens: int
    score: float
    why: Json
    validity: str
    citations: List[str]
    ref: str = ""
    kind: str = ""
    scope: str = ""
    superseded: List[str] = field(default_factory=list)
    raw: Json = field(default_factory=dict, repr=False)

    @property
    def id(self) -> str:
        """The first memory id of this item (the record it is anchored on)."""
        return self.memory_ids[0] if self.memory_ids else ""

    @classmethod
    def from_json(cls, d: Json) -> "ContextItem":
        return cls(memory_ids=list(d.get("memory_ids") or []), text=d.get("text", ""), tokens=int(d.get("tokens", 0)),
                   score=float(d.get("score", 0.0)), why=dict(d.get("why") or {}), validity=d.get("validity", ""),
                   citations=list(d.get("citations") or []), ref=d.get("ref", ""), kind=d.get("kind", ""),
                   scope=d.get("scope", ""), superseded=list(d.get("superseded") or []), raw=d)


@dataclass
class ContextPack:
    """The result of ``recall``: a budgeted, cited pack of context items."""
    items: List[ContextItem]
    tokens: int
    budget: Json
    coverage: float
    snapshot: str
    degraded: List[str]
    request_id: str
    abstain_hint: bool = False
    notes: List[str] = field(default_factory=list)
    tokenizer: str = ""
    raw: Json = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: Json) -> "ContextPack":
        return cls(items=[ContextItem.from_json(x) for x in d.get("items") or []], tokens=int(d.get("tokens", 0)),
                   budget=dict(d.get("budget") or {}), coverage=float(d.get("coverage", 0.0)),
                   snapshot=str(d.get("snapshot", "")), degraded=list(d.get("degraded") or []),
                   request_id=d.get("request_id", ""), abstain_hint=bool(d.get("abstain_hint", False)),
                   notes=list(d.get("notes") or []), tokenizer=d.get("tokenizer", ""), raw=d)

    @property
    def ids(self) -> List[str]:
        return [i.id for i in self.items]


@dataclass
class Memory:
    """A stored record as the server shows it to you (links to records you cannot read are already dropped)."""
    id: str
    tenant: str
    kind: str
    content: str
    readers: Optional[List[str]]
    origin: str
    observed_at: Optional[str]
    status: str
    version: int
    scope: str = ""
    writers: Optional[List[str]] = None
    actor: str = ""
    role: str = ""
    thread: str = ""
    seq: int = 0
    load: str = "on_query"
    claim: Optional[Json] = None
    event_time: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    expires_at: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    meta: Json = field(default_factory=dict)
    sources: List[Json] = field(default_factory=list)
    derived_from: List[str] = field(default_factory=list)
    supersedes: List[str] = field(default_factory=list)
    superseded_by: Optional[str] = None
    written_by: Optional[str] = None
    confidence: float = 1.0
    support: int = 1
    raw: Json = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: Json) -> "Memory":
        return cls(
            id=d["id"], tenant=d.get("tenant", ""), kind=d.get("kind", ""), content=d.get("content", ""),
            readers=list(d["readers"]) if d.get("readers") is not None else None, origin=d.get("origin", ""),
            observed_at=d.get("observed_at"), status=d.get("status", ""), version=int(d.get("version", 0)),
            scope=d.get("scope", ""), writers=list(d["writers"]) if d.get("writers") is not None else None,
            actor=d.get("actor", ""), role=d.get("role", ""), thread=d.get("thread", ""), seq=int(d.get("seq", 0)),
            load=d.get("load", "on_query"), claim=d.get("claim"), event_time=d.get("event_time"),
            valid_from=d.get("valid_from"), valid_to=d.get("valid_to"), expires_at=d.get("expires_at"),
            labels=list(d.get("labels") or []), meta=dict(d.get("meta") or {}), sources=list(d.get("sources") or []),
            derived_from=list(d.get("derived_from") or []), supersedes=list(d.get("supersedes") or []),
            superseded_by=d.get("superseded_by"), written_by=d.get("written_by"),
            confidence=float(d.get("confidence", 1.0)), support=int(d.get("support", 1)), raw=d)

    def to_input(self) -> Json:
        """The record as a ``MemoryInput`` (the fields a client may write back, for an upsert).

        ``supersedes`` is left out on purpose: it only means something in supersede mode.
        """
        # entities and time_refs are derived by the engine from the content; leave them out so a changed content
        # gets fresh ones instead of the old record's
        meta = {k: v for k, v in self.meta.items() if k not in ENGINE_DERIVED_META}
        out: Json = {"id": self.id, "kind": self.kind, "content": self.content, "scope": self.scope,
                     "readers": self.readers, "writers": self.writers, "actor": self.actor, "origin": self.origin,
                     "thread": self.thread, "seq": self.seq, "load": self.load, "labels": list(self.labels),
                     "meta": meta, "sources": [dict(s) for s in self.sources],
                     "derived_from": list(self.derived_from), "confidence": self.confidence, "support": self.support}
        if self.role:
            out["role"] = self.role
        if self.claim:
            out["claim"] = dict(self.claim)
        for k in ("event_time", "valid_from", "valid_to", "expires_at"):
            v = getattr(self, k)
            if v is not None:
                out[k] = v
        return out


@dataclass
class RememberResult:
    ids: List[str]
    replayed: bool
    stage: str
    degraded: List[str]
    request_id: str
    #: positions whose returned id differs from the id you asked for: the server matched an existing identical
    #: record (same writer, readers, labels, thread, actor and content) instead of writing a new one
    deduplicated: List[int] = field(default_factory=list)
    raw: Json = field(default_factory=dict, repr=False)

    @property
    def id(self) -> str:
        return self.ids[0] if self.ids else ""


@dataclass
class ForgetResult:
    dry_run: bool
    affected: List[str]
    degraded: List[str]
    request_id: str
    raw: Json = field(default_factory=dict, repr=False)


@dataclass
class ProfileItem:
    id: str
    kind: str
    text: str


@dataclass
class Profile:
    items: List[ProfileItem]
    tokens: int
    budget: Json
    degraded: List[str]
    request_id: str
    raw: Json = field(default_factory=dict, repr=False)

    @property
    def text(self) -> str:
        return "\n".join(i.text for i in self.items)


@dataclass
class Whoami:
    id: str
    tenant: str
    groups: List[str]
    tokens: List[str]
