# Second independent audit of Smartware 0.1.1

Run by a fresh agent with no prior context, on commit 5e12bce, with read access to the repository and the vendored projects, and permission to run tests and small probes (no benchmark jobs). Reproduced here as delivered, lightly reformatted. Responses are in `AUDIT2_RESPONSE.md`.

## Critical

**C1. M67/M68 broken: the thread channel breaks ties using the position of unreadable memories.** `smartware/engine.py:279` (`self.threads = list(by_thread)`, ordered by each thread's first memory, readable or not) and `smartware/engine.py:480` (`np.lexsort((lt, -th_b[lt]))` uses that thread index as the tie-break). With interleaved threads, 29 of 5,760 comparisons differed for default parameters and 49 for the shipped linear ranker, some at top-1, at every granularity. Patching thread order to the thread key gave 0 mismatches. The conformance test and the governance side-channel check both inserted threads contiguously, so neither could detect it.

**C2. Implicit supersession ignored readers and writers.** Any tenant member could hide another user's private fact by writing a newer claim with the same key, and the hidden superseder's id leaked through `get`, `history` and `include.history`.

**C3. De-duplication ignored readers and principal; `meta.idempotency_key` was global across tenants.** A second principal writing the same text got back another principal's private id (silent data loss and an existence oracle); a second tenant reusing a key got the first tenant's id.

**C4. `import_` had no authorisation and bypassed every write-time invariant**, and an edge-only bundle from another tenant could overwrite an edge by id.

## Major

- **M1.** `"labels": "secret"` was stored as a list of characters, bypassing the secret gate.
- **M2.** Label changes on a source never reached derived records.
- **M3.** `reflect` by a less-privileged principal could supersede summaries it could not read.
- **M4.** `approve` did not require read access and did not run supersession.
- **M5.** `known_at` ignored when a supersession was learned; expiry followed `as_of` (time travel bypassed retention); `include.history` returned evidence dated after `as_of`.
- **M6.** M41 failed under the shipped ranker: the `prefer` multiplier was applied but not reported in `why`.
- **M7.** Snapshots and change cursors were global, leaking cross-tenant activity.
- **M8.** The spec's own `channel:` and `role:` tokens could not work with the engine's token computation.
- **M9.** Several conformance tests checked weaker properties than their MUSTs (M67 text-only and contiguous threads; M20; M25, M30, M42, M53 vacuous; M02 required an existence oracle; M60 test contradicted M60; M18 missed nested fields and types).
- **M10.** R@B is inflated by snippets that claim whole turns, most visibly on LME-R (Smartware R@1000 minus Ans@1000 is 0.177 against 0.060 for BM25). Report Ans@B as co-primary and credit a turn only if its content is present.
- **M11.** "Equal tuning budget" is not equal: Smartware had 47 full-dev coordinate-descent evaluations over 20 knobs, five model families with five-fold CV, four packing variants and an ablation selection; baselines get at most 24 combinations screened on a subset with the top 4 on full dev. Baseline tuning was unfinished while default baselines were being scored on test.
- **M12.** `train_smartware.py` does not produce the shipped parameters (they came from `train_ablation.py` plus a manual copy). v0.1.1 was built after the v1 Smartware LoCoMo test run had been seen. The v0.1.1 test run was logged as `+dirty`. `results/raw/` was untracked.
- **M13.** The governance side-channel check could not see C1 (text-only, unit granularity, contiguous sessions); `post_filter` filtered admin spans and dropped a whole span when any member was unreadable; the 8-word shingle detector could not see turns shorter than 8 words (6.3% of test turns); the spec quoted a v1 governance figure as fact.

## Minor

Float32 and float64 BM25 paths differ in the last digits (breaks exact M67 on scores); the opt-in query-expansion channel violates M33; `granularity: thread` returned every unthreaded memory as one item; `neighbours` raised on retracted targets, treated unthreaded memories as one thread and ignored validity and expiry; `forget` dry-run listed others' private derived ids; the re-ingest block revealed that hidden content existed; idempotency keys were shared across principals; `Engine.recall` could exceed its budget; `profile` ignored expiry; HTTP `/jobs/{id}` passed no principal; declared limits were not enforced; non-numeric parameters crashed; persistence dropped audit, blocks, keys and the change feed, and MCP persistence kept one tenant; `redact` left claim fields, source spans and hashes and an embedding-cache key; Appendix B's feature count was wrong (32 non-zero weights; `rr_sub`, `rr_qexp` always 0 but declared); the encoder sha256 cannot be reproduced (unseeded SVD, file predates the script); latency timing depends on run order through a shared cache; 39 LME-R questions have gold sessions later on the question day (inadmissible for Smartware at `as_of = question time`) and 12 test questions fall back to distractors dated after the question (filtered by Smartware, not by time-agnostic baselines); several spec wording issues (process MUSTs, "positional" ids, M28 vs `cascade: false`, defaulted `as_of` and determinism, expiry vs M20, quota-map semantics, undeclared dedup window, `allow_reingest` rejected, `actor` spoofing); an unclosed `keys.json`.

## Verified OK

All 83 tests passed. Expansion uses only admissible units and threads are keyed by `(tenant, thread)`. With thread order patched, default and shipped configurations had zero M67 mismatches over 60 seeds across granularities, labels, drafts, quotas, prefer, kinds, `as_of`/`known_at` and history; BM25 and segment statistics, window and thread aggregates, entity damping, PRF and role detection are all computed over the admissible set. In recall the secret gate needs both `include.labels` and `label:secret`; `scope.within` is path-based; upsert requires `if_version`; versions bump on supersession and forget; `dry_run` changes nothing; reads do not mutate; evidence cannot be superseded. Results were identical across six `PYTHONHASHSEED` values. Tuning paths read dev only; LME-R distractor pools are split-exclusive. `SmartwareSystem` uses only `q.text` and `q.time`; thread labels are neutral; every system receives the same query. Primary metrics are applied identically to all systems; QA batches never hold two items of one question.
