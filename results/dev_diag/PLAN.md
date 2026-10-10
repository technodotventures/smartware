# Dev-only diagnosis of ORP's answer-recall gap (written 10 Oct 2026, before any dev run in this round)

Observed on official LongMemEval-S/M test (seen, not to be tuned on): ORP leads evidence recall (R@B) but trails on
answer recall (Ans@B) and evidence-text recall (E@B); R@B minus E@B is about 9 points for ORP and 0 for Hindsight.
Hypothesis: long turns are cut to 4-sentence snippets chosen by raw query-term overlap, so the item claims the gold
turn but often drops the sentence that carries the answer.

Data: dev splits only. LoCoMo dev (3 conversations), LME-R dev (88), LongMemEval-S dev (88), LongMemEval-M dev (88,
final check only because it is slow). Test splits are not read in this round.

Selection rule, fixed now: primary = mean over the dev benchmarks of mean(Ans@500, Ans@1000, Ans@2000); a variant is
accepted only if it also does not lower mean E@1000 and does not lower mean R@1000 by more than 1 point. Ties go to the
simpler variant. The winner is frozen, then scored once on LongMemEval-S and -M test as "ORP v0.1.3", reported next to
v0.1.2, with the caveat that S and M test results had been seen before the diagnosis began.

## Findings on dev (10 Oct 2026)

1. **Date convention, not ranking.** In LongMemEval every gold session dated after its question falls on the question's
   own calendar day (dev: 15 of 15 late gold sessions, 0.6 to 19 hours later). Question times are day-precise. 11 of
   34 dev temporal-reasoning questions are affected. Setting each question time to the end of its day, for every
   system (`bench/data.py`, `day=True`), lifts ORP on LongMemEval-S dev from R@1000 84.0 to 92.5, Ans@1000 71.8 to
   74.1, SessAll@5 73.9 to 86.4; on LongMemEval-M dev from R@1000 75.3 to 80.4, Ans@1000 68.6 to 70.1. ORP's engine
   and parameters are unchanged; only the question time the harness passes changes, for every system.
2. **Snippet selection is not the cause.** Hybrid (idf + dense) snippet scoring, wider snippets and no snippets were
   tried; none beat the shipped snippets on the primary objective (best: hybrid, 77.28 vs 77.29), so the engine is left
   as it is (tie goes to the simpler).
3. **Much of the remaining Ans@B gap is a metric artefact.** Many temporal answers are computed numbers ("15",
   "6 months", "3 weeks ago"). Ans@B gives credit when the bare number appears anywhere in the context, and numbered
   lists, percentage scores and ids supply such numbers by chance (for example "6. Memory (59%)"). Counting only answer
   words that are not bare numbers, ORP leads on LongMemEval-S dev at every budget (strict times: 71.2 / 77.0 / 83.2
   at 500 / 1,000 / 2,000 tokens; mem0 67.8 / 78.8 / 83.3; Supermemory Company Brain 68.6 / 73.4 / 75.4; BM25
   68.1 / 75.8 / 81.4).

## Test protocol for this round (fixed before any run with day-precision times)

- Benches `lmesd` and `lmemd`: the official S and M test questions with day-precision question times, for every system.
  ORP and every baseline keep their frozen configurations (nothing re-tuned).
- LongMemEval-S: every system rerun. Systems that never read the question time must give identical rows to the
  earlier `lmes` run; this is checked and reported.
- LongMemEval-M: only systems that read the question time are rerun (orp, orp-rrf, gbrain, hindsight, letta, onyx,
  sylph); the others' rows are reused from the `lmem` run, justified by the S identity check.
- Added secondary metric `answ@B` (answer words without bare numbers), reported for S. It was introduced after the
  S and M test results were seen and is labelled post hoc.
- Verdicts as before against the best of the 15. The original strict-time results stay in the report unchanged.

## Test outcome (10 Oct 2026)

- LongMemEval-S, day-precision times, all 19 systems rerun: ORP 5 better, 7 level, 0 worse against the best of the 15
  (R@500/1000/2000 +4.6 to +6.2; AnsW@500 +3.8 and AnsW@1000 +3.1 against mem0; raw Ans@B level). The 14 systems that
  never read the question time gave identical rows to the strict-time run (gbrain and onyx included).
- LongMemEval-M, day-precision times, 7 time-reading systems rerun, 12 reused: ORP 4 better (R@500/1000/2000 +7.6 to
  +10.8, E@1000 +3.5), 5 level, 0 worse.
