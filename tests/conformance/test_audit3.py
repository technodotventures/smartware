"""Regression tests for the third audit (results/AUDIT3.md), oracle side. The product engine has the same cases in
packages/server/test/audit3.test.js, and tools/fuzz/diff_fuzz.py checks the two engines answer alike."""
from __future__ import annotations

import json
import unittest

from orp import rankers
from orp.api import ORPError, Service
from orp.embedders import HashingEmbedder
from orp.model import Principal

A = Principal("alice", frozenset(), tenant="t1")
ROOT = Principal("root", frozenset({"*"}), tenant="t1")


def svc():
    return Service(HashingEmbedder(), rankers.params("orp-rrf-1"))


def err(fn, *a):
    try:
        fn(*a)
    except ORPError as e:
        return e.code
    return "ok"


class Audit3(unittest.TestCase):
    def test_batch_is_all_or_nothing(self):
        s = svc()
        s.remember({"memories": [{"id": "dup", "content": "exists already"}]}, A)
        self.assertEqual(err(s.remember, {"memories": [{"id": "new1", "content": "first"}, {"id": "dup", "content": "x"}]}, A),
                         "version_conflict")
        self.assertEqual(s.get(["new1"], A)["memories"], [])
        self.assertEqual(err(s.remember, {"memories": [{"id": "z", "content": "a"}, {"id": "z", "content": "b"}]}, A),
                         "invalid_parameter")

    def test_forget_stays_in_its_tenant(self):
        s = svc()
        alice, bob = Principal("alice", tenant="A"), Principal("bob", tenant="B")
        s.remember({"memories": [{"id": "src", "content": "Alice moved to Paris"}]}, alice)
        s.remember({"memories": [{"id": "sum", "kind": "summary", "content": "Alice lives in Paris", "origin": "extracted",
                                  "derived_from": ["src"]}]}, alice)
        s.forget({"ids": ["src"], "mode": "retract"}, alice)
        s.remember({"memories": [{"id": "x", "content": "unrelated"}]}, bob)
        self.assertEqual(s.forget({"ids": ["x"], "mode": "purge"}, bob)["affected"], ["x"])
        g = s.get(["sum"], Principal("root", frozenset({"*"}), tenant="A"))["memories"]
        self.assertEqual(g[0]["status"], "retracted")

    def test_history_after_claim_moves(self):
        s = svc()
        s.remember({"memories": [{"id": "g", "kind": "fact", "content": "Bob works at Acme", "valid_from": "2024-01-01",
                                  "claim": {"subject": "bob", "predicate": "job", "object": "acme"}}]}, A)
        s.remember({"memories": [{"id": "g", "kind": "fact", "content": "Bob lives in Rome", "valid_from": "2024-01-01",
                                  "claim": {"subject": "bob", "predicate": "city", "object": "rome"}}],
                    "mode": "upsert", "if_version": 1}, A)
        self.assertEqual(s.history("bob", "job", A)["versions"], [])
        s.remember({"memories": [{"id": "h", "kind": "fact", "content": "Bob works at Globex", "valid_from": "2025-01-01",
                                  "claim": {"subject": "bob", "predicate": "job", "object": "globex"}}]}, A)
        g = s.get(["g"], A)["memories"][0]
        self.assertEqual((g["status"], g["superseded_by"]), ("active", None))

    def test_purge_restores_superseded(self):
        for mode in ("retract", "purge"):
            s = svc()
            for i, (city, vf) in enumerate((("paris", "2024-01-01"), ("rome", "2025-01-01"))):
                s.remember({"memories": [{"id": ["old", "new"][i], "kind": "fact", "content": f"Alice lives in {city}",
                                          "valid_from": vf, "claim": {"subject": "alice", "predicate": "city", "object": city}}]}, A)
            s.forget({"ids": ["new"], "mode": mode}, A)
            old = s.get(["old"], ROOT)["memories"][0]
            self.assertEqual((old["status"], old["superseded_by"]), ("active", None), mode)

    def test_reserved_meta(self):
        s = svc()
        s.remember({"memories": [{"id": "other", "content": "first", "meta": {"idempotency_key": "K"}}]}, A)
        s.remember({"memories": [{"id": "doc", "content": "v1"}]}, A)
        r = s.remember({"memories": [{"id": "doc", "content": "v2", "meta": {"idempotency_key": "K"}}],
                        "mode": "upsert", "if_version": 1}, A)
        self.assertEqual(r["ids"], ["doc"])
        self.assertEqual([m["id"] for m in s.get(["doc", "other"], A)["memories"]], ["doc", "other"])
        for meta in ({"_idempotency_key": "K"}, {"entities": ["Forged"]}, {"time_refs": []}):
            self.assertEqual(err(s.remember, {"memories": [{"content": "x", "meta": meta}]}, A), "invalid_parameter")

    def test_replay_before_validation(self):
        s = svc()
        s.remember({"memories": [{"id": "src", "content": "meeting notes"}]}, A)
        body = {"memories": [{"kind": "summary", "content": "summary", "origin": "extracted", "derived_from": ["src"]}],
                "idempotency_key": "req-42"}
        first = s.remember(body, A)
        s.forget({"ids": ["src"], "mode": "purge", "cascade": False}, A)
        again = s.remember(body, A)
        self.assertEqual((again["ids"], again["replayed"]), (first["ids"], True))

    def test_timestamps(self):
        s = svc()
        for v in (5, True, "20240101", "2024-01-01T10:00:00+05:30:15", "2024-W01-1", "2024-001", "2024-01-01T24:00:00"):
            self.assertEqual(err(s.remember, {"memories": [{"content": "p", "event_time": v}]}, A), "invalid_parameter", v)
        for v in ("2024-01-01T10", "2024-01-01T10:00:00.1234567", "2024-01-01 10:00Z"):
            self.assertEqual(err(s.remember, {"memories": [{"content": "p " + v, "event_time": v}]}, A), "ok", v)

    def test_import_checks_before_writing(self):
        s = svc()
        b = Principal("root", frozenset({"*"}), tenant="B")
        s.remember({"memories": [{"id": "p", "content": "beta"}, {"id": "q", "content": "gamma"}]}, b)
        bundle = {"memories.jsonl": json.dumps({"id": "n1", "content": "new", "kind": "event", "tenant": "B"}),
                  "edges.jsonl": "\n".join(json.dumps(e) for e in (
                      {"id": "edge_ok", "type": "relates", "from": "p", "to": "q"},
                      {"id": "edge_bad", "type": "relates", "from": "p", "to": "nope"})),
                  "manifest": {"tenant": "B", "tombstones": []}}
        self.assertEqual(err(s.import_, bundle, b), "invalid_parameter")
        self.assertEqual(s.edges("p", b), [])
        self.assertEqual(s.get(["n1"], b)["memories"], [])
        self.assertEqual(err(s.import_, {"memories.jsonl": "", "edges.jsonl": "{bad"}, b), "invalid_parameter")
        self.assertEqual(err(s.import_, {"memories.jsonl": "[1]"}, b), "invalid_parameter")

    def test_export_sorted(self):
        s = svc()
        s.remember({"memories": [{"id": c, "content": w} for c, w in zip("abcd", ("alpha", "beta", "gamma", "delta"))]}, ROOT)
        s.link({"type": "relates", "from": "b", "to": "d"}, ROOT)
        s.link({"type": "relates", "from": "a", "to": "b"}, ROOT)
        s.forget({"ids": ["d"], "mode": "purge", "cascade": False}, ROOT)
        s.forget({"ids": ["c"], "mode": "purge"}, ROOT)
        ex = s.export(ROOT)
        ids = [json.loads(l)["id"] for l in ex["edges.jsonl"].split("\n") if l]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual([x["id"] for x in ex["manifest"]["tombstones"]], ["c", "d"])

    def test_reflect_scope_and_kinds(self):
        s = svc()
        s.remember({"memories": [{"content": "standup notes", "scope": "team/a", "thread": "t-a"},
                                 {"content": "planning notes", "scope": "team/b", "thread": "t-b"}]}, A)
        self.assertEqual(err(s.reflect, {"kinds": ["procedure"]}, A), "unsupported_parameter")
        r = s.reflect({"scope": "team/a/"}, A)
        self.assertEqual(len(r["created"]), 1)
        self.assertEqual(s.get(r["created"], A)["memories"][0]["thread"], "t-a")

    def test_meta_depth_limited(self):
        s = svc()
        deep = {}
        cur = deep
        for _ in range(100):
            cur["a"] = {}
            cur = cur["a"]
        self.assertEqual(err(s.remember, {"memories": [{"content": "x", "meta": {"d": deep}}]}, A), "invalid_parameter")
        self.assertEqual(err(s.remember, {"memories": [{"content": "x", "meta": {"d": {"e": [1, {"f": 2}]}}}]}, A), "ok")


if __name__ == "__main__":
    unittest.main()
