"""Retries: only reads and writes carrying an idempotency key are ever repeated."""
from __future__ import annotations

import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List

from smartware_client import SmartwareClient, RateLimitedError, TransportError, UnavailableError

from ._server import ServerTestCase


class _Stub:
    """A tiny HTTP server that answers 503 a set number of times per route, and counts every request."""

    def __init__(self) -> None:
        self.counts: Dict[str, int] = {}
        self.fail: Dict[str, int] = {}
        self.bodies: List[dict] = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:  # keep test output quiet
                pass

            def _reply(self) -> None:
                verb = self.path.split("?")[0].rstrip("/").split("/")[-1]
                n = int(self.headers.get("content-length") or 0)
                if n:
                    stub.bodies.append(json.loads(self.rfile.read(n)))
                stub.counts[verb] = stub.counts.get(verb, 0) + 1
                if stub.fail.get(verb, 0) != 0:
                    stub.fail[verb] -= 1
                    body = {"error": {"code": "unavailable", "message": "try later", "details": {}}}
                    status = 503
                elif verb == "whoami":
                    body, status = {"id": "alice", "tenant": "acme", "groups": [], "tokens": []}, 200
                elif verb == "recall":
                    body, status = {"items": [], "tokens": 0, "budget": {"tokens": 2000}, "coverage": 0,
                                    "snapshot": "s", "degraded": [], "request_id": "r"}, 200
                elif verb == "forget":
                    body, status = {"dry_run": True, "affected": [], "degraded": [], "request_id": "r"}, 200
                else:
                    body, status = {"ids": ["x"], "replayed": False, "stage": "indexed", "degraded": [],
                                    "request_id": "r"}, 200
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = _reply

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _client(url: str, retries: int = 3, **kw) -> SmartwareClient:
    c = SmartwareClient(url, token="t", tenant="acme", retries=retries, **kw)
    c.delays = []  # type: ignore[attr-defined]
    c._t._sleep = c.delays.append  # type: ignore[attr-defined]  # record instead of waiting
    return c


class StubRetryTests(ServerTestCase):
    """Uses an in-process stub for exact request counts (the class still owns a real server, unused here)."""

    principals = {"alice": {"tenant": "acme"}}

    def setUp(self) -> None:
        self.stub = _Stub()

    def tearDown(self) -> None:
        self.stub.close()

    def test_write_without_key_is_sent_once(self) -> None:
        c = _client(self.stub.url)
        c.principal  # whoami first, so only remember is counted below
        self.stub.fail["remember"] = -1  # always 503
        with self.assertRaises(UnavailableError):
            c.remember("pay the invoice")
        self.assertEqual(self.stub.counts["remember"], 1)
        self.assertEqual(c.delays, [])  # type: ignore[attr-defined]

    def test_write_with_key_is_retried(self) -> None:
        c = _client(self.stub.url, retries=3)
        c.principal
        self.stub.fail["remember"] = 2
        r = c.remember("pay the invoice", idempotency_key="invoice-42")
        self.assertEqual(r.ids, ["x"])
        self.assertEqual(self.stub.counts["remember"], 3)
        keys = {b.get("idempotency_key") for b in self.stub.bodies if "memories" in b}
        self.assertEqual(keys, {"invoice-42"})  # every attempt carries the same key
        self.stub.fail["remember"] = -1
        self.stub.counts["remember"] = 0
        with self.assertRaises(UnavailableError):
            c.remember("pay the invoice", idempotency_key="invoice-43")
        self.assertEqual(self.stub.counts["remember"], 4)  # 1 + retries, then give up

    def test_run_writes_are_retried_with_the_same_derived_key(self) -> None:
        run = _client(self.stub.url).run("r9")
        run.principal
        self.stub.fail["remember"] = 1
        run.remember("step output")
        self.assertEqual(self.stub.counts["remember"], 2)
        self.assertEqual([b["idempotency_key"] for b in self.stub.bodies if "memories" in b], ["run:r9:1"] * 2)

    def test_reads_are_retried_with_backoff(self) -> None:
        c = _client(self.stub.url)
        self.stub.fail["recall"] = 2
        c.recall("anything")
        self.assertEqual(self.stub.counts["recall"], 3)
        self.assertEqual(len(c.delays), 2)  # type: ignore[attr-defined]
        self.assertTrue(all(0 <= d <= 5 for d in c.delays))  # type: ignore[attr-defined]

    def test_forget_is_not_retried_but_a_dry_run_is(self) -> None:
        c = _client(self.stub.url)
        self.stub.fail["forget"] = 1
        with self.assertRaises(UnavailableError):
            c.forget("x")
        self.assertEqual(self.stub.counts["forget"], 1)
        self.stub.fail["forget"] = 1
        c.forget("x", dry_run=True)
        self.assertEqual(self.stub.counts["forget"], 3)

    def test_network_errors(self) -> None:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # nothing listens here now
        c = _client(f"http://127.0.0.1:{port}", retries=2)
        c._who = type("W", (), {"id": "alice"})()  # skip whoami: the point is the write
        with self.assertRaises(TransportError) as cm:
            c.remember("x")
        self.assertIn("not retried", str(cm.exception))
        self.assertEqual(c.delays, [])  # type: ignore[attr-defined]
        with self.assertRaises(TransportError):
            c.recall("x")
        self.assertEqual(len(c.delays), 2)  # type: ignore[attr-defined]


class RealServerRateLimitTests(ServerTestCase):
    """Against the real server with a one-request burst: reads ride out 429s, plain writes do not repeat."""

    principals = {"alice": {"tenant": "acme"}}
    server_args = ["--rate", "2:1"]

    def test_429_is_retried_for_reads_only(self) -> None:
        c = SmartwareClient(self.server.url, token=self.server.tokens["alice"], tenant="acme", retries=5)
        c.principal
        for _ in range(4):  # back-to-back reads exceed the burst; each one still succeeds
            c.recall("anything at all")
        c.recall("drain")  # make sure the next request finds an empty bucket
        with self.assertRaises(RateLimitedError):
            c.remember("written at most once")
        # wait out the bucket, then confirm the refused write was never stored
        found = [m for m in c.advanced.iter_list() if m.content == "written at most once"]
        self.assertEqual(found, [])

    def test_adapter_writes_carry_keys_and_ride_out_429(self) -> None:
        from smartware_client.langgraph import SmartwareStore
        c = SmartwareClient(self.server.url, token=self.server.tokens["alice"], tenant="acme", retries=5)
        store = SmartwareStore(c)
        c.recall("drain")
        store.put(("busy",), "k", {"text": "first"})
        store.put(("busy",), "k", {"text": "second"})  # an upsert under the rate limit
        self.assertEqual(store.get(("busy",), "k").value, {"text": "second"})


if __name__ == "__main__":
    unittest.main()
