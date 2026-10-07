"""ORP HTTP binding (spec §8.2). Standard library only.

    python -m orp.http_server --port 8787 --keys keys.json [--store data.orp.json] [--embedder sif:path]

keys.json maps bearer tokens to principals, which is how the principal is bound
server-side (spec §6.1):
    {"tok_alice": {"id": "alice", "groups": ["sales"], "tenant": "acme"}}
"""
from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import embedders
from .api import ORPError, Service
from .model import Principal

_LOCK = threading.RLock()


def make_handler(svc: Service, keys: dict[str, Principal], store: str | None):
    def persist():
        if not store:
            return
        tenants = {p.tenant for p in keys.values()}
        data = {t: svc.export(Principal("system", frozenset({"*"}), tenant=t)) for t in tenants}
        tmp = store + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, store)

    class H(BaseHTTPRequestHandler):
        server_version = "orp-reference/0.1"

        def _principal(self) -> Principal:
            auth = self.headers.get("Authorization", "")
            tok = auth[7:] if auth.startswith("Bearer ") else ""
            p = keys.get(tok)
            if p is None:
                raise ORPError("unauthorized", "missing or unknown bearer token")
            return p

        def _send(self, code: int, body: dict):
            data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _route(self, method: str):
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            try:
                pr = self._principal()
                if len(parts) < 3 or parts[0] != "v1":
                    raise ORPError("not_found", u.path)
                if parts[1] != pr.tenant:
                    raise ORPError("forbidden", "tenant mismatch")
                verb = parts[2]
                body = {}
                if method == "POST":
                    n = int(self.headers.get("Content-Length", "0") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                with _LOCK:
                    if method == "GET" and verb == "capabilities":
                        out = svc.capabilities(pr)
                    elif method == "POST" and verb in ("recall", "remember", "forget", "feedback", "reflect",
                                                       "profile", "link"):
                        if verb == "remember" and "If-Match" in self.headers:
                            body.setdefault("if_version", int(self.headers["If-Match"].strip('"')))
                        out = getattr(svc, verb)(body, pr)
                        if verb in ("remember", "forget", "feedback", "reflect", "link"):
                            persist()
                    elif method == "GET" and verb == "memories" and len(parts) == 4:
                        r = svc.get([parts[3]], pr)
                        if not r["memories"]:
                            raise ORPError("not_found", parts[3])
                        out = r["memories"][0]
                    elif method == "GET" and verb == "memories" and len(parts) == 5 and parts[4] == "neighbours":
                        out = svc.neighbours(parts[3], int(q.get("before", 2)), int(q.get("after", 2)), pr)
                    elif method == "GET" and verb == "history":
                        out = svc.history(q["subject"], q["predicate"], pr)
                    elif method == "GET" and verb == "changes":
                        out = svc.changes(int(q.get("cursor", 0)), pr)
                    elif method == "GET" and verb == "jobs" and len(parts) == 4:
                        out = svc.job(parts[3])
                    elif method == "POST" and verb == "approve" and len(parts) == 4:
                        out = svc.approve(parts[3], pr)
                        persist()
                    elif method == "GET" and verb == "export":
                        out = svc.export(pr)
                    else:
                        raise ORPError("not_found", u.path)
                self._send(200, out)
            except ORPError as e:
                code = {"unauthorized": 401, "forbidden": 403, "not_found": 404, "version_conflict": 409,
                        "rate_limited": 429}.get(e.code, 400)
                self._send(code, e.to_json())
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                self._send(400, {"error": {"code": "invalid_parameter", "message": str(e), "details": {}}})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def log_message(self, *a):
            pass

    return H


def build(keys_path: str | None, store: str | None, embedder: str | None):
    raw = json.load(open(keys_path)) if keys_path else {"dev": {"id": "dev", "groups": ["*"], "tenant": "default"}}
    keys = {k: Principal(v["id"], frozenset(v.get("groups", [])), tenant=v.get("tenant", "default"))
            for k, v in raw.items()}
    svc = Service(embedders.load(embedder))
    if store and os.path.exists(store):
        for tenant, bundle in json.load(open(store)).items():
            svc.import_(bundle, Principal("system", frozenset({"*"}), tenant=tenant))
    return svc, keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--keys")
    ap.add_argument("--store")
    ap.add_argument("--embedder", default="hashing")
    a = ap.parse_args()
    svc, keys = build(a.keys, a.store, a.embedder)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(svc, keys, a.store))
    print(f"ORP HTTP listening on http://{a.host}:{a.port}/v1/<tenant>/...", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
