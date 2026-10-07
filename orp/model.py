"""ORP data model: the records every conforming implementation exchanges.

See spec/ORP-0.1.md for normative text. Field names here match the wire format.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

KINDS = ("event", "document", "fact", "preference", "procedure", "observation", "summary", "profile", "entity")
ORIGINS = ("observed", "extracted", "inferred", "human", "imported", "seed")
STATUSES = ("active", "superseded", "retracted", "draft", "expired")


def new_id() -> str:
    return "mem_" + uuid.uuid4().hex[:24]


@dataclass(frozen=True)
class Principal:
    """Who is asking. `id` plus every group/role/channel the caller belongs to."""

    id: str
    groups: frozenset[str] = frozenset()
    tenant: str = "default"

    def tokens(self) -> frozenset[str]:
        return frozenset({f"principal:{self.id}", "public", *(f"group:{g}" for g in self.groups),
                          *(g for g in self.groups if g.startswith("label:"))})


SYSTEM = Principal("system", frozenset({"*"}))


@dataclass
class Claim:
    """Optional structured form of a fact: subject, predicate, object.
    cardinality "one" means a newer valid claim with the same (subject, predicate) supersedes the old one."""

    subject: str
    predicate: str
    object: str
    cardinality: str = "one"

    def key(self) -> str:
        return f"{self.subject.lower().strip()}|{self.predicate.lower().strip()}"


@dataclass
class Source:
    uri: str  # e.g. slack://C123/p1712345, file://notes/a.md#L10-14, turn://conv/S1/3
    span: str = ""
    hash: str = ""


@dataclass
class Memory:
    content: str
    kind: str = "event"
    id: str = field(default_factory=new_id)
    tenant: str = "default"
    scope: str = ""  # hierarchical path, e.g. "org/acme/team/sales/user/u7"
    readers: frozenset[str] | None = None  # access tokens; None = public within tenant
    writers: frozenset[str] | None = None  # tokens allowed to modify/forget; None = creator only
    origin: str = "observed"  # observed | extracted | inferred | human | imported | seed
    load: str = "on_query"  # always | on_scope | on_query
    version: int = 1
    support: int = 1
    actor: str = ""  # who said/asserted it (speaker, agent id, connector)
    role: str = ""  # user | assistant | system | tool | human-name
    thread: str = ""  # conversation/session/document id
    seq: int = 0  # position inside thread
    observed_at: datetime | None = None  # transaction time: when the system learned it
    event_time: datetime | None = None  # when the described thing happened (if known)
    valid_from: datetime | None = None  # validity window of the assertion (world time)
    valid_to: datetime | None = None
    expires_at: datetime | None = None  # retention / TTL
    status: str = "active"
    supersedes: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    claim: Claim | None = None
    sources: list[Source] = field(default_factory=list)
    derived_from: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)  # sensitivity / topical labels
    confidence: float = 1.0
    meta: dict[str, Any] = field(default_factory=dict)

    def content_hash(self) -> str:
        return hashlib.sha256(f"{self.tenant}|{self.thread}|{self.actor}|{self.content}".encode()).hexdigest()[:32]

    def to_json(self) -> dict:
        d = asdict(self)
        d["readers"] = sorted(self.readers) if self.readers is not None else None
        d["writers"] = sorted(self.writers) if self.writers is not None else None
        for k in ("observed_at", "event_time", "valid_from", "valid_to", "expires_at"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        return d

    @staticmethod
    def from_json(d: dict) -> "Memory":
        d = dict(d)
        for k in ("observed_at", "event_time", "valid_from", "valid_to", "expires_at"):
            if d.get(k):
                d[k] = datetime.fromisoformat(d[k])
        for k in ("readers", "writers"):
            if d.get(k) is not None:
                d[k] = frozenset(d[k])
        if d.get("claim"):
            d["claim"] = Claim(**d["claim"])
        d["sources"] = [Source(**s) for s in d.get("sources", [])]
        return Memory(**d)


@dataclass
class RecallRequest:
    query: str
    principal: Principal = SYSTEM
    tenant: str = "default"
    as_of: datetime | None = None  # world time to evaluate validity at (default: now)
    known_at: datetime | None = None  # transaction time cut-off (what did we know then?)
    scope: str = ""  # restrict to a scope subtree
    kinds: tuple[str, ...] | None = None
    budget_tokens: int | None = 2000
    max_items: int = 50
    include_history: bool = False  # also return superseded versions, labelled
    mode: str = "balanced"  # fast | balanced | deep
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContextItem:
    memory_ids: list[str]
    text: str
    score: float
    why: dict[str, float]  # per-channel contribution
    tokens: int
    citations: list[str]
    valid: str = "current"  # current | superseded | historical
    turn_ids: list[str] = field(default_factory=list)  # benchmark adapter only


@dataclass
class ContextPack:
    items: list[ContextItem]
    tokens: int
    budget: int | None
    coverage: float  # 0..1 estimate that memory contains an answer
    snapshot: str  # opaque token: same request + snapshot -> same pack
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        return "\n".join(i.text for i in self.items)

    def to_json(self) -> dict:
        return json.loads(json.dumps(asdict(self), default=str))
