# Blind QA protocol (pre-registered for the v0.1.1 run)

- Questions: 150 LoCoMo test + 120 LME-R test questions, stratified by category (seeded), `bench.qa_export`.
- Context: each system's own rendering, packed to 1,000 tokens with the primary packer (chars/4, greedy).
- Batches: 15 items, never two items of the same question in one batch (`bench.qa_blind batches`).
- Readers: Claude Sonnet sub-agents, one agent per 3 batches, brief `READER.md`; they see uid, question, date, context only.
- Judges: Claude Sonnet sub-agents, 150 items per batch, brief `JUDGE.md`; they see uid, question, gold, answer only.
- Scoring: `bench.qa_blind score` (accuracy, paired bootstrap 95% CI vs ORP, per category).
- Disclosure: a v1 run (pre-audit) first used Claude Haiku readers with 45-item batches; it was abandoned because
  batches 4-5 failed and 9-11 were answered only partially (15 of 45), and was rerun with 15-item batches and Sonnet
  readers. Both v1 answer sets are kept in results/v1/qa_answers (Haiku, partial) and results/v1/qa_answers15 (Sonnet).

## v0.1.2 run (pre-registered before any v0.1.2 QA output was seen)

- Systems: ORP (shipped), the three baselines with the highest **dev** tuning objective (Supermemory Company Brain
  0.723, mem0 0.717, Hindsight 0.698; `results/baseline_tuning.json`), each with its dev-tuned configuration, and the
  BM25 reference. Choosing by dev, not test, avoids picking comparators on the scored data.
- Everything else as above: 150 LoCoMo + 120 LME-R test questions, 1,000-token contexts, 15-item batches, Sonnet
  readers (3 batches per agent) and Sonnet judges (150 items per agent), same briefs.
- Command: `python -m bench.qa_export --systems orp,supermemory-company-brain@tuned,mem0@tuned,hindsight@tuned,bm25`
