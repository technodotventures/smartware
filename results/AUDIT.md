# Independent audit of Smartware 0.1, the reference engine and Smartware Bench

Auditor: independent pass. I had no access to how the work was produced. Date: 2026-10-08. Repo state: HEAD `4f11341`, with untracked `results/qa_answers*/` and `results/qa_judge/`.
Everything below was checked by reading code and re-running it. Scratch scripts sit outside the repo. The only repo writes were this file and the raw jsonl that `bench.run` regenerated. Those regenerated files are byte-identical to the originals (checked with `cmp`).

---

## Verdicts on the headline claims

### (a) Smartware has higher evidence recall than all 15 baselines at 500/1000/2000/4000 tokens on LoCoMo-test and LME-H-test. Verdict: **SUPPORTED as measured, with material caveats.**

- **Reproduction.** I re-ran `bench.run --bench locomo --systems smartware,supermemory-company-brain,mem0 --split test`. Every metric matched `results/summary.json` exactly (R@500/1000/2000/4000, ans@1000, MRR, n=1152), and the raw jsonl was byte-identical.
  - smartware: 72.0 / 80.2 / 86.1 / 89.5.
  - Company Brain: 59.4 / 71.4 / 78.5 / 82.8.
  - mem0: 55.6 / 62.7 / 67.6 / 68.5.
- **Margins are well outside noise.** Smartware minus the best baseline at each budget, with paired query bootstrap and 95% CI:
  - LoCoMo: +12.7 [10.4, 15.1], +8.8 [6.7, 10.9], +7.5 [5.7, 9.2], +6.7 [5.2, 8.4].
  - LoCoMo again, clustered by conversation (n=7): R@1000 +7.6 to +10.3, R@4000 +3.9 to +9.9. Smartware wins in all 7 test conversations.
  - LME-H: +12.5 [8.8, 16.1] (vs mem0), +7.5 [3.8, 11.1] (mem0), +7.1 [4.3, 10.0] (graphiti), +7.7 [5.6, 10.0] (mem0).
- **Claim honesty (check 3).** I sampled 10-15 test queries per conversation and 40 LME tasks, packed at 2,000 tokens, and checked whether each claimed turn's text appears in `Item.text`:

  | system | LoCoMo full / partial / unverifiable | LME full / partial / unverifiable |
  |---|---|---|
  | smartware | 100% / 0% / 0% (2,515 claims) | 94.5% / 5.5% / 0% (945; partial = "…" snippets) |
  | supermemory-company-brain | 99.5% / 0.3% / 0.2% (3,268) | 51.7% / 48.1% / 0.2% (590) |
  | mem0 | 100% / 0% / 0% (3,194) | 25.8% / 74.2% / 0% (756; sentence chunks of long turns) |
  | graphiti | 2.5% / 60.2% / **37.3%** (3,587) | 0.8% / 99.2% / 0% (1,215) |

  Smartware's claims are honest: an item claims only turns whose text it contains. Snippets are marked "…", and long units are claimed after snippeting, which is lenient but declared. The lenient part is that LME snippets claim the whole turn even though the gold sentence may have been cut. Graphiti's "unverifiable" 37% are not dishonest either: they are pronoun-rewritten sentence facts ("John will do John's best!") and fragments ("Tim: Wow!"). Graphiti's dedup also merges identical facts across many turns, so one fact like "Tim: Wow!" claims 6+ different turns. That *inflates* graphiti's recall, against Smartware's interest.
- **Caveats that make "higher than all 15 projects" stronger than the evidence.** See issues 4, 5, 8, 9, 10 and 11:
  - the baselines are LLM-free proxies whose extraction step was replaced;
  - the encoder is a PPMI-SVD stand-in;
  - LME-H is a reconstruction;
  - the token counter penalises JSON-heavy formats;
  - Smartware's learned ranker is fitted on the same benchmark families, while the baselines got zero tuning;
  - without the learned ranker (`smartware-rrf`), Smartware **loses** on LME R@1000 (73.8 vs mem0 74.7) and R@2000 (78.8 vs graphiti 82.8).

  The claim must be stated as "this reference engine, fitted on dev splits of these two benchmarks, beats LLM-free re-implementations of 15 projects under one shared weak encoder". It must not be stated as "Smartware beats mem0/Graphiti/...".

### (b) All tuning used dev splits only; test was scored once. Verdict: **PARTLY SUPPORTED.**

