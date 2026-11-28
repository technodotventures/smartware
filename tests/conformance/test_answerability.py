"""What memory could not answer (§5.7 uncovered), names entities are known by (§3.4 aliases), and proposals that
replace a memory once approved (§4.1). tools/bridge/run_suite.py runs these against the product engine too."""
from __future__ import annotations

import unittest

from smartware import rankers
from smartware.api import SmartwareError, Service
from smartware.embedders import HashingEmbedder
from smartware.model import Principal

A = Principal("alice", frozenset({"team"}), tenant="t1")
B = Principal("bob", frozenset({"team"}), tenant="t1")


def svc():
    return Service(HashingEmbedder(), rankers.params("smartware-rrf-1"))


def err(fn, *a):
    try:
        fn(*a)
    except SmartwareError as e:
        return e.code
    return "ok"


def recall(s, q, pr, **kw):
    body = {"query": q, "budget": {"tokens": 800, "standing": 0}, **kw}
    return s.recall(body, pr)


def texts(pack):
    return [i["text"] for i in pack["items"]]


class Uncovered(unittest.TestCase):
    def test_M81_uncovered_lists_query_words_no_item_contains(self):
        s = svc()
        s.remember({"memories": [{"kind": "fact", "content": "Maya lives in Lisbon", "readers": ["group:team"]}]}, A)
        r = recall(s, "Where does Maya live, and what is her advertising budget?", A)
        self.assertTrue(any("Lisbon" in t for t in texts(r)))
        self.assertEqual(r["uncovered"], ["advertising", "budget"])

    def test_M81_uncovered_is_every_content_word_when_the_pack_abstains(self):
        s = svc()
        s.remember({"memories": [{"kind": "fact", "content": "Maya lives in Lisbon", "readers": ["group:team"]}]}, A)
        r = recall(s, "Maya's advertising budget", A, min_coverage=1.0)
        self.assertTrue(r["abstain_hint"])
        self.assertEqual(r["uncovered"], ["maya's", "advertising", "budget"])

    def test_M81_a_record_the_asker_cannot_read_never_covers_a_word(self):
        s = svc()
        s.remember({"memories": [{"kind": "fact", "content": "The advertising budget is 40k",
                                  "readers": ["principal:alice"]}]}, A)
        self.assertEqual(recall(s, "advertising budget", A)["uncovered"], [])
        self.assertEqual(recall(s, "advertising budget", B)["uncovered"], ["advertising", "budget"])


class Aliases(unittest.TestCase):
    def setUp(self):
        self.s = svc()
        self.s.remember({"memories": [{"kind": "procedure", "content": f"Post the {w} report in #{w}.",
                                       "readers": ["group:team"]} for w in ("weekly", "sales", "hiring", "budget",
                                                                            "support", "design")]}, A)
        self.rule = self.s.remember({"memories": [
            {"kind": "procedure", "content": "Changelogs go only to #eng-releases.", "readers": ["group:team"]}]},
            A)["ids"][0]

    def top(self, pr):
        return [i["memory_ids"][0] for i in recall(self.s, "post it in the shipping room", pr,
                                                   budget={"tokens": 800, "items": 3, "standing": 0})["items"]]

    def entity(self, readers):
        return self.s.remember({"memories": [{"kind": "entity", "content": "#eng-releases", "readers": readers,
                                              "meta": {"aliases": ["shipping room", "eng releases"]}}]}, A)["ids"][0]

    def test_M82_a_name_an_entity_is_known_by_finds_what_its_other_names_find(self):
        self.assertNotIn(self.rule, self.top(A))
        ent = self.entity(["group:team"])
        self.assertIn(self.rule, self.top(A))
        r = recall(self.s, "shipping room", A)
        self.assertIn(ent, [i["memory_ids"][0] for i in r["items"]])
        self.assertTrue(any("(also: shipping room; eng releases)" in t for t in texts(r)))
        self.assertEqual(r["uncovered"], [])

    def test_M82_an_entity_the_asker_cannot_read_changes_nothing_they_see(self):
        before = recall(self.s, "post it in the shipping room", B)
        self.entity(["principal:alice"])
        after = recall(self.s, "post it in the shipping room", B)
        for k in ("items", "uncovered", "coverage", "notes"):
            self.assertEqual(after[k], before[k], k)

    def test_aliases_must_be_a_list_of_strings(self):
        for bad in ("shipping room", [""], [3], ["x" * 201], ["a"] * 33):
            self.assertEqual(err(self.s.remember, {"memories": [{"kind": "entity", "content": "#eng",
                                                                 "meta": {"aliases": bad}}]}, A),
                             "invalid_parameter", bad)
        # on other kinds, meta.aliases is ordinary client meta
        self.assertEqual(err(self.s.remember, {"memories": [{"kind": "fact", "content": "x", "meta": {"aliases": 3}}]},
                             A), "ok")


class ProposedReplacement(unittest.TestCase):
    def test_M83_a_proposal_replaces_what_it_names_only_once_approved(self):
        s = svc()
        old = s.remember({"memories": [{"kind": "procedure", "content": "Invoices go out on the 1st."}]}, A)["ids"][0]
        new = s.remember({"memories": [{"kind": "procedure", "content": "Invoices go out on the 15th.",
                                        "supersedes": [old]}], "mode": "propose"}, A)["ids"][0]
        got = {m["id"]: m for m in s.get([old, new], A)["memories"]}
        self.assertEqual((got[old]["status"], got[new]["status"]), ("active", "draft"))
        s.approve(new, A)
        got = {m["id"]: m for m in s.get([old, new], A)["memories"]}
        self.assertEqual((got[old]["status"], got[old]["superseded_by"]), ("superseded", new))
        self.assertEqual((got[new]["status"], got[new]["supersedes"]), ("active", [old]))

    def test_M83_a_proposal_may_only_name_what_its_writer_may_write(self):
        s = svc()
        old = s.remember({"memories": [{"kind": "procedure", "content": "Invoices go out on the 1st.",
                                        "readers": ["group:team"]}]}, A)["ids"][0]
        self.assertEqual(err(s.remember, {"memories": [{"kind": "procedure", "content": "Invoices on the 15th.",
                                                        "supersedes": [old]}], "mode": "propose"}, B), "forbidden")

    def test_M83_approval_leaves_alone_what_is_no_longer_current(self):
        s = svc()
        old = s.remember({"memories": [{"kind": "procedure", "content": "Invoices go out on the 1st."}]}, A)["ids"][0]
        new = s.remember({"memories": [{"kind": "procedure", "content": "Invoices go out on the 15th.",
                                        "supersedes": [old]}], "mode": "propose"}, A)["ids"][0]
        s.forget({"ids": [old], "mode": "retract"}, A)
        s.approve(new, A)
        got = {m["id"]: m for m in s.get([new], A)["memories"]}
        self.assertEqual((got[new]["status"], got[new]["supersedes"]), ("active", []))


if __name__ == "__main__":
    unittest.main()
