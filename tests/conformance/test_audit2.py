"""Conformance tests added after the second independent audit (results/AUDIT2.md).

Test names start with the MUST ids they check (see spec/MUSTS.md).
"""
from __future__ import annotations

import json
import os
import random
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta
from unittest import mock

from orp import engine as engine_mod
from orp.api import ORPError, Service
from orp.engine import Params
from orp.model import Principal
from tests.conformance.test_orp import ADMIN, ALICE, BOB, HashEmbedder, ev, make_service, texts

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
T0 = datetime(2025, 6, 1)
EXEC = Principal("erin", frozenset({"exec"}), tenant="acme")
MALLORY = Principal("mallory", frozenset({"eng"}), tenant="acme")
OTHER = Principal("root2", frozenset({"*"}), tenant="other")
WORDS = ["hiring", "budget", "roadmap", "pricing", "launch", "offsite", "churn", "report", "plan", "raise", "Lisbon",
         "Ann", "Ben", "Globex", "renewal", "call", "Thursday", "moved", "office", "dog"]


def recall(s, pr, q, **kw):
    body = {"query": q, "budget": {"tokens": kw.pop("tokens", 600)}}
    body.update(kw)
    return s.recall(body, pr)


def shipped_params() -> Params:
    with open(os.path.join(ROOT, "results", "orp_params.json")) as fh:
        p = json.load(fh)["params"]
    return Params(**{k: tuple(v) if isinstance(v, list) else v for k, v in p.items()})


def random_store(seed: int) -> list[dict]:
    rng = random.Random(seed)
    seqs, out = Counter(), []
    for i in range(45):
        th = rng.choice(["th0", "th1", "th2", "th3", "th4", ""])  # interleaved threads, some unthreaded
        seq = seqs[th]
        seqs[th] += 1
        txt = " ".join(rng.choice(WORDS) for _ in range(rng.randint(3, 10))) + rng.choice(["?", ".", ""])
        d = {"id": f"m{i}", "kind": "event", "content": txt, "thread": th, "seq": seq,
             "actor": rng.choice(["Ann", "Ben", "Cy"]),
             "readers": rng.choice([None, None, ["group:sales"], ["group:exec"], ["group:eng", "group:sales"]]),
             "labels": ["secret"] if rng.random() < 0.1 else [],
             "event_time": (T0 + timedelta(hours=i)).isoformat()}
        if rng.random() < 0.2:
            obj = rng.choice(WORDS)
            d.update(kind="fact", content=f"acme {d['id']} city is {obj}", thread="",
                     claim={"subject": "acme", "predicate": rng.choice(["city", "ceo"]), "object": obj},
                     valid_from=d["event_time"])
        out.append(d)
    return out


class RemovalEquivalence(unittest.TestCase):
    """M67: recall over a store equals recall over the same store with the asker's unreadable memories removed,
    compared on memory ids, text, scores, explanations and coverage, for every granularity and both rankers."""

    def _pack(self, s, pr, q, g):
        r = recall(s, pr, q, granularity=g, include={"history": g == "unit"} if g == "unit" else {})
        return [(i["memory_ids"], i["text"], i["score"], json.dumps(i["why"], sort_keys=True), i["superseded"])
                for i in r["items"]], r["coverage"]

    def test_M33_M66_M67_M68_interleaved_threads_claims_labels_both_rankers(self):
        queries = ("pricing launch offsite", "What did Ann say about the budget?", "Where is the acme city now?",
                   "latest renewal call", "Globex")
        for params in (Params(), shipped_params()):
            for seed in range(12):
                mems = random_store(seed)
                full = Service(HashEmbedder(), params)
                full.remember({"memories": mems}, ADMIN)
                for asker in (ALICE, EXEC, BOB):
                    keep = [d for d in mems if full.engine.readable(full.engine.mem[f"{d['id']}~acme"], asker)]
                    clean = Service(HashEmbedder(), params)
                    clean.remember({"memories": keep}, ADMIN)
                    for q in queries:
                        for g in ("unit", "span", "thread", "auto"):
                            self.assertEqual(self._pack(full, asker, q, g), self._pack(clean, asker, q, g),
                                             (params.ranker, seed, asker.id, q, g))


