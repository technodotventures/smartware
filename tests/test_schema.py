"""The schema pack (schema/, generated from packages/core/src/schema.ts) agrees with the spec's field table and with
the oracle's validators; real responses validate against it (when the jsonschema package is installed)."""
import json
import os
import re
import unittest

from orp import api
from orp.api import Service
from orp.embedders import HashingEmbedder
from orp.model import Principal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SD = os.path.join(ROOT, "schema", "orp-0.1")


def load(name):
    with open(os.path.join(SD, name + ".schema.json")) as f:
        return json.load(f)


def props(name):
    return set(load(name)["properties"])


class SchemaDrift(unittest.TestCase):
    def test_validators_match_schema(self):
        self.assertEqual(props("MemoryInput"), api._MEM_KEYS)
        self.assertEqual(props("RememberRequest"), api._REMEMBER_KEYS)
        self.assertEqual(props("RecallRequest"), api._RECALL_KEYS)
        self.assertEqual(props("ForgetRequest"), api._FORGET_KEYS)

    def test_spec_field_table_covered(self):
        spec = open(os.path.join(ROOT, "spec", "ORP-0.1.md")).read()
        table = spec[spec.index("### 3.1 The Memory record"):spec.index("### 3.2 Kinds")]
        fields = set()
        for row in table.splitlines():
            m = re.match(r"\|\s*(`[^|]+)\|", row)
            if m:
                fields |= set(re.findall(r"`([a-z_]+)`", m.group(1)))
        self.assertTrue(fields)
        self.assertEqual(fields - props("Memory"), set())


try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None


@unittest.skipUnless(jsonschema, "jsonschema not installed")
class ResponsesValidate(unittest.TestCase):
    def check(self, name, obj):
        jsonschema.validate(obj, load(name), cls=jsonschema.Draft202012Validator)

    def test_responses(self):
        a = Principal("alice", frozenset({"sales"}), tenant="acme")
        s = Service(HashingEmbedder())
        body = {"memories": [{"content": "Alice moved to Berlin", "kind": "fact", "readers": ["group:sales"],
                              "claim": {"subject": "alice", "predicate": "lives_in", "object": "Berlin"},
                              "event_time": {"start": "2025-01-01", "end": None, "granularity": "day"}}]}
        self.check("RememberRequest", body)
        r = s.remember(body, a)
        self.check("RememberResponse", r)
        self.check("ContextPack", s.recall({"query": "where does alice live"}, a))
        self.check("Memory", s.get(r["ids"], a)["memories"][0])
        self.check("ForgetResponse", s.forget({"ids": r["ids"], "dry_run": True}, a))
        self.check("ChangesResponse", s.changes(0, a))
        self.check("Bundle", s.export(Principal("root", frozenset({"*"}), tenant="acme")))


if __name__ == "__main__":
    unittest.main()
