# Confirmation on held-out test questions (pre-registered 10 Oct 2026, before export)

Questions: the 232 LongMemEval-S test questions not used in QA v3 (`questions.json`), day-precision times. Their
retrieval metrics were seen in earlier runs; no QA outcome on them has been seen.

Systems: `orp-clean` (chosen on dev, `results/qa4dev/selection.json`), `orp` (v0.1.2 rendering), all 15 baselines at
their raw-turn dev-tuned settings, BM25, and the gold-evidence oracle. 1,000-token budget.

Readers and judges: Claude Sonnet sub-agents here; DeepSeek V3 (`deepseek-chat`, temperature 0) on the user's machine.
Same v3 briefs, blinding, batching and statistics (`bench/qa_round.py`).

Primary question, per reader: is `orp-clean` more accurate than every one of the 15 baselines and BM25? Reported as
orp-clean minus each system with paired bootstrap 95% CIs and Holm-adjusted p over the 17 comparisons (16 plus orp).
"Better" means a Holm-adjusted p below 0.05 with a positive difference; "level" otherwise unless the difference is
negative and significant. Both readers are reported whatever they show. If `orp-clean` holds up, it becomes the
engine default (v0.1.3); nothing else in the engine changes.

## Outcome (11 Oct 2026)

All 4,408 items answered and judged by both reader-and-judge pairs (DeepSeek inputs verified unchanged by SHA-256).
- Claude Sonnet: ORP v0.1.3 82.3% (oracle 94.8%). Better than 15 of the 16 comparators after Holm (14 baselines and
  BM25); level with Hindsight (80.2%, +2.2 [-2.6, +6.9]); worse than none. Level with v0.1.2 rendering (+0.9).
- DeepSeek V3: ORP v0.1.3 58.6% (oracle 61.6%). Better than 10 of 16; level with Hindsight (58.6%), mem0 (58.2%),
  BM25, Supermemory Company Brain, Graphiti and Letta; worse than none. Better than v0.1.2 rendering (+6.9 [+2.6,
  +11.2], Holm p 0.014).
Per the protocol, `orp-clean` became the engine default (v0.1.3); the ranking is unchanged.