- **The code is clean.** The splits are `bench/data.py:121` (LoCoMo conversations 0-2 are dev) and `bench/data.py:131-132,174` (LME md5 hash, 20% dev).
  - `tune.py`, `ltr.py`, `tune_pack.py` and `tune_pack_cv.py` all go through `devtune.tasks/evaluate` or `ltr.collect`, which filter `q.split == "dev"` (`devtune.py:36`, `ltr.py:37`).
  - `smartware_variants.py` only loads JSON parameters.
  - No tuning path reads test queries.
- **Refit check.** I re-ran `ltr.collect` and refitted the chosen GBDT config on the first 36 features. It reproduces the shipped `ltr_params_v1.json` model exactly (133 trees, max |Δ| = 0). So no test data entered the fit.
- **File times.**
  - `tuned_params.json` 02:57:02, `ltr_params_v1.json` 04:11:27 and `pack_cv.json` 04:41:11 all predate `raw/locomo/smartware.test.jsonl` (04:48:20) and `raw/lme/smartware.test.jsonl` (04:53:13).
  - Git history agrees: commits `18789aa` and `a4a78af` contain no `smartware`/`smartware-rrf` test entries in `summary.json`.
- **"Scored once" is not verifiable, and the history contradicts the spirit of it.**
  - The raw/ and cache/ directories are git-ignored (`.gitignore:1-2`), so any earlier test scoring of Smartware would leave no trace. Absence of evidence is all that can be shown.
  - Both baseline test runs (02:48-02:50 LoCoMo; 02:53-04:03 LME) happened *before* any Smartware tuning (tune.log ends 03:03, ltr 04:04-04:14). The same session then iterated through 3 ranker generations (v1, v2 `ltr_params.json` with 39 features, then `pack_params.json` vs `pack_cv.json`), with baseline test numbers in hand.
  - The author also edited `spec/Smartware-0.1.md` §5.5 at 04:47:49, eleven seconds before the final `smartware_variants.py` edit, to excuse the learned ranker from dialogue-span expansion.
  - None of this proves test peeking. It does mean the selection of which Smartware variant to ship (`ltr_params_v1` rather than v2, `expand_*=False`) was made after test numbers for all competitors were visible. Model selection relative to *known competitor scores* is a mild form of adaptive overfitting that a sceptical reviewer will raise.
- **Dev/test contamination on LME (issue 6).** 2,547 of the 3,520 haystack sessions in the dev tasks are gold evidence sessions of test questions, because distractors are sampled from the shared pool. Four evidence sessions are shared outright. The ranker was fitted on candidate pools drawn from test-question gold sessions, labelled negative. That probably *hurts* Smartware slightly, but it breaks the independence claim.
- **The shared dense encoder was trained on all benchmark text**, test questions and answers-bearing turns included (`bench/data.py:178-193`, `encoders.py`). It is shared by all systems, so it is not an Smartware-specific advantage. But "test was never touched" is false at the encoder level.

### (c) In blind QA, readers answered better from Smartware's context than from the 4 strongest baselines. Verdict: **PARTLY SUPPORTED.**

- **The numbers recompute exactly** from `results/qa/_key.json` and `results/qa_judge/v*.json`: 1,345 verdicts, no missing or extra, no conflicts.
  - LoCoMo (n=150): smartware 70.7, cognee 63.3, company-brain 62.0, mem0 59.3, graphiti 53.3.
  - LME (n=119): smartware 79.0, mem0 68.9, graphiti 65.5, company-brain 62.2, cognee 32.8.
  - Reader answers are identical to judge inputs, and gold matches the key.
  - My paired bootstrap reproduces `smartware_minus_ci`, e.g. LoCoMo vs cognee +7.3 [1.3, 13.3]. All lower bounds are > 0.
- **No system names leak.** I grepped `results/qa15/*.json` and `results/qa_judge/j*.json`. The only "smartware" hits are inside "incorporate".
- **Blinding is cosmetic, though: every context is identifiable from its format.**
  - 269/269 smartware contexts start with `## date · S3` headers.
  - 269/269 company-brain contexts have `n. Memory (58%) <22-char id>:`.
  - 269/269 graphiti contexts are `{"fact": ..., "valid_at": ...}`.
  - mem0 contexts carry `(on YYYY-MM-DD)` suffixes.
  - The readers were Claude agents that also see the authors' framing (`qa_answers15/_READER_BRIEF.md`). Readers could not know which format is "Smartware", so the bias risk is low, but "blind" overstates it.
