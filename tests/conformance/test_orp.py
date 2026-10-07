"""ORP 0.1 conformance suite. Each test cites the spec clause it checks.

Run: python3 -m unittest discover -s tests -v
Engines other than the reference can be tested by replacing make_service().
"""
from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime, timedelta

import numpy as np

from orp.api import ORPError, Service
from orp.model import Principal

T0 = datetime(2026, 1, 1)


class HashEmbedder:
    """Deterministic bag-of-words embedder for tests (no model weights needed)."""

    dim = 128

    def embed(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, np.float32)
        for w in text.lower().split():
            h = int(hashlib.md5(w.strip(".,!?").encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_many(self, texts):
        return np.stack([self.embed(t) for t in texts]) if texts else np.zeros((0, self.dim), np.float32)


class RandomEmbedder(HashEmbedder):
    """Dense channel that carries no signal: lexical evidence must still surface (spec §5.3)."""

    def embed(self, text):
        rng = np.random.default_rng(int(hashlib.md5(text.encode()).hexdigest()[:8], 16))
        v = rng.normal(size=self.dim).astype(np.float32)
        return v / np.linalg.norm(v)


def make_service(embedder=None) -> Service:
    return Service(embedder or HashEmbedder())


ALICE = Principal("alice", frozenset({"sales"}), tenant="acme")
BOB = Principal("bob", frozenset({"eng"}), tenant="acme")
EVE = Principal("eve", frozenset({"sales"}), tenant="other")
ADMIN = Principal("root", frozenset({"*"}), tenant="acme")


def texts(pack):
    return "\n".join(i["text"] for i in pack["items"])


def ev(content, **kw):
    d = {"kind": "event", "content": content}
    d.update(kw)
    return d


class L1Core(unittest.TestCase):
    def setUp(self):
        self.s = make_service()

    def test_remember_and_recall(self):  # §4.1, §4.2
        self.s.remember({"memories": [ev("The quarterly offsite is in Hobart", actor="alice", thread="t1")]}, ALICE)
        p = self.s.recall({"query": "where is the offsite?", "budget": {"tokens": 500}}, ALICE)
        self.assertIn("Hobart", texts(p))
        self.assertTrue(p["items"][0]["citations"])
        self.assertIn("why", p["items"][0])

    def test_idempotent_replay(self):  # §4.1
        b = {"memories": [ev("hello world", thread="t")], "idempotency_key": "k1"}
        a = self.s.remember(b, ALICE)
        c = self.s.remember(b, ALICE)
        self.assertEqual(a["ids"], c["ids"])
        self.assertTrue(c["replayed"])
        self.assertEqual(len(self.s.engine.mem), 1)

    def test_unknown_parameters_rejected(self):  # P5, §4.1, §4.2
        with self.assertRaises(ORPError) as e:
            self.s.remember({"memories": [ev("x")], "dreaming": True}, ALICE)
        self.assertEqual(e.exception.code, "unsupported_parameter")
        with self.assertRaises(ORPError):
            self.s.recall({"query": "x", "hybrid_alpha": 0.3}, ALICE)
        with self.assertRaises(ORPError):
            self.s.recall({"query": "x", "mode": "turbo"}, ALICE)

    def test_empty_query_is_not_everything(self):  # §4.2
        with self.assertRaises(ORPError):
            self.s.recall({"query": "  "}, ALICE)

    def test_budget_is_a_hard_cap(self):  # §5.5
        self.s.remember({"memories": [ev(f"note {i} about the migration plan and the rollout schedule " * 3,
                                         thread=f"t{i}", seq=0) for i in range(40)]}, ALICE)
        for b in (60, 200, 700):
            p = self.s.recall({"query": "migration rollout", "budget": {"tokens": b}}, ALICE)
            self.assertLessEqual(p["tokens"], b)
            self.assertLessEqual(sum(i["tokens"] for i in p["items"]), b)

    def test_lexical_channel_nominates_independently(self):  # §5.3
        s = make_service(RandomEmbedder())
        filler = [ev(f"general chatter number {i} about lunch plans", thread=f"f{i}") for i in range(300)]
        s.remember({"memories": filler + [ev("The vault code word is XANADU-7", thread="secret-ish")]}, ALICE)
        p = s.recall({"query": "what is the XANADU-7 code word", "budget": {"tokens": 300}}, ALICE)
        self.assertIn("XANADU-7", texts(p))

    def test_get_respects_acl(self):  # §6.2
        ids = self.s.remember({"memories": [ev("salary bands", readers=["group:hr"])]}, ADMIN)["ids"]
        self.assertEqual(self.s.get(ids, ALICE)["memories"], [])
        self.assertEqual(self.s.get(ids, ALICE)["not_found"], ids)

    def test_forget_retract_hides(self):  # §4.4
        ids = self.s.remember({"memories": [ev("Temporary door code 4412", thread="t")]}, ALICE)["ids"]
        self.s.forget({"ids": ids}, ALICE)
        p = self.s.recall({"query": "door code", "budget": {"tokens": 300}}, ALICE)
        self.assertNotIn("4412", texts(p))

    def test_capabilities_declare_fusion_and_tokenizer(self):  # §5.3, §4.2
        c = self.s.capabilities()
        self.assertIn("fusion", c)
        self.assertIn("tokenizer", c)
        self.assertIn("L3", c["conformance"])

    def test_cross_tenant_isolation(self):  # §2 tenant
        self.s.remember({"memories": [ev("acme launch date is March 3", thread="t")]}, ALICE)
        p = self.s.recall({"query": "launch date", "budget": {"tokens": 300}}, EVE)
        self.assertEqual(p["items"], [])


class L2Temporal(unittest.TestCase):
    def setUp(self):
        self.s = make_service()

    def _fact(self, obj, vf, who=ALICE, **kw):
        return self.s.remember({"memories": [{"kind": "fact", "content": f"Maya lives in {obj}",
                                              "claim": {"subject": "Maya", "predicate": "lives_in", "object": obj},
                                              "valid_from": vf.isoformat(), **kw}]}, who)["ids"][0]

    def test_supersession_and_as_of(self):  # §5.2, §5.6
        paris = self._fact("Paris", T0 - timedelta(days=700))
        lisbon = self._fact("Lisbon", T0 + timedelta(days=100))
        now = self.s.recall({"query": "Where does Maya live?", "as_of": (T0 + timedelta(days=200)).isoformat(),
                             "budget": {"tokens": 400}}, ALICE)
        self.assertIn("Lisbon", texts(now))
        self.assertNotIn("Paris", texts(now))
        then = self.s.recall({"query": "Where does Maya live?", "as_of": (T0 - timedelta(days=30)).isoformat(),
                              "budget": {"tokens": 400}}, ALICE)
        self.assertIn("Paris", texts(then))
        self.assertNotIn("Lisbon", texts(then))
        m = self.s.get([paris], ALICE)["memories"][0]
        self.assertEqual(m["status"], "superseded")
        self.assertEqual(m["superseded_by"], lisbon)

    def test_backfill_does_not_override_newer(self):  # §5.6 (order by validity, not ingestion)
        lisbon = self._fact("Lisbon", T0 + timedelta(days=100))
        paris = self._fact("Paris", T0 - timedelta(days=700))  # ingested later, but older in the world
        st = {m["id"]: m["status"] for m in self.s.get([lisbon, paris], ALICE)["memories"]}
        self.assertEqual(st[lisbon], "active")
        self.assertEqual(st[paris], "superseded")

    def test_history_chain(self):  # §4 history
        self._fact("Paris", T0 - timedelta(days=700))
        self._fact("Lisbon", T0 + timedelta(days=100))
        h = self.s.history("Maya", "lives_in", ALICE)["versions"]
        self.assertEqual([v["object"] for v in h], ["Paris", "Lisbon"])
        self.assertEqual(h[0]["status"], "superseded")

    def test_include_history_labels_superseded(self):  # §5.2
        self._fact("Paris", T0 - timedelta(days=700))
        self._fact("Lisbon", T0 + timedelta(days=100))
        p = self.s.recall({"query": "Maya lives", "as_of": (T0 + timedelta(days=200)).isoformat(),
                           "include": {"history": True}, "budget": {"tokens": 400}}, ALICE)
        vals = {i["validity"] for i in p["items"]}
        self.assertIn("superseded", vals)
        self.assertTrue(any(i["text"].startswith("[superseded]") for i in p["items"]))

    def test_cardinality_many_never_supersedes(self):  # §5.6
        for lang in ("Portuguese", "French"):
            self.s.remember({"memories": [{"kind": "fact", "content": f"Maya speaks {lang}",
                                           "claim": {"subject": "Maya", "predicate": "speaks", "object": lang,
                                                     "cardinality": "many"}}]}, ALICE)
        st = [m.status for m in self.s.engine.mem.values()]
        self.assertEqual(st, ["active", "active"])

    def test_known_at(self):  # §4.2
        self.s.remember({"memories": [ev("Budget approved at 2M", thread="t")]}, ALICE)
        cut = datetime.now() - timedelta(seconds=1)
        p = self.s.recall({"query": "budget approved", "known_at": cut.isoformat(), "budget": {"tokens": 300}},
                          ALICE)
        self.assertEqual(p["items"], [])

    def test_relative_time_resolved(self):  # §4.1, §5.5
        s = self.s
        s.engine.remember  # write through engine to control observed_at
        from orp.model import Memory
        s.engine.remember(Memory(content="I went to the support group yesterday", actor="Caroline", thread="s1",
                                 tenant="acme", observed_at=datetime(2023, 5, 8, 13, 56)))
        p = s.recall({"query": "When did Caroline go to the support group?", "as_of": "2023-06-01T00:00:00",
                      "budget": {"tokens": 300}}, ALICE)
        self.assertIn("7 May 2023", texts(p))

    def test_as_of_is_the_clock(self):  # §4.2 (no wall-clock leakage)
        from orp.model import Memory
        self.s.engine.remember(Memory(content="Deploy freeze starts Friday", thread="t", tenant="acme",
                                      observed_at=datetime(2024, 1, 1)))
        a = self.s.recall({"query": "deploy freeze", "as_of": "2024-02-01T00:00:00", "budget": {"tokens": 300}}, ALICE)
        b = self.s.recall({"query": "deploy freeze", "as_of": "2024-02-01T00:00:00", "budget": {"tokens": 300}}, ALICE)
        self.assertEqual([i["text"] for i in a["items"]], [i["text"] for i in b["items"]])
        self.assertEqual(a["snapshot"], b["snapshot"])


class L3Governed(unittest.TestCase):
    def setUp(self):
        self.s = make_service()

    def test_pre_ranking_admissibility_preserves_recall(self):  # §5.1
        priv = [ev(f"Project Falcon pricing discussion round {i}: discount tiers", readers=["group:exec"],
                   thread=f"p{i}") for i in range(200)]
        pub = [ev("Project Falcon public pricing page goes live on the 14th", readers=["group:sales"], thread="pub")]
        self.s.remember({"memories": priv + pub}, ADMIN)
        p = self.s.recall({"query": "Project Falcon pricing", "budget": {"tokens": 400}}, ALICE)
        self.assertIn("goes live on the 14th", texts(p))
        self.assertNotIn("discount tiers", texts(p))

    def test_provenance_closed_derived_memory(self):  # §6.3
        a = self.s.remember({"memories": [ev("Dana is leaving the company in June", readers=["group:hr"],
                                             thread="dm")]}, ADMIN)["ids"][0]
        b = self.s.remember({"memories": [ev("Team offsite planning continues", thread="general")]}, ADMIN)["ids"][0]
        d = self.s.remember({"memories": [{"kind": "summary", "origin": "extracted", "derived_from": [a, b],
                                           "content": "Dana leaving in June; offsite planning continues"}]},
                            ADMIN)["ids"][0]
        self.assertEqual(self.s.get([d], ALICE)["memories"], [])
        p = self.s.recall({"query": "Dana leaving", "budget": {"tokens": 400}}, ALICE)
        self.assertNotIn("Dana", texts(p))
        self.assertNotIn(d, json.dumps(self.s.export(ALICE)))
        hr = Principal("hal", frozenset({"hr"}), tenant="acme")
        self.assertEqual(len(self.s.get([d], hr)["memories"]), 1)

    def test_reclose_when_source_acl_narrows(self):  # §6.3
        a = self.s.remember({"memories": [ev("Roadmap: ship v2 in Q3", thread="t", id="src1")]}, ADMIN)["ids"][0]
        d = self.s.remember({"memories": [{"kind": "summary", "origin": "extracted", "derived_from": [a],
                                           "content": "v2 ships Q3"}]}, ADMIN)["ids"][0]
        self.assertEqual(len(self.s.get([d], ALICE)["memories"]), 1)
        self.s.remember({"memories": [ev("Roadmap: ship v2 in Q3", thread="t", id="src1", readers=["group:exec"])],
                         "mode": "upsert", "if_version": 1}, ADMIN)
        self.assertEqual(self.s.get([d], ALICE)["memories"], [])

    def test_get_by_id_and_neighbours_enforce_acl(self):  # §6.2
        ids = self.s.remember({"memories": [ev("open", thread="t", seq=0),
                                            ev("hidden", thread="t", seq=1, readers=["group:exec"]),
                                            ev("open again", thread="t", seq=2)]}, ADMIN)["ids"]
        n = self.s.neighbours(ids[0], 0, 5, ALICE)["memories"]
        self.assertEqual([x["content"] for x in n], ["open", "open again"])

    def test_read_write_separation(self):  # §6.2
        ids = self.s.remember({"memories": [ev("shared note", thread="t", writers=["principal:alice"])]},
                              ALICE)["ids"]
        with self.assertRaises(ORPError) as e:
            self.s.forget({"ids": ids}, BOB)
        self.assertEqual(e.exception.code, "forbidden")
        self.s.forget({"ids": ids}, ALICE)

    def test_dry_run_and_cascade(self):  # §4.4
        a = self.s.remember({"memories": [ev("Client ACME churn risk high", thread="t")]}, ALICE)["ids"][0]
        d = self.s.remember({"memories": [{"kind": "observation", "origin": "inferred", "derived_from": [a],
                                           "content": "Churn risk concentrated in ACME"}]}, ALICE)["ids"][0]
        r = self.s.forget({"ids": [a], "dry_run": True}, ALICE)
        self.assertEqual(set(r["affected"]), {a, d})
        self.assertEqual(self.s.engine.mem[d].status, "active")
        self.s.forget({"ids": [a], "mode": "redact"}, ALICE)
        self.assertEqual(self.s.engine.mem[d].content, "[redacted]")
        self.assertEqual(self.s.engine.mem[d].status, "retracted")

    def test_retract_blocks_reingest(self):  # §4.4
        b = {"memories": [ev("Do not store my phone number 0400 000 000", thread="t", actor="alice")]}
        ids = self.s.remember(b, ALICE)["ids"]
        self.s.forget({"ids": ids}, ALICE)
        with self.assertRaises(ORPError):
            self.s.remember(b, ALICE)

    def test_secret_label_gate(self):  # §6.4
        self.s.remember({"memories": [ev("API key for staging is sk-test-123", labels=["secret"], thread="t")]}, ALICE)
        p = self.s.recall({"query": "staging API key", "budget": {"tokens": 300}}, ALICE)
        self.assertNotIn("sk-test", texts(p))

    def test_edge_closure(self):  # §3.4
        a = self.s.remember({"memories": [ev("Alpha", thread="a")]}, ADMIN)["ids"][0]
        b = self.s.remember({"memories": [ev("Beta", thread="b", readers=["group:exec"])]}, ADMIN)["ids"][0]
        self.s.link({"type": "relates", "from": a, "to": b}, ADMIN)
        self.assertEqual(self.s.edges(a, ALICE), [])
        self.assertEqual(len(self.s.edges(a, ADMIN)), 1)

    def test_changes_feed_filtered(self):  # §4.7
        self.s.remember({"memories": [ev("exec only", readers=["group:exec"], thread="x")]}, ADMIN)
        self.s.remember({"memories": [ev("everyone", thread="y")]}, ADMIN)
        ch = self.s.changes(0, ALICE)["changes"]
        self.assertEqual(len(ch), 1)


class L4Adaptive(unittest.TestCase):
    def setUp(self):
        self.s = make_service()

    def test_reflect_lineage_and_closure(self):  # §4.6
        ids = self.s.remember({"memories": [ev("Kickoff: we chose Postgres for the ledger service", thread="t", seq=0,
                                               readers=["group:eng"]),
                                            ev("We will revisit sharding in Q4 after load tests", thread="t", seq=1,
                                               readers=["group:eng"])]}, ADMIN)["ids"]
        r = self.s.reflect({"threads": ["t"]}, ADMIN)
        summ = self.s.engine.mem[r["created"][0]]
        self.assertEqual(set(summ.derived_from), set(ids))
        self.assertEqual(summ.origin, "extracted")
        self.assertEqual(summ.readers, frozenset({"group:eng"}))
        r2 = self.s.reflect({"threads": ["t"]}, ADMIN)
        self.assertEqual(r2["created"], [])  # idempotent per (scope, cursor)

    def test_reflect_supersedes_previous_summary(self):  # §4.6
        self.s.remember({"memories": [ev("first decision about pricing tiers", thread="t", seq=0)]}, ADMIN)
        first = self.s.reflect({"threads": ["t"]}, ADMIN)["created"][0]
        self.s.remember({"memories": [ev("second decision: drop the free tier entirely", thread="t", seq=1)]}, ADMIN)
        second = self.s.reflect({"threads": ["t"]}, ADMIN)["created"][0]
        self.assertEqual(self.s.engine.mem[first].status, "superseded")
        self.assertEqual(self.s.engine.mem[first].superseded_by, second)

    def test_feedback_creates_draft_rule_needing_approval(self):  # §4.5, §7
        t = self.s.remember({"memories": [ev("Draft outreach email to ACME", thread="t")]}, ALICE)["ids"][0]
        r = self.s.feedback({"target": t, "signal": "draft_final",
                             "draft": "Hey there!!! Super excited to share our amazing synergy platform with you",
                             "final": "Hi Dana, sharing a short note on how we cut month-end close time."}, ALICE)
        rule = r["rule_candidate"]
        self.assertEqual(self.s.engine.mem[rule].status, "draft")
        p = self.s.recall({"query": "when drafting similar content", "budget": {"tokens": 400}}, ALICE)
        self.assertNotIn(rule, json.dumps(p))
        self.s.approve(rule, ALICE)
        self.assertEqual(self.s.engine.mem[rule].status, "active")

    def test_profile_requires_budget_and_orders(self):  # §4.3
        with self.assertRaises(ORPError):
            self.s.profile({"subject": "alice"}, ALICE)
        self.s.remember({"memories": [
            {"kind": "fact", "content": "Alice is the APAC sales lead", "load": "always"},
            {"kind": "procedure", "content": "Always reply in British English", "load": "always"},
            {"kind": "preference", "content": "Alice prefers tables over prose", "load": "always"},
            {"kind": "fact", "content": "Alice once mentioned liking sushi"}]}, ALICE)
        p = self.s.profile({"budget": {"tokens": 30}}, ALICE)
        self.assertEqual(p["items"][0]["text"], "Always reply in British English")
        self.assertLessEqual(p["tokens"], 30)
        self.assertNotIn("sushi", json.dumps(p))

    def test_export_import_round_trip(self):  # §8.3
        self.s.remember({"memories": [ev("alpha note", thread="t", seq=0),
                                      {"kind": "fact", "content": "Maya lives in Lisbon",
                                       "claim": {"subject": "Maya", "predicate": "lives_in", "object": "Lisbon"}}]},
                        ADMIN)
        b1 = self.s.export(ADMIN)
        s2 = make_service()
        s2.import_(b1, ADMIN)
        b2 = s2.export(ADMIN)
        self.assertEqual(b1["memories.jsonl"], b2["memories.jsonl"])
        self.assertEqual(b1["manifest"]["sha256"], b2["manifest"]["sha256"])


if __name__ == "__main__":
    unittest.main()
