"""Benchmark corpora.

LoCoMo-10 (snap-research/locomo): 10 long conversations, 1,986 QA with evidence turn ids.
LME-R ("LongMemEval oracle, recombined"): LongMemEval questions (oracle file vendored
in getzep/graphiti) with a reconstructed haystack: each question's evidence sessions
plus distractor sessions drawn (seeded) from evidence sessions of OTHER questions in the
SAME split, dated before the question where possible, 40 sessions per question. Dev and
test haystacks are therefore disjoint apart from the few evidence sessions the dataset
itself shares between questions. The official LongMemEval_S haystack is on Hugging
Face, which this sandbox cannot reach; this reconstruction follows the same
recipe (evidence sessions hidden among unrelated user-assistant sessions).
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

VENDOR = os.path.join(os.path.dirname(__file__), "..", "..", "vendor")
LOCOMO = os.path.join(VENDOR, "snap-research_locomo", "data", "locomo10.json")
LME_ORACLE = os.path.join(VENDOR, "getzep_graphiti", "tests", "evals", "data", "longmemeval_data", "longmemeval_oracle.json")


@dataclass
class Turn:
    id: str
    session_id: str
    idx: int  # position in session
    speaker: str  # display name ("Caroline", "user", "assistant")
    role: str  # "user" | "assistant" | speaker name
    text: str
    ts: datetime


@dataclass
class Corpus:
    id: str
    turns: list[Turn]
    session_order: list[str]
    session_ts: dict[str, datetime]
    speakers: list[str] = field(default_factory=list)

    def by_id(self) -> dict[str, Turn]:
        return {t.id: t for t in self.turns}


@dataclass
class Query:
    qid: str
    text: str
    time: datetime
    gold_turns: set[str]
    gold_sessions: set[str]
    category: str
    answer: str = ""
    split: str = "test"


@dataclass
class Task:
    corpus: Corpus
    queries: list[Query]


# ---------------------------------------------------------------- LoCoMo
_LOCOMO_CAT = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop", 5: "adversarial"}


def _parse_locomo_dt(s: str) -> datetime:
    s = s.strip()
    for fmt in ("%I:%M %p on %d %B, %Y", "%I:%M %p on %d %b, %Y", "%I:%M %p on %B %d, %Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise ValueError(s)


def load_locomo(include_adversarial: bool = False, dev_convs: int = 3) -> list[Task]:
    data = json.load(open(LOCOMO))
    tasks = []
    for ci, c in enumerate(data):
        conv = c["conversation"]
        cid = c["sample_id"]
        turns, order, sts = [], [], {}
        n = 1
        while f"session_{n}" in conv:
            sid = f"{cid}:S{n}"
            ts0 = _parse_locomo_dt(conv[f"session_{n}_date_time"])
            sts[sid] = ts0
            order.append(sid)
            for i, t in enumerate(conv[f"session_{n}"]):
                text = t["text"]
                if t.get("blip_caption"):
                    text = f"{text} [shares a photo: {t['blip_caption']}]"
                turns.append(Turn(id=f"{cid}:{t['dia_id']}", session_id=sid, idx=i, speaker=t["speaker"],
                                  role=t["speaker"], text=text, ts=ts0 + timedelta(seconds=30 * i)))
            n += 1
        corpus = Corpus(id=cid, turns=turns, session_order=order, session_ts=sts,
                        speakers=[conv["speaker_a"], conv["speaker_b"]])
        valid = {t.id for t in turns}
        qtime = max(sts.values()) + timedelta(days=1)
        qs = []
        for qi, q in enumerate(c["qa"]):
            cat = _LOCOMO_CAT[q["category"]]
            if cat == "adversarial" and not include_adversarial:
                continue
            ev = set()
            for e in q.get("evidence", []):
                for part in re.split(r"[;,\s]+", str(e)):
                    part = part.strip()
                    if re.fullmatch(r"D\d+:\d+", part) and f"{cid}:{part}" in valid:
                        ev.add(f"{cid}:{part}")
            if not ev:
                continue
            gs = {f"{cid}:S{p.split(':')[1][1:]}" for p in ev}
            ans = q.get("answer", q.get("adversarial_answer", ""))
            qs.append(Query(qid=f"{cid}#{qi}", text=q["question"], time=qtime, gold_turns=ev, gold_sessions=gs,
                            category=cat, answer=str(ans), split="dev" if ci < dev_convs else "test"))
        tasks.append(Task(corpus, qs))
    return tasks


# ---------------------------------------------------------------- LongMemEval
def _parse_lme_dt(s: str) -> datetime:
    return datetime.strptime(s.split(" (")[0] + " " + s.split(") ")[1], "%Y/%m/%d %H:%M")


def _is_dev(qid: str) -> bool:
    return int(hashlib.md5(qid.encode()).hexdigest(), 16) % 5 == 0  # 20% dev


def load_lme(n_sessions: int = 40, seed: int = 7, limit: int | None = None) -> list[Task]:
    data = json.load(open(LME_ORACLE))
    pool: dict[str, tuple[datetime, list[dict]]] = {}
    split_of_session: dict[str, set[str]] = {}
    for q in data:
        sp = "dev" if _is_dev(q["question_id"]) else "test"
        for sid, dt, sess in zip(q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"]):
            pool.setdefault(sid, (_parse_lme_dt(dt), sess))
            split_of_session.setdefault(sid, set()).add(sp)
    # distractors come only from sessions owned exclusively by the question's own split, so dev haystacks never
    # contain test questions' evidence sessions (and vice versa)
    pools = {sp: sorted(sid for sid, sps in split_of_session.items() if sps == {sp}) for sp in ("dev", "test")}
    tasks = []
    for qn, q in enumerate(data):
        if q["question_id"].endswith("_abs"):
            continue
        if limit and len(tasks) >= limit:
            break
        rng = random.Random(f"{seed}:{q['question_id']}")
        qtime = _parse_lme_dt(q["question_date"])
        own = list(q["haystack_session_ids"])
        pool_ids = pools["dev" if _is_dev(q["question_id"]) else "test"]
        cand = [s for s in pool_ids if s not in own and pool[s][0] <= qtime]
        if len(cand) < n_sessions - len(own):
            cand = [s for s in pool_ids if s not in own]
        distract = rng.sample(cand, max(0, n_sessions - len(own)))
        sids = own + distract
        sids.sort(key=lambda s: (pool[s][0], s))
        turns, sts = [], {}
        gold_turns = set()
        for sid in sids:
            ts0, sess = pool[sid]
            usid = f"{q['question_id']}:{sid}"
            sts[usid] = ts0
            for i, t in enumerate(sess):
                tid = f"{usid}:{i}"
                turns.append(Turn(id=tid, session_id=usid, idx=i, speaker=t["role"], role=t["role"],
                                  text=t["content"], ts=ts0 + timedelta(seconds=60 * i)))
                if sid in q["answer_session_ids"] and t.get("has_answer"):
                    gold_turns.add(tid)
        corpus = Corpus(id=q["question_id"], turns=turns, session_order=[f"{q['question_id']}:{s}" for s in sids],
                        session_ts=sts, speakers=["user", "assistant"])
        gs = {f"{q['question_id']}:{s}" for s in q["answer_session_ids"]}
        tasks.append(Task(corpus, [Query(qid=q["question_id"], text=q["question"], time=qtime, gold_turns=gold_turns,
                                         gold_sessions=gs, category=q["question_type"], answer=str(q["answer"]),
                                         split="dev" if _is_dev(q["question_id"]) else "test")]))
    return tasks


def all_texts() -> list[str]:
    """Union of benchmark text used to train the shared dense encoder."""
    out = []
    for t in load_locomo(include_adversarial=True):
        out += [x.text for x in t.corpus.turns]
        out += [q.text for q in t.queries]
    data = json.load(open(LME_ORACLE))
    seen = set()
    for q in data:
        out.append(q["question"])
        for sid, sess in zip(q["haystack_session_ids"], q["haystack_sessions"]):
            if sid in seen:
                continue
            seen.add(sid)
            out += [t["content"] for t in sess]
    return out