- **Smartware's LME contexts expose LongMemEval's own session ids** (`answer_4be1b6b4_2`) in its thread headers (`engine.py:658`). In 75 of 116 Smartware LME contexts, the most frequent id prefix is the gold question's prefix: a dataset artefact that groups a question's evidence sessions. Other systems do not print session ids. It is unclear how much a reader exploits this.
- **A first run was discarded.** `results/qa_answers/` holds 495 answers from an earlier run (45-item batches, batches 4-5 missing, `temp_batch_*.txt` placeholder files). That run was abandoned. Re-asking the same uids gave identical answers only 148/495 times, and "I don't know" for smartware fell from 58 to 9. The switch from 45-item to 15-item batches is not documented anywhere. These files are untracked and not mentioned in RESULTS/METHODOLOGY. Re-running an evaluation and reporting the second run, without disclosure, is the most embarrassing item here even if innocent.
- **The QA result is not reported** in `RESULTS.md` or `METHODOLOGY.md`. No script computes `qa_blind.json` (grep finds none), and the reader and judge model ids are not recorded. `bench/qa.py` describes a different, API-based pipeline (Haiku answerer and judge) that was not used.
- **48/1,345 items shared a question with another item in the same 15-item reader batch**, so cross-item leakage was possible for those.

### (d) The governance benchmark shows pre-ranking admissibility plus provenance closure avoids leaks and recall loss. Verdict: **PARTLY SUPPORTED. The leak half is circular, and the recall half is not shown.**

- **Implementation.** The strategies are implemented as described (`bench/governance.py:67-122`).
- **Leak result is guaranteed by construction.** `pre_filter` and `post_filter@k` use the same `_can_read` check, so 0% leakage for both is guaranteed, not measured. `scope_only` (no ACL at all) leaking ~100% is also guaranteed. The only non-trivial comparison is `derived_bank`. There, the benchmark's own extractive summaries are made public by fiat (`governance.py:60-63`). That is a strawman for "bank/dataset-level consolidation", not a measured behaviour of any baseline.
- **Recall loss is not shown.** `post_filter@50` beats `pre_filter` at 50% visibility: 88.6 vs 87.4. RESULTS.md shows 82.2 vs 82.1, with the stale pre-update numbers. At 15% visibility (`governance_v15.json`), pre-filter wins (95.6 vs 93.9 @50, 83.9 @10).
- **The engine's own pre-filter is not leak-free.** The metric would not catch it because leak counting uses `memory_ids`, and the leaked neighbour does appear there.
  - Dialogue expansion in `_pack` (`engine.py:636-644`) pulls the previous/next turn with no admissibility check.
  - I confirmed this: a short public reply "Yes, Globex sounds right" returned the exec-only prompt "the acquisition target is Globex at 40M?" to a non-exec principal. The `expand_next` path did the same.
  - This even crosses **tenants**: two tenants using the same thread id "general" got a cross-tenant prompt merge (`by_thread` keys on `thread` only, `engine.py:242`).
  - The benchmark config disables `expand_next` and `expand_prev_q` but not the `prompt_merge_words` merge. So `pre_filter`'s 0.0% leak in governance.json is only true because LoCoMo ACLs are per-session, and the prompt merge never crosses a session.
- **Side channels.** Window, thread and neighbour features are built over *all* memories, inadmissible ones included (`engine.py:249-306`, features `cover_next`/`nb_bm25`/`prev_is_question` at `engine.py:581-598`). I confirmed that an exec-only turn mentioning "layoffs march" moves its public neighbour from rank 20 to rank 1 for a non-exec asker, under both rankers. This violates §5.1 ("Every channel ... MUST draw only from the admissible set") and §5.10.
- **The table is stale.** The RESULTS.md governance table predates the final params (git `a4a78af` numbers: 82.1/77.2/82.2/79.4/81.6) and contradicts `results/governance.json` (87.4/79.9/88.6/84.1/87.4, derived-bank leak 29.1 not 40.9).

### (e) The conformance suite checks the spec's MUSTs, and the reference engine passes it. Verdict: **NOT SUPPORTED.**

- `python3 -m unittest discover -s tests -t .` runs 35 tests: OK (with ResourceWarnings for unclosed sockets and subprocess).
- But the spec has 60 MUST/MUST NOT and 1 REQUIRED. The suite has 35 tests, and many MUSTs are untested *and* violated by the engine, which still advertises `"conformance": ["L1","L2","L3","L4"]` (`api.py:95`).
- Probes I ran against `Service(HashEmbedder())`:

