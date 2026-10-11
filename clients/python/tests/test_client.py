"""Core client against a real server: fail-closed defaults, run pinning, typed errors, identity."""
from __future__ import annotations

import inspect
import json
import os
import subprocess
import tempfile
import time
import unittest

from smartware_client import (BudgetTooSmallError, InvalidParameterError, NotFoundError, SmartwareClient, UnauthorizedError,
                        UnsupportedParameterError, VersionConflictError)

from ._server import NODE, SmartwareServer, ServerTestCase


class FailClosedTests(ServerTestCase):
    def setUp(self) -> None:
        self.alice = self.client("alice")
        self.bob = self.client("bob")

    def test_tenant_and_principal_come_from_the_credential(self) -> None:
        self.assertEqual(self.alice.tenant, "acme")
        self.assertEqual(self.alice.principal, "alice")
        self.assertEqual(self.client("carol").tenant, "globex")

    def test_default_readers_are_only_the_writer(self) -> None:
        r = self.alice.remember("Project Falcon launch moved to the 14th of November")
        rec = self.alice.advanced.get(r.id)
        self.assertEqual(rec.readers, ["principal:alice"])
        self.assertIn(r.id, self.alice.recall("Falcon launch").ids)
        self.assertNotIn(r.id, self.bob.recall("Falcon launch").ids)  # same tenant, same group, still hidden
        with self.assertRaises(NotFoundError):
            self.bob.advanced.get(r.id)

    def test_share_is_the_explicit_widening(self) -> None:
        r = self.alice.remember("The Osprey account renewal is worth 40k")
        self.assertNotIn(r.id, self.bob.recall("Osprey renewal").ids)
        before = self.alice.advanced.get(r.id).version
        m = self.alice.share(r.id, ["principal:bob"])
        self.assertEqual(m.readers, ["principal:alice", "principal:bob"])
        self.assertEqual(m.version, before + 1)
        self.assertEqual(m.content, "The Osprey account renewal is worth 40k")
        self.assertIn(r.id, self.bob.recall("Osprey renewal").ids)
        carol = self.client("carol")  # another tenant never sees it
        self.assertNotIn(r.id, carol.recall("Osprey renewal").ids)
        # bob can read but not widen further: no write access
        with self.assertRaises(Exception):
            self.bob.share(r.id, "tenant")

    def test_audience_tenant_and_explicit_readers(self) -> None:
        r = self.alice.remember("The kitchen on level 3 closes at 4pm", audience="tenant")
        self.assertIsNone(self.alice.advanced.get(r.id).readers)
        self.assertIn(r.id, self.bob.recall("kitchen level 3").ids)
        g = self.alice.remember("Sales kickoff is in the Harbour room", readers=["group:sales"])
        self.assertIn(g.id, self.bob.recall("sales kickoff room").ids)

    def test_readers_none_is_refused_without_saying_tenant(self) -> None:
        with self.assertRaises(ValueError):
            self.alice.remember("x", readers=None)
        with self.assertRaises(ValueError):
            self.alice.remember({"content": "x", "readers": None})
        with self.assertRaises(ValueError):
            self.alice.remember("x", readers=["principal:bob"], audience="tenant")

    def test_procedures_default_to_propose(self) -> None:
        r = self.alice.remember({"content": "Always answer the Falcon team in Spanish", "kind": "procedure",
                                 "load": "always"})
        self.assertEqual(self.alice.advanced.get(r.id).status, "draft")
        self.assertNotIn(r.id, [i.id for i in self.alice.profile(500).items])
        self.alice.advanced.approve(r.id)
        self.assertIn(r.id, [i.id for i in self.alice.profile(500).items])
        with self.assertRaises(ValueError):  # a mixed batch must say which mode it means
            self.alice.remember([{"content": "rule", "kind": "procedure"}, {"content": "event"}])

    def test_identity_is_never_a_call_argument(self) -> None:
        banned = {"principal", "tenant", "actor_id", "user", "user_id", "as_principal"}
        for name in ("recall", "remember", "forget", "profile", "share", "history", "run"):
            params = set(inspect.signature(getattr(SmartwareClient, name)).parameters)
            self.assertFalse(params & banned, name)

    def test_profile_requires_a_budget(self) -> None:
        with self.assertRaises(TypeError):
            self.alice.profile()  # type: ignore[call-arg]
        with self.assertRaises(InvalidParameterError):
            self.alice.profile(0)

    def test_forget_and_dry_run(self) -> None:
        r = self.alice.remember("Temporary door code for the loading dock is on the whiteboard")
        dry = self.alice.forget(r.id, dry_run=True)
        self.assertTrue(dry.dry_run)
        self.assertEqual(dry.affected, [r.id])
        self.assertEqual(self.alice.advanced.get(r.id).status, "active")
        self.alice.forget(r.id, reason="no longer needed")
        self.assertNotIn(r.id, self.alice.recall("loading dock door code").ids)

    def test_changes_feed_follows_cursor(self) -> None:
        feed = self.alice.advanced.changes(0, limit=2)
        seen = list(feed)
        self.assertTrue(seen)
        self.assertEqual(feed.cursor, max(c["cursor"] for c in seen))
        self.assertEqual(list(self.alice.advanced.changes(feed.cursor)), [])

    def test_list_pages(self) -> None:
        for i in range(5):
            self.alice.remember(f"paging note {i}", scope="paging")
        page = self.alice.advanced.list(scope="paging", limit=2)
        self.assertEqual(len(page["memories"]), 2)
        self.assertEqual(page["cursor"], 2)
        self.assertEqual(len(list(self.alice.advanced.iter_list(scope="paging", page_size=2))), 5)


