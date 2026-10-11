# QA dev round: reader-friendly Smartware rendering (pre-registered 10 Oct 2026, before any export)

Why: with a DeepSeek V3 reader Smartware's contexts drew the most "I don't know" answers (QA v3 external run). Inspection
on dev found a concrete rendering fault: Smartware prints a "## date · S11" header only the first time a conversation
appears, so a later item from another conversation is shown under the wrong date heading.

Data: LongMemEval-S **dev** split (88 questions), day-precision question times. No test question is used.

Variants (engine ranking unchanged; only how the packed context is written):
- `smartware`: shipped v0.1.2 rendering.
- `smartware-clean`: every line starts with its own date ("[22 May 2023] user: ..."), no conversation labels.
- `smartware-clean-chrono`: as clean, items shown oldest first (selection still by rank and budget).
- `smartware-clean-nodates`: as clean-chrono, without the "(two weeks ago = ...)" notes.
- `smartware-clean-full`: as clean-chrono, whole turns instead of 4-sentence snippets.
References: `mem0@tuned`, `bm25`. Budget 1,000 tokens.

Readers and judges: Claude Sonnet sub-agents here (same v3 briefs and blinding) and DeepSeek V3 (`deepseek-chat`,
temperature 0) on the user's machine with `tools/qa3_external.py`.

Selection rule: the variant with the highest mean of (Claude accuracy, DeepSeek accuracy) on dev, provided its Claude
accuracy is no more than 3 points below `smartware`'s. Within 1 point, the earlier variant in the list above wins (simpler).
If no variant beats `smartware` on the mean, `smartware` stays.

Then: the winner and `smartware` are scored once on the 232 LongMemEval-S test questions not used in QA v3, against all 15
baselines, BM25 and the oracle, with both readers (protocol to be written before that export).

## Outcome and selection (10 Oct 2026)

| Variant | Claude | DeepSeek V3 | Mean |
|---|---|---|---|
| smartware (shipped) | 86.4 | 43.2 | 64.8 |
| **smartware-clean** | 89.8 | 56.8 | **73.3** |
| smartware-clean-chrono | 88.6 | 46.6 | 67.6 |
| smartware-clean-nodates | 88.6 | 40.9 | 64.8 |
| smartware-clean-full | 85.2 | 47.7 | 66.5 |
| mem0 (reference) | 81.8 | 59.1 | 70.5 |
| BM25 (reference) | 79.5 | 51.1 | 65.3 |

By the rule above the winner is `smartware-clean` (Claude accuracy above `smartware`'s, so the 3-point guard is met). With the
DeepSeek reader it is 13.6 points above `smartware` (95% CI +5.7 to +21.6). Showing items oldest first hurt the DeepSeek
reader; rank order stays. One reader omission was rerun alone (`RERUNS.log`).

## Outcome (10 Oct 2026)

Dev accuracy, Claude / DeepSeek / mean: smartware 86.4 / 43.2 / 64.8; smartware-clean 89.8 / 56.8 / 73.3; smartware-clean-chrono
88.6 / 46.6 / 67.6; smartware-clean-nodates 88.6 / 40.9 / 64.8; smartware-clean-full 85.2 / 47.7 / 66.5; mem0 81.8 / 59.1 / 70.5;
BM25 79.5 / 51.1 / 65.3. By the rule above the winner is **smartware-clean** (`results/qa4dev/selection.json`). Its DeepSeek
gain over smartware is +13.6 points (95% CI +5.7 to +21.6). One Claude reader omission was rerun alone (`RERUNS.log`).
