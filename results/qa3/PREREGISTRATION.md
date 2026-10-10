# Blind QA v3 on official LongMemEval-S (pre-registered 10 Oct 2026, before any v3 context is exported)

Purpose: an end-to-end test that does not depend on our retrieval metrics. A reader model answers each question from
the context a system returned; a separate judge grades it. This is how LongMemEval itself is scored.

## Fixed choices and the bias each one removes

| Choice | Bias it removes |
|---|---|
| Official `longmemeval_s_cleaned.json` test split, not our rebuild | Our own data construction |
| 150 test questions, stratified by question type, seeded (seed 3), drawn before any context is exported | Cherry-picking questions |
| **All 15 baselines** at their raw-turn dev-tuned settings, plus BM25, plus ORP v0.1.2 shipped | Choosing weak comparators |
| An **oracle** context (the gold evidence turns only, dated) as a ceiling | A reader too weak to tell systems apart |
| Day-precision question times for every system (end of the question's day) | The date quirk, applied to all alike |
| Same budget for all: 1,000 tokens (chars/4), each system's own rendering, greedy packing | Unequal context sizes |
| Readers see only uid, question, question date and context; systems unnamed; items shuffled; no two items of the same question in one batch | Readers favouring a system or comparing contexts |
| Judges see only uid, question, question type, gold and answer | Judges favouring a system |
| Judge rules follow LongMemEval's official per-type rules (off-by-one days accepted for temporal questions; updated answer required for knowledge updates; preference answers judged on use of the user's stated preferences) | Our own grading rules |
| A second, independent judge pass on a seeded 20% subset; agreement (Cohen's kappa) reported | A single noisy judge |
| Paired bootstrap 95% CIs for ORP against every system, plus Holm-adjusted p-values over the 16 comparisons | Picking the comparison after the fact |
| Reader and judge: Claude Sonnet sub-agents; ORP was designed by Claude (Opus) | Disclosed, not removable here |

## Reported

Accuracy per system (overall and per question type), ORP minus each system with CI and Holm-adjusted p, oracle
ceiling, judge agreement. All raw answers, verdicts and the uid-to-system key are kept in `results/qa3/`.
No question, system, prompt or rule is changed after export. Failed reader batches are rerun with the same brief and
logged; nothing is dropped silently.

## Outcome (10 Oct 2026)

All 2,700 reader answers and verdicts collected (one item omitted by its reader was rerun alone with the same brief;
`RERUNS.log`). ORP 80.7% correct; best of the 15: Graphiti and mem0 72.7%, Hindsight 72.0%; BM25 71.3%; oracle
95.3%. ORP minus each system is positive with a 95% interval above zero for all 16 comparisons, Holm-adjusted p at
most 0.031. Second judge agreement 99.8% on 540 items (kappa 0.996). Weakest type for ORP: single-session-assistant
(77.8% against BM25's 94.4%), where long assistant replies are cut to snippets.

## Addendum: external reader and judge (fixed 10 Oct 2026, before any external output exists)

To remove the shared-vendor concern, the same 2,700 blind items are re-read and re-judged by a non-Claude model via
OpenRouter (default `deepseek/deepseek-chat`, temperature 0), run on the user's machine with
`tools/qa3_external.py` because this workspace cannot reach other model APIs. Inputs carry no system labels
(`external/items.json`: uid, question, date, context; `external/judge_meta.json`: question, type, gold). Same briefs,
one item per call. Scored with the same statistics against the hidden key. Reported next to the Claude-read results,
whatever the outcome; calls that still fail after six retries are reported as missing, not dropped.
Amendment (before any external output): the run may call DeepSeek's own API directly (`--provider deepseek`, model
`deepseek-chat`, the same DeepSeek V3 model as the OpenRouter default). Nothing else changes.
