"""System interface every baseline and the reference engine implement."""
from __future__ import annotations

from dataclasses import dataclass, field

from .data import Corpus, Query, Turn
from .encoders import Encoders


@dataclass
class Item:
    """One unit of returned context. `text` is exactly what would be placed in the
    agent's context window (it is what token budgets are charged for);
    `turn_ids` are the source turns it covers (used to score evidence recall)."""

    turn_ids: list[str]
    text: str
    score: float = 0.0
    meta: dict = field(default_factory=dict)


class System:
    name = "base"
    family = ""
    faithful_notes = ""

    def build(self, corpus: Corpus, enc: Encoders) -> None:
        raise NotImplementedError

    def retrieve(self, q: Query, k: int = 30) -> list[Item]:
        raise NotImplementedError


def fmt_turn(t: Turn, date: bool = True) -> str:
    d = t.ts.strftime("%Y-%m-%d") if date else ""
    return f"[{d}] {t.speaker}: {t.text}" if date else f"{t.speaker}: {t.text}"
