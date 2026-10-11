"""Differential fuzz: the same random op sequences through the Python oracle (smartware.api.Service, HashingEmbedder,
smartware-rrf-1, exact_dot) and the TypeScript Service (tools/bridge/server.mjs, HashingEncoder, same params).
Responses are compared after normalising engine-assigned ids (registered in creation order on each side),
request ids, wall-clock timestamps and snapshot tokens.

    python3 diff_fuzz.py [seeds=20] [steps=120] [seed0=1] [--reopen]

--reopen: every 25 steps the TS service is saved to a SQLite file and reopened from it (tests saveTo + lazy load);
note the reopened service is store-backed, whose failed batches roll back (a known divergence), so expect that.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import copy
import time
from dataclasses import asdict, replace

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from smartware import rankers  # noqa: E402
from smartware.api import SmartwareError, Service  # noqa: E402
from smartware.embedders import HashingEmbedder  # noqa: E402
from smartware.model import Principal  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ID_PROB = next((a.split("=")[1] for a in sys.argv if a.startswith("--idprob=")), "0.6")
FROZEN = "--frozen-clock" in sys.argv
import smartware.clock as _pyclock  # noqa: E402
from datetime import datetime as _dt, timedelta as _td  # noqa: E402
_FAKE = [None]
_real_now = _pyclock.now
_pyclock.now = lambda: _FAKE[0] if _FAKE[0] is not None else _real_now()
args = [a for a in sys.argv[1:] if not a.startswith("--")]
N_SEEDS = int(args[0]) if len(args) > 0 else 20
N_STEPS = int(args[1]) if len(args) > 1 else 120
SEED0 = int(args[2]) if len(args) > 2 else 1
REOPEN = "--reopen" in sys.argv
VERBOSE = "--verbose" in sys.argv
ATOMIC = "--atomic-oracle" in sys.argv
DUMP = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--dump=")), None)

proc = subprocess.Popen(["node", os.path.join(HERE, "diff_server.mjs")], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
_n = [0]


def rpc(**msg):
    _n[0] += 1
    msg["id"] = _n[0]
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


PARAMS = rankers.params("smartware-rrf-1")
PD = {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(PARAMS).items() if k != "exact_dot"}

ENGINE = re.compile(r"mem_[0-9a-f]{24}")
EDGE = re.compile(r"edge_[0-9a-f]{16}")
JOB = re.compile(r"job_[0-9a-f]{12}")
NOW = re.compile(r"20(2[6-9]|[3-9]\d)-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?")


class Side:
    def __init__(self):
        self.ids = {}

    def reg(self, x):
        if isinstance(x, str) and (ENGINE.fullmatch(x) or EDGE.fullmatch(x) or JOB.fullmatch(x)) and x not in self.ids:
            self.ids[x] = f"<{x[:3]}{len(self.ids)}>"

    def norm(self, o, key=None):
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if k in ("request_id",):
                    continue
                if k in ("snapshot", "sha256"):
                    out[k] = "<x>"
                    continue
                out[self.norm(k) if isinstance(k, str) else k] = self.norm(v, k)
            return out
        if isinstance(o, list):
            return [self.norm(x) for x in o]
        if isinstance(o, float):
            return round(o, 4)
        if isinstance(o, str):
            o = ENGINE.sub(lambda m: self.ids.get(m.group(0), "<mem?>"), o)
            o = EDGE.sub(lambda m: self.ids.get(m.group(0), "<edge?>"), o)
            o = JOB.sub(lambda m: self.ids.get(m.group(0), "<job?>"), o)
            o = NOW.sub("<now>", o)
            return o
        return o


def register(side, verb, res):
    if not isinstance(res, dict):
        return
    for i in res.get("ids", []) or []:
        side.reg(i)
    for i in res.get("created", []) or []:
        side.reg(i)
    side.reg(res.get("rule_candidate"))
    side.reg(res.get("job"))
    if verb == "link":
        side.reg(res.get("id"))
    for k, v in sorted((res.get("id_map") or {}).items(), key=lambda kv: side.norm(kv[0])):
        side.reg(v)


def pr_of(p):
    return Principal(p["id"], frozenset(p["groups"]), tenant=p["tenant"])


def py_call(s, verb, p, a):
    pr = pr_of(p)
    f = {"remember": lambda: s.remember(a[0], pr), "forget": lambda: s.forget(a[0], pr),
         "feedback": lambda: s.feedback(a[0], pr), "approve": lambda: s.approve(a[0], pr),
         "reflect": lambda: s.reflect(a[0], pr), "link": lambda: s.link(a[0], pr), "import": lambda: s.import_(a[0], pr),
         "recall": lambda: s.recall(a[0], pr), "get": lambda: s.get(a[0], pr, tuple(a[1])),
         "history": lambda: s.history(a[0], a[1], pr), "neighbours": lambda: s.neighbours(a[0], a[1], a[2], pr),
         "thread": lambda: s.thread(a[0], pr), "profile": lambda: s.profile(a[0], pr), "edges": lambda: s.edges(a[0], pr),
         "changes": lambda: s.changes(a[0], pr, a[1]), "export": lambda: s.export(pr),
         "audit_log": lambda: s.audit_log(pr)}[verb]
    try:
        return {"ok": f()}
    except SmartwareError as e:
        return {"err": e.code}
    except Exception as e:  # noqa: BLE001
        return {"crash": f"{type(e).__name__}: {e}"[:200]}


TS_VERB = {"import": "import_"}


def ts_call(h, verb, p, a):
    out = rpc(op="call", h=h, verb=TS_VERB.get(verb, verb), args=a, principal={"id": p["id"], "groups": sorted(p["groups"]), "tenant": p["tenant"]})
    if "err" in out:
        return {"err": out["err"]["code"]}
    if "crash" in out:
        return {"crash": out["crash"].split("\n")[0][:200]}
    return {"ok": out["ok"]}


def retenant(b, t):
    def fix(l):
        d = json.loads(l)
        d["tenant"] = t
        return json.dumps(d)
    return {"manifest": dict(b["manifest"], tenant=t),
            "memories.jsonl": "\n".join(fix(l) for l in b["memories.jsonl"].split("\n") if l.strip()),
            "edges.jsonl": "\n".join(fix(l) for l in b["edges.jsonl"].split("\n") if l.strip())}


def canon_lists(verb, v):
    """Lists whose order is by engine id (random per side) are compared as multisets."""
    if not isinstance(v, dict):
        return v
    v = dict(v)
    if verb == "forget" and isinstance(v.get("affected"), list):
        v["affected"] = sorted(v["affected"])
    if verb == "export":
        v["memories.jsonl"] = sorted(json.dumps(json.loads(l), sort_keys=True) for l in v["memories.jsonl"].split("\n") if l.strip()) if isinstance(v["memories.jsonl"], str) else v["memories.jsonl"]
        v["edges.jsonl"] = sorted(json.dumps(json.loads(l), sort_keys=True) for l in v["edges.jsonl"].split("\n") if l.strip()) if isinstance(v["edges.jsonl"], str) else v["edges.jsonl"]
    return v


def first_diff(a, b, path=""):
    if a == b:
        return None
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            d = first_diff(a.get(k, "<absent>"), b.get(k, "<absent>"), f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_diff(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    if isinstance(a, str) and isinstance(b, str):
        i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
        return f"{path}: (first difference at char {i}) py=...{a[max(0, i - 150):i + 150]!r} ts=...{b[max(0, i - 150):i + 150]!r}"
    return f"{path}: py={json.dumps(a, default=str)[:400]} ts={json.dumps(b, default=str)[:400]}"


seen = {}
total = 0
for seed in range(SEED0, SEED0 + N_SEEDS):
    ops = json.loads(subprocess.run(["node", os.path.join(HERE, "dump_ops.mjs"), str(seed), str(N_STEPS), ID_PROB], capture_output=True, text=True).stdout)
    py = Service(HashingEmbedder(), replace(PARAMS, exact_dot=True))
    h = rpc(op="new", params=PD)["ok"]["handle"]
    PY, TS = Side(), Side()
    hist = []
    py_edges, ts_edges = [], []
    for step, op in enumerate(ops):
        verb, p, a = op["verb"], op["pr"], json.loads(json.dumps(op["args"]))
        if verb in ("list", "unlink"):
            continue  # TS-only extensions
        if verb == "remember" and a[0].get("if_version") == "CUR":
            m0 = a[0]["memories"][0]
            g = py.get([m0["id"]], Principal("root", frozenset({"*"}), tenant=p["tenant"]), ("secret",))
            a[0]["if_version"] = g["memories"][0]["version"] if g["memories"] else 1
        if verb == "import":
            other = "t2" if p["tenant"] == "t1" else "t1"
            pb = py.export(Principal("root", frozenset({"*"}), tenant=other))
            a = [retenant(pb, p["tenant"])]
        if FROZEN:  # both clocks frozen at the same instant for the whole step, one second apart per step
            base = _dt(2026, 10, 11) + _td(seconds=step)
            _FAKE[0] = base
            rpc(op="clock", ms=int((base - _dt(1970, 1, 1)).total_seconds() * 1000))
        else:
            time.sleep(0.002)  # distinct millisecond observed_at per step (TS clock resolution is 1 ms, Python's 1 us)
        if ATOMIC and step >= 25 and verb in ("remember", "forget", "feedback", "approve", "reflect", "link", "import"):
            snap = copy.deepcopy(py)
            r_py = py_call(py, verb, p, a)
            if "err" in r_py:
                py = snap  # model the store-backed TS service, whose failed writes roll back
        else:
            r_py = py_call(py, verb, p, a)
        if verb == "import":  # TS gets its own export (engine ids differ)
            tb = ts_call(h, "export", {"id": "root", "groups": ["*"], "tenant": "t2" if p["tenant"] == "t1" else "t1"}, [])["ok"]
            a = [retenant(tb, p["tenant"])]
        r_ts = ts_call(h, verb, p, a)
        register(PY, verb, r_py.get("ok"))
        register(TS, verb, r_ts.get("ok"))
        total += 1
        hist.append((step, verb, json.dumps(a)[:300], list(r_py)[0], list(r_ts)[0]))
        A = canon_lists(verb, PY.norm(r_py.get("ok"))) if "ok" in r_py else r_py
        B = canon_lists(verb, TS.norm(r_ts.get("ok"))) if "ok" in r_ts else r_ts
        d = first_diff(A, B)
        if DUMP is not None and step == DUMP:
            print("PY:", json.dumps(A, indent=1, default=str)[:6000]); print("TS:", json.dumps(B, indent=1, default=str)[:6000])
            for x in hist: print("    hist", x)
        if d:
            key = (verb, re.sub(r"\[\d+\]", "[]", d.split(":")[0]), str(r_py.get("err") or r_py.get("crash", "")[:40]), str(r_ts.get("err") or r_ts.get("crash", "")[:40]))
            if key not in seen:
                seen[key] = (seed, step)
                print(f"\nDIVERGENCE seed={seed} step={step} verb={verb} principal={p}")
                print("  args:", json.dumps(a)[:700])
                print("  ", d)
                if VERBOSE:
                    for x in hist[-8:]:
                        print("    hist", x)
            # resync is impossible in general; stop this seed after a state-changing divergence
            if verb in ("remember", "forget", "feedback", "approve", "reflect", "link", "import"):
                break
        if REOPEN and step % 25 == 24:
            path = os.path.join(tempfile.mkdtemp(prefix="smartware-fuzz-"), "reopen.sqlite")
            rpc(op="save", h=h, path=path)
            rpc(op="reopen", h=h, store=path)
print(f"\ndone: seeds={N_SEEDS} steps={N_STEPS} calls={total} distinct divergences={len(seen)}")
proc.stdin.close()
sys.exit(1 if seen else 0)
