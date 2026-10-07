"""End-to-end tests of the HTTP and MCP bindings (spec §8)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from orp import http_server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class HTTPBinding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        keys = {"tok_a": {"id": "alice", "groups": ["sales"], "tenant": "acme"},
                "tok_b": {"id": "bob", "groups": ["eng"], "tenant": "acme"},
                "tok_e": {"id": "eve", "groups": [], "tenant": "other"}}
        kp = os.path.join(cls.tmp, "keys.json")
        json.dump(keys, open(kp, "w"))
        cls.store = os.path.join(cls.tmp, "store.json")
        svc, k = http_server.build(kp, cls.store, "hashing")
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), http_server.make_handler(svc, k, cls.store))
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, method, path, tok, body=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json",
                                              **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_flow(self):
        c, r = self.call("POST", "/v1/acme/remember", "tok_a", {"memories": [
            {"kind": "event", "content": "Renewal call with Globex moved to Thursday", "thread": "crm", "seq": 1,
             "readers": ["group:sales"]}]})
        self.assertEqual(c, 200)
        mid = r["ids"][0]
        c, r = self.call("POST", "/v1/acme/recall", "tok_a", {"query": "When is the Globex renewal call?",
                                                              "budget": {"tokens": 300}})
        self.assertEqual(c, 200)
        self.assertIn("Thursday", r["items"][0]["text"])
        c, r = self.call("POST", "/v1/acme/recall", "tok_b", {"query": "Globex renewal", "budget": {"tokens": 300}})
        self.assertEqual(r["items"], [])  # pre-ranking ACL
        c, r = self.call("GET", f"/v1/acme/memories/{mid}", "tok_b")
        self.assertEqual(c, 404)  # forbidden looks like absent
        c, r = self.call("POST", "/v1/other/recall", "tok_a", {"query": "x"})
        self.assertEqual(c, 403)
        c, r = self.call("POST", "/v1/acme/recall", "nope", {"query": "x"})
        self.assertEqual(c, 401)
        c, r = self.call("POST", "/v1/acme/recall", "tok_a", {"query": "x", "rerank": "aggregate"})
        self.assertEqual((c, r["error"]["code"]), (400, "unsupported_parameter"))
        c, r = self.call("POST", "/v1/acme/remember", "tok_a", {"memories": [
            {"id": mid, "kind": "event", "content": "Renewal call with Globex moved to Friday", "thread": "crm"}],
            "mode": "upsert"}, headers={"If-Match": '"7"'})
        self.assertEqual(c, 409)
        c, r = self.call("GET", "/v1/acme/capabilities", "tok_a")
        self.assertEqual(r["spec"], "0.1")
        self.assertTrue(os.path.exists(self.store))


class MCPBinding(unittest.TestCase):
    def test_stdio_session(self):
        store = os.path.join(tempfile.mkdtemp(), "s.json")
        env = dict(os.environ, ORP_PRINCIPAL="stevie", ORP_TENANT="tv", PYTHONPATH=ROOT)
        p = subprocess.Popen([sys.executable, "-m", "orp.mcp_server", "--store", store], cwd=ROOT, env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

        def rpc(i, method, params=None):
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
            p.stdin.flush()
            return json.loads(p.stdout.readline())

        try:
            init = rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "t", "version": "0"}})
            self.assertIn("instructions", init["result"])
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            tools = rpc(2, "tools/list")["result"]["tools"]
            names = {t["name"] for t in tools}
            self.assertTrue({"orp_recall", "orp_remember", "orp_forget"} <= names)
            forget = [t for t in tools if t["name"] == "orp_forget"][0]
            self.assertTrue(forget["annotations"]["destructiveHint"])
            r = rpc(3, "tools/call", {"name": "orp_remember", "arguments": {"memories": [
                {"kind": "fact", "content": "Stevie prefers British spelling",
                 "claim": {"subject": "Stevie", "predicate": "spelling", "object": "British"}, "load": "always"}]}})
            mid = r["result"]["structuredContent"]["ids"][0]
            r = rpc(4, "tools/call", {"name": "orp_recall", "arguments": {"query": "What spelling does Stevie prefer?"}})
            self.assertIn("British", r["result"]["content"][0]["text"])
            r = rpc(5, "resources/read", {"uri": f"orp://tv/mem/{mid}"})
            self.assertIn("British", r["result"]["contents"][0]["text"])
            sub = rpc(7, "resources/subscribe", {"uri": "orp://tv/thread/prefs"})
            self.assertEqual(sub["result"], {})
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {
                "name": "orp_remember", "arguments": {"memories": [{"content": "Stevie likes flat whites",
                                                                    "thread": "prefs"}]}}}) + "\n")
            p.stdin.flush()
            first = json.loads(p.stdout.readline())
            second = json.loads(p.stdout.readline())
            note = first if first.get("method") else second
            self.assertEqual(note["method"], "notifications/resources/updated")
            self.assertEqual(note["params"]["uri"], "orp://tv/thread/prefs")
            r = rpc(6, "tools/call", {"name": "orp_recall", "arguments": {"query": "x", "who_am_i": "admin"}})
            self.assertIn("error", r)  # identity cannot be asserted through arguments
            self.assertTrue(os.path.exists(store))
        finally:
            p.kill()


if __name__ == "__main__":
    unittest.main()
