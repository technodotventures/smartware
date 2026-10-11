"""One clock for the engine: naive datetimes in UTC.

ORP stores naive timestamps. They are UTC by convention, so a server's local timezone never shifts validity,
as_of or retention. Inputs with an offset are converted to UTC before the offset is dropped.
"""
from __future__ import annotations

from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse(v: str) -> datetime:
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


_EPOCH = datetime(1970, 1, 1)


def epoch(d: datetime) -> float:
    """Seconds since 1970-01-01 for a naive UTC datetime (datetime.timestamp() would assume local time)."""
    return (d - _EPOCH).total_seconds()
