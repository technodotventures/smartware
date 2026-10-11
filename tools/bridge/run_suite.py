"""Run the oracle's conformance suite against the TypeScript service (product engine) through tools/bridge/server.mjs.

    python3 tools/bridge/run_suite.py            # every conformance test, Service replaced by the TS bridge
"""
import json
import os
import subprocess
import sys
import unittest
from dataclasses import asdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
import orp.api as api  # noqa: E402
from orp.engine import Engine as PyEngine, Params  # noqa: E402
from orp.model import Memory  # noqa: E402

_proc = subprocess.Popen(["node", os.path.join(ROOT, "tools", "bridge", "server.mjs")], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True, bufsize=1)
_n = [0]


def rpc(**msg):
    _n[0] += 1
    msg["id"] = _n[0]
    _proc.stdin.write(json.dumps(msg, default=str) + "\n")
    _proc.stdin.flush()
    out = json.loads(_proc.stdout.readline())
    if "err" in out:
        e = out["err"]
        raise api.ORPError(e["code"], e["message"], **(e.get("details") or {}))
    if "crash" in out:
        raise RuntimeError("TS crash: " + out["crash"])
    return out["ok"]


def _pr(p):
    return {"id": p.id, "groups": sorted(p.groups), "tenant": p.tenant}


class _MemRef:
    """A Memory whose attribute writes go through to the TS engine (tests poke observed_at and the like)."""

    def __init__(self, h, mid, m):
        object.__setattr__(self, "_h", h)
        object.__setattr__(self, "_mid", mid)
        object.__setattr__(self, "_m", m)

    def __getattr__(self, k):
        return getattr(object.__getattribute__(self, "_m"), k)

    def __setattr__(self, k, v):
        setattr(self._m, k, v)
        rpc(op="mem_patch", h=self._h, mid=self._mid, field=k, value=v.isoformat() if hasattr(v, "isoformat") else v)


class _MemProxy:
    def __init__(self, h):
        self.h = h

    def __getitem__(self, i):
        d = rpc(op="mem_get", h=self.h, mid=i)
        if d is None:
            raise KeyError(i)
        return _MemRef(self.h, i, Memory.from_json(d))

    def get(self, i, default=None):
        d = rpc(op="mem_get", h=self.h, mid=i)
        return Memory.from_json(d) if d is not None else default

    def __contains__(self, i):
        return rpc(op="mem_get", h=self.h, mid=i) is not None

    def keys(self):
        return rpc(op="mem_ids", h=self.h)

    def __iter__(self):
        return iter(self.keys())

    def __len__(self):
        return len(self.keys())

    def values(self):
        return [self[i] for i in self.keys()]

    def items(self):
        return [(i, self[i]) for i in self.keys()]


class _Item:
    def __init__(self, d):
        self.__dict__.update(d)


class _EngineProxy:
    def __init__(self, h, params):
        self.h, self.p = h, params
        self.mem = _MemProxy(h)

    def remember(self, items, principal=None):
        from orp.model import SYSTEM
        items = [items] if isinstance(items, Memory) else items
        return rpc(op="engine_remember", h=self.h, memories=[m.to_json() for m in items], principal=_pr(principal or SYSTEM))

    def touch(self, tenant=None):
        rpc(op="touch", h=self.h, tenant=tenant)

    def readable(self, m, pr, labels=()):
        m = object.__getattribute__(m, "_m") if isinstance(m, _MemRef) else m
        return PyEngine.readable(None, m, pr, labels)

    def recall(self, req):
        d = {"query": req.query, "principal": _pr(req.principal), "tenant": req.tenant,
             "as_of": req.as_of.isoformat() if req.as_of else None,
             "known_at": req.known_at.isoformat() if req.known_at else None, "scope": req.scope,
             "kinds": list(req.kinds) if req.kinds else None, "budget_tokens": req.budget_tokens,
             "max_items": req.max_items, "include_history": req.include_history, "filters": req.filters}
        pack = rpc(op="engine_recall", h=self.h, req=d)
        pack["items"] = [_Item(i) for i in pack["items"]]
        return _Item(pack)


class _EdgesProxy(dict):
    pass


class TSService:
    def __init__(self, embedder=None, params: Params | None = None, tokenizer="orp-approx-1"):
        name = type(embedder).__name__ if embedder is not None else "HashEmbedder"
        enc = {"RandomEmbedder": "random", "Broken": "broken", "HashingEmbedder": "hashing"}.get(name, "md5")
        pd = {}
        if params is not None:
            pd = {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(params).items() if k != "exact_dot"}
        self.h = rpc(op="new", encoder=enc, params=pd)["handle"]
        self.engine = _EngineProxy(self.h, params or Params())

    @property
    def _edges(self):
        return rpc(op="edges_raw", h=self.h)

    def _call(self, verb, pr, *args):
        return rpc(op="call", h=self.h, verb=verb, args=list(args), principal=_pr(pr))

    def capabilities(self, principal=None):
        from orp.model import SYSTEM
        return self._call("capabilities", principal or SYSTEM)

    def save(self, path):
        rpc(op="save", h=self.h, path=path)

    def load(self, path):
        rpc(op="load", h=self.h, path=path)

    def remember(self, body, principal):
        return self._call("remember", principal, body)

    def recall(self, body, principal):
        return self._call("recall", principal, body)

    def profile(self, body, principal):
        return self._call("profile", principal, body)

    def get(self, ids, principal, labels=()):
        return self._call("get", principal, ids, list(labels))

    def neighbours(self, mem_id, before, after, principal):
        return self._call("neighbours", principal, mem_id, before, after)

    def thread(self, thread, principal):
        return self._call("thread", principal, thread)

    def history(self, subject, predicate, principal):
        return self._call("history", principal, subject, predicate)

    def link(self, body, principal):
        return self._call("link", principal, body)

    def edges(self, mem_id, principal):
        return self._call("edges", principal, mem_id)

    def forget(self, body, principal):
        return self._call("forget", principal, body)

    def feedback(self, body, principal):
        return self._call("feedback", principal, body)

    def approve(self, mem_id, principal):
        return self._call("approve", principal, mem_id)

    def reflect(self, body, principal):
        return self._call("reflect", principal, body)

    def job(self, job_id, principal):
        return self._call("job", principal, job_id)

    def changes(self, cursor, principal, limit=500):
        return self._call("changes", principal, cursor, limit)

    def audit_log(self, principal):
        return self._call("audit_log", principal)

    def export(self, principal):
        return self._call("export", principal)

    def import_(self, bundle, principal):
        return self._call("import_", principal, bundle)


api.Service = TSService
if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests", "conformance"), top_level_dir=ROOT)
    suite.addTests(unittest.defaultTestLoader.loadTestsFromName("tests.test_schema"))  # TS responses vs the schema pack
    r = unittest.TextTestRunner(verbosity=1 if "-q" in sys.argv else 2).run(suite)
    _proc.stdin.close()
    sys.exit(0 if r.wasSuccessful() else 1)