| Spec MUST | Engine behaviour | Tested? |
|---|---|---|
| §5.1 every channel and neighbour expansion only from the admissible set | violated: inadmissible prev/next turn text returned (`engine.py:636-644`); inadmissible text shapes window/thread/feature scores | no |
| §2/§3.1 tenant is a hard boundary | violated: same `thread` id in two tenants are merged as neighbours, and cross-tenant text is returned; a `*` principal of tenant A can `get` and `forget` tenant B records (`api.py:78-80, 83-84`); ids are global, not per tenant (`api.py:157-160`) | no |
| §5.2 return only memories valid at `as_of` (`valid_from ≤ as_of < valid_to`) | violated: `valid_to` is never checked; a fact with `valid_to` in the past is returned as current; `valid_from` gets a +1 day slack (`engine.py:351`); events after `as_of` are returned | no (only via supersession) |
| §5.2 time-travel result "is returned as current" | violated: returned as `validity: superseded` with `[superseded]` prefix (`engine.py:662-663, 671`) | no |
| §5.2 `event`/`document` "never superseded" | violated: `remember(mode="supersede")` supersedes an event (`api.py:171-179`) | no |
| §3.1 `version` increments on every change | violated: `forget` and auto-supersession leave `version` unchanged (`engine.py:160-163, 213`) | no |
| §3.1 `sources` MUST for non-observed | violated: `reflect` summaries have `sources=[]` | no |
| §3.1 `event_time` is an interval `{start,end,granularity}` | rejected with `invalid_parameter` (`api.py:141`) | no |
| §4.1 upsert "requires `if_version` when the record exists" | violated: upsert without `if_version` is accepted (`api.py:163`) | no |
| §4.1 reject unsupported enum values | violated: `wait`, `role`, `load` accept anything | partly (only `mode`) |
| §4.2 `scope.within` narrows admissibility | broken: string-prefix test, so `org/acme` admits `org/acme-evil/...` (`engine.py:355`) | no |
| §4.2 `kinds` is a per-kind quota | silently ignored (quota `{"event":2}` returned 10 items) | no |
| §4.2 `granularity`, `include.drafts`, `include.neighbours`, `scope.prefer` | accepted and ignored, against P5 "honoured or rejected" | no |
| §4 all responses carry `request_id` and `degraded[]` | `request_id` is absent everywhere; `degraded` is hard-coded `[]` (`api.py:254`) | no |
| §5.4 every applied feature MUST appear in `why` | `why` lists channel RRF contributions only, never the GBDT/feature terms | no |
| §5.9 same request + snapshot gives the same pack | violated: `forget` changes the pack without changing `snapshot` (snapshot hashes only the last 50 ids and the count, `engine.py:700-701`) | weak (two identical calls) |
| §5.3 scores higher-is-better with a declared scale | `capabilities.score_scale` says "fused-rrf-normalised", but the shipped ranker returns a GBDT sigmoid (`engine.py:514-518`, `api.py:106`) | no |
| §5.10 learned rankers MUST NOT use features computed over inadmissible memories | violated (see (d)) | no |
| §6.4 `secret` not returned unless included | `get` and `neighbours` return secret content (`api.py:285-306`); recall drops secrets *after* ranking (`api.py:234`), the post-ranking filtering §5.1 calls non-conformant | recall only |
| §6.4 `redact` erases content in the record and every derived record and index | `meta.entities` survives redact and is exported (I confirmed "Globex Corporation" in the export after redact) (`engine.py:213-216`) | partly |
| §3.4 an edge is readable only if both endpoints are | `export` emits every edge in the tenant regardless of endpoint ACL (`api.py:462`) | `edges()` only |
| §6.5 audit retrievable by `role:auditor` | the engine audit list is never exposed; `changes` uses group `auditor` | no |
| §8.3 import lossless; tombstones exported | the round trip is tested only for public records and ADMIN; tombstones are ids only; there is no `id_map` | partly |
| §5.8 MUST NOT return empty success when a scorer failed | no failure detection at all | no |

