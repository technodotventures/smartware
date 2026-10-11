"""Letta .af import: core memory blocks become always-loaded profile records that profile() returns."""
from __future__ import annotations

import json
import os
import unittest

from smartware_client.letta import import_af

from ._server import ServerTestCase

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "travel_planner.af")


class LettaImportTests(ServerTestCase):
    def setUp(self) -> None:
        self.alice = self.client("alice")

    def test_import_af(self) -> None:
        out = import_af(FIXTURE, self.alice)
        self.assertEqual(out["format"], "agent")
        self.assertEqual(out["version"], "0.6.45")
        a = out["agents"][0]
        self.assertEqual(a["agent"], "travel_planner")
        self.assertEqual(len(a["profile_ids"]), 2)  # human and persona; the empty scratchpad is skipped
        self.assertEqual(a["skipped"], {"empty_blocks": 1, "system_messages": 1, "tool_messages": 1,
                                        "heartbeats": 2, "tool_calls_without_reply": 1})
        self.assertEqual(len(a["event_ids"]), 5)
        self.assertEqual(len(a["document_ids"]), 2)
        self.assertIn("tools", a["not_imported"])

        prof = self.alice.profile(1000)
        texts = [i.text for i in prof.items]
        self.assertTrue(any("Priya Natarajan" in t for t in texts))
        self.assertTrue(any(t.startswith("I am Sam") for t in texts))
        self.assertTrue(set(a["profile_ids"]) <= {i.id for i in prof.items})
        rec = self.alice.advanced.get(a["profile_ids"][0])
        self.assertEqual((rec.kind, rec.load, rec.origin), ("profile", "always", "imported"))
        self.assertEqual(rec.meta["block_label"], "human")
        self.assertEqual(rec.readers, ["principal:alice"])

        thread = self.alice.advanced.thread("travel_planner")
        self.assertEqual([m["content"] for m in thread], [
            "More human than human is our motto.",
            "Hi Sam, I'm planning a trip to Kyoto in early April for the cherry blossoms.",
            "Lovely choice. Early April is peak sakura season, so book the Philosopher's Path walk for early morning.",
            "I'm vegetarian and allergic to peanuts, so please keep that in mind for restaurants.",
            "Noted: vegetarian and no peanuts. Shigetsu at Tenryu-ji serves Buddhist vegetarian cuisine."])
        ev = self.alice.advanced.get(a["event_ids"][1])
        self.assertEqual((ev.role, ev.event_time[:10]), ("user", "2025-03-29"))
        self.assertIn(a["document_ids"][0], self.alice.recall("Kyoto bus day pass", kinds=["document"]).ids)

        # importing again writes nothing new
        before = len(list(self.alice.advanced.changes()))
        again = import_af(FIXTURE, self.alice)
        self.assertEqual(again["agents"][0]["profile_ids"], a["profile_ids"])
        self.assertEqual(len(list(self.alice.advanced.changes())), before)

        # fail closed: another principal in the tenant sees none of it
        bob = self.client("bob")
        self.assertFalse(set(a["profile_ids"]) & {i.id for i in bob.profile(1000).items})

    def test_multi_entity_layout(self) -> None:
        data = {
            "agents": [{"id": "agent-0", "name": "support_bot", "block_ids": ["block-0", "block-1"],
                        "messages": [
                            {"id": "message-0", "role": "user", "created_at": "2025-07-01T10:00:00Z",
                             "content": [{"type": "text", "text": "My order 5521 arrived damaged"}]},
                            {"id": "message-1", "role": "assistant", "created_at": "2025-07-01T10:00:05Z",
                             "content": [{"type": "text", "text": "Sorry about that, a replacement is on its way."}]}]}],
            "blocks": [{"id": "block-0", "label": "persona", "value": "I am Jo, a patient support agent.",
                        "limit": 2000},
                       {"id": "block-1", "label": "human", "value": "Customer since 2021, ships to Perth.",
                        "limit": 2000}],
            "tools": [], "groups": [], "files": [], "sources": [], "mcp_servers": [],
            "metadata": {"revision_id": "rev-1"},
        }
        out = import_af(json.loads(json.dumps(data)), self.alice, audience="tenant")
        a = out["agents"][0]
        self.assertEqual((out["format"], len(a["profile_ids"]), len(a["event_ids"])), ("multi", 2, 2))
        texts = [i.text for i in self.client("bob").profile(1000).items]  # audience="tenant" was explicit
        self.assertIn("I am Jo, a patient support agent.", texts)

    def test_rejects_non_agent_files(self) -> None:
        with self.assertRaises(ValueError):
            import_af({"hello": "world"}, self.alice)


if __name__ == "__main__":
    unittest.main()
