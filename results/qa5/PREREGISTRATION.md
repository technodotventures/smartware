# Confirmation: clean-rendered ORP on held-out LongMemEval-S test questions (pre-registered 10 Oct 2026, before export)

- Questions: the 232 LongMemEval-S test questions (non-abstention) **not** in the QA v3 sample. Their QA outcomes have
  never been seen. (Their retrieval metrics were, in the strict and day-precision runs.)
- Systems: `orp-clean` (the dev winner: ORP v0.1.2 ranking with `render="clean"`), `orp` (shipped rendering), all 15
  baselines at their raw-turn dev-tuned settings, BM25, and the oracle. 1,000-token budget, day-precision times.
- Readers and judges: Claude Sonnet sub-agents (v3 briefs, same blinding and batching) and DeepSeek V3 (`deepseek-chat`,
  temperature 0, run on the user's machine with `tools/qa3_external.py`).
- Primary: `orp-clean` minus each of the 16 comparators (15 baselines and BM25), paired bootstrap 95% CI, Holm over the
  16, reported separately for each reader. Secondary: `orp-clean` minus `orp`.
- Everything is reported as it comes out. No change to questions, systems, rendering or briefs after export.