- **Spec internal contradictions and ambiguities:**
  1. §5.1 forbids "post-ranking filtering" outright, while the governance benchmark presents post-filter@50 as having *better* recall. The spec's rationale ("silently loses recall") is not supported by its own results at 50% visibility.
  2. §5.2 says time-travel hits are "returned as current". §5.5 and the engine mark them superseded.
  3. §5.5 "MUST NOT exceed `budget.tokens`" conflicts with §5.7: abstention returns an empty pack. That one is fine. But §4.3 makes `budget.tokens` REQUIRED for `profile` while recall silently defaults it to 2000 (`api.py:195`).
  4. §3.3: retract "blocks re-ingest", but §4.1 idempotency says replay "MUST return the original ids". A retracted record's replay must now both return its id and be refused.
  5. §4.2 lets `kinds` be "a per-kind quota map or list", and §3.2 says recall requests "MAY set per-kind quotas". "MAY" for the client conflicts with P5 "honoured or rejected" when the engine ignores it.
  6. §6.3 asserts "No project we studied enforces this". That is a factual claim about 15 codebases, made in a normative document.
  7. §5.5 was amended (`with granularity: span ... With auto, the engine decides (the reference engine's learned ranker returns units...)`) to fit the reference engine's benchmark configuration. Normative text should not cite one implementation's tuning.
  8. §9: "The conformance suite tests each MUST in this document", which is false (see the table).
  9. §5.3 requires "at least ... a dense channel", but §5.8 allows `dense_unavailable` degradation. It is unclear whether a degraded engine is conformant.

---

## Issues ranked by severity

### Critical

**1. The reference engine leaks inadmissible text through dialogue expansion, including across tenants.** `smartware/engine.py:633-644`, `smartware/engine.py:240-248`.
- `_pack` merges `prev[k]`/`next[k]` without consulting the admissibility mask.
- `by_thread` is keyed on `thread` only, not `(tenant, thread)`.
- Repro: an exec-only prompt followed by a public short reply in the same thread lets a sales principal recall the exec text. The same happens across tenants that share the thread id "general".
- This falsifies the spec's central selling point (P3, §5.1, §6.3) in the reference implementation. Conformance tests pass only because they never place mixed-ACL turns in one thread.
- **Fix:** pass `mask` into `_pack` and require `mask[pk]`/`mask[nk]`. Key threads on `(tenant, thread)`. Add conformance tests for mixed-ACL threads and cross-tenant same-thread ids.

**2. Inadmissible memories shape ranking (side channel), violating §5.1/§5.10.** `smartware/engine.py:249-306` (window BM25/dense, thread BM25/centroid built over all memories), `engine.py:575-598` (`cover`, `cover_next`, `nb_bm25`, `prev_is_question`, `next_reply` read neighbours regardless of ACL).
- Repro: an exec-only note containing the query term moves its public neighbour from rank 20 to rank 1 for a non-exec principal.
- Both the RRF and GBDT rankers are affected. Ranking order then reveals the existence of private content.
- **Fix:** build window, thread and neighbour features per request over the admissible subgraph only (mask neighbours and recompute thread aggregates on the mask), or cache per ACL equivalence class. Add a test asserting the ranking is invariant to inadmissible content.

**3. Conformance claim is false.** `smartware/api.py:95` advertises L1-L4. `spec/Smartware-0.1.md:376` says the suite tests "each MUST". At least 20 MUSTs are untested, and most of those are violated (see the table in (e)), including tenancy, the validity window, versioning, `request_id`/`degraded`, `why` features, `scope.within`, the secret gate on get/neighbours, edge closure on export, and redact residue.
- **Fix:** derive a MUST checklist mechanically from the spec (one test id per MUST). Mark untested ones `xfail` and drop the advertised levels until they pass. Fix the probes listed above, each with a test.

### Major

**4. The headline over-generalises from LLM-free proxies to named products.** `results/RESULTS.md:1-60`, `spec/Smartware-0.1.md:14,22`, `README.md:3`.
- The baselines replace the very step (LLM extraction) that most of these products are built around.
- There is no external anchor: no baseline was run in its real form even once, for example mem0 OSS in `infer=False` mode, which is runnable offline.
- The table labels rows "mem0", "Graphiti (Zep)" and so on with no qualifier.
- **Fix:** relabel rows "mem0 recall path (LLM-free re-impl.)". Add one sanity anchor per family where feasible: run the real mem0 `infer=False` / Graphiti BM25+cosine search with the same embedder, and report agreement with the proxy.

