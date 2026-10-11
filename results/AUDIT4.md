# Fourth audit: Postgres store, several nodes, bundles and importers (11 October 2026)

Two reviewers with no prior context examined the code added after the third audit: the Postgres wire client and
store, tenant leases and request forwarding between nodes, the async store port, on-disk bundles and the importers.
One worked on security, the other on correctness under concurrency and across stores; each finding came with a
runnable reproduction. Every finding was accepted. The reproductions are now regression tests
(`packages/server/test/pg.test.js`, `packages/server/test/audit3.test.js`, `tests/conformance/test_audit3.py`).

## Findings and fixes

| # | Severity | Finding | Fix |
|---|---|---|---|
| 1 | Critical | The write fence read the lease row without locking it, at the start of the transaction. A write that stalled after the fence could commit after its lease ran out and another node had taken the tenant: two writers, a stale reader, and colliding change cursors | The fence now updates (locks and extends) the lease row inside the write's transaction, so a takeover waits for the commit; it runs again as the last statement, so the lease runs from the commit; all lease times use the clock at the statement (`clock_timestamp()`), not the transaction start |
| 2 | High | Any caller with a valid token could make a node send a request to a host of their choosing and read the reply (SSRF through forwarding, using an absolute-form or `//host` request target) | Only path request targets are accepted (anything else is 400); a forward goes to the owning node's origin with only the path and query of the request; hop-by-hop headers are dropped |
| 3 | High | Lease renewal ran under the verb lock, so a verb longer than the lease (a big import, a slow embedding service) could never commit, even with a single node | Renewal runs outside the verb lock; a lease that ran out while no other node took it is still the writer's (the fence checks node and epoch, and extends it) |
| 4 | Medium | Two processes given the same node id shared one lease (and one's shutdown released the other's) | Every process appends a random incarnation to its node id; a restarted node with the same URL takes its old leases over at once (with a new epoch) |
| 5 | Medium | A NUL character in an id, key or claim was cut short in store keys (lost idempotency, broken claim chains) and failed as HTTP 500 on Postgres | NUL is refused in any string of a request, in both engines; key files and tokens refuse control characters in ids, tenants and groups |
| 6 | Medium | Releasing an idle tenant freed its records but kept its index | Unloading a tenant drops its index, its hash entries and any index build in flight |
| 7 | Medium | Re-importing a bundle whose engine ids collide with another tenant's made new copies each time | A colliding id is remapped to an id derived from the tenant and the old id, in both engines, so a re-import de-duplicates |
| 8 | Medium | The failed-authentication limit (per address) could lock out every user behind one proxy, and its table was cleared wholesale when full | Only failures count, and a valid token is never refused because of others' failures; the table drops its oldest entries instead |
| 9 | Low | Export, import, export was not byte-equal for redacted records (derived entities recomputed) | Redact keeps an empty entity list, as the engine would derive from "[redacted]", in both engines |
| 10 | Low | A purged record's edges stayed in the store and re-attached to a later record with the same client id | Purge removes the edges of what it purges, in both engines |
| 11 | Low | Bundle reader limits were loose (2 GiB, overlapping entries accepted) and a manifest could omit the data files' hashes | 512 MiB uncompressed in all; overlapping entries and entries running into the central directory are refused; the manifest must hash `memories.jsonl` and `edges.jsonl`. The hashes catch damage, not tampering, and the docs now say so |
| 12 | Low | Postgres client: unknown `sslmode` values fell back to `prefer`; `require` with a root certificate did not verify; a password could be sent in clear text without TLS; a SCRAM server's final proof was optional; a malformed message length could loop; large messages were re-copied per chunk; the SCRAM iteration count was unbounded | Unknown modes are refused; `require` plus a root certificate verifies the chain (as libpq); clear-text passwords only over TLS; SCRAM must prove the server; message lengths are bounded and checked; chunks are joined once per message; iterations are capped |
| 13 | Low | Store connection failures surfaced as HTTP 500; a pool waiter could hang if reconnecting failed; `close()` could race an in-flight write or renewal; a background warm-up could take leases after close; very short leases were accepted | Connection failures are `unavailable` (503, retryable); waiters are rejected on failure; close waits for any renewal and for the verb lock, and later acquisitions are refused; leases must last at least 3 s; idle pooled connections no longer keep a process alive |
| 14 | Low | Store files and SQLite backups (every tenant's memories and the snapshot secret) were created with the default file mode | Created owner-only (0600; directories 0700) in both engines |

Documented rather than changed: re-importing from another project's file restores records that were purged since
(a purge erases, so nothing remains to block on); a retracted record stays retracted. A Letta import with
`--readers tenant` puts the agent's core memory into every reader's context.

## Verification after the fixes

| Check | Result |
|---|---|
| TypeScript tests (all packages) | 104 pass (12 more run when a Postgres is configured: all pass) |
| Postgres tests, over TLS, including certificate verification | 12 pass |
| Oracle conformance suite / against the product engine / on Postgres | 138 / 127 / 127 pass |
| Python client | 49 pass |
| `orp doctor --self` | L4 |
| Persistence fuzz, SQLite and Postgres (4 x 100 each) | 0 failing checks |
| Differential fuzz, oracle vs product (10 x 120) and on Postgres with restarts (6 x 100) | 0 divergences |
| Strict parity, LoCoMo test, fast mode | 100% identical packs |
| Release gate, schema pack | Pass |
