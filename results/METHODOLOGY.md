# ORP-Bench methodology and limitations (v0.1.2)

This replaces the v1 methodology (kept in `results/v1/`). Changes after the two independent audits are listed in `results/AUDIT_RESPONSE.md` and `results/AUDIT2_RESPONSE.md`.

## What is measured

Agents receive a bounded context window. We therefore score **what lands in the first B tokens of the context a system returns**, at B = 500, 1,000, 2,000 and 4,000 tokens.

- **R@B (evidence recall):** share of the gold evidence turns contained in that context. An item contains a turn when the item was built from that turn (a sentence-level fact from a turn counts as containing it; a whole session chunk contains all its turns). This convention is lenient to fact- and sentence-level systems.
- **Full@B:** all gold turns contained.
- **Ans@B (answer-token recall):** share of the gold answer's content words (stemmed, stopwords removed) present in that context. It does not depend on how a system claims turns. **Co-primary with R@B.**
- **E@B (evidence-text recall):** like R@B, but a gold turn counts only if the items claiming it carry at least half of its distinct content words. This removes credit for snippets or sentence-level units that claim a long turn without its content (same rule for every system; added after the second audit, so only runs from v0.1.2 onwards report it).
- **SessAll@5:** LongMemEval's session-level retrieval metric over the order in which sessions first appear in the returned context.
- **MRR** of the first item containing gold evidence, and **latency** (p50 and p95 per query, measured separately on an otherwise idle machine).

Token cost is charged on exactly the text each system would put into the agent's context (its own formatting, headers, JSON wrappers, profiles).

- **Primary:** tokens = characters / 4 (format-neutral), greedy packing in rank order that skips an item which does not fit and tries the next one.
- **Secondary (`s_` metrics):** a word-and-punctuation token counter with packing that stops at the first item that overflows (the v1 convention).

## Data