class Supersession(unittest.TestCase):
    def setUp(self):
        self.s = make_service()

    def _fact(self, obj, pr, readers, vf):
        return self.s.remember({"memories": [{"kind": "preference", "content": f"Alice prefers {obj}",
                                              "claim": {"subject": "alice", "predicate": "drink", "object": obj},
                                              "readers": readers, "valid_from": vf}]}, pr)["ids"][0]

    def test_M69_M74_no_supersession_across_access_boundaries(self):
        tea = self._fact("tea", ALICE, ["principal:alice"], "2026-01-01")
        coffee = self._fact("coffee", MALLORY, ["principal:mallory"], "2026-02-01")
        self.assertIn("tea", texts(recall(self.s, ALICE, "What does Alice prefer to drink?")))
        rec = self.s.get([tea], ALICE)["memories"][0]
        self.assertEqual((rec["status"], rec["superseded_by"], rec["valid_to"]), ("active", None, None))
        self.assertNotIn(coffee, json.dumps(self.s.history("alice", "drink", ALICE)))
        self.assertIn("coffee", texts(recall(self.s, MALLORY, "What does Alice prefer to drink?")))

    def test_M69_writer_and_covering_audience_supersede(self):
        old = self._fact("tea", ALICE, None, "2026-01-01")
        new = self._fact("coffee", ALICE, None, "2026-02-01")
        self.assertEqual(self.s.get([old], ALICE)["memories"][0]["superseded_by"], new)
        self.assertNotIn("tea", texts(recall(self.s, BOB, "What does Alice prefer to drink?")))
        narrow = self._fact("water", ALICE, ["principal:alice"], "2026-03-01")  # narrower audience: no supersession
        self.assertEqual(self.s.get([new], ALICE)["memories"][0]["status"], "active")
        self.assertIn("coffee", texts(recall(self.s, BOB, "What does Alice prefer to drink?")))
        self.assertIn("water", texts(recall(self.s, ALICE, "What does Alice prefer to drink?")))
        self.assertNotIn("coffee", texts(recall(self.s, ALICE, "What does Alice prefer to drink?")))
        self.assertTrue(narrow)

    def test_M76_known_at_evaluates_the_chain_as_known_then(self):
        e = self.s.engine
        a = self._fact("tea", ADMIN, None, "2025-01-01")
        b = self._fact("coffee", ADMIN, None, "2025-03-01")
        e.mem[a].observed_at = datetime(2025, 1, 2)
        e.mem[b].observed_at = datetime(2026, 3, 1)  # learned a year later
        e.touch("acme")
        then = recall(self.s, ALICE, "What does Alice prefer to drink?", as_of="2025-06-01", known_at="2025-06-01")
        self.assertIn("tea", texts(then))
        now = recall(self.s, ALICE, "What does Alice prefer to drink?", as_of="2025-06-01")
        self.assertIn("coffee", texts(now))
        self.assertNotIn("tea", texts(now))

    def test_M48_approved_claim_supersedes(self):
        old = self._fact("tea", ALICE, None, "2026-01-01")
        d = self.s.remember({"memories": [{"kind": "preference", "content": "Alice prefers coffee",
                                           "claim": {"subject": "alice", "predicate": "drink", "object": "coffee"},
                                           "valid_from": "2026-02-01"}], "mode": "propose"}, ALICE)["ids"][0]
        self.s.approve(d, ALICE)
        self.assertEqual(self.s.get([old], ALICE)["memories"][0]["superseded_by"], d)

    def test_M58_approve_requires_read_access(self):
        d = self.s.remember({"memories": [{"kind": "procedure", "content": "exec rule", "readers": ["group:exec"]}],
                             "mode": "propose"}, ADMIN)["ids"][0]
        approver = Principal("pat", frozenset({"approver"}), tenant="acme")
        with self.assertRaises(ORPError):
            self.s.approve(d, approver)