**5. Asymmetric optimisation budget.**
- Smartware got coordinate descent over 20 parameters, a GBDT with 36 features chosen by grouped CV, and packing chosen by CV, all on dev splits of *the same two benchmarks*.
- Every baseline was frozen at "faithful" defaults, and baseline authors were forbidden to inspect scores (`baselines/_BRIEF.md` rule 7). Several baselines ship tunable knobs (mem0 `threshold`/`top_k`; Graphiti rerankers; Company Brain fusion weights, which are themselves *assumed*).
- Without the learned ranker, Smartware is not best on LME R@1000/R@2000 (`summary.json`: smartware-rrf 73.8/78.8 vs mem0 74.7, graphiti 82.8).
- The ranker also learns dataset priors. On LME, Smartware's top-5 items start with a session-opening turn 31.7% of the time, vs 18.3% for BM25 and mem0 (base rate 8.7%). This is the LongMemEval construction artefact METHODOLOGY.md says was excluded. It re-enters via `prev_is_question`, `order_pos` and `log_tokens`. Recall split by gold position shows Smartware flat (77.4% session-start vs 77.1% other) while graphiti and smartware-rrf are much worse on session-start gold (60.7 and 63.2 vs 73.5 and 72.9). That is consistent with a learned prior.
- **Fix:**
  - report `smartware-rrf` as the protocol headline and the GBDT as an add-on;
  - give each baseline the same dev budget (tune its documented knobs on dev with the same objective);
  - add a held-out third benchmark the ranker never saw (e.g. a MSC/PerLTQA-style set, or LongMemEval_S once downloadable);
  - ablate position-correlated features.

**6. LME dev/test contamination and a non-standard haystack.** `bench/data.py:137-175`.
- Distractors are drawn from the pool of all questions' evidence sessions. 2,547/3,520 dev-haystack sessions are test-question gold sessions, and 4 evidence sessions are shared between dev and test questions.
- The haystack (40 sessions, all evidence sessions from other questions) is unlike LongMemEval_S (~50 sessions, mostly ShareGPT/UltraChat filler).
- The vendored `xiaowu0162_LongMemEval/data` has no haystack file, so the reconstruction is unavoidable here, but it is labelled "LongMemEval" in tables.
- **Fix:** sample distractors only from sessions that are not gold for any question in the other split (or split the session pool). Rename the benchmark "LME-oracle-recombined", and footnote it in every table.

**7. Blind QA run was repeated; the first run is undisclosed; the pipeline is unscripted.** `results/qa_answers/` (495 answers, 45-item batches, placeholder temp files) vs `results/qa_answers15/` (1,345, 15-item batches).
- No code produces `qa_blind.json` or the reader and judge batches. `bench/qa.py` documents a different pipeline (Haiku via API) that was not used.
- The reader and judge models are not recorded, and the QA results are absent from RESULTS.md and METHODOLOGY.md.
- **Fix:** commit the export, reader, judge and scoring scripts. Record the model ids and prompts. Report both runs, or justify discarding the first (e.g. it was incomplete) in METHODOLOGY.md. Pre-register batch size.

**8. QA blinding is cosmetic; Smartware contexts carry dataset session ids.** `engine.py:657-659` prints `thread.split(":")[-1]`, which for LME is `answer_<hash>_<n>`. That groups a question's evidence sessions and is visible only in Smartware contexts (75/116 have the gold prefix as the majority label). All five systems' outputs are format-identifiable.
- **Fix:** replace thread labels with opaque per-pack ordinals in the benchmark adapter. Normalise every system's context into one neutral rendering (date + speaker + text) for the reader, or at least test reader agreement on a format-scrambled subset.

**9. The token counter penalises punctuation-heavy formats, which disfavours JSON and id-bearing baselines.** `bench/text.py:56-59` counts each punctuation mark as a token, times 1.1.
- Graphiti's JSON wrapper costs 40 "tokens" for 78 chars (about 22 BPE tokens). Smartware's `## 2023-04-16 (Sun) · S3` header costs 13 for 25 chars. The harness's own 22-char random id costs 1, whereas BPE would charge ~6-10.
- Re-packing with chars/4: graphiti's LoCoMo R@500 rises 54.4 to 60.3 and R@1000 62.8 to 68.2, while Smartware moves 72.0 to 73.3 and 80.2 to 81.1. Rankings hold, but the graphiti gap shrinks by ~5 points.
- METHODOLOGY calls this "~1.25 per word", which is not what the code does.
- **Fix:** use a real BPE tokenizer (cl100k/o200k via a vendored vocab), or at least report a chars/4 sensitivity table.

