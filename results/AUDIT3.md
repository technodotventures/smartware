# Third audit: product engine, server and oracle (11 October 2026)

Two independent reviews ran against v0.2.0 before this release: a correctness review (persistence and
differential fuzzing of the TypeScript product engine against the Python oracle, plus targeted reproductions) and a
security review of the running HTTP and MCP server. Every finding was accepted and fixed in both engines where it
applied. Each reproduction is now a regression test (`packages/server/test/audit3.test.js`, 21 tests;
`tests/conformance/test_audit3.py`, 11 tests, which also run against the product engine through the bridge suite),
and the auditors' fuzzers are kept in `tools/fuzz` and run in CI.

## Findings and fixes

| # | Severity | Finding | Fix |
|---|---|---|---|
| 1 | High | A forget in one tenant could cascade into another tenant's derived records (retract or purge them, tombstone them under the wrong tenant, leave the other tenant's counters stale) | Cascades are confined to the caller's tenant and need a source in the hit set; each pass works on a snapshot. Both engines |
| 2 | High | Client `meta.idempotency_key` was read as the engine's own idempotency key: an upsert could delete the record being written and answer with another record's id | The engine key is `_idempotency_key`, which clients cannot set (keys starting `_` are reserved). Both engines |
| 3 | High | Import could overwrite another tenant's edges and tombstones when that tenant was not loaded yet | Edge ids and tombstone ids are checked against the store as well as memory and remapped on collision |
| 4 | High | A failing batch was all-or-nothing in the SQLite build but not in memory or in the oracle (first records kept) | `remember` validates and plans the whole batch, then mutates. Duplicate ids within one batch are rejected. Both engines |
| 5 | High | An upsert that changed or removed a record's claim left it in the old claim chain: `history` crashed (HTTP 500) and a later claim on the old key superseded it | The record is unchained before the new version is stored, and the stale chains are resolved again. Both engines |
| 6 | Medium | Any failed write cleared the whole in-memory audit log | Rollback truncates the audit log to its length at the start of the verb |
| 7 | Medium | Rollback unloaded every loaded tenant, so one bad request from any tenant forced every tenant to reload and rebuild its index | Only the tenants the verb touched are reloaded; counters and change feeds are reset with them |
| 8 | Medium | Rollback only noticed some kinds of pending change (an import that changed only edges or tombstones stayed in memory but not on disk) | Any non-empty journal counts as dirty. Import also checks every edge endpoint before writing anything |
| 9 | Medium | A tenant was marked loaded before its rows were read, so one bad row left it half-loaded and writable | The tenant is marked loaded only after every row is in; a failed load is undone and retried on the next request (fails closed) |
| 10 | Medium | A preload index build could install a partition built from objects that a reload had replaced | Per-tenant generation counter, bumped on invalidate and checked before installing a build |
| 11 | Medium | Purging the current version of a claim did not restore the version it superseded | Purge re-resolves the claim chain the same way retract does. Both engines |
| 12 | Medium | Purge and redact left the stored vectors of erased text in the SQLite file (remote embedding encoder) | Purge and redact delete the cached and stored vectors of the record's exact texts |
| 13 | Medium | An embedding service outage turned every request into HTTP 500 | Writes fail with `unavailable` (HTTP 503, retryable). Recall answers from the lexical channels with `degraded: ["dense_unavailable"]` when only the query could not be embedded (spec §5.8) |
| 14 | Medium | HTTP `DELETE /memories/{id}` with `If-Match` checked the version and forgot in two separate steps, so a concurrent update could be retracted | The version check runs inside the forget, under the same lock |
| 15 | Medium | An idempotent retry was re-validated before the replay check, so a retry could fail after the first call succeeded | The replay check runs first (M17). Both engines |
| 16 | Medium | Failed authentication was not rate limited | Per-address limit on failed authentication (default 1 per second, burst 20; `authFailRate`) answering 429 |
| 17 | Medium | Metrics and log labels used raw paths, so callers could grow the metrics table without bound and put ids in labels | Route templates (`GET /v1/:tenant/memories/:id`); unknown paths are `other` |
| 18 | Low | A malformed percent-encoded path crashed into HTTP 500 | 400 `invalid_parameter` |
| 19 | Low | Numbers were accepted as raw timestamps; several ISO 8601 forms were handled differently by the two engines | ISO 8601 / RFC 3339 strings only, one grammar in both engines; numbers are `invalid_parameter` (M18) |
| 20 | Low | Clients could set the engine-derived meta keys `entities` and `time_refs`, steering the entity and temporal channels | Rejected on input in both engines; the clients and the mem0 shim strip them before an upsert |
| 21 | Low | The mem0 `PUT` dropped the record's claim, lineage and time fields and kept the old text's derived meta | It keeps every client-settable field, keeps the claim only when the text is unchanged, and gets fresh derived meta |
| 22 | Low | `x-list` checked parameter names but not values; MCP tools checked argument names but not types | Values validated; MCP arguments are checked against each tool's declared schema (type, enum, range, items) |
| 23 | Low | Export order depended on SQLite row order, so an export was not byte-stable across a restart | Edges sorted by id, tombstones by id with sorted keys. Both engines |
| 24 | Low | `reflect` ignored `scope` and `kinds` | Honoured; kinds other than `summary` are `unsupported_parameter`. Both engines |
| 25 | Low | Store flush wrote inserts before deletes, so an id purged and re-used in one verb could vanish | Deletes first; re-marking a record dirty removes it from the pending deletes |
| 26 | Low | Feedback was kept in memory as well as in the store, growing without bound | Kept in memory only when there is no store |
| 27 | Low | Deeply nested `meta` was accepted and serialised recursively | `meta` deeper than 32 levels is `invalid_parameter` in both engines (spec §3.1 now says so) |
| 28 | Low | Malformed import lines (records, edges, tombstones) raised internal errors | `invalid_parameter`, checked before anything changes. Both engines |

Release hygiene found alongside: GitHub Actions are now pinned to commit SHAs, and CI installs the test-only Python
dependencies from a pinned `requirements-dev.txt` with binary wheels only.

## Verification after the fixes

| Check | Result |
|---|---|
| TypeScript tests (core, server, clients, adapters, connectors) | 89 pass |
| Oracle conformance suite | 134 pass |
| Oracle suite against the product engine (bridge) | 123 pass |
| Python client | 48 pass |
| `smartware doctor --self` | L4 |
| Persistence fuzz, 6 seeds x 120 steps | 0 failing checks |
| Differential fuzz, oracle vs product, 25 seeds x 120 steps, client ids, frozen clock | 0 divergences in 2,900 calls |
| Same, saving and reopening the product engine from SQLite every 25 steps, 12 seeds | 0 divergences in 1,376 calls |
| Strict parity, LoCoMo test, shipped linear ranker and fast mode | 100% identical packs (1,152 questions each) |
| Strict parity, LongMemEval-S, 60 haystacks | 100% identical packs |
| `smartware eval` on LoCoMo test | R@500 74.2, R@2000 85.4, MRR 59.3: unchanged |
| Release gate, schema pack check | Pass |

With engine-assigned (random) ids the differential fuzz shows a handful of near-tie orderings that differ between the
two engines. These come from ties broken by id, and each engine draws its own random ids; they disappear when ids are
the same on both sides, which is the comparison above.

## Not changed

- A row that cannot be parsed (for example one written by a newer version) makes its tenant refuse requests until the
  row is fixed or the software upgraded. This is deliberate: serving a partial tenant would be worse.
- Recall during an embedding outage is lexical only; there is no fallback encoder.