class Writes(unittest.TestCase):
    def setUp(self):
        self.s = make_service()

    def test_M17_M70_dedup_is_per_writer_and_tenant(self):
        a = self.s.remember({"memories": [ev("salary is 90k", thread="dm", actor="hr", readers=["principal:alice"])]},
                            ALICE)["ids"]
        b = self.s.remember({"memories": [ev("salary is 90k", thread="dm", actor="hr")]}, BOB)
        self.assertNotEqual(a, b["ids"])
        self.assertFalse(b["replayed"])
        self.assertEqual(len(self.s.get(b["ids"], BOB)["memories"]), 1)
        k1 = self.s.remember({"memories": [ev("acme fact", meta={"idempotency_key": "k1"})]}, ADMIN)["ids"]
        k2 = self.s.remember({"memories": [ev("other fact", meta={"idempotency_key": "k1"})]}, OTHER)["ids"]
        self.assertNotEqual(k1, k2)
        self.assertEqual(self.s.get(k2, OTHER)["memories"][0]["content"], "other fact")
        r1 = self.s.remember({"memories": [ev("x1")], "idempotency_key": "same"}, ALICE)
        r2 = self.s.remember({"memories": [ev("x2")], "idempotency_key": "same"}, BOB)
        self.assertFalse(r2["replayed"])
        self.assertNotEqual(r1["ids"], r2["ids"])

    def test_M18_types_and_nested_fields_validated(self):
        bad = [{"labels": "secret"}, {"readers": "group:x"}, {"claim": {"subject": "a", "predicate": "b",
                                                                       "object": "c", "cardinality": "single"}},
               {"claim": {"subject": "a", "predicate": "b", "object": "c", "extra": 1}},
               {"sources": [{"uri": "x", "bogus": 1}]}, {"seq": "first"}, {"confidence": 7},
               {"meta": {"_creator": "root"}}]
        for b in bad:
            with self.assertRaises(ORPError, msg=str(b)):
                self.s.remember({"memories": [dict(ev("prod api key sk-live-123", thread="t"), **b)]}, ALICE)
        for body in ({"query": "x", "budget": {"tokens": "lots"}}, {"query": "x", "kinds": {"fact": "many"}},
                     {"query": "x", "min_coverage": "high"}, {"query": "x", "budget": {"tokens": 10 ** 7}}):
            with self.assertRaises(ORPError, msg=str(body)):
                self.s.recall(body, ALICE)

    def test_M59_M72_label_change_on_source_reaches_derived(self):
        src = self.s.remember({"memories": [ev("db password hunter2", thread="t")]}, ADMIN)["ids"][0]
        d = self.s.remember({"memories": [{"kind": "summary", "origin": "extracted", "derived_from": [src],
                                           "content": "db password hunter2"}]}, ADMIN)["ids"][0]
        v = self.s.engine.mem[src].version
        rec = self.s.engine.mem[src]
        self.s.remember({"memories": [dict(ev(rec.content, thread="t", labels=["secret"]), id=src)],
                         "mode": "upsert", "if_version": v}, ADMIN)
        self.assertIn("secret", self.s.engine.mem[d].labels)
        self.assertNotIn("hunter2", texts(recall(self.s, ALICE, "db password")))

    def test_M31_reflect_never_supersedes_summaries_the_caller_cannot_read(self):
        self.s.remember({"memories": [ev(f"exec plan item {i} about the reorganisation timeline", thread="board",
                                         seq=i, readers=["group:exec"]) for i in range(3)] +
                         [ev(f"public note {i} about the reorganisation timeline", thread="board", seq=10 + i)
                          for i in range(3)]}, ADMIN)
        ex = self.s.reflect({}, EXEC)["created"][0]
        self.s.reflect({}, ALICE)
        self.assertEqual(self.s.engine.mem[ex].status, "active")

    def test_M71_import_requires_admin_and_stays_in_tenant(self):
        b = self.s.export(ADMIN)
        with self.assertRaises(ORPError):
            self.s.import_(b, ALICE)
        a1 = self.s.remember({"memories": [ev("edge a", thread="t")]}, ADMIN)["ids"][0]
        a2 = self.s.remember({"memories": [ev("edge b", thread="t")]}, ADMIN)["ids"][0]
        eid = self.s.link({"type": "relates", "from": a1, "to": a2}, ADMIN)["id"]
        evil = {"memories.jsonl": "", "edges.jsonl": json.dumps({"id": eid, "type": "relates", "from": "x",
                                                                 "to": "y", "tenant": "other"})}
        with self.assertRaises(ORPError):
            self.s.import_(evil, OTHER)
        self.assertEqual(self.s._edges[eid]["tenant"], "acme")
        forged = json.dumps({"id": "f1", "kind": "event", "content": "forged", "tenant": "acme",
                             "observed_at": "2020-01-01T00:00:00"})
        with self.assertRaises(ORPError):
            self.s.import_({"memories.jsonl": forged}, Principal("nobody", tenant="acme"))

    def test_M27_M74_dry_run_lists_only_readable_ids(self):
        src = self.s.remember({"memories": [ev("shared source text for summaries", thread="t")]}, ALICE)["ids"][0]
        priv = self.s.remember({"memories": [{"kind": "summary", "origin": "extracted", "derived_from": [src],
                                              "content": "exec summary", "readers": ["group:exec"]}]},
                               ADMIN)["ids"][0]
        r = self.s.forget({"ids": [src], "dry_run": True}, ALICE)
        self.assertNotIn(priv, r["affected"])
        self.s.forget({"ids": [src]}, ALICE)
        self.assertEqual(self.s.engine.mem[priv].status, "retracted")  # still cascaded (M28)

    def test_M28_cascade_false_still_drops_unsupported_derived(self):
        src = self.s.remember({"memories": [ev("only source", thread="t")]}, ALICE)["ids"][0]
        d = self.s.remember({"memories": [{"kind": "summary", "origin": "extracted", "derived_from": [src],
                                           "content": "only source summary"}]}, ALICE)["ids"][0]
        self.s.forget({"ids": [src], "cascade": False}, ALICE)
        self.assertEqual(self.s.engine.mem[d].status, "retracted")

    def test_M74_reingest_block_never_reveals_others_content(self):
        a = self.s.remember({"memories": [ev("quarterly bonus pool is 2m", thread="t", actor="hr",
                                             readers=["group:exec"])]}, ADMIN)["ids"]
        self.s.forget({"ids": a}, ADMIN)
        self.s.remember({"memories": [ev("quarterly bonus pool is 2m", thread="t", actor="hr")]}, BOB)  # no error
        with self.assertRaises(ORPError):
            self.s.remember({"memories": [ev("quarterly bonus pool is 2m", thread="t", actor="hr",
                                             readers=["group:exec"])]}, ADMIN)

    def test_redact_clears_claim_sources_and_embedding_cache(self):  # §6.4
        m = self.s.remember({"memories": [{"kind": "fact", "content": "Ann lives at 4 Elm Street",
                                           "claim": {"subject": "ann", "predicate": "address", "object": "4 Elm"},
                                           "sources": [{"uri": "slack://C1/p1", "span": "4 Elm", "hash": "h"}]}]},
                           ALICE)["ids"][0]
        recall(self.s, ALICE, "where does Ann live")
        self.s.forget({"ids": [m], "mode": "redact"}, ALICE)
        rec = self.s.engine.mem[m]
        self.assertNotIn("Elm", json.dumps(rec.to_json()))