**10. Stop-at-first-overflow packing disfavours systems that return large items.** `bench/metrics.py:15-30` stops at the first item that does not fit, so one oversized chunk ends the context, while Smartware's own engine snippets long units to avoid this. This is a harness choice that matches Smartware's design.
- Measured on LME-test with a skip-and-continue packer (R@500/1000/2000, stop vs skip):
  - dense reference: 56.7/65.2/75.1 vs **66.4/73.6/81.3** (+8 to +10);
  - Company Brain: 53.0/64.4/74.4 vs **61.4/69.4/76.8** (+8 at 500);
  - Letta: 50.0/63.2/75.6 vs **63.9/69.5/78.3** (+14 at 500);
  - mem0: 63.4/74.7/82.6 vs 65.6/76.2/83.2;
  - graphiti: unchanged;
  - smartware: 76.0/82.2/90.0 vs 76.8/83.1/90.2.
- Smartware still leads, but the rule costs whole-turn systems up to 10 points on long-turn data.
- **Fix:** report skip-and-continue packing alongside, and justify the choice in METHODOLOGY.

**11. Governance benchmark is circular and its table is stale.** `bench/governance.py:113-122`, `results/RESULTS.md:95-103`.
- Pre-filter and post-filter share the same ACL check, so 0% leak is by construction.
- `derived_bank` is a constructed strawman.
- Leak detection uses `memory_ids`, and the scenario has per-session ACLs only, so it cannot detect issue 1.
- The RESULTS table predates the final params and contradicts `governance.json`: RESULTS shows pre-filter 82.1 vs post@50 82.2, while governance.json has 87.4 vs 88.6.
- At 50% visibility post-filter@50 has *higher* recall than pre-filter, which undercuts the §5.1 rationale.
- **Fix:**
  - regenerate RESULTS.md (`bench.report`) and report both visibility levels;
  - add intra-thread mixed ACLs and cross-tenant same-thread ids;
  - measure leakage on returned *text* (substring of any unreadable unit), not on `memory_ids`;
  - compare against real baseline behaviours (Letta tag post-limit, Hindsight graph arm) instead of a constructed bank.

**12. "Test scored once" is unverifiable by design.** `.gitignore:1-2` excludes `results/raw/` and `results/cache/` (including `dense.pkl`, the encoder all numbers depend on). There is no script that trains `dense.pkl` (no call site for `DenseEncoder.train`), so the benchmark cannot be reproduced from the repo.
- **Fix:** commit `bench/train_encoder.py` and the encoder's hash. Commit raw per-query results (the spec's own §10 rule requires publishing them). Log every test invocation with a timestamp and git hash in an append-only file.

**13. Time-validity semantics broken in the engine.** `smartware/engine.py:346-353`.
- `valid_to` is never checked except for superseded records.
- `valid_from` gets +1 day slack.
- `kind == "event"` bypasses the `valid_from` check, so events observed after `as_of` are returned (time travel leaks the future).
- Time-travel hits are labelled superseded, against §5.2.
- **Fix:** implement `valid_from ≤ as_of < valid_to` exactly, plus `observed_at ≤ as_of` for evidence, and label by validity at `as_of`. Add tests.

**14. `secret` gate is post-ranking and missing on get/neighbours.** `smartware/api.py:229-237, 285-306`. `get` and `neighbours` return secret content to any reader.
- **Fix:** add the label gate to `_mask` and `_can_read`. Test it on every verb.

### Minor

**15. `scope.within` is a string prefix, not a path prefix.** `engine.py:355`. `org/acme` admits `org/acme-evil`. **Fix:** compare path segments.

**16. Snapshot does not identify index state.** `engine.py:700-701` hashes the last 50 ids and the count. Status, ACL and content changes do not change it, so §5.9 is violated. **Fix:** maintain a monotonic mutation counter, or a hash of `(id, version, status)`.

**17. `version` not bumped on forget or auto-supersession; upsert accepted without `if_version`.** `engine.py:160-163, 213`, `api.py:163`. **Fix:** bump on every mutation, and require `if_version` for upsert of an existing record.

**18. `capabilities` misdescribes the shipped ranker.** `api.py:101-107`. `score_scale` and `coverage_method` describe RRF while GBDT is active. `fitted_on` omits that the hash split has session overlap with test. **Fix:** derive these fields from `p.ranker`.

