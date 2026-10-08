"""LLM-extraction track: every system stores the same LLM-extracted facts.

The facts were extracted once by Claude Haiku sub-agents following bench/extraction/EXTRACT.md (modelled on mem0's
ADDITIVE_EXTRACTION_PROMPT) and frozen in results/extraction/out/*.jsonl, so this track is reproducible without a
model. Like the shared encoders, one shared extraction keeps the comparison about memory architecture and retrieval,
not about which project has the better extraction prompt.

  "<system>+facts"      the system's own recall path over a store of facts instead of raw turns (what mem0,
                        Hindsight, LangMem, MemOS and similar store after LLM extraction). Each fact claims the turns
                        it cites, so evidence recall stays comparable.
  "orp+evidence"        ORP as the protocol intends: raw turns stay as evidence and the facts are stored as derived
                        memories (kind fact, derived_from the cited turns, provenance-closed).
"""
from __future__ import annotations

import glob
import json
import os
from collections import defaultdict

from .data import Corpus, Turn
from .system import Item

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "results", "extraction", "out")
_FACTS: dict[str, list[dict]] | None = None


def load_facts() -> dict[str, list[dict]]:
    """key -> facts [{text, turn_ids, session}], deduplicated by text, in file order. LoCoMo facts are keyed by
    conversation id; LME-R facts by session id (sessions recur across haystacks, so they are extracted once)."""
    global _FACTS
    if _FACTS is None:
        _FACTS = defaultdict(list)
        seen = defaultdict(set)
        for f in sorted(glob.glob(os.path.join(OUT, "*.jsonl"))):
            lme = os.path.basename(f).startswith("lme_")
            for line in open(f):
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                tids = [t for t in r.get("turn_ids", []) if isinstance(t, str)]
                if not tids or not r.get("text"):
                    continue
                if os.path.basename(f).startswith("hotpot_"):
                    key = "hotpot50"
                else:
                    key = tids[0].rsplit(":", 1)[0] if lme else tids[0].split(":")[0]
                if r["text"] in seen[key]:
                    continue
                seen[key].add(r["text"])
                _FACTS[key].append({"text": r["text"].strip(), "turn_ids": tids, "session": r.get("session")})
    return _FACTS


def facts_for(corpus: Corpus) -> list[tuple[str, list[str]]]:
    """(fact text, cited turn ids in this corpus) for every fact about this corpus's sessions."""
    F = load_facts()
    if corpus.id in F:
        return [(f["text"], f["turn_ids"]) for f in F[corpus.id]]
    out = []
    for usid in corpus.session_order:  # LME-R: "<question id>:<session id>"
        sid = usid.split(":", 1)[1] if ":" in usid else usid
        for f in F.get(sid, []):
            out.append((f["text"], [f"{corpus.id}:{t}" for t in f["turn_ids"]]))
    return out


def fact_corpus(corpus: Corpus) -> tuple[Corpus, dict[str, list[str]]]:
    """A corpus whose units are the extracted facts; returns it and fact id -> cited turn ids."""
    by_id = corpus.by_id()
    per_sess = defaultdict(list)
    cites = {}
    for i, (text, cited) in enumerate(facts_for(corpus)):
        tids = [t for t in cited if t in by_id]
        if not tids:
            continue
        first = min((by_id[t] for t in tids), key=lambda t: t.idx)
        per_sess[first.session_id].append((first.idx, i, first, tids, text))
    turns = []
    for sid in corpus.session_order:
        for k, (_, i, first, tids, text) in enumerate(sorted(per_sess.get(sid, []), key=lambda x: (x[0], x[1]))):
            fid = f"{corpus.id}#F{i}"
            cites[fid] = tids
            turns.append(Turn(id=fid, session_id=sid, idx=k, speaker=first.speaker, role=first.role, text=text,
                              ts=first.ts))
    return Corpus(id=corpus.id, turns=turns, session_order=list(corpus.session_order),
                  session_ts=dict(corpus.session_ts), speakers=list(corpus.speakers)), cites


class FactStore:
    """Wraps any system so that it stores and recalls the extracted facts instead of raw turns."""

    def __init__(self, base):
        self.base = base
        self.name = getattr(base, "name", "system") + "+facts"

    def build(self, corpus, enc):
        fc, self.cites = fact_corpus(corpus)
        self.base.build(fc, enc)

    def retrieve(self, q, k=30):
        out = []
        for it in self.base.retrieve(q, k=k):
            tids = list(dict.fromkeys(t for f in it.turn_ids for t in self.cites.get(f, [f])))
            out.append(Item(tids, it.text, it.score, it.meta))
        return out


def orp_with_evidence(**kw):
    """ORP storing raw turns as evidence and the facts as derived memories (spec §3, §6.3)."""
    from orp.engine import ORPSystem
    from orp.model import Claim, Memory  # noqa: F401
    from .orp_variants import _load

    class ORPEvidence(ORPSystem):
        name = "orp+evidence"

        def build(self, corpus, enc):
            super().build(corpus, enc)
            eng = self.eng
            mid_of = {m.meta["turn_id"]: m.id for m in eng.mem.values()}
            by_id = corpus.by_id()
            label = {s: f"S{i + 1}" for i, s in enumerate(corpus.session_order)}
            mems = []
            for i, (text, cited) in enumerate(facts_for(corpus)):
                tids = [t for t in cited if t in by_id]
                if not tids:
                    continue
                first = min((by_id[t] for t in tids), key=lambda t: t.idx)
                mems.append(Memory(content=text, kind="fact", origin="extracted", actor="",
                                   thread=first.session_id + "#facts", seq=first.idx * 1000 + i,
                                   observed_at=max(by_id[t].ts for t in tids),
                                   derived_from=[mid_of[t] for t in tids],
                                   meta={"turn_ids": tids, "idempotency_key": f"{corpus.id}#F{i}",
                                         "thread_label": label.get(first.session_id, "") + " memories"}))
            eng.remember(mems)
            eng._build()

    params = {**_load(kw.pop("_params", "orp_params.json")), **kw}
    return ORPEvidence(**params)