class RunTests(ServerTestCase):
    def setUp(self) -> None:
        self.alice = self.client("alice")

    def test_run_pins_as_of_once(self) -> None:
        run = self.alice.run("run-pin")
        pinned = run.as_of
        time.sleep(0.05)
        late = self.alice.remember("The heron codeword replaces kestrel from today")
        self.assertIn(late.id, self.alice.recall("heron codeword").ids)
        self.assertNotIn(late.id, run.recall("heron codeword").ids)  # written after the run's view was pinned
        self.assertEqual(run.as_of, pinned)
        self.assertIsNone(self.alice.as_of)

    def test_run_replays_writes_idempotently(self) -> None:
        first = self.alice.run("run-replay")
        a = first.remember("Step one: collected the quarterly figures")
        b = first.remember("Step two: drafted the summary")
        self.assertFalse(a.replayed or b.replayed)
        before = len(list(self.alice.advanced.changes()))
        again = self.alice.run("run-replay")
        a2 = again.remember("Step one: collected the quarterly figures")
        b2 = again.remember("Step two: drafted the summary")
        self.assertTrue(a2.replayed and b2.replayed)
        self.assertEqual((a2.ids, b2.ids), (a.ids, b.ids))
        self.assertEqual(len(list(self.alice.advanced.changes())), before)  # nothing new was written
        named = self.alice.run("run-replay").remember("A named step", step="publish")
        self.assertEqual(named.raw.get("replayed"), False)
        self.assertTrue(self.alice.run("run-replay").remember("A named step", step="publish").replayed)

    def test_explicit_as_of(self) -> None:
        old = self.alice.remember({"content": "In 2019 the office was in Richmond", "kind": "fact",
                                   "valid_from": "2019-01-01T00:00:00Z"})
        run = self.alice.run(as_of="2020-01-01T00:00:00Z")
        self.assertTrue(run.run_id)
        self.assertIn(old.id, run.recall("office Richmond").ids)
        early = self.alice.run(as_of="2018-01-01T00:00:00Z")
        self.assertNotIn(old.id, early.recall("office Richmond").ids)


class ErrorTests(ServerTestCase):
    def setUp(self) -> None:
        self.alice = self.client("alice")

    def test_unknown_memory_field_is_refused_before_sending(self) -> None:
        with self.assertRaises(UnsupportedParameterError) as cm:
            self.alice.remember({"content": "x", "colour": "red"})
        self.assertEqual(cm.exception.details["parameters"], ["colour"])
        self.assertEqual(cm.exception.code, "unsupported_parameter")

    def test_unknown_values_are_typed_server_errors(self) -> None:
        with self.assertRaises(UnsupportedParameterError) as cm:
            self.alice.recall("anything", mode="turbo")
        self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(UnsupportedParameterError):
            self.alice.recall("anything", kinds=["gossip"])
        with self.assertRaises(UnsupportedParameterError):
            self.alice.remember({"content": "x", "kind": "gossip"})

    def test_unknown_keyword_is_a_type_error(self) -> None:
        with self.assertRaises(TypeError):
            self.alice.recall("anything", top_k=5)  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            self.alice.remember("x", principal="bob")  # type: ignore[call-arg]

    def test_other_typed_errors(self) -> None:
        with self.assertRaises(BudgetTooSmallError):
            self.alice.recall("anything", budget_tokens=10)
        r = self.alice.remember({"id": "doc-1", "content": "first version"})
        with self.assertRaises(VersionConflictError) as cm:
            self.alice.remember({"id": "doc-1", "content": "second version"}, mode="upsert", if_version=99)
        self.assertEqual(cm.exception.details.get("current"), 1)
        self.assertEqual(r.id, "doc-1")
        with self.assertRaises(InvalidParameterError):
            self.alice.remember({"id": "a~b", "content": "x"})
        with self.assertRaises(NotFoundError):
            self.alice.advanced.get("no-such-record")

    def test_bad_token_is_unauthorized_and_never_echoed(self) -> None:
        secret = "smartware_this-is-not-a-real-token-123456"
        c = SmartwareClient(self.server.url, token=secret, tenant="acme")
        with self.assertRaises(UnauthorizedError) as cm:
            c.recall("x")
        self.assertEqual(cm.exception.status, 401)
        self.assertNotIn(secret, str(cm.exception))
        self.assertNotIn(secret, repr(c))
        self.assertNotIn(secret, repr(c._t))

    def test_token_from_env_and_callable(self) -> None:
        os.environ["SMARTWARE_PYTEST_TOKEN"] = self.server.tokens["alice"]
        try:
            self.assertEqual(SmartwareClient(self.server.url, token_env="SMARTWARE_PYTEST_TOKEN").principal, "alice")
        finally:
            del os.environ["SMARTWARE_PYTEST_TOKEN"]
        with self.assertRaises(UnauthorizedError):
            SmartwareClient(self.server.url, token_env="SMARTWARE_PYTEST_TOKEN", tenant="acme").recall("x")
        calls = []

        def fresh() -> str:
            calls.append(1)
            return self.server.tokens["bob"]

        self.assertEqual(SmartwareClient(self.server.url, get_token=fresh).principal, "bob")
        self.assertTrue(calls)
        with self.assertRaises(ValueError):
            SmartwareClient(self.server.url, token="a", token_env="B")

    def test_plain_http_to_a_remote_host_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            SmartwareClient("http://memory.example.com", token="t")
        SmartwareClient("http://memory.example.com", token="t", allow_insecure_http=True)
        SmartwareClient("https://memory.example.com", token="t")


