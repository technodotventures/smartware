# QA dev round: reader-friendly ORP rendering (pre-registered 10 Oct 2026, before any export)

Why: with a DeepSeek V3 reader ORP's contexts drew the most "I don't know" answers (QA v3 external run). Inspection
on dev found a concrete rendering fault: ORP prints a "## date · S11" header only the first time a conversation
appears, so a later item from another conversation is shown under the wrong date heading.

Data: LongMemEval-S **dev** split (88 questions), day-precision question times. No test question is used.

Variants (engine ranking unchanged; only how the packed context is written):
- `orp`: shipped v0.1.2 rendering.
- `orp-clean`: every line starts with its own date ("[22 May 2023] user: ..."), no conversation labels.
- `orp-clean-chrono`: as clean, items shown oldest first (selection still by rank and budget).
- `orp-clean-nodates`: as clean-chrono, without the "(two weeks ago = ...)" notes.
- `orp-clean-full`: as clean-chrono, whole turns instead of 4-sentence snippets.
References: `mem0@tuned`, `bm25`. Budget 1,000 tokens.

Readers and judges: Claude Sonnet sub-agents here (same v3 briefs and blinding) and DeepSeek V3 (`deepseek-chat`,
temperature 0) on the user's machine with `tools/qa3_external.py`.

Selection rule: the variant with the highest mean of (Claude accuracy, DeepSeek accuracy) on dev, provided its Claude
accuracy is no more than 3 points below `orp`'s. Within 1 point, the earlier variant in the list above wins (simpler).
If no variant beats `orp` on the mean, `orp` stays.

Then: the winner and `orp` are scored once on the 232 LongMemEval-S test questions not used in QA v3, against all 15
baselines, BM25 and the oracle, with both readers (protocol to be written before that export).

## Outcome and selection (10 Oct 2026)

| Variant | Claude | DeepSeek V3 | Mean |
|---|---|---|---|
| orp (shipped) | 86.4 | 43.2 | 64.8 |
| **orp-clean** | 89.8 | 56.8 | **73.3** |
| orp-clean-chrono | 88.6 | 46.6 | 67.6 |
| orp-clean-nodates | 88.6 | 40.9 | 64.8 |
| orp-clean-full | 85.2 | 47.7 | 66.5 |
| mem0 (reference) | 81.8 | 59.1 | 70.5 |
| BM25 (reference) | 79.5 | 51.1 | 65.3 |

By the rule above the winner is `orp-clean` (Claude accuracy above `orp`'s, so the 3-point guard is met). With the
DeepSeek reader it is 13.6 points above `orp` (95% CI +5.7 to +21.6). Showing items oldest first hurt the DeepSeek
reader; rank order stays. One reader omission was rerun alone (`RERUNS.log`).
