# ORP-Bench methodology and limitations

## What is measured

Agents receive a bounded context window. We therefore score **what lands in the first B tokens of the context a system returns**, at B = 500, 1,000, 2,000 and 4,000 tokens:

- **R@B (evidence recall):** share of the gold evidence turns contained in that context. An item "contains" a turn when the item was built from that turn (a sentence-level fact from a turn counts as containing it, a whole session chunk contains all its turns). This convention is lenient to fact- and sentence-level systems.
- **Full@B:** all gold turns contained.
- **Ans@B (answer-token recall):** share of the gold answer's content words (stemmed, stopwords removed) present in that context. Independent of how a system claims turns.
- **SessAll@5 / SessAny@5:** LongMemEval's session-level retrieval metric over the order in which sessions first appear in the returned context.
- **MRR** of the first item containing gold evidence, and **latency** (p50/p95 per query, single core).

Token cost is charged on exactly the text each system would put into the agent's context (its own formatting, headers, JSON wrappers, profiles). Items are packed in rank order; packing stops at the first item that would overflow (the first item is always admitted, truncated).

## Data

- **LoCoMo-10** (`snap-research/locomo`, vendored): 10 conversations, ~27 sessions each. Categories 1-4 (multi-hop, temporal, open-domain, single-hop); category 5 (adversarial, unanswerable) excluded. Questions whose evidence ids do not resolve are dropped. Conversations 0-2 are **dev**, 3-9 are **test** (1,152 questions).
- **LME-H** (LongMemEval): the oracle file (vendored in `getzep/graphiti`) gives each question's evidence sessions and `has_answer` turns. The official `_s` haystack is on Hugging Face, which this sandbox cannot reach, so each question's haystack is rebuilt with the same recipe: its evidence sessions plus distractor sessions sampled (seeded) from other questions' sessions dated before the question, 40 sessions in total. Abstention questions are excluded. A hash-based 20% of questions is **dev**, the rest **test** (382).

## Systems

Each of the 15 projects was studied from source (reports in `reports/`), then its recall path was reimplemented without an LLM by the same engineer who studied it, following a written brief: faithful constants, the strongest shipped mode that needs no LLM or paid reranker, embedder-scale thresholds converted to percentiles where the project's embedder differs, no tuning on any benchmark, no score inspection (only a smoke test that prints no metrics). Approximations (e.g. LLM fact extraction replaced by sentence or turn units) are documented in each module's docstring and `faithful_notes`.

All systems, including ORP, use the **same encoders**: Okapi BM25 (k1=1.2, b=0.75) over a shared tokenizer/stemmer, and a shared dense sentence embedder (PPMI-SVD word vectors + SIF, trained once on the union of benchmark text, because no neural weights can be downloaded here). Results therefore compare **memory architecture and retrieval logic**, not model quality.

## The ORP reference engine

Tuned only on dev: channel weights by coordinate descent (`bench/tune.py`), then a learned ranker (gradient-boosted trees over 39 declared features) selected by 5-fold grouped cross-validation on dev and fitted on all dev data (`bench/ltr.py`). Test splits are scored once by `bench/run.py`. The engine claims a turn only when the item text came from that turn, and snippets of long turns are marked with "…".

## Limitations

1. **No LLM in the loop.** Most projects use an LLM to extract facts at write time and some rerank with LLMs. These reimplementations replace extraction with deterministic units, which favours systems whose design is retrieval-centric. Absolute numbers are not comparable to published LoCoMo/LongMemEval accuracy figures, which are end-to-end QA scores with a judge.
2. **Shared, modest encoder.** A strong neural embedder would raise dense-heavy systems more than lexical ones. The ranking between systems could change.
3. **Reconstructed LongMemEval haystack.** Distractors come from other questions' sessions, which are topically closer to each other than the official haystack. This is likely harder for lexical systems.
4. **Faithfulness is best-effort.** Each baseline reproduces documented behaviour from code reading, not by running the project. Closed engines (Supermemory) are approximated from public schemas and clients.
5. **Dataset priors.** The learned ranker can absorb benchmark-specific regularities. We excluded one obvious construction artefact (LongMemEval puts the fact in a session's first user turn) from the feature set, keep features generic, and report RRF-only results alongside.
