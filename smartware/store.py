"""SQLite store for the reference servers (replaces the pickle file).

One file, schema `smartware-store-1`, shared with the TypeScript engine so either can open a store the other wrote.
Everything in it is data: JSON text and integers. Nothing is evaluated on load.

Tables
    smartware_meta(key, value)                      schema, secret (hex), epoch, wseq, mutations
    memories(iid, tenant, ord, record)        record = the internal JSON form (Memory.to_json, engine meta kept)
    claims(tenant, key, pos, iid)             claim chains in registration order
    dedup(hkey, tenant, iid)                  de-duplication keys (JSON array, Python json.dumps form) -> internal id
    idem(tenant, principal, key, ids)         verb-level idempotency keys -> ids (JSON array)
    blocked(tenant, chash, pos, iid)          re-ingest blocks after forget
    edges(id, tenant, body)                   typed links
    tombstones(iid, tenant, body)             purge tombstones
    changes(tenant, cursor, body)             per-tenant change feed
    audit(seq, tenant, body)                  append-only audit log
    jobs(id, tenant, body)                    reflect jobs
    feedback(seq, tenant, body)               feedback signals
    counters(tenant, tmut, tcount)            per-tenant snapshot counters
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter, defaultdict

from .model import Memory

SCHEMA = "smartware-store-1"

DDL = """
CREATE TABLE IF NOT EXISTS smartware_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memories (iid TEXT PRIMARY KEY, tenant TEXT NOT NULL, ord INTEGER NOT NULL, record TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS memories_tenant_ord ON memories (tenant, ord);
CREATE TABLE IF NOT EXISTS claims (tenant TEXT NOT NULL, key TEXT NOT NULL, pos INTEGER NOT NULL, iid TEXT NOT NULL,
                                   PRIMARY KEY (tenant, key, pos));
CREATE TABLE IF NOT EXISTS dedup (hkey TEXT PRIMARY KEY, tenant TEXT NOT NULL, iid TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS dedup_tenant ON dedup (tenant);
CREATE TABLE IF NOT EXISTS idem (tenant TEXT NOT NULL, principal TEXT NOT NULL, key TEXT NOT NULL, ids TEXT NOT NULL,
                                 PRIMARY KEY (tenant, principal, key));
CREATE TABLE IF NOT EXISTS blocked (tenant TEXT NOT NULL, chash TEXT NOT NULL, pos INTEGER NOT NULL, iid TEXT NOT NULL,
                                    PRIMARY KEY (tenant, chash, pos));
CREATE TABLE IF NOT EXISTS edges (id TEXT PRIMARY KEY, tenant TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tombstones (iid TEXT PRIMARY KEY, tenant TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS changes (tenant TEXT NOT NULL, cursor INTEGER NOT NULL, body TEXT NOT NULL,
                                    PRIMARY KEY (tenant, cursor));
CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY, tenant TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS audit_tenant ON audit (tenant, seq);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, tenant TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feedback (seq INTEGER PRIMARY KEY, tenant TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters (tenant TEXT PRIMARY KEY, tmut INTEGER NOT NULL, tcount INTEGER NOT NULL);
"""

TABLES = ("memories", "claims", "dedup", "idem", "blocked", "edges", "tombstones", "changes", "audit", "jobs",
          "feedback", "counters")


def _j(x) -> str:
    return json.dumps(x, sort_keys=True, default=str)  # ensure_ascii: byte-identical to the TypeScript writer


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    new = path != ":memory:" and not os.path.exists(path)
    con = sqlite3.connect(path)
    if new:
        os.chmod(path, 0o600)  # every tenant's memories and the snapshot secret: owner-only
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(DDL)
    row = con.execute("SELECT value FROM smartware_meta WHERE key='schema'").fetchone()
    if row is None:
        con.execute("INSERT INTO smartware_meta VALUES ('schema', ?)", (SCHEMA,))
        con.commit()
    elif row[0] != SCHEMA:
        raise ValueError(f"{path}: store schema {row[0]!r}, this engine reads {SCHEMA!r}")
    return con


def is_sqlite(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def save(svc, path: str):
    """Write the whole service state in one transaction (atomic: a crash leaves the previous state)."""
    eng = svc.engine
    con = connect(path)
    try:
        with con:
            for t in TABLES:
                con.execute(f"DELETE FROM {t}")
            meta = {"secret": eng._secret.hex(), "epoch": str(eng._epoch), "wseq": str(eng._wseq),
                    "mutations": str(eng.mutations)}
            con.executemany("INSERT OR REPLACE INTO smartware_meta VALUES (?, ?)", meta.items())
            pos = {i: k for k, i in enumerate(eng.order)}
            con.executemany("INSERT INTO memories VALUES (?, ?, ?, ?)",
                            [(i, m.tenant, pos.get(i, len(pos)), _j(m.to_json())) for i, m in eng.mem.items()])
            con.executemany("INSERT INTO claims VALUES (?, ?, ?, ?)",
                            [(t, k, n, i) for (t, k), ids in eng._claims.items() for n, i in enumerate(ids)])
            con.executemany("INSERT INTO dedup VALUES (?, ?, ?)", [(_j(list(h)), h[1], i) for h, i in eng._hash.items()])
            con.executemany("INSERT INTO idem VALUES (?, ?, ?, ?)",
                            [(t, p, k, _j(ids)) for (t, p, k), ids in svc._keys.items()])
            con.executemany("INSERT INTO blocked VALUES (?, ?, ?, ?)",
                            [(t, h, n, i) for (t, h), ids in svc._blocked.items() for n, i in enumerate(ids)])
            con.executemany("INSERT INTO edges VALUES (?, ?, ?)",
                            [(e, d.get("tenant", ""), _j(d)) for e, d in svc._edges.items()])
            con.executemany("INSERT INTO tombstones VALUES (?, ?, ?)",
                            [(i, d.get("tenant", ""), _j(d)) for i, d in svc._tombstones.items()])
            con.executemany("INSERT INTO changes VALUES (?, ?, ?)",
                            [(t, c["cursor"], _j(c)) for t, feed in svc._changes.items() for c in feed])
            tomb = {i: d.get("tenant", "") for i, d in svc._tombstones.items()}

            def tenant_of(i):
                m = eng.mem.get(i)
                return m.tenant if m is not None else tomb.get(i, "")
            con.executemany("INSERT INTO audit VALUES (?, ?, ?)",
                            [(n, tenant_of(a.get("id")), _j(a)) for n, a in enumerate(eng.audit)])
            con.executemany("INSERT INTO jobs VALUES (?, ?, ?)",
                            [(j, d.get("tenant", ""), _j(d)) for j, d in svc._jobs.items()])
            con.executemany("INSERT INTO feedback VALUES (?, ?, ?)",
                            [(n, tenant_of(f.get("target")), _j(f)) for n, f in enumerate(svc._feedback)])
            ts = set(eng.tmut) | set(eng.tcount)
            con.executemany("INSERT INTO counters VALUES (?, ?, ?)",
                            [(t, int(eng.tmut[t]), int(eng.tcount[t])) for t in sorted(ts)])
    finally:
        con.close()


def load(svc, path: str):
    eng = svc.engine
    con = connect(path)
    try:
        meta = dict(con.execute("SELECT key, value FROM smartware_meta"))
        if "secret" in meta:
            eng._secret = bytes.fromhex(meta["secret"])
        eng._epoch = int(meta.get("epoch", 0))
        eng._wseq = int(meta.get("wseq", 0))
        eng.mutations = int(meta.get("mutations", 0))
        rows = con.execute("SELECT iid, record FROM memories ORDER BY ord").fetchall()
        eng.mem, eng.order = {}, []
        for iid, rec in rows:
            m = Memory.from_json(json.loads(rec))
            eng.mem[iid] = m
            eng.order.append(iid)
        eng._has_expiry = any(m.expires_at is not None for m in eng.mem.values())
        eng._claims = defaultdict(list)
        for t, k, _, i in con.execute("SELECT * FROM claims ORDER BY tenant, key, pos"):
            eng._claims[(t, k)].append(i)
        eng._hash = {}
        for h, _, i in con.execute("SELECT hkey, tenant, iid FROM dedup"):
            hk = json.loads(h)
            eng._hash[tuple(tuple(x) if isinstance(x, list) else x for x in hk)] = i
        eng.audit = [json.loads(b) for (b,) in con.execute("SELECT body FROM audit ORDER BY seq")]
        eng.tmut, eng.tcount = Counter(), Counter()
        for t, a, b in con.execute("SELECT tenant, tmut, tcount FROM counters"):
            eng.tmut[t], eng.tcount[t] = a, b
        svc._keys = {(t, p, k): json.loads(ids) for t, p, k, ids in con.execute("SELECT * FROM idem")}
        svc._blocked = defaultdict(list)
        for t, h, _, i in con.execute("SELECT * FROM blocked ORDER BY tenant, chash, pos"):
            svc._blocked[(t, h)].append(i)
        svc._edges = {e: json.loads(b) for e, _, b in con.execute("SELECT * FROM edges")}
        svc._tombstones = {i: json.loads(b) for i, _, b in con.execute("SELECT * FROM tombstones")}
        svc._changes = defaultdict(list)
        for t, _, b in con.execute("SELECT * FROM changes ORDER BY tenant, cursor"):
            svc._changes[t].append(json.loads(b))
        svc._jobs = {j: json.loads(b) for j, _, b in con.execute("SELECT * FROM jobs")}
        svc._feedback = [json.loads(b) for (b,) in con.execute("SELECT body FROM feedback ORDER BY seq")]
    finally:
        con.close()
    eng._dirty = True