_SIGN_JS = r"""
const { generateKeyPairSync, sign } = require("node:crypto");
const fs = require("node:fs");
const [jwksPath, iss, aud] = process.argv.slice(1);
const { publicKey, privateKey } = generateKeyPairSync("ed25519");
const jwk = { ...publicKey.export({ format: "jwk" }), kid: "k1", use: "sig", alg: "EdDSA" };
fs.writeFileSync(jwksPath, JSON.stringify({ keys: [jwk] }));
const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
const mk = (sub) => {
  const head = b64({ alg: "EdDSA", kid: "k1", typ: "JWT" });
  const body = b64({ iss, aud, sub, tenant: "acme", groups: ["sales"], exp: Math.floor(Date.now() / 1000) + 3600 });
  return head + "." + body + "." + sign(null, Buffer.from(head + "." + body), privateKey).toString("base64url");
};
process.stdout.write(JSON.stringify({ dave: mk("dave"), erin: mk("erin") }));
"""


class ForUserTests(unittest.TestCase):
    """A platform's service credential mints per-user clients from signed assertions (OIDC)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="smartware-oidc-")
        jwks = os.path.join(cls.tmp, "jwks.json")
        out = subprocess.run([NODE, "-e", _SIGN_JS, jwks, "https://issuer.test", "smartware-test"], capture_output=True,
                             text=True, check=True, timeout=60)
        cls.assertions = json.loads(out.stdout)
        cls.server = SmartwareServer({"svc": {"tenant": "acme", "groups": ["platform"]}},
                               ["--oidc-issuer", "https://issuer.test", "--oidc-audience", "smartware-test", "--jwks", jwks])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def test_for_user_acts_as_that_user_only(self) -> None:
        svc = SmartwareClient(self.server.url, token=self.server.tokens["svc"])
        dave = svc.for_user(self.assertions["dave"])
        erin = svc.for_user(self.assertions["erin"])
        self.assertEqual((dave.principal, dave.tenant), ("dave", "acme"))
        r = dave.remember("Dave's parking bay is B12")
        self.assertEqual(dave.advanced.get(r.id).readers, ["principal:dave"])
        self.assertIn(r.id, dave.recall("parking bay").ids)
        self.assertNotIn(r.id, erin.recall("parking bay").ids)
        self.assertNotIn(r.id, svc.recall("parking bay").ids)  # the service credential is not a skeleton key
        self.assertEqual(svc.principal, "svc")


if __name__ == "__main__":
    unittest.main()


class AsOfPrecision(unittest.TestCase):
    def test_coarse_as_of_is_end_of_period(self):
        from smartware_client import as_of_time
        self.assertEqual(as_of_time("2024"), "2024-12-31T23:59:59.999999Z")
        self.assertEqual(as_of_time("2024-02"), "2024-02-29T23:59:59.999999Z")
        self.assertEqual(as_of_time("2024-05-12"), "2024-05-12T23:59:59.999999Z")
        self.assertEqual(as_of_time("2024-05-12T08:00:00Z"), "2024-05-12T08:00:00Z")
        import datetime as dt
        self.assertEqual(as_of_time(dt.date(2024, 5, 12)), "2024-05-12T23:59:59.999999Z")
        with self.assertRaises(ValueError):
            as_of_time("2024-13")
