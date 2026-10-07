# ORP-Bench methodology and limitations (v0.1.1)

This replaces the v1 methodology (kept in `results/v1/`). Changes after the independent audit are listed in `results/AUDIT_RESPONSE.md`.

## What is measured

Agents receive a bounded context window. We therefore score **what lands in the first B tokens of the context a system returns**, at B = 500, 1,000, 2,000 and 4,000 tokens.

- **R@B (evidence recall):** share of the gold evidence turns contained in that context. An item contains a turn when the item was built from that turn (a sentence-level fact from a turn counts as containing it; a whole session chunk contains all its turns). This convention is lenient to fact- and sentence-level systems.
- **Full@B:** all gold turns contained.
- **Ans@B (answer-token recall):** share of the gold answer's content words (stemmed, stopwords removed) present in that context. It does not depend on how a system claims turns.
- **SessAll@5:** LongMemEval's session-level retrieval metric over the order in which sessions first appear in the returned context.
- **MRR** of the first item containing gold evidence, and **latency** (p50 and p95 per query, measured separately on an otherwise idle machine).

Token cost is charged on exactly the text each system would put into the agent's context (its own formatting, headers, JSON wrappers, profiles).

- **Primary:** tokens = characters / 4 (format-neutral), greedy packing in rank order that skips an item which does not fit and tries the next one.
- **Secondary (`s_` metrics):** a word-and-punctuation token counter with packing that stops at the first item that overflows (the v1 convention).

## Data

- **LoCoMo-10** (`snap-research/locomo`, vendored): 10 conversations, about 27 sessions each. Categories 1-4 (multi-hop, temporal, open-domain, single-hop); category 5 (adversarial, unanswerable) excluded. Questions whose evidence ids do not resolve are dropped. Conversations 0-2 are **dev**, 3-9 are **test** (1,152 questions).
- **LME-R (LongMemEval oracle, recombined).** The oracle file (vendored in `getzep/graphiti`) gives each question's evidence sessions and `has_answer` turns. The official `_s` haystack is on Hugging Face, which this sandbox cannot reach, so each question's haystack is rebuilt: its evidence sessions plus seeded distractor sessions dated before the question, 40 sessions in total. A hash-based 20% of questions is **dev** (88), the rest **test** (382). Distractors are drawn **only from sessions owned by questions of the same split**, so no dev question's evidence appears in a test haystack or the reverse (the dataset itself shares 4 evidence sessions across splits). Abstention questions are excluded. This is not the official LongMemEval-S benchmark and numbers are not comparable to published ones.

## Systems

Each of the 15 projects was studied from source (reports in `reports/`), then its recall path was reimplemented without an LLM by the engineer who studied it, following a written brief (`baselines/_BRIEF.md`): faithful constants, the strongest shipped mode that needs no LLM or paid reranker, embedder-scale thresholds converted to percentiles where the project's embedder differs. These are **LLM-free re-implementations of the recall path, not the products**. Approximations (for example LLM fact extraction replaced by sentence or turn units) are documented in each module's docstring and `faithful_notes`.

All systems, including ORP, use the **same encoders**: Okapi BM25 (k1=1.2, b=0.75) over a shared tokeniser and stemmer, and a shared dense sentence embedder (PPMI-SVD word vectors with SIF weighting) trained once, without labels, on the union of benchmark text, because no neural weights can be downloaded here. `bench/train_encoder.py` rebuilds it; its hash is in `results/ENCODER.sha256`. Results therefore compare **memory architecture and retrieval logic**, not model quality.

## Equal tuning budget

- **ORP:** channel weights by coordinate descent on dev (`bench/tune.py`), then a learned ranker chosen by grouped five-fold cross-validation on dev among RRF, logistic regression and gradient-boosted trees, with and without five position- and length-correlated features (`bench/ltr.py`, `bench/train_orp.py`). The shipped model is logistic regression over 34 features (`results/orp_params.json`).
- **Baselines:** each engineer exposed 2-4 documented knobs per project (`TUNABLE`, at most 24 combinations, defaults unchanged; `baselines/_TUNING_BRIEF.md`). `bench/tune_baselines.py` screens every combination on a fixed dev subset (1 LoCoMo conversation, 20 LME-R questions), promotes the best 4 to the full dev splits, and keeps the best by the same objective ORP was tuned on (mean over benchmarks of 0.5 x mean R@500/1000/2000 + 0.5 x mean Ans@500/1000/2000). Graphiti and Hindsight were evaluated on their full grids on full dev. Results: `results/baseline_tuning.json`.
- Both default and dev-tuned baselines are reported. ORP is also reported without any learned model (`orp-rrf`) and with the dropped features (`orp-allfeat`).

## Test discipline

Test splits are read only by `bench/run.py` (and the QA export, governance and latency scripts, which use frozen configurations). Every invocation of `bench/run.py` on test is appended, with the git hash, to `results/TEST_RUNS.log`. Raw per-query results are in `results/raw/`. No ORP or baseline configuration was changed after test results were seen; the v1 test numbers (pre-audit) are kept in `results/v1/`.

## Governance (ORP-Gov v2)

See the docstring of `bench/governance.py`. Each LoCoMo test conversation becomes a workspace; sessions are channels with seeded membership for 6 principals; 20% of turns are additionally private inside their channel; a second tenant reuses the same thread ids. Strategies share the ORP ranker and differ only in access control. Leakage is detected on returned text (8-word shingles that occur only in text the asker cannot read). The ranking side channel compares the top 10 with and without the asker's unreadable memories in the store.

## Blind QA

Protocol, briefs and scripts are in `bench/qa_briefs/` and `bench/qa_blind.py`. Readers (Claude Sonnet sub-agents) answer from each system's 1,000-token context only, with system names hidden and items shuffled; separate judge agents grade against gold without system names. The v1 run (Haiku readers that partly failed, rerun with Sonnet) is disclosed there.

## Limitations

1. **No LLM in the loop for retrieval.** Most projects use an LLM to extract facts at write time and some rerank with LLMs. These re-implementations replace extraction with deterministic units, which favours retrieval-centric designs. Absolute numbers are not comparable to published LoCoMo or LongMemEval accuracy figures. No real product was run: the sandbox has no package registry access.
2. **Shared, modest encoder**, trained on all benchmark text (unsupervised, shared by every system). A strong neural embedder would raise dense-heavy systems more than lexical ones, and the ranking between systems could change.
3. **Reconstructed LongMemEval haystack** (LME-R). Distractors come from other questions' sessions, which are topically closer to each other than the official haystack.
4. **Faithfulness is best-effort.** Each baseline reproduces documented behaviour from code reading. Closed engines (Supermemory) are approximated from public schemas and clients.
5. **Dataset priors.** A learned ranker can absorb benchmark regularities. Features are generic, position- and length-correlated features were dropped by cross-validation, and the unlearned `orp-rrf` is reported alongside.
6. **Each system keeps its own rendering** in QA (part of what is tested), so blinding hides names but not formats.