**19. Redact leaves residue.** `engine.py:213-216` keeps `meta.entities` (named entities from the erased text), and export publishes them. **Fix:** clear all derived `meta` on redact.

**20. Export leaks edges between unreadable memories.** `api.py:462`. **Fix:** apply `edges()` closure in `export`.

**21. Spec claims about other projects are unsourced in the spec itself.** `spec/Smartware-0.1.md:17-22, 319`: e.g. "No project we studied enforces this", "accepted-but-ignored parameters in at least six projects". **Fix:** cite report sections (file:line) in an appendix, or soften.

**22. METHODOLOGY says the ranker uses "39 declared features"; the shipped model uses 36.** `results/METHODOLOGY.md:24`, `spec/Smartware-0.1.md:407`. `ltr_params_v1.json` has 36 features; features 36-38 are computed but unused. **Fix:** state 36, or ship v2.

**23. `smartware-rrf` latency and `smartware` latency are measured with 2 workers on 2 cores.** p50 20-48 ms vs baselines at 1-7 ms (`run_smartware_test.log`, `run_test.log`). These are not mentioned as a trade-off in RESULTS. **Fix:** add a latency column caveat, and benchmark single-process.

**24. Baseline faithfulness spot-check (task 5).** mem0, Graphiti and Company Brain constants match both their report §13 and the vendored source:
- mem0: sigmoid (5,0.7)/(7,0.6)/(9,0.5)/(10,0.5)/(12,0.5) `scoring.py:32-40`; `max(limit*4,60)` `main.py:1655`; threshold 0.1 `main.py:1399`; entity top 500 `main.py:1789`, damping 0.001 `main.py:1816`, 8 query entities `main.py:1761`, dedup 0.95 `main.py:1166`; additive `/max_possible` fusion `scoring.py:97-119`.
- Graphiti: `DEFAULT_MIN_SCORE=0.6` `search_utils.py:65`; `rrf(rank_const=1)` `search_utils.py:1775-1781`; `2*limit` candidates `search.py:286-297`; `MAX_QUERY_LENGTH=128`; recipe `EDGE_HYBRID_SEARCH_RRF` `search_config_recipes.py:111`.
- Company Brain: threshold 0.3 `search-brain-format.ts:1`; `limit: 40` `search-brain.ts:73`; 60 msgs / 8,000 chars `channel-observe.ts:37-38`; tag caps 10/500/20,000 `profile-recall.ts:16-18`.

Places where the baselines look weakened or strengthened:
- mem0 maps the 0.1 threshold to the 25th percentile of the query's cosines, a choice that trims ~25% of every corpus before pooling. With N=200 it only trims the tail, so the impact is small.
- mem0's phatic-skip drops 7/1,839 LoCoMo gold turns (0.4%) and 1/271 LME gold turns from the index entirely (a mild weakening).
- Graphiti's raw-cosine conversion `(1+cos)/2 > 0.6 → cos > 0.2` is correct only for Neo4j. Falkor uses the same normalisation, so this is fine.
- Graphiti's fact dedup merges very short facts across many turns ("Tim: Wow!" claims 6+ turns), which *strengthens* its recall artificially (37% of its LoCoMo claims are not verifiable from item text).
- Graphiti runs with `limit = k = 50` facts. Graphiti's MCP and REST defaults are `max_facts=10` (`mcp_server/src/graphiti_mcp_server.py:619`, `server/graph_service/dto/retrieve.py:13`). Using 50 is generous to it.
- Company Brain's 0.7/0.3 fusion weights are an admitted assumption, and the closed server cannot be checked.

**Fix:** report per-baseline sensitivity to its assumed constants (Company Brain fusion weights, mem0 threshold percentile), and cap Graphiti fact-merge claims to verifiable turns.

**25. Test-suite hygiene.** `tests/test_transports.py` leaves subprocesses and sockets open (ResourceWarnings). **Fix:** close them in `tearDown`.

---

## What survived scrutiny

- **The numbers reproduce bit-for-bit.** Raw jsonl was identical, and summary metrics matched to every digit.
- **Tuning code paths never read test queries.** The shipped GBDT is exactly reproducible from dev data alone.
- **Smartware's per-item turn claims are honest** (100% verifiable on LoCoMo; 94.5% full + 5.5% marked snippets on LME), and more conservative than graphiti's.
- **Margins hold under conversation-clustered bootstrap**, and blind-QA accuracy recomputes exactly from the key and verdicts with no system names in reader or judge inputs.