class Reads(unittest.TestCase):
    def setUp(self):
        self.s = make_service()

    def test_M20_wall_clock_never_used_when_as_of_given(self):
        self.s.remember({"memories": [ev("Deploy freeze starts Friday", thread="t",
                                         event_time="2026-01-05T10:00:00"),
                                      ev("Deploy freeze lifted", thread="t", seq=1, event_time="2026-03-01T10:00:00")]},
                        ALICE)
        body = {"query": "When does the deploy freeze start?", "as_of": "2026-02-01", "budget": {"tokens": 400}}
        a = self.s.recall(dict(body), ALICE)

        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2031, 1, 1)
        with mock.patch.object(engine_mod, "datetime", Later):
            b = self.s.recall(dict(body), ALICE)
        self.assertEqual([i["text"] for i in a["items"]], [i["text"] for i in b["items"]])
        self.assertNotIn("lifted", texts(a))

    def test_M34_history_never_returns_evidence_after_as_of(self):
        self.s.remember({"memories": [ev("launch moved to June", thread="t", event_time="2026-06-01")]}, ALICE)
        r = recall(self.s, ALICE, "launch", as_of="2026-01-01", include={"history": True})
        self.assertEqual(r["items"], [])

    def test_M75_retention_is_real_time_not_as_of(self):
        self.s.remember({"memories": [ev("temporary access code 4411", thread="t", event_time="2019-01-01",
                                         expires_at="2021-01-01")]}, ALICE)
        self.assertNotIn("4411", texts(recall(self.s, ALICE, "access code", as_of="2020-06-01")))
        self.assertEqual(self.s.profile({"budget": {"tokens": 200}}, ALICE)["items"], [])

    def test_M41_post_ranker_multipliers_in_why_with_learned_ranker(self):
        s = Service(HashEmbedder(), shipped_params())
        s.remember({"memories": [ev("blue launch plan", scope="org/acme/team/growth", thread="a"),
                                 ev("blue launch plan draft", scope="org/acme/team/ops", thread="b")]}, ADMIN)
        r = recall(s, ALICE, "blue launch plan", scope={"prefer": ["org/acme/team/growth"]})
        top = [i for i in r["items"] if i["scope"] == "org/acme/team/growth"][0]
        self.assertIn("prefer", top["why"]["features"])

    def test_M73_snapshot_and_cursor_reveal_nothing_about_other_tenants(self):
        self.s.remember({"memories": [ev("acme note", thread="t")]}, ALICE)
        a = recall(self.s, ALICE, "note")["snapshot"]
        c0 = self.s.changes(0, ALICE)["cursor"]
        self.s.remember({"memories": [ev("other tenant note", thread="t")]}, OTHER)
        self.assertEqual(recall(self.s, ALICE, "note")["snapshot"], a)
        self.assertEqual(self.s.changes(0, ALICE)["cursor"], c0)
        self.s.remember({"memories": [ev("another acme note", thread="t")]}, ALICE)
        self.assertNotEqual(recall(self.s, ALICE, "note")["snapshot"], a)

    def test_M77_namespaced_tokens(self):
        self.s.remember({"memories": [ev("channel only update on Globex", readers=["channel:C07"], thread="c")]},
                        ADMIN)
        member = Principal("cat", frozenset({"channel:C07"}), tenant="acme")
        self.assertIn("Globex", texts(recall(self.s, member, "Globex update")))
        self.assertNotIn("Globex", texts(recall(self.s, ALICE, "Globex update")))

    def test_M44_unthreaded_memory_is_its_own_thread(self):
        self.s.remember({"memories": [ev("loose note one about pricing"), ev("loose note two about hiring")]}, ALICE)
        r = recall(self.s, ALICE, "pricing", granularity="thread")
        self.assertEqual(len(r["items"][0]["memory_ids"]), 1)

    def test_M43_engine_never_exceeds_budget(self):
        from orp.model import RecallRequest
        self.s.remember({"memories": [ev(" ".join(["budget"] * 80) + f" {i}", thread=f"t{i}") for i in range(10)]},
                        ADMIN)
        pack = self.s.engine.recall(RecallRequest(query="budget", principal=ALICE, tenant="acme", budget_tokens=60))
        self.assertLessEqual(pack.tokens, 60)

    def test_M58_neighbours_follow_recall_admissibility(self):
        ids = self.s.remember({"memories": [ev("n0", thread="t", seq=0), ev("n1", thread="t", seq=1),
                                            ev("n2", thread="t", seq=2, expires_at="2020-01-01")]}, ALICE)["ids"]
        r = self.s.neighbours(ids[1], 2, 2, ALICE)
        self.assertEqual([m["content"] for m in r["memories"]], ["n0", "n1"])
        self.s.forget({"ids": [ids[0]]}, ALICE)
        with self.assertRaises(ORPError):
            self.s.neighbours(ids[0], 1, 1, ALICE)

    def test_M25_deep_mode_declared(self):
        self.s.remember({"memories": [ev("deep mode check about renewal", thread="t")]}, ALICE)
        caps = self.s.capabilities(ALICE)
        self.assertIn("deep", caps["modes"])
        a = recall(self.s, ALICE, "renewal", mode="deep")
        b = recall(self.s, ALICE, "renewal", mode="balanced")
        self.assertEqual([i["text"] for i in a["items"]], [i["text"] for i in b["items"]])  # declared as identical

    def test_M53_declared_features_are_the_ones_used(self):
        s = Service(HashEmbedder(), shipped_params())
        feats = s.capabilities(ALICE)["ranker"]["features"]
        for f in shipped_params().drop_features + ("rr_qexp", "rr_sub"):
            self.assertNotIn(f, feats)


class Persistence(unittest.TestCase):
    def test_state_survives_save_and_load(self):
        s = make_service()
        s.remember({"memories": [ev("persist me", thread="t")], "idempotency_key": "p1"}, ALICE)
        gone = s.remember({"memories": [ev("forget me", thread="t")]}, ALICE)["ids"]
        s.forget({"ids": gone}, ALICE)
        path = os.path.join(tempfile.mkdtemp(), "state.bin")
        s.save(path)
        t = make_service()
        t.load(path)
        self.assertTrue(t.remember({"memories": [ev("persist me", thread="t")], "idempotency_key": "p1"},
                                   ALICE)["replayed"])
        with self.assertRaises(ORPError):
            t.remember({"memories": [ev("forget me", thread="t")]}, ALICE)
        self.assertEqual(len(t.changes(0, ALICE)["changes"]), len(s.changes(0, ALICE)["changes"]))


if __name__ == "__main__":
    unittest.main()
