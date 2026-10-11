"""Production hardening of the reference build (v0.1.4): SQLite store, safe encoder files, UTC clock, rankers."""
import json
import os
import pickle
import tempfile
import unittest

from orp import embedders, rankers, store
from orp.api import ORPError, Service
from orp.embedders import HashingEmbedder
from orp.model import Principal

A = Principal("alice", frozenset({"sales"}), tenant="acme")
ROOT = Principal("root", frozenset({"*"}), tenant="acme")


def _svc():
    s = Service(HashingEmbedder())
    s.remember({"memories": [
        {"content": "Alice moved to Berlin", "kind": "fact", "readers": ["group:sales"],
         "claim": {"subject": "alice", "predicate": "lives_in", "object": "Berlin"}},
        {"content": "We met on Monday about the Q3 plan", "thread": "t1", "seq": 1},
        {"content": "Bob said the budget is 40k", "thread": "t1", "seq": 2, "labels": ["secret"]}],
        "idempotency_key": "k1"}, A)
    s.remember({"memories": [{"content": "Alice moved to Paris", "kind": "fact", "readers": ["group:sales"],
                              "claim": {"subject": "alice", "predicate": "lives_in", "object": "Paris"}}]}, A)
    s.reflect({}, A)
    return s


def _strip(d):
    d = dict(d)
    d.pop("request_id", None)
    return d


class StoreTest(unittest.TestCase):
    def test_round_trip_is_lossless(self):
        s = _svc()
        p = os.path.join(tempfile.mkdtemp(), "orp.db")
        s.save(p)
        self.assertTrue(store.is_sqlite(p))
        t = Service(HashingEmbedder())
        t.load(p)
        self.assertEqual(_strip(s.export(ROOT)), _strip(t.export(ROOT)))
        r1, r2 = _strip(s.recall({"query": "where does alice live"}, A)), _strip(t.recall({"query": "where does alice live"}, A))
        self.assertEqual(r1, r2)  # same items and the same snapshot token (the secret persists)
        self.assertTrue(t.remember({"memories": [{"content": "x"}], "idempotency_key": "k1"}, A)["replayed"])
        self.assertEqual(s.audit_log(Principal("aud", frozenset({"auditor"}), tenant="acme"))["entries"],
                         t.audit_log(Principal("aud", frozenset({"auditor"}), tenant="acme"))["entries"])

    def test_legacy_pickle_refused_without_opt_in(self):
        p = os.path.join(tempfile.mkdtemp(), "old.bin")
        with open(p, "wb") as f:
            pickle.dump({"service": {}, "engine": {}}, f)
        os.environ.pop("ORP_ALLOW_PICKLE", None)
        with self.assertRaises(ValueError):
            Service(HashingEmbedder()).load(p)


class EncoderTest(unittest.TestCase):
    def test_pickle_encoder_refused(self):
        p = os.path.join(tempfile.mkdtemp(), "enc.pkl")
        with open(p, "wb") as f:
            pickle.dump(({}, None, None, None, 0.001), f)
        os.environ.pop("ORP_ALLOW_PICKLE", None)
        with self.assertRaises(ValueError):
            embedders.load("sif:" + p)

    def test_orpenc_round_trip(self):
        import numpy as np
        from orp import encfile
        p = os.path.join(tempfile.mkdtemp(), "e.orpenc")
        vecs = np.random.RandomState(0).rand(3, 4).astype("float32")
        encfile.write(p, {"a": 0, "b": 1, "c": 2}, vecs, np.array([.5, .3, .2], "float32"), np.zeros(4, "float32"), 1e-3)
        e = embedders.load("sif:" + p)
        self.assertEqual(e.embed("a b").shape, (4,))
        with open(p, "r+b") as f:  # any change to the payload is caught
            f.seek(-1, 2)
            f.write(b"\x01")
        with self.assertRaises(ValueError):
            embedders.load("sif:" + p)


class ClockTest(unittest.TestCase):
    def test_offsets_are_converted_not_dropped(self):
        s = Service(HashingEmbedder())
        ids = s.remember({"memories": [{"content": "standup", "event_time": "2024-05-01T10:00:00+10:00"}]}, A)["ids"]
        self.assertEqual(s.get(ids, A)["memories"][0]["event_time"], "2024-05-01T00:00:00")


class RankerTest(unittest.TestCase):
    def test_auto_uses_learned_ranker_only_with_its_encoder(self):
        name, p, notes = rankers.choose("auto", HashingEmbedder())
        self.assertEqual((name, p.ranker), ("orp-rrf-1", "rrf"))
        self.assertTrue(notes)

        class Fake:
            name = rankers.spec("orp-linear-1")["fit_encoder"]
        name, p, notes = rankers.choose("auto", Fake())
        self.assertEqual((name, p.ranker, len(p.lin_w)), ("orp-linear-1", "linear", 39))

    def test_capabilities_name_the_ranker_and_encoder(self):
        s = Service(HashingEmbedder(), rankers.params("orp-linear-1"))
        c = s.capabilities(A)
        self.assertEqual(c["ranker"]["name"], "orp-linear-1")
        self.assertEqual(c["encoder"], "hashing-512")


if __name__ == "__main__":
    unittest.main()
