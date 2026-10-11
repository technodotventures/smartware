"""One store file, two engines: the Python oracle and the TypeScript product engine read and write the same SQLite
store (schema smartware-store-1). Skipped when node or the compiled TypeScript engine is not available."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from smartware import rankers
from smartware.api import Service
from smartware.embedders import HashingEmbedder
from smartware.model import Principal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "bridge", "store_tool.mjs")
DIST = os.path.join(ROOT, "packages", "core", "dist", "index.js")
A = Principal("alice", frozenset({"sales"}), tenant="acme")
ROOTP = Principal("root", frozenset({"*"}), tenant="acme")


def _pj(p):
    return json.dumps({"id": p.id, "groups": sorted(p.groups), "tenant": p.tenant})


def node(*args):
    out = subprocess.run(["node", TOOL, *args], capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


def strip(d):
    d = dict(d)
    d.pop("request_id", None)
    return d


@unittest.skipUnless(shutil.which("node") and os.path.exists(DIST), "needs node and the compiled TypeScript engine")
class StoreInterop(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "shared.db")
        s = Service(HashingEmbedder(), rankers.params("smartware-rrf-1"))
        s.remember({"memories": [
            {"content": "Alice moved to Berlin in March", "kind": "fact", "readers": ["group:sales"], "event_time": "2025-03-01",
             "claim": {"subject": "alice", "predicate": "lives_in", "object": "Berlin"}},
            {"content": "We agreed the Q3 budget is 40k", "thread": "t1", "seq": 1, "event_time": "2025-05-01"},
            {"content": "Bob will send the contract on Friday", "thread": "t1", "seq": 2, "event_time": "2025-05-01"}],
            "idempotency_key": "k1"}, A)
        s.remember({"memories": [{"content": "Alice moved to Paris", "kind": "fact", "readers": ["group:sales"],
                                  "event_time": "2025-09-01",
                                  "claim": {"subject": "alice", "predicate": "lives_in", "object": "Paris"}}]}, A)
        gone = s.remember({"memories": [{"content": "temporary note", "thread": "t2"}]}, A)["ids"]
        s.forget({"ids": gone, "mode": "purge"}, A)
        s.reflect({}, A)
        s.save(self.path)
        self.py = s

    def test_ts_reads_what_python_wrote(self):
        self.assertEqual(strip(self.py.export(ROOTP)), node(self.path, "export", _pj(ROOTP)))
        py = strip(self.py.recall({"query": "where does alice live", "as_of": "2027-01-01T00:00:00"}, A))
        ts = node(self.path, "recall", _pj(A), "where does alice live")
        self.assertEqual([i["memory_ids"] for i in py["items"]], [i["memory_ids"] for i in ts["items"]])
        self.assertEqual(py["snapshot"], ts["snapshot"])  # same secret and counters, so the same snapshot token

    def test_python_reads_what_ts_wrote(self):
        r = node(self.path, "remember", _pj(A), json.dumps({"memories": [{"content": "Carol joined the sales team",
                                                                            "thread": "t1", "seq": 3}],
                                                             "idempotency_key": "k2"}))
        self.assertFalse(r["replayed"])
        again = node(self.path, "remember", _pj(A), json.dumps({"memories": [{"content": "x"}], "idempotency_key": "k1"}))
        self.assertTrue(again["replayed"])  # Python's idempotency key, honoured by TypeScript
        t = Service(HashingEmbedder(), rankers.params("smartware-rrf-1"))
        t.load(self.path)
        self.assertTrue(t.remember({"memories": [{"content": "y"}], "idempotency_key": "k2"}, A)["replayed"])
        self.assertEqual(strip(t.export(ROOTP)), node(self.path, "export", _pj(ROOTP)))
        self.assertEqual(len(t.changes(0, A)["changes"]), len(node(self.path, "changes", _pj(A))["changes"]))


if __name__ == "__main__":
    unittest.main()
