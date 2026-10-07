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
