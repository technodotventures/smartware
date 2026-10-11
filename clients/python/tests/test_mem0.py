"""The mem0 shim: user/agent/run ids are scope and filters, never the principal."""
from __future__ import annotations

import unittest
from typing import List, Tuple

from smartware_client import NotFoundError, SmartwareClient
from smartware_client.mem0 import Memory

from ._server import ServerTestCase


class Mem0Tests(ServerTestCase):
    def setUp(self) -> None:
        self.degraded: List[Tuple[str, List[str]]] = []
        alice = SmartwareClient(self.server.url, token=self.server.tokens["alice"],
                          on_degraded=lambda v, d: self.degraded.append((v, d)))
        self.m = Memory(alice)
        self.bob_m = Memory(self.client("bob"))

    def test_add_search_get_shapes(self) -> None:
        out = self.m.add([{"role": "user", "content": "I'm vegetarian and I love Sichuan food"},
                          {"role": "assistant", "content": "Noted, I'll suggest meat-free Sichuan dishes."}],
                         user_id="priya", metadata={"source": "onboarding"}, infer=False)
        self.assertEqual([r["event"] for r in out["results"]], ["ADD", "ADD"])
        first = out["results"][0]
        self.assertEqual(first["memory"], "I'm vegetarian and I love Sichuan food")
        rec = self.m.client.advanced.get(first["id"])
        self.assertEqual(rec.scope, "mem0/user/priya")
        self.assertEqual(rec.readers, ["principal:alice"])  # the principal is the key's, not "priya"
        hits = self.m.search("what food does she like?", user_id="priya", limit=5)["results"]
        self.assertTrue(hits)
        h = next(x for x in hits if x["id"] == first["id"])
        for k in ("id", "memory", "hash", "metadata", "score", "created_at", "updated_at", "user_id"):
            self.assertIn(k, h)
        self.assertEqual(h["user_id"], "priya")
        self.assertEqual(h["metadata"], {"source": "onboarding"})
        got = self.m.get(first["id"])
        self.assertEqual(got["memory"], first["memory"])
        self.assertEqual(len(self.m.get_all(user_id="priya")["results"]), 2)
        self.assertEqual(self.m.get_all(user_id="priya", filters={"role": "assistant"})["results"][0]["role"],
                         "assistant")

    def test_another_key_cannot_read_by_naming_the_user(self) -> None:
        mid = self.m.add("My passport expires in March", user_id="priya-2", infer=False)["results"][0]["id"]
        self.assertEqual(self.bob_m.search("passport", user_id="priya-2")["results"], [])
        self.assertEqual(self.bob_m.get_all(user_id="priya-2")["results"], [])
        self.assertIsNone(self.bob_m.get(mid))
        with self.assertRaises(NotFoundError):
            self.bob_m.update(mid, "My passport expires in April")
        with self.assertRaises(NotFoundError):
            self.bob_m.delete(mid)
        with self.assertRaises(NotFoundError):
            self.bob_m.history(mid)
        self.bob_m.delete_all(user_id="priya-2")  # finds nothing of alice's to delete
        self.assertEqual(self.m.get(mid)["memory"], "My passport expires in March")

    def test_ids_map_to_scope_segments(self) -> None:
        r = self.m.add("Prefers morning stand-ups", user_id="u7", agent_id="planner", run_id="r1", infer=False)
        mid = r["results"][0]["id"]
        self.assertEqual(self.m.client.advanced.get(mid).scope, "mem0/user/u7/agent/planner/run/r1")
        self.assertEqual([x["id"] for x in self.m.search("stand-ups", user_id="u7")["results"]], [mid])
        self.assertEqual([x["id"] for x in self.m.search("stand-ups", user_id="u7", agent_id="planner")["results"]],
                         [mid])
        self.assertEqual(self.m.search("stand-ups", user_id="u7", run_id="r2")["results"], [])
        self.assertEqual(self.m.search("stand-ups", user_id="u8")["results"], [])
        self.assertEqual([x["id"] for x in self.m.get_all(agent_id="planner")["results"]], [mid])  # after a gap
        self.assertIn(("mem0.search", ["mem0:filter_applied_after_recall"]),
                      (self.m.search("stand-ups", agent_id="planner"), self.degraded)[1])
        with self.assertRaises(ValueError):
            self.m.search("stand-ups")  # mem0 also requires at least one id

    def test_update_delete_history(self) -> None:
        mid = self.m.add("Lives in Carlton", user_id="u9", infer=False)["results"][0]["id"]
        self.assertEqual(self.m.update(mid, "Lives in Brunswick"), {"message": "Memory updated successfully!"})
        self.assertEqual(self.m.get(mid)["memory"], "Lives in Brunswick")
        self.assertIsNotNone(self.m.get(mid)["updated_at"])
        hist = self.m.history(mid)
        self.assertEqual([h["event"] for h in hist], ["ADD", "UPDATE"])
        self.assertEqual(hist[-1]["new_memory"], "Lives in Brunswick")
        self.assertEqual(self.m.delete(mid), {"message": "Memory deleted successfully!"})
        self.assertIsNone(self.m.get(mid))
        self.assertEqual([h["event"] for h in self.m.history(mid)], ["ADD", "UPDATE", "DELETE"])
        again = self.m.add("Lives in Carlton", user_id="u9", infer=False)  # deleted text can be added again
        self.assertEqual(len(again["results"]), 1)

    def test_delete_all_is_scoped(self) -> None:
        self.m.add("Owns a red kelpie", user_id="dog-owner-1", infer=False)
        keep = self.m.add("Owns a grey whippet", user_id="dog-owner-2", infer=False)["results"][0]["id"]
        self.m.delete_all(user_id="dog-owner-1")
        self.assertEqual(self.m.get_all(user_id="dog-owner-1")["results"], [])
        self.assertEqual(self.m.get(keep)["memory"], "Owns a grey whippet")
        with self.assertRaises(ValueError):
            self.m.delete_all()

    def test_infer_is_declared_and_reset_refused(self) -> None:
        self.m.add("Speaks Tamil and English", user_id="u10")  # infer defaults to True, as in mem0
        self.assertTrue(any(v == "mem0.add" for v, _ in self.degraded))
        with self.assertRaises(NotImplementedError):
            self.m.reset()
        with self.assertRaises(Exception):
            self.m.add("x", user_id="u10", prompt="extract facts")


if __name__ == "__main__":
    unittest.main()
