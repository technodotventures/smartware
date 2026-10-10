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
