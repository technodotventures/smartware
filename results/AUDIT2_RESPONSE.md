# Response to the second independent audit (results/AUDIT2.md)

Every finding was accepted. Fixes are in v0.1.2 (commits 644daee onwards). New conformance tests are in `tests/conformance/test_audit2.py`; the auditor's own probes (`m67_fuzz.py` over 30 seeds, the thread-tie and supersession reproducers, `probes_api.py`) now report no violations. 110 tests pass; 70 MUST ids are each mapped to at least one test (`spec/MUSTS.md`).

| # | Finding | What changed |
|---|---|---|
| C1 | Thread ties used hidden units' positions | Thread ties break on each thread's first **admissible** position. New test: a removal-equivalence fuzz over interleaved threads, unthreaded memories, claims, labels and mixed ACLs, both rankers, all four granularities, comparing memory ids, text, scores, `why`, `superseded` and coverage (`test_M33_M66_M67_M68_...`). The governance side-channel check now interleaves channels and compares ids and scores at two granularities. |
| C2 | Supersession ignored ACLs | A version may supersede another only if the writer of the later-written record may write the superseded one; recall applies such pairs over the asker's admissible set; stored status changes only when the newer version's audience covers the older one's. Spec §5.6, new M69. Responses never show ids of unreadable records (M74). |
| C3 | Dedup across principals and tenants | Idempotency keys are scoped to (tenant, writer, key); content dedup to (tenant, writer, readers, labels, thread, actor, content). Client ids are per tenant (stored as `<id>~<tenant>`, shown as the client's id), so neither collisions nor errors reveal another tenant's ids. M02 test rewritten; new M70. |
| C4 | `import_` unauthorised | Import requires the tenant admin role, checks every record and edge belongs to the tenant, re-applies closure, labels, dedup and claim chains, remaps colliding engine ids. New M71. |
| M1 | `labels` as a string | Every field is type-checked at every nesting level (M18 broadened). |
| M2 | Labels not re-inherited | `reclose` re-inherits sensitivity labels on source change. New M72. |
| M3 | `reflect` superseded unreadable summaries | A new summary supersedes only summaries with exactly its audience (M31 text extended). |
| M4 | `approve` | Requires read access; an approved claim runs supersession. |
| M5 | Time semantics | Claim currency is decided over the admissible set, so `known_at` sees what was known then (M76); retention is enforced in real time (M75); history never returns evidence after `as_of`. |
| M6 | `prefer` missing from `why` | Post-ranker multipliers are reported under the learned ranker too, and declared in `capabilities`. |
| M7 | Global snapshot and cursor | Per-tenant, HMAC-keyed snapshots and per-tenant change feeds. New M73. |
| M8 | Token namespaces | Normative token grammar in §6.1; namespaced memberships (`channel:C07`) are tokens as written. New M77. `written_by` (authenticated) is now separate from `actor` (asserted). |
| M9 | Weak tests | M67 test replaced by the fuzz above; M20 now patches the engine clock to 2031 and checks identical results; M25 checks `deep` is identical to `balanced` as declared; M53 checks declared features exclude inactive ones; M60 test now requires the label opt-in on `get`; M18 covers nested fields, types and limits. M30 and M42 remain property tests of features the shipped engine does not have (no feedback-driven ranking, no recency boost); they pass because the engine has neither, which is what the MUSTs require. |
| M10 | R@B inflated by snippets | New evidence-text recall (E@B): a turn counts only if the claiming items carry at least half its content words, same rule for all systems. Ans@B is co-primary in the report. On LoCoMo E@B equals R@B for Smartware (short turns); LME-R figures are in the report. |
| M11 | Unequal tuning | Stated plainly in the methodology; `smartware-rrf` is highlighted as the like-for-like architecture comparison. Baseline tuning was completed before any tuned baseline touched test. |
| M12 | Provenance and discipline | `train_ablation.py --ship` now performs the step that was manual; the two-command pipeline is documented. The methodology states that v1 test results were seen before v0.1.1 was built, so LoCoMo and LME-R are not pristine for v0.1.1, and adds HotpotQA-50 as a never-seen, out-of-domain held-out check. Raw results are committed. |
| M13 | Governance blind spots | Interleaved channels; id and score comparison at unit and auto granularity; post-filter ranks spans with a shared index and removes unreadable units inside each item (no longer drops whole spans or inherits the admin's budget); leak detection works per line and catches lines shorter than 8 words. The stale v1 figure is gone from the spec. |
| minor | Float paths | The engine always takes the masked BM25 path (`exact=True`), so scores are bit-identical under removal. v0.1.2's LoCoMo test results are identical to v0.1.1 query by query. |
| minor | Query-expansion channel | Removed (it was off by default and violated M33). |
| minor | Thread granularity on unthreaded memories; `neighbours` | An unthreaded memory is its own thread; `neighbours` uses recall's admissibility (validity, expiry, status, labels) and returns `not_found` for retracted targets. |
| minor | `dry_run` and re-ingest oracles | `forget` lists only ids the caller may read (cascades still apply); re-ingest blocks apply only to writers who could read the retracted record; upsert cannot resurrect a retracted record without `allow_reingest`. |
| minor | Engine budget | `Engine.recall` packs greedily within the budget. |
| minor | API robustness | Declared limits enforced; non-numeric parameters rejected; HTTP `/jobs` binds the principal; `allow_reingest` accepted on `remember`. |
| minor | Persistence | Reference servers persist full state (records, keys, blocks, feeds, audit) for every tenant. |
| minor | Redaction residue | Claim fields, source spans and hashes and embedding-cache entries are cleared. |
| minor | Feature count | Capabilities and Appendix B list only active features; `rr_sub` and `rr_qexp` are no longer declared. |
| minor | Encoder hash | The rebuild script is seeded, never overwrites the shipped encoder, and reports agreement with it; the methodology explains why the shipped file is not byte-reproducible. |
| minor | Latency order | Each system starts with a cold encoder cache. |
| minor | LME-R quirks | Left in place and disclosed; `bench/lme_quirks.py` lists the 45 affected test questions and the report shows results without them. |
| minor | Spec wording | M01, M63, M64 retired (ids never reused); MUSTS.md wording fixed; M28 now defines `cascade: false`; determinism includes the resolved `as_of`; retention vs M20 clarified; quota maps restrict and cap; dedup window declared; `allow_reingest` accepted. |
| minor | `keys.json` handle | Closed. |
