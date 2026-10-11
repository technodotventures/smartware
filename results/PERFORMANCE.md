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
- Cold build happens once per tenant per process. `orp serve --http` warms every tenant in the background at start
  (most recently written first, in small chunks, so requests keep being served); `--no-preload` turns this off.

Exact-preserving optimisations made after the first measurement (each re-verified against the oracle: LoCoMo and
LongMemEval-S parity still 100% identical packs, encoder vectors bit-identical):
- per-token hash cache in the hashing encoder (first recall at 3,000 units: 23.5 s to 1.8 s);
- cached neighbour dot products for the dense-window channel;
- four-accumulator and sparse (non-zero query coordinates only) float32 dot products with identical rounding;
- segment windows identical to their unit share the unit's vector;
- per-word weighted vectors in the SIF encoder; bounded encoder cache; 1.5x buffer growth.