- **LoCoMo-10** (`snap-research/locomo`, vendored): 10 conversations, about 27 sessions each. Categories 1-4 (multi-hop, temporal, open-domain, single-hop); category 5 (adversarial, unanswerable) excluded. Questions whose evidence ids do not resolve are dropped. Conversations 0-2 are **dev**, 3-9 are **test** (1,152 questions).
- **LME-R (LongMemEval oracle, recombined).** The oracle file (vendored in `getzep/graphiti`) gives each question's evidence sessions and `has_answer` turns. The official `_s` haystack is on Hugging Face, which this sandbox cannot reach, so each question's haystack is rebuilt: its evidence sessions plus seeded distractor sessions dated before the question, 40 sessions in total. A hash-based 20% of questions is **dev** (88), the rest **test** (382). Distractors are drawn **only from sessions owned by questions of the same split**, so no dev question's evidence appears in a test haystack or the reverse (the dataset itself shares 4 evidence sessions across splits). Abstention questions are excluded. This is not the official LongMemEval-S benchmark and numbers are not comparable to published ones. Two construction quirks were found by the second audit and are left in place (changing test data after seeing results would be worse): 33 test questions have a gold turn timestamped after the question time (inadmissible to an engine that honours `as_of`, which hurts ORP), and 15 have distractor sessions dated after the question (filtered by ORP, not by time-agnostic baselines, which hurts them). `results/lme_quirks.json` lists them; the report also gives LME-R results without these 45 questions.
- **HotpotQA-50** (Cognee's evaluation set, vendored in `topoteretes_cognee/evals/old`): 500 Wikipedia paragraphs shared by 50 multi-hop questions, each paragraph its own document. Only answers are labelled, so only Ans@B is scored. Added after the v0.1.1 freeze as an out-of-domain held-out check: no system, ORP included, was tuned or developed on it, and it was run once with frozen configurations. 50 questions give wide confidence intervals.

## Systems

Each of the 15 projects was studied from source (reports in `reports/`), then its recall path was reimplemented without an LLM by the engineer who studied it, following a written brief (`baselines/_BRIEF.md`): faithful constants, the strongest shipped mode that needs no LLM or paid reranker, embedder-scale thresholds converted to percentiles where the project's embedder differs. These are **LLM-free re-implementations of the recall path, not the products**. Approximations (for example LLM fact extraction replaced by sentence or turn units) are documented in each module's docstring and `faithful_notes`.

All systems, including ORP, use the **same encoders**: Okapi BM25 (k1=1.2, b=0.75) over a shared tokeniser and stemmer, and a shared dense sentence embedder (PPMI-SVD word vectors with SIF weighting) trained once, without labels, on the union of LoCoMo and LongMemEval text including question strings (not HotpotQA), because no neural weights can be downloaded here. The shipped file's hash is in `results/ENCODER.sha256`. It was built before the SVD start vector was seeded, so `bench/train_encoder.py` builds an equivalent but not byte-identical encoder (it writes a separate file and reports how closely the word-similarity structure agrees). Results therefore compare **memory architecture and retrieval logic**, not model quality.

## Tuning budget (not equal)

- **ORP:** channel weights by coordinate descent on dev (`bench/tune.py`), then a learned ranker chosen by grouped five-fold cross-validation on dev among RRF, logistic regression and gradient-boosted trees, with and without five position- and length-correlated features (`bench/ltr.py`, `bench/train_orp.py`). The shipped model is logistic regression over 34 features (`results/orp_params.json`).
- **Baselines:** each engineer exposed 2-4 documented knobs per project (`TUNABLE`, at most 24 combinations, defaults unchanged; `baselines/_TUNING_BRIEF.md`). `bench/tune_baselines.py` screens every combination on a fixed dev subset (1 LoCoMo conversation, 20 LME-R questions), promotes the best 4 to the full dev splits, and keeps the best by the same objective ORP was tuned on (mean over benchmarks of 0.5 x mean R@500/1000/2000 + 0.5 x mean Ans@500/1000/2000). Graphiti and Hindsight were evaluated on their full grids on full dev. Results: `results/baseline_tuning.json`.
- **The budgets are not equal, and we say so.** ORP had 47 full-dev coordinate-descent evaluations over about 20 knobs, five model families with five-fold cross-validation, four packing variants and an ablation; each baseline had at most 24 documented configurations screened on a dev subset, with the best 4 on full dev. The baseline knobs are the ones each project exposes; ORP's are its own. `orp-rrf` (hand-tuned weights, no learned model) is the closest like-for-like comparison of architecture.
- Both default and dev-tuned baselines are reported. ORP is also reported without any learned model (`orp-rrf`) and with the dropped features (`orp-allfeat`).

## Test discipline

Test splits are read only by `bench/run.py` (and the QA export, governance and latency scripts, which use frozen configurations). Every invocation of `bench/run.py` on test is appended, with the git hash, to `results/TEST_RUNS.log`. Raw per-query results are in `results/raw/` (v0.1.1 run) and `results/raw_v012/` (v0.1.2 runs).

What was seen when:
- **v1** (pre-audit) ORP and baseline test results were seen before v0.1.1 was built. v0.1.1 changed the metrics, the LME-R construction, the engine's access-control internals and re-tuned ORP on dev. The v0.1.1 ranker was chosen on dev, but its developers had seen v1 test numbers on the same test conversations, so LoCoMo and LME-R are not pristine held-out sets for v0.1.1. HotpotQA-50 is.
- **v0.1.1** was frozen at commit 8e00159 and scored once (the log entry is marked `+dirty` because result logs were being written; the engine and parameters were committed).
- **v0.1.2** fixes the second audit's governance and correctness findings without touching ranking parameters. Re-running it on test gives per-query results identical to v0.1.1 on all 1,152 LoCoMo questions (LME-R comparison in the report). The engine was not changed in response to test numbers.

## Governance (ORP-Gov v2)

See the docstring of `bench/governance.py`. Each LoCoMo test conversation becomes a workspace; sessions are channels with seeded membership for 6 principals; 20% of turns are additionally private inside their channel; a second tenant reuses the same thread ids; turns are inserted in a seeded interleaved order across channels. Strategies share the ORP ranker and differ only in access control. Leakage is detected on returned text, per line: 8-word shingles, or the whole line when shorter, counted only when that text occurs nowhere the asker can read. The ranking side channel compares the top 10 items (memory ids, text and scores, at unit and auto granularity) with and without the asker's unreadable memories in the store. Post-filtering ranks with a shared index (statistics over all memories), cuts to the top k items and removes every unreadable unit from each item.

## Blind QA

Protocol, briefs and scripts are in `bench/qa_briefs/` and `bench/qa_blind.py`. Readers (Claude Sonnet sub-agents) answer from each system's 1,000-token context only, with system names hidden and items shuffled; separate judge agents grade against gold without system names. The v1 run (Haiku readers that partly failed, rerun with Sonnet) is disclosed there.

## Limitations

1. **No LLM in the loop for retrieval.** Most projects use an LLM to extract facts at write time and some rerank with LLMs. These re-implementations replace extraction with deterministic units, which favours retrieval-centric designs. Absolute numbers are not comparable to published LoCoMo or LongMemEval accuracy figures. No real product was run: the sandbox has no package registry access.
2. **Shared, modest encoder**, trained on all benchmark text (unsupervised, shared by every system). A strong neural embedder would raise dense-heavy systems more than lexical ones, and the ranking between systems could change.
3. **Reconstructed LongMemEval haystack** (LME-R). Distractors come from other questions' sessions, which are topically closer to each other than the official haystack.
4. **Faithfulness is best-effort.** Each baseline reproduces documented behaviour from code reading. Closed engines (Supermemory) are approximated from public schemas and clients.
5. **Dataset priors.** A learned ranker can absorb benchmark regularities. Features are generic, position- and length-correlated features were dropped by cross-validation, and the unlearned `orp-rrf` is reported alongside.
6. **Each system keeps its own rendering** in QA (part of what is tested), so blinding hides names but not formats.

## LLM-extraction track (added 8 Oct 2026)

Facts were extracted once from every LoCoMo session by Claude Haiku sub-agents (17 runs) following `bench/extraction/EXTRACT.md`, a brief modelled on mem0's `ADDITIVE_EXTRACTION_PROMPT` (self-contained 15-80-word memories, names resolved, relative dates made absolute, each citing its source turns). The 2,813 memories are frozen in `results/extraction/out/`, so the track reruns without a model. Every system, ORP included, may store either raw turns or these facts (ORP may also keep raw turns as evidence with the facts as derived memories); the choice was made per system on the three dev conversations by the same objective (`results/facts_dev.json`), then each choice was scored once on test. A fact claims the turns it cites, so R@B stays comparable; E@B is not reported for this track because facts compress turns by design. LME-R was extracted the same way (140 Haiku runs over its 940 unique sessions, 13.6k memories; facts are keyed by session and mapped into every haystack that contains the session), with store choices made on the LME-R dev split (`results/facts_dev_lme.json`). No system was re-tuned for fact stores: baselines keep the knobs tuned on raw turns, and ORP keeps its ranker trained on raw turns. Caveats: one shared extraction is not each project's own prompt and model; Haiku is a small model.

### Re-tuned fact track (pre-registered 9 Oct 2026, before any re-tuned test run)

The first fact track kept every system's knobs from raw-turn tuning. In the re-tuned track:
- each baseline's documented knobs are re-tuned on the dev fact stores with the same screening procedure (`python -m bench.tune_baselines all --facts` → `results/baseline_tuning_facts.json`, systems named `<name>@ftuned+facts`);
- ORP's ranker is refitted on the dev fact stores (`python -m bench.train_orp_facts` → `results/orp_facts_params.json`, system `orp-ft+facts`), choosing among the unchanged ranker, the hand-tuned RRF ranker and logistic regression by grouped five-fold CV;
- for LoCoMo and LME-R, every system's configuration (raw turns tuned, facts with raw-tuned knobs, facts re-tuned; for ORP also turns as evidence plus facts) is chosen on that benchmark's dev split;
- HotpotQA-50 has no dev split: each system uses the configuration with the highest mean dev objective over LoCoMo and LME-R, and HotpotQA facts were extracted the same way (6 Haiku runs, 1,398 memories);
- each chosen configuration is scored once per benchmark. LoCoMo and LME-R test results have been seen several times by now, so HotpotQA-50 is the only clean held-out check.

## Official LongMemEval-M check (pre-registered 9 Oct 2026, before any LongMemEval-M test run)

The user supplied the official `longmemeval_m_cleaned.json` (xiaowu0162/longmemeval). It holds the same 500 questions as LongMemEval-S and the oracle file, each with its own official haystack: 460-490 sessions, about 4,900 turns and 1.2M tokens per question, against 40 sessions in LME-R. It was compacted on the user's machine into deduplicated sessions (51,661 unique, no session id with two different contents; `bench/data.py:load_lme_m`). Same question ids as LME-R, so the same dev/test split (88 / 382 non-abstention questions).

Protocol, fixed before any test number is seen:
- **Raw-turn track only.** LLM extraction of 540k unique turns (about 130M input tokens) is out of budget, so there is no fact track here.
- **Frozen configurations, no tuning on LongMemEval-M:** `orp` (shipped ranker), `orp-rrf` (no learned model), every baseline at its raw-turn dev-tuned configuration (`<name>@tuned`, `results/baseline_tuning.json`), plus the `bm25` and `dense` references. Two dev questions were used only to time the systems; their numbers play no part in any choice.
- Each configuration is scored once on the 382 test questions (`python -m bench.run --bench lmem`), raw results in `results/raw_lmem/`.
- Verdicts as elsewhere: paired bootstrap of ORP against the best of the 15 per metric (R@B and Ans@B co-primary, B = 500 to 4,000), reported on all 382 and without the questions affected by the dataset's own date quirks. The official data has the same quirks found in LME-R: 37 of the 470 questions have a gold evidence session dated after the question (mostly hours later on the same day), inadmissible to an engine that honours `as_of`; 107 have some haystack session dated after the question, which ORP filters and time-agnostic baselines do not.
- The shared encoder was trained without labels on LoCoMo and LongMemEval oracle text; most LongMemEval-M distractor text (ShareGPT and UltraChat sessions) was not in its training data. This applies to every system equally.
- This is the cleanest test in the suite: new haystacks no system or configuration was developed on. The questions and their evidence sessions are the same as LME-R's, whose test results had been seen.

**Outcome (scored once, 9 Oct 2026).** Against the best of the 15 on each metric, ORP is better on 2 (R@500 +6.9, R@1000 +4.8), level on 5 and worse on 2 (Ans@500 -3.9 against Supermemory Company Brain, SessAll@5 -3.4 against Onyx). Without the 29 test questions whose gold evidence the official data dates after the question: 4 better, 5 level, 0 worse. Plain BM25 over turns matches ORP on answer recall (Ans@1000 66.9 vs 64.4, CI -5.9 to +1.0) while ORP leads it on evidence recall by 7.1 points. Operational notes, all in `results/TEST_RUNS.log`: the first invocation was stopped before any system finished to speed up the mem0 re-implementation (identical outputs on dev); workers of the second were killed by the memory limit and it was resumed from its per-question progress files after clearing the embedding cache between haystacks. No configuration changed between invocations.

## Official LongMemEval-S check (pre-registered 10 Oct 2026, before any LongMemEval-S test run)

The user supplied `longmemeval_s_cleaned.json`. Same 500 questions; each has its own official haystack of 38-62 sessions (about 490 turns, 115k tokens). Its distractor sessions differ from LongMemEval-M's. Compacted with `tools/compact_lme_m.py` (19,195 unique sessions, no id conflicts), loaded by `bench/data.py:load_lme_s`, bench name `lmes`. Same dev/test split (88 / 382).

Protocol: identical to the LongMemEval-M check above, written before any S number was seen. Raw turns only, the same frozen configurations (`orp`, `orp-rrf`, every baseline `@tuned` from raw-turn dev tuning, `bm25`, `dense`), scored once on the 382 test questions, verdicts against the best of the 15 on all questions and without the dataset's date quirks (`results/lmes_quirks.json`: 29 test questions with gold evidence dated after the question, 54 with any session dated after it). No fact track: the test haystacks hold 15,424 unique sessions (about 40M tokens), roughly 16 times the LME-R extraction, so extraction was judged out of budget. The LongMemEval-M results were seen before this check was set up; nothing was changed in response to them.

**Outcome (scored once, 10 Oct 2026).** Against the best of the 15 on each metric, ORP is better on 0, level on 3 (evidence recall R@500 to R@2000) and worse on 6: evidence-text recall (-6.2 against Hindsight), answer recall at every budget (-4.0 to -4.9) and SessAll@5 (-7.6 against Onyx). Without the 29 test questions whose gold evidence is dated after the question: 3 better (R@500 to R@2000, +4.6 to +6.6), 5 level, 1 worse (Ans@2000, -3.1). Against plain BM25 over turns, ORP leads evidence recall by 7.0 points and is level on answer recall (-3.6, CI -7.5 to +0.1). The run completed in one invocation.

## Day-precision question times (dev diagnosis 10 Oct 2026; protocol in `results/dev_diag/PLAN.md`)

A dev-only diagnosis found that every LongMemEval gold session dated after its question falls on the question's own day, so question times are day-precise. The `lmesd` and `lmemd` benches pass every system the end of the question's day as its time. No system's configuration changes. This was found after the S and M test results had been seen; the strict-time results remain the primary pre-registered ones. A post-hoc secondary metric, `answ@B`, scores answer words without bare numbers, because computed numeric answers get chance credit from list numbering, scores and ids.

**Outcome (post hoc, 10 Oct 2026).** With day-precision question times, ORP against the best of the 15: LongMemEval-S 5 better, 7 level, 0 worse (including AnsW@500 and AnsW@1000); LongMemEval-M 4 better, 5 level, 0 worse. Raw Ans@B is level on both. These runs follow a fix found after the test results had been seen, so they are reported as post hoc next to the strict-time results, not instead of them.
