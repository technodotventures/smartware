"""The LangGraph store shim: put/get/search/delete round trips, namespace isolation, fail-closed audience."""
from __future__ import annotations

import asyncio
import datetime as dt
import time
import unittest

from smartware_client.langgraph import GetOp, SmartwareStore, PutOp, SearchOp

from ._server import ServerTestCase


class LangGraphStoreTests(ServerTestCase):
    def setUp(self) -> None:
        self.store = SmartwareStore(self.client("alice"))

    def test_put_get_round_trip(self) -> None:
        value = {"text": "Prefers oat milk flat whites", "tags": ["coffee", "diet"], "nested": {"n": 3, "ok": True},
                 "_private_looking": "kept", "unicode": "café ☕"}
        self.store.put(("users", "u1", "prefs"), "coffee", value)
        item = self.store.get(("users", "u1", "prefs"), "coffee")
        self.assertIsNotNone(item)
        self.assertEqual(item.value, value)
        self.assertEqual(item.key, "coffee")
        self.assertEqual(item.namespace, ("users", "u1", "prefs"))
        self.assertIsInstance(item.created_at, dt.datetime)
        self.assertIsNone(self.store.get(("users", "u1", "prefs"), "missing"))

    def test_update_keeps_created_at(self) -> None:
        ns = ("users", "u1", "profile")
        self.store.put(ns, "city", {"text": "Lives in Brisbane"})
        first = self.store.get(ns, "city")
        time.sleep(0.01)
        self.store.put(ns, "city", {"text": "Moved to Hobart"})
        second = self.store.get(ns, "city")
        self.assertEqual(second.value, {"text": "Moved to Hobart"})
        self.assertEqual(second.created_at, first.created_at)
        self.assertGreater(second.updated_at, first.updated_at)

    def test_search_within_namespace(self) -> None:
        self.store.put(("users", "u2", "notes"), "n1", {"text": "Allergic to shellfish, carries an EpiPen"})
        self.store.put(("users", "u3", "notes"), "n1", {"text": "Allergic to shellfish as well"})
        self.store.put(("users", "u2", "notes"), "n2", {"text": "Supports the Western Bulldogs"})
        hits = self.store.search(("users", "u2"), query="shellfish allergy")
        self.assertEqual([(h.namespace, h.key) for h in hits][:1], [(("users", "u2", "notes"), "n1")])
        self.assertTrue(all(h.namespace[:2] == ("users", "u2") for h in hits))
        self.assertIsNotNone(hits[0].score)
        both = self.store.search(("users",), query="shellfish allergy", limit=10)
        self.assertEqual({h.namespace[1] for h in both if h.key == "n1"}, {"u2", "u3"})
        listed = self.store.search(("users", "u2", "notes"))  # no query: list the namespace
        self.assertEqual({h.key for h in listed}, {"n1", "n2"})
        filtered = self.store.search(("users", "u2", "notes"), filter={"text": "Supports the Western Bulldogs"})
        self.assertEqual([h.key for h in filtered], ["n2"])

    def test_same_key_and_value_in_two_namespaces_stay_separate(self) -> None:
        a, b = ("team", "red"), ("team", "blue")
        self.store.put(a, "motto", {"text": "Ship it"})
        self.store.put(b, "motto", {"text": "Ship it"})
        self.store.delete(a, "motto")
        self.assertIsNone(self.store.get(a, "motto"))
        self.assertEqual(self.store.get(b, "motto").value, {"text": "Ship it"})
        self.assertEqual([h.namespace for h in self.store.search(("team",), query="ship it")], [b])

    def test_delete_then_put_again(self) -> None:
        ns = ("users", "u4")
        self.store.put(ns, "k", {"text": "temporary fact about the u4 sandbox"})
        self.store.delete(ns, "k")
        self.assertIsNone(self.store.get(ns, "k"))
        self.assertEqual(self.store.search(ns, query="u4 sandbox"), [])
        self.store.delete(ns, "never-existed")  # deleting a missing key is a no-op, as in LangGraph
        self.store.put(ns, "k", {"text": "temporary fact about the u4 sandbox"})
        self.assertEqual(self.store.get(ns, "k").value, {"text": "temporary fact about the u4 sandbox"})

    def test_other_principals_cannot_see_items(self) -> None:
        self.store.put(("shared",), "plan", {"text": "Quarterly plan draft for the Falcon launch"})
        bob = SmartwareStore(self.client("bob"))
        self.assertIsNone(bob.get(("shared",), "plan"))
        self.assertEqual(bob.search(("shared",), query="Falcon quarterly plan"), [])
        bob.put(("shared",), "plan", {"text": "Bob's own plan"})  # his own key space: no collision
        self.assertEqual(self.store.get(("shared",), "plan").value["text"], "Quarterly plan draft for the Falcon launch")
        open_store = SmartwareStore(self.client("alice"), audience="tenant", per_principal_keys=False, scope_root="lg-open")
        open_store.put(("wiki",), "wifi", {"text": "Guest wifi password is on the fridge"})
        bob_open = SmartwareStore(self.client("bob"), per_principal_keys=False, scope_root="lg-open")
        self.assertEqual(bob_open.get(("wiki",), "wifi").value["text"], "Guest wifi password is on the fridge")

    def test_list_namespaces(self) -> None:
        s = SmartwareStore(self.client("alice"), scope_root="lg-ns")
        for ns in [("a", "x", "1"), ("a", "y"), ("b", "x")]:
            s.put(ns, "k", {"text": "v"})
        self.assertEqual(s.list_namespaces(), [("a", "x", "1"), ("a", "y"), ("b", "x")])
        self.assertEqual(s.list_namespaces(prefix=("a",)), [("a", "x", "1"), ("a", "y")])
        self.assertEqual(s.list_namespaces(suffix=("x",)), [("b", "x")])
        self.assertEqual(s.list_namespaces(prefix=("*", "x")), [("a", "x", "1"), ("b", "x")])
        self.assertEqual(s.list_namespaces(max_depth=1), [("a",), ("b",)])

    def test_batch_and_async(self) -> None:
        ns = ("batch",)
        res = self.store.batch([PutOp(ns, "a", {"text": "alpha centauri"}), GetOp(ns, "a"),
                                SearchOp(ns, None, 5, 0, "alpha centauri")])
        self.assertIsNone(res[0])
        self.assertEqual(res[1].value, {"text": "alpha centauri"})
        self.assertEqual([h.key for h in res[2]], ["a"])

        async def go():
            await self.store.aput(ns, "b", {"text": "beta pictoris"})
            got = await self.store.aget(ns, "b")
            hits = await self.store.asearch(ns, query="beta pictoris")
            await self.store.adelete(ns, "b")
            return got, hits, await self.store.aget(ns, "b"), await self.store.alist_namespaces(prefix=ns)

        got, hits, gone, spaces = asyncio.run(go())
        self.assertEqual(got.value, {"text": "beta pictoris"})
        self.assertEqual(hits[0].key, "b")
        self.assertIsNone(gone)
        self.assertEqual(spaces, [ns])

    def test_ttl_is_refused_and_losses_are_declared(self) -> None:
        with self.assertRaises(NotImplementedError):
            self.store.put(("t",), "k", {"text": "v"}, ttl=5)
        caps = self.store.capabilities()
        self.assertEqual(caps["level"], "L2")
        self.assertTrue(any(x.startswith("as_of") for x in caps["losses"]))


if __name__ == "__main__":
    unittest.main()
