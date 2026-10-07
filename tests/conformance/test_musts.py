"""One test per MUST in spec/ORP-0.1.md (ids as in spec/MUSTS.md).

Test names start with the MUST id(s) they check, e.g. test_M33_ranking_invariant_to_inadmissible.
bench/../tools/musts.py builds spec/MUSTS.md from these names.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta

import numpy as np

from orp.api import ORPError, Service
from orp.model import Memory, Principal
from tests.conformance.test_orp import ADMIN, ALICE, BOB, EVE, HashEmbedder, RandomEmbedder, ev, make_service, texts

EXEC = Principal("erin", frozenset({"exec"}), tenant="acme")
ACME_ROOT = ADMIN
OTHER_ROOT = Principal("root2", frozenset({"*"}), tenant="other")


def recall(s, pr, q, **kw):
    body = {"query": q, "budget": {"tokens": kw.pop("tokens", 600)}}
    body.update(kw)
    return s.recall(body, pr)


class Records(unittest.TestCase):
    def setUp(self):
        self.s = make_service()

    def test_M02_ids_unique_per_tenant(self):
        # the same client id in two tenants names two independent records; neither tenant can detect the other
        a = self.s.remember({"memories": [ev("acme note", id="shared-id")]}, ACME_ROOT)["ids"]
        b = self.s.remember({"memories": [ev("other note", id="shared-id")]}, OTHER_ROOT)["ids"]
        self.assertEqual(a, ["shared-id"])
        self.assertEqual(b, ["shared-id"])
        self.assertEqual(self.s.get(["shared-id"], ACME_ROOT)["memories"][0]["content"], "acme note")
        self.assertEqual(self.s.get(["shared-id"], OTHER_ROOT)["memories"][0]["content"], "other note")
        with self.assertRaises(ORPError) as e:  # same tenant, same id: a conflict, not a silent overwrite
            self.s.remember({"memories": [ev("acme again", id="shared-id")]}, ACME_ROOT)
        self.assertEqual(e.exception.code, "version_conflict")

    def test_M03_tenant_is_hard_boundary_even_for_admins(self):
        ids = self.s.remember({"memories": [ev("acme secret roadmap", thread="general")]}, ACME_ROOT)["ids"]
        self.s.remember({"memories": [ev("other tenant chatter", thread="general")]}, OTHER_ROOT)
        self.assertEqual(self.s.get(ids, OTHER_ROOT)["memories"], [])
        with self.assertRaises(ORPError):
            self.s.forget({"ids": ids}, OTHER_ROOT)
        self.assertNotIn("roadmap", texts(recall(self.s, OTHER_ROOT, "roadmap chatter", granularity="span")))

    def test_M04_M05_M06_M07_M08_M09_required_fields_present(self):
        ids = self.s.remember({"memories": [ev("x", thread="t")]}, ALICE)["ids"]
        m = self.s.get(ids, ALICE)["memories"][0]
        for k in ("id", "tenant", "kind", "content", "readers", "origin", "observed_at", "status", "version"):
            self.assertIn(k, m)

    def test_M10_M48_supersession_links_by_validity_time(self):
        a = self.s.remember({"memories": [{"kind": "fact", "content": "CEO is Ana", "valid_from": "2026-05-01",
                                           "claim": {"subject": "acme", "predicate": "ceo", "object": "Ana"}}]},
                            ALICE)["ids"][0]
        b = self.s.remember({"memories": [{"kind": "fact", "content": "CEO is Bo", "valid_from": "2025-01-01",
                                           "claim": {"subject": "acme", "predicate": "ceo", "object": "Bo"}}]},
                            ALICE)["ids"][0]
        mb = self.s.get([b], ALICE)["memories"][0]
        self.assertEqual(mb["superseded_by"], a)
        self.assertIn(b, self.s.get([a], ALICE)["memories"][0]["supersedes"])

    def test_M11_version_increments_on_every_change(self):
        a = self.s.remember({"memories": [{"kind": "fact", "content": "plan A", "valid_from": "2025-01-01",
                                           "claim": {"subject": "x", "predicate": "plan", "object": "A"}}]},
                            ALICE)["ids"][0]
        v0 = self.s.engine.mem[a].version
        self.s.remember({"memories": [{"kind": "fact", "content": "plan B", "valid_from": "2026-01-01",
                                       "claim": {"subject": "x", "predicate": "plan", "object": "B"}}]}, ALICE)
        v1 = self.s.engine.mem[a].version
        self.assertGreater(v1, v0)
        self.s.forget({"ids": [a]}, ALICE)
        self.assertGreater(self.s.engine.mem[a].version, v1)

    def test_M12_M13_sources_and_lineage_for_non_observed(self):
        with self.assertRaises(ORPError):
            self.s.remember({"memories": [{"kind": "observation", "origin": "inferred", "content": "x"}]}, ALICE)
        a = self.s.remember({"memories": [ev("raw evidence", thread="t")]}, ALICE)["ids"][0]
        d = self.s.remember({"memories": [{"kind": "observation", "origin": "inferred", "content": "y",
                                           "derived_from": [a]}]}, ALICE)["ids"][0]
        self.assertTrue(self.s.get([d], ALICE)["memories"][0]["sources"])

    def test_M14_M15_M62_round_trip_and_rebuild(self):
        self.s.remember({"memories": [ev("Quarterly planning moved to Hobart", thread="t", meta={"x-acme-k": 7})]},
                        ACME_ROOT)
        b1 = self.s.export(ACME_ROOT)
        s2 = make_service()
        s2.import_(b1, ACME_ROOT)
        self.assertEqual(b1["memories.jsonl"], s2.export(ACME_ROOT)["memories.jsonl"])
        self.assertIn('"x-acme-k": 7', b1["memories.jsonl"])
        self.assertIn("Hobart", texts(recall(s2, ACME_ROOT, "where is planning")))  # indexes rebuilt from records

    def test_M16_neighbours_and_whole_threads(self):
        ids = self.s.remember({"memories": [ev(f"turn {i} about the launch", thread="t", seq=i) for i in range(5)]},
                              ALICE)["ids"]
        n = self.s.neighbours(ids[2], 1, 1, ALICE)["memories"]
        self.assertEqual([x["seq"] for x in n], [1, 2, 3])
        p = recall(self.s, ALICE, "launch", granularity="thread")
        self.assertEqual(len(p["items"][0]["memory_ids"]), 5)


class Writes(unittest.TestCase):
    def setUp(self):
        self.s = make_service()

    def test_M17_idempotent_replay(self):
        b = {"memories": [ev("a")], "idempotency_key": "k"}
        self.assertEqual(self.s.remember(b, ALICE)["ids"], self.s.remember(b, ALICE)["ids"])

    def test_M18_unknown_fields_and_enum_values_rejected(self):
        for bad in ({"memories": [ev("x")], "nope": 1}, {"memories": [ev("x")], "wait": "eventually"},
                    {"memories": [ev("x", role="robot")]}, {"memories": [ev("x", load="sometimes")]},
                    {"memories": [ev("x", kind="thought")]}, {"memories": [ev("x", origin="dreamed")]}):
            with self.assertRaises(ORPError) as e:
                self.s.remember(bad, ALICE)
            self.assertEqual(e.exception.code, "unsupported_parameter")

    def test_M19_M31_reflect_never_rewrites_evidence(self):
        ids = self.s.remember({"memories": [ev("We chose Postgres for the ledger service", thread="t", seq=0),
                                            ev("Sharding is deferred to Q4", thread="t", seq=1)]}, ADMIN)["ids"]
        before = [self.s.engine.mem[i].content for i in ids]
        self.s.reflect({}, ADMIN)
        self.assertEqual(before, [self.s.engine.mem[i].content for i in ids])
        self.assertTrue(all(self.s.engine.mem[i].status == "active" for i in ids))

    def test_M36_evidence_cannot_be_superseded(self):
        a = self.s.remember({"memories": [ev("raw turn", thread="t")]}, ALICE)["ids"][0]
        with self.assertRaises(ORPError):
            self.s.remember({"memories": [{"kind": "fact", "content": "new", "supersedes": [a]}],
                             "mode": "supersede"}, ALICE)
        with self.assertRaises(ORPError):
            self.s.remember({"memories": [ev("replacement turn", supersedes=[a])], "mode": "supersede"}, ALICE)

    def test_S41_upsert_requires_if_version(self):
        self.s.remember({"memories": [ev("doc v1", id="d1")]}, ALICE)
        with self.assertRaises(ORPError):
            self.s.remember({"memories": [ev("doc v2", id="d1")], "mode": "upsert"}, ALICE)
        self.s.remember({"memories": [ev("doc v2", id="d1")], "mode": "upsert", "if_version": 1}, ALICE)


class RecallSemantics(unittest.TestCase):
    def setUp(self):
        self.s = make_service()

    def test_M20_as_of_is_the_clock_no_future_evidence(self):
        self.s.engine.remember(Memory(content="Budget approved at 2M", thread="t", tenant="acme",
                                      observed_at=datetime(2024, 6, 1)))
        p = recall(self.s, ALICE, "budget approved", as_of="2024-01-01T00:00:00")
        self.assertEqual(p["items"], [])
        p = recall(self.s, ALICE, "budget approved", as_of="2024-07-01T00:00:00")
        self.assertIn("2M", texts(p))

    def test_M21_prefer_is_soft(self):
        self.s.remember({"memories": [ev("Launch checklist for team red", scope="org/acme/red", thread="r"),
                                      ev("Launch checklist for team blue", scope="org/acme/blue", thread="b")]}, ALICE)
        p = recall(self.s, ALICE, "launch checklist", scope={"prefer": ["org/acme/red"]})
        t = texts(p)
        self.assertIn("red", t)
        self.assertIn("blue", t)
        self.assertLess(t.index("red"), t.index("blue"))

    def test_M22_kinds_list_and_quota(self):
        self.s.remember({"memories": [ev(f"pricing note {i}", thread=f"t{i}") for i in range(6)] +
                         [{"kind": "fact", "content": f"pricing fact {i}"} for i in range(3)]}, ALICE)
        p = recall(self.s, ALICE, "pricing", kinds=["fact"])
        self.assertTrue(p["items"] and all(i["kind"] == "fact" for i in p["items"]))
        p = recall(self.s, ALICE, "pricing", kinds={"event": 2, "fact": 1})
        kinds = [i["kind"] for i in p["items"]]
        self.assertLessEqual(kinds.count("event"), 2)
        self.assertLessEqual(kinds.count("fact"), 1)

    def test_M23_M24_M25_M39_M40_M44_M53_capabilities_declare_everything(self):
        c = self.s.capabilities()
        for k in ("tokenizer", "fusion", "score_scale", "granularity_auto", "ranker"):
            self.assertIn(k, c)
        self.assertIn("budget.tokens", c["defaults"])
        self.assertIn("name", c["ranker"])
        self.assertIn("fitted_on", c["ranker"])
        self.assertIn("deep", c["modes"])  # M25: deep mode behaviour declared

    def test_M26_profile_requires_budget(self):
        with self.assertRaises(ORPError):
            self.s.profile({}, ALICE)

    def test_M32_M33_M54_ranking_invariant_to_inadmissible_content(self):
        base = [ev(f"weekly sync notes item {i} about hiring and planning", thread=f"t{i}", seq=0) for i in range(30)]
        target = ev("ok, sounds good", thread="t5", seq=1)
        self.s.remember({"memories": base + [target]}, ADMIN)
        q = "layoffs march announcement"
        before = [i["memory_ids"] for i in recall(self.s, ALICE, q, granularity="unit")["items"]]
        self.s.remember({"memories": [ev("layoffs march announcement drafted", thread="t5", seq=2,
                                         readers=["group:exec"])]}, ADMIN)
        after = [i["memory_ids"] for i in recall(self.s, ALICE, q, granularity="unit")["items"]]
        self.assertEqual(before, after)

    def test_M32_M33_M54b_M66_M67_identical_to_store_without_inadmissible(self):
        mems = []
        for t in range(6):
            for i in range(6):
                priv = (t + i) % 3 == 0
                mems.append(ev(f"thread {t} turn {i}: notes on hiring plan and budget {'secret raise' if priv else ''}",
                               thread=f"th{t}", seq=i, readers=["group:exec"] if priv else None,
                               actor="Ann" if i % 2 else "Ben"))
        full = make_service()
        full.remember({"memories": mems}, ADMIN)
        clean = make_service()
        clean.remember({"memories": [m for m in mems if m.get("readers") is None]}, ADMIN)
        for q in ("hiring plan", "What did Ann say about the budget?", "secret raise", "notes"):
            for g in ("unit", "span", "auto"):
                a = [i["text"] for i in recall(full, ALICE, q, granularity=g)["items"]]
                b = [i["text"] for i in recall(clean, ALICE, q, granularity=g)["items"]]
                self.assertEqual(a, b, (q, g))

    def test_M34_M47_history_labelled(self):
        self.s.remember({"memories": [{"kind": "fact", "content": "Maya lives in Paris", "valid_from": "2024-01-01",
                                       "claim": {"subject": "Maya", "predicate": "city", "object": "Paris"}},
                                      {"kind": "fact", "content": "Maya lives in Lisbon", "valid_from": "2026-04-01",
                                       "claim": {"subject": "Maya", "predicate": "city", "object": "Lisbon"}}]}, ALICE)
        p = recall(self.s, ALICE, "Maya lives", as_of="2026-06-01T00:00:00", include={"history": True})
        old = [i for i in p["items"] if "Paris" in i["text"]][0]
        self.assertEqual(old["validity"], "superseded")
        self.assertTrue(old["text"].startswith("[superseded]"))
        self.assertTrue(old["superseded"])

    def test_M35_time_travel_is_current(self):
        self.s.remember({"memories": [{"kind": "fact", "content": "Maya lives in Paris", "valid_from": "2024-01-01",
                                       "claim": {"subject": "Maya", "predicate": "city", "object": "Paris"}},
                                      {"kind": "fact", "content": "Maya lives in Lisbon", "valid_from": "2026-04-01",
                                       "claim": {"subject": "Maya", "predicate": "city", "object": "Lisbon"}}]}, ALICE)
        p = recall(self.s, ALICE, "Maya lives", as_of="2025-06-01T00:00:00")
        item = [i for i in p["items"] if "Paris" in i["text"]][0]
        self.assertEqual(item["validity"], "current")
        self.assertNotIn("Lisbon", texts(p))

    def test_M35b_valid_to_respected(self):
        self.s.remember({"memories": [{"kind": "fact", "content": "Promo code SPRING is active",
                                       "valid_from": "2026-03-01", "valid_to": "2026-04-01"}]}, ALICE)
        self.assertEqual(recall(self.s, ALICE, "promo code", as_of="2026-05-01T00:00:00")["items"], [])
        self.assertIn("SPRING", texts(recall(self.s, ALICE, "promo code", as_of="2026-03-15T00:00:00")))

    def test_M37_M38_lexical_and_dense_nominate_independently(self):
        s = make_service(RandomEmbedder())
        s.remember({"memories": [ev(f"lunch chatter {i}", thread=f"f{i}") for i in range(200)] +
                    [ev("vault word XANADU-7", thread="v")]}, ALICE)
        self.assertIn("XANADU-7", texts(recall(s, ALICE, "XANADU-7 vault")))

    def test_M42_recency_is_relevance_gated(self):
        s = self.s
        s.engine.remember(Memory(content="The Q3 pricing decision: keep the free tier", thread="old", tenant="acme",
                                 observed_at=datetime(2025, 1, 1)))
        for i in range(10):
            s.engine.remember(Memory(content=f"Random lunch chatter number {i}", thread=f"new{i}", tenant="acme",
                                     observed_at=datetime(2026, 1, 1) + timedelta(days=i)))
        top = recall(s, ALICE, "What is the latest on the pricing decision?", as_of="2026-02-01T00:00:00")["items"][0]
        self.assertIn("pricing", top["text"])

    def test_M49_no_similarity_only_supersession(self):
        ids = self.s.remember({"memories": [{"kind": "fact", "content": "Maya lives in Paris, France"},
                                            {"kind": "fact", "content": "Maya lives in Lisbon, Portugal"}]},
                              ALICE)["ids"]
        self.assertEqual([self.s.engine.mem[i].status for i in ids], ["active", "active"])

    def test_M41_every_feature_in_why(self):
        self.s.remember({"memories": [ev("Alice: pricing deck is ready", actor="Alice", thread="t")]}, ALICE)
        it = recall(self.s, ALICE, "Is the Alice pricing deck ready?")["items"][0]
        self.assertIn("features", it["why"])
        self.assertIn("coverage", it["why"]["features"])
        self.assertIn("ranker", it["why"])

    def test_M43_budget_never_exceeded(self):
        self.s.remember({"memories": [ev("rollout plan detail " * 40, thread=f"t{i}") for i in range(20)]}, ALICE)
        for b in (60, 300, 900):
            p = recall(self.s, ALICE, "rollout plan", tokens=b)
            self.assertLessEqual(p["tokens"], b)

    def test_M45_expansion_only_admissible(self):
        self.s.remember({"memories": [ev("is the acquisition target Globex at 40M?", thread="d", seq=0,
                                         readers=["group:exec"], actor="ceo"),
                                      ev("Yes, Globex sounds right", thread="d", seq=1, actor="cfo")]}, ADMIN)
        for g in ("auto", "span", "thread"):
            t = texts(recall(self.s, ALICE, "Globex", granularity=g))
            self.assertNotIn("40M", t, g)
        self.assertIn("40M", texts(recall(self.s, EXEC, "Globex", granularity="span")))

    def test_M46_snippets_marked_and_cite_full_unit(self):
        long = " ".join(f"Sentence {i} covers topic {i}." for i in range(80)) + " The vault code is 4412."
        mid = self.s.remember({"memories": [ev(long, thread="t")]}, ALICE)["ids"][0]
        it = recall(self.s, ALICE, "vault code", tokens=2000)["items"][0]
        self.assertIn("…", it["text"])
        self.assertIn(mid, it["memory_ids"])

    def test_M50_M51_degraded_reported(self):
        class Broken(HashEmbedder):
            calls = 0

            def embed(self, text):
                Broken.calls += 1
                if text.startswith("Q:"):
                    raise RuntimeError("embedder down")
                return super().embed(text)
        s = make_service(Broken())
        s.remember({"memories": [ev("Q: lexical still works for invoices", thread="t")]}, ALICE)
        p = recall(s, ALICE, "Q: invoices")
        self.assertIn("dense_unavailable", p["degraded"])
        self.assertTrue(p["items"])

    def test_M52_M68_snapshot_changes_with_state(self):
        ids = self.s.remember({"memories": [ev("door code 1234", thread="t")]}, ALICE)["ids"]
        a = recall(self.s, ALICE, "door code")
        b = recall(self.s, ALICE, "door code")
        self.assertEqual((a["snapshot"], [i["text"] for i in a["items"]]), (b["snapshot"], [i["text"] for i in b["items"]]))
        self.s.forget({"ids": ids}, ALICE)
        self.assertNotEqual(a["snapshot"], recall(self.s, ALICE, "door code")["snapshot"])

    def test_M55_items_carry_scope(self):
        self.s.remember({"memories": [ev("Quota policy", scope="org/acme/sales", thread="t")]}, ALICE)
        self.assertEqual(recall(self.s, ALICE, "quota policy")["items"][0]["scope"], "org/acme/sales")

    def test_M55b_scope_within_is_path_based(self):
        self.s.remember({"memories": [ev("Evil twin plan", scope="org/acme-evil/x", thread="e"),
                                      ev("Real plan", scope="org/acme/x", thread="r")]}, ALICE)
        t = texts(recall(self.s, ALICE, "plan", scope={"within": "org/acme"}))
        self.assertIn("Real", t)
        self.assertNotIn("Evil", t)


class Governance(unittest.TestCase):
    def setUp(self):
        self.s = make_service()

    def test_M56_identity_not_assertable_in_body(self):
        with self.assertRaises(ORPError):
            self.s.recall({"query": "x", "principal": "root"}, ALICE)

    def test_M58_get_neighbours_history_check_readers(self):
        a = self.s.remember({"memories": [{"kind": "fact", "content": "Exec bonus pool is 2M", "readers": ["group:exec"],
                                           "claim": {"subject": "bonus", "predicate": "pool", "object": "2M"}}]},
                            ADMIN)["ids"][0]
        self.assertEqual(self.s.get([a], ALICE)["memories"], [])
        with self.assertRaises(ORPError):
            self.s.neighbours(a, 1, 1, ALICE)
        self.assertEqual(self.s.history("bonus", "pool", ALICE)["versions"], [])

    def test_M59_closure_on_write_and_on_change(self):
        a = self.s.remember({"memories": [ev("Roadmap v2 in Q3", id="src", thread="t")]}, ADMIN)["ids"][0]
        d = self.s.remember({"memories": [{"kind": "summary", "origin": "extracted", "derived_from": [a],
                                           "content": "v2 in Q3"}]}, ADMIN)["ids"][0]
        self.s.remember({"memories": [ev("Roadmap v2 in Q3", id="src", thread="t", readers=["group:exec"])],
                         "mode": "upsert", "if_version": 1}, ADMIN)
        self.assertEqual(self.s.get([d], ALICE)["memories"], [])

    def test_M60_secret_gate_on_every_verb(self):
        ids = self.s.remember({"memories": [ev("staging key sk-test-999", labels=["secret"], thread="t")]},
                              ADMIN)["ids"]
        self.assertNotIn("sk-test", texts(recall(self.s, ALICE, "staging key")))
        self.assertNotIn("sk-test", texts(recall(self.s, ALICE, "staging key", include={"labels": ["secret"]})))
        self.assertEqual(self.s.get(ids, ALICE)["memories"], [])
        self.assertNotIn("sk-test", json.dumps(self.s.export(ALICE)))
        holder = Principal("sam", frozenset({"label:secret"}), tenant="acme")
        self.assertNotIn("sk-test", texts(recall(self.s, holder, "staging key")))  # must also ask for it
        self.assertIn("sk-test", texts(recall(self.s, holder, "staging key", include={"labels": ["secret"]})))
        self.assertEqual(self.s.get(ids, holder)["memories"], [])  # get applies the same two conditions
        self.assertEqual(len(self.s.get(ids, holder, labels=("secret",))["memories"]), 1)
        self.assertEqual(self.s.get(ids, ALICE, labels=("secret",))["memories"], [])

    def test_M60b_derived_inherits_secret_label(self):
        a = self.s.remember({"memories": [ev("prod password hunter2", labels=["secret"], thread="t")]}, ADMIN)["ids"][0]
        d = self.s.remember({"memories": [{"kind": "summary", "origin": "extracted", "derived_from": [a],
                                           "content": "password is hunter2"}]}, ADMIN)["ids"][0]
        self.assertIn("secret", self.s.engine.mem[d].labels)

    def test_M61_reads_do_not_mutate(self):
        self.s.remember({"memories": [ev("read-only check", thread="t")]}, ALICE)
        snap = json.dumps(self.s.export(ADMIN)["memories.jsonl"])
        for _ in range(3):
            recall(self.s, ALICE, "read-only")
        self.assertEqual(snap, json.dumps(self.s.export(ADMIN)["memories.jsonl"]))

    def test_redact_leaves_no_residue(self):  # §6.4
        ids = self.s.remember({"memories": [ev("Acquiring Globex Corporation next week", thread="t")]}, ALICE)["ids"]
        self.s.forget({"ids": ids, "mode": "redact"}, ALICE)
        self.assertNotIn("Globex", self.s.export(ADMIN)["memories.jsonl"])

    def test_export_respects_edge_closure(self):  # §3.4
        a = self.s.remember({"memories": [ev("Alpha", thread="a")]}, ADMIN)["ids"][0]
        b = self.s.remember({"memories": [ev("Beta", thread="b", readers=["group:exec"])]}, ADMIN)["ids"][0]
        self.s.link({"type": "relates", "from": a, "to": b}, ADMIN)
        self.assertEqual(self.s.export(ALICE)["edges.jsonl"], "")

    def test_audit_only_for_auditors(self):  # §6.5
        self.s.remember({"memories": [ev("x", thread="t")]}, ALICE)
        with self.assertRaises(ORPError):
            self.s.audit_log(ALICE)
        aud = Principal("aud", frozenset({"auditor"}), tenant="acme")
        self.assertTrue(self.s.audit_log(aud)["entries"])

    def test_envelope_request_id_and_degraded(self):  # §4
        r = self.s.remember({"memories": [ev("x")]}, ALICE)
        self.assertIn("request_id", r)
        self.assertIn("degraded", r)
        self.assertIn("request_id", recall(self.s, ALICE, "x"))


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.s = make_service()

    def test_M27_dry_run_changes_nothing(self):
        a = self.s.remember({"memories": [ev("source", thread="t")]}, ALICE)["ids"][0]
        d = self.s.remember({"memories": [{"kind": "observation", "origin": "inferred", "derived_from": [a],
                                           "content": "derived"}]}, ALICE)["ids"][0]
        snap = self.s.export(ADMIN)["memories.jsonl"]
        r = self.s.forget({"ids": [a], "dry_run": True}, ALICE)
        self.assertEqual(set(r["affected"]), {a, d})
        self.assertEqual(snap, self.s.export(ADMIN)["memories.jsonl"])

    def test_M28_cascade_retracts_unsupported_summary(self):
        a = self.s.remember({"memories": [ev("Client churn risk", thread="t")]}, ALICE)["ids"][0]
        d = self.s.remember({"memories": [{"kind": "summary", "origin": "extracted", "derived_from": [a],
                                           "content": "churn risk"}]}, ALICE)["ids"][0]
        self.s.forget({"ids": [a]}, ALICE)
        self.assertEqual(self.s.engine.mem[d].status, "retracted")

    def test_M29_rule_candidates_are_drafts(self):
        t = self.s.remember({"memories": [ev("outreach draft", thread="t")]}, ALICE)["ids"][0]
        r = self.s.feedback({"target": t, "signal": "draft_final", "draft": "Hey!!! amazing synergy",
                             "final": "Hi Dana, a short note"}, ALICE)
        self.assertEqual(self.s.engine.mem[r["rule_candidate"]].status, "draft")

    def test_M30_feedback_does_not_change_ranking(self):
        ids = self.s.remember({"memories": [ev(f"report {i} on churn", thread=f"t{i}") for i in range(5)]}, ALICE)["ids"]
        before = [i["memory_ids"] for i in recall(self.s, ALICE, "churn report")["items"]]
        self.s.feedback({"target": ids[4], "signal": "helpful"}, ALICE)
        self.assertEqual(before, [i["memory_ids"] for i in recall(self.s, ALICE, "churn report")["items"]])


if __name__ == "__main__":
    unittest.main()
