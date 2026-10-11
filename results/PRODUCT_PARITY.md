# Product engine parity (TypeScript @orp/core against the Python oracle)

The product engine (packages/core) is a port of the reference engine (orp/engine.py), restructured for production:
per-tenant partitions, an incremental index, a SQLite store. The oracle is its specification. Three checks, all
reproducible from this repository.

## 1. Strict differential parity (tools/parity)

The same haystacks and questions go to both engines; packs are compared item by item. The oracle runs with
`exact_dot=True` and an exact SIF encoder: dense scores accumulate in float64 and round once, which is how the product
engine always scores. (The published benchmark used BLAS `sgemv`, which rounds a row differently depending on its
position in the matrix, so identical texts such as "Tim: Thanks!" got different scores. That made the oracle break
ties by rounding artefacts; the product engine scores identical text identically.)

| Set | Config | Questions | Packs identical (ids, order, text) | Same top item |
|---|---|---|---|---|
| LoCoMo-10 test | fast (no PRF, RRF) | 1,152 | 100.00% | 100.00% |
| LoCoMo-10 test | orp-rrf-1 | 1,152 | 100.00% | 100.00% |
| LoCoMo-10 test | orp-linear-1 (shipped) | 1,152 | 100.00% | 100.00% |
| LongMemEval-S test, first 60 | orp-linear-1 | 60 | 100.00% | 100.00% |

Against the oracle as benchmarked (BLAS dense scores, `ORP_PARITY_BLAS=1`): 99.6 to 99.9% identical, 100% same top item.

    python3 tools/parity/dump.py locomo test orp-linear-1 out/ && node tools/parity/compare.mjs out/locomo_test_orp-linear-1.jsonl.gz orp-linear-1

## 2. The oracle's own conformance suite against the product engine (tools/bridge)

`python3 tools/bridge/run_suite.py` runs every conformance test in tests/conformance, unchanged, with the Python
Service replaced by the TypeScript service (plus schema validation of its responses): all pass. One store file is
shared by both engines: `tests/test_interop.py` writes with each and reads with the other; exports are byte-identical,
manifest hash included.

## 3. Benchmark reproduction with orp eval

`orp eval` (TypeScript, metrics ported from bench/metrics.py) on the published ORP-Bench test sets, SIF encoder,
shipped ranker. Published numbers are the oracle's (RESULTS.md).

| Set | Metric | Published (oracle) | Product engine |
|---|---|---|---|
| LoCoMo-10 test, n=1152 | R@500 / R@1000 / R@2000 | 74.3 / 79.7 / 85.7 | 74.2 / 79.3 / 85.4 |
| | Ans@500 / Ans@1000 / Ans@2000 | 73.3 / 78.8 / 84.2 | 73.6 / 79.0 / 84.5 |
| | SessAll@5 / MRR | 80.2 / 59.3 | 80.2 / 59.3 |
| LongMemEval-S test, day precision, n=382 | R@500 / R@1000 / R@2000 | 80.2 / 88.0 / 91.7 | 79.7 / 88.5 / 91.8 |
| | E@1000 | 79.0 | 79.4 |
| | Ans@500 / Ans@1000 / Ans@2000 | 60.1 / 67.9 / 76.0 | 60.0 / 67.9 / 75.9 |
| | SessAll@5 | 87.4 | 87.4 |

Every difference is under 0.5 points and well inside the 95% intervals (about plus or minus 2 to 4 points); they come
from the BLAS tie artefacts described above. The product engine therefore inherits the published results, within this
tolerance, on these two sets.

    python3 tools/eval/export_bench.py locomo test locomo_test.orpbench.jsonl
    orp eval --data locomo_test.orpbench.jsonl --encoder sif:results/cache/sif-ppmi-256.orpenc --systems orp,bm25,dense
