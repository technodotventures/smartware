# Procedure-replay benchmark report

Contract: benchmarks/procedure-replay/contract-v1.json
Command: node scripts/run-procedure-replay-benchmark.mjs benchmarks/procedure-replay/contract-v1.json

## Verdict
Adopt / Trial / Assess / Hold / Reject (fill from the emitted JSON).

## Arms
| Arm | Memory lane | p50 turns | p50 tool calls | tests passed |
|---|---|---|---|---|
| A | none | | | |
| B | procedure + receipt | | | |
| C | full skill/manual | | | |

## Gates
| Gate | Result |
|---|---|
| first_proof | |
| correctness | |
| utility (turns <=81% A, tool calls <=60% A) | |
| context (<=500 tokens, B < C) | |
| safety | live run required |
| lifecycle | live run required |
| reproducibility | |

## Reproducibility
Solver, node version, fixture sha256, replication count, and raw per-run arrays
are emitted by the runner. Record them verbatim here. Totals are computed by the
runner, never by hand.
