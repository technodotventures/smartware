# Product engine performance

`node tools/bench/scale.mjs <set> <N> [encoder]`: one tenant holding N real LongMemEval-S turns (long assistant
answers, about 10 two-sentence segment windows per turn), SQLite store, one Node process on a 2-core cloud VM.
Recall is the full balanced pipeline with a 2,000-token budget; "fast" skips pseudo-relevance feedback.

| Units in the tenant | Encoder | Writes/s | Cold build (once per tenant per process) | Recall p50 / p95 | Fast p50 / p95 | RSS |
|---|---|---|---|---|---|---|
| 1,000 | sif-ppmi-256 | 4,857 | 0.3 s | 28 / 46 ms | 23 / 30 ms | 0.3 GB |
| 3,000 | hashing-512 | 4,119 | 1.8 s | 46 / 63 ms | 38 / 48 ms | 0.4 GB |
| 10,000 | hashing-512 | 4,787 | 6.4 s | 76 / 98 ms | 71 / 86 ms | 0.7 GB |
| 10,000 | sif-ppmi-256 | 4,670 | 8.0 s | 84 / 102 ms | 74 / 98 ms | 0.7 GB |
| 50,000 | sif-ppmi-256 | 4,619 | 45 s | 290 / 396 ms | 294 / 367 ms | 2.0 GB |

LoCoMo-style chat (short turns, mostly one segment each): about 5 ms per recall for a 600-turn conversation
(tools/parity/compare.mjs).

What this means:
- Recall cost is linear in the size of the asker's tenant, because admissibility and collection statistics are
  computed exactly over the admissible set (M33, M67). Tenants never affect each other: a large tenant does not slow a
  small one.
- Up to about 10,000 units per tenant, recall stays under about 100 ms, small next to a model call. Most personal and
  team memories sit well inside this.
- From about 50,000 units, recall is 0.3 to 0.4 s and memory is about 40 KB per long unit. Large tenants should be
  split by scope (recall within a scope only scores that scope's records) until an approximate dense index lands (an
  ANN behind the Index port, with the exact scorer re-ranking its candidates; parity is then measured, not assumed).
- Cold build happens once per tenant per process. `smartware serve --http` warms every tenant in the background at start
  (most recently written first, in small chunks, so requests keep being served); `--no-preload` turns this off.

Exact-preserving optimisations made after the first measurement (each re-verified against the oracle: LoCoMo and
LongMemEval-S parity still 100% identical packs, encoder vectors bit-identical):
- per-token hash cache in the hashing encoder (first recall at 3,000 units: 23.5 s to 1.8 s);
- cached neighbour dot products for the dense-window channel;
- four-accumulator and sparse (non-zero query coordinates only) float32 dot products with identical rounding;
- segment windows identical to their unit share the unit's vector;
- per-word weighted vectors in the SIF encoder; bounded encoder cache; 1.5x buffer growth.

## SQLite and Postgres stores (11 October 2026)

Same machine, hashing encoder, Postgres 16 on localhost (no network latency), one process. The engine and index are
identical; only where a verb's changes are written differs (one transaction per call either way; Postgres sends the
whole transaction as one pipelined round trip).

| Store | remember, 1 record | remember, 100 records | recall (1,200 units, warm) | first recall after restart (load and index 1,200 units) |
|---|---|---|---|---|
| SQLite | 0.8 ms | 12 ms | 8 ms | 76 ms |
| Postgres | 2.1 ms | 26 ms | 5 ms | 87 ms |

Over a network, add one round trip per write and per tenant load. Recall never touches the store once a tenant is
loaded.

## Second performance pass (16 October 2026)

Every change below is exact: packs are identical to the build before the pass (a differential harness replays the same
writes and recalls against both builds, both encoders, both rankers, with filters, time words and ACL slices: 0
differences over several seeds), and the conformance suite, schema pack and release gate still pass. Measured on a
synthetic corpus (the LongMemEval data and the real SIF encoder are not in the repository), same 2-core VM.

| Case | Before | After |
|---|---|---|
| Hashing, 10,000 units, balanced recall p50 | 41.5 ms | 19 ms |
| Hashing, 10,000 units, first recall (cold build) | 10.3 s | 4.6 s |
| Hashing, 10,000 units, RSS | 528 MB | 376 MB |
| Hashing, 1,000 units, recall p50 | 8.4 ms | 5.5 ms |
| Hashing, 300 units, recall p50 | 5.6 ms | 3.3 ms |
| SIF-style dense, 10,000 units, recall p50 | 56.6 ms | 43.9 ms |
| remember p50 | 0.73 ms | 0.43 ms |
| recall straight after a write p50 | 11 ms | 7.3 ms |

What changed:
- BLAKE2b is generated (tools/gen-blake2b.mjs, drift-checked in CI) with the state held in int32 locals: about 5x faster.
- The hashing encoder is sparse end to end: a token and bigram hash cache, and segment vectors stored column-major
  (sparse.ts) so one query scores every segment in a scatter pass, with the same four-partial-sum rounding as dot32.
- Neighbour dot products for the dense-window channel come from a symmetric open-addressing cache (pairdots.ts).
- Top-k selection uses a typed-array heap; BM25 scoring has an all-admissible fast path; token counting is a scan, not a regex.

Not changed, on purpose: the SQLite write path (a flush is about 0.12 ms p50; pragma tuning gained about 10% of the
mean). Dense-encoder recall is now dominated by the memory-bound 256-d dot over every row (about two thirds of the
time); only an approximate index or parallelism would move it further.

Known, unchanged by this pass: tools/fuzz persist_fuzz and diff_fuzz report one distinct claims-order divergence on
both the original and the new build.
