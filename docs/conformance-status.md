# Implementation conformance

**Target:** Specification v1.6.16 (five-verb surface), Protocol v0.5.0,
Schemas v0.5.0. The v0.5.0 conformance surface is **the five core memory verbs
(OBSERVE, RECALL, REFLECT, REVISE, FORGET) plus FORGET.SCOPE.**

Smartware is beta software. The repository provides executable evidence for
the implementation boundaries below; it does not claim exhaustive
Specification v1.6.16 conformance.

## Protocol v0.5.0 migration note (NOT a break)

- v0.4.x servers are backward-compatible on the five verbs: every v0.4.2
  semantic and wire invariant of those verbs is preserved unchanged in v0.5.0.
  Existing five-verb client code runs unchanged against a v0.5.0 server.
- v0.4.x servers are **non-conformant on scope-erasure**: they have no
  FORGET.SCOPE and cannot meet its atomicity, same-commit grant revocation,
  exact-count audit, or lane-exhaustive purge requirements.
- The v0.4.2 contract and schema set are retained and remain valid for
  five-verb conformance claims. This is a migration note, deliberately **not**
  a break — the anti-pattern avoided is mem0's v2→v3 churn, where a protocol
  revision broke consumers with no migration path.
- The v0.5.0 contract and the v0.5.0 schema set ship together; a mismatch
  between them blocks conformance until corrected. See the
  [v0.5.0 contract change history](protocol/smartware-protocol-v0.5.0.md).

## Release identity

- The package version is **0.8.1**; `package.json` and `src/version.ts` are kept
  in sync and are the single source of truth for the version string.
- 0.8.1 carries the v0.5.0 protocol surface, and adds queue **timing** to the
  core's `compileQueueStats()` (`oldest_pending_at`, `last_completed_at`,
  `last_failed_at`, read from the queue's existing `timingStats()`). An
  embedding host can age the queue instead of inferring a stalled drain from
  counts that do not move, which is how a queue stalled for two days with
  nothing to alert on. The change is additive; existing callers are unaffected.
- The earlier published `0.6.3` on the npm registry **predates the v0.5.0
  surface** and does not contain `schemas/v0.5.0`; integrators following the
  v0.5.0 documentation must not pin `0.6.3`.

## Verified baseline

Verified 2026-09-15 on Node v26.5.1 for the **L1 record writer's `supersedes` and the forgotten
branch that required it** (`wip/smarty/l1-forgotten-supersedes`, kanban `t_3ba3ee39`, ADR-0014 — one
`src/` line plus a published-schema relaxation, no wire change): **541 tests across 74 files**, 31
schema files, `claim.schema.json` the only schema file changed (`SHA256SUMS` regenerated;
`supersedes` removed from one `required` array, every other property and branch unchanged). The delta
over the entry below is 9 tests — 8 in the new `test/layer1/claim-record-conformance.test.ts` (every
state `insertClaim` can write, read as raw JSONL bytes, asserting the complete Ajv error list is empty:
v1/v2 active, v2 demoted, v1/v2 forgotten, the born-forgotten legacy shape through `backfillTombstones`,
and the replay tombstone-retirement path) plus one schema-level fixture group in
`test/schemas-v0.5.0.test.ts` for the forgotten branch. The writer now emits `supersedes: version - 1`
whenever `version > 1` (it had derived the number and dropped it — measured invalid on the v2 active and
v2 demoted shapes too, not only the forgotten one the card named), and a version-1 forgotten record no
longer has to name a prior version that does not exist. LC-04 reconstruction from a backfilled
tombstone was measured: `valid=false` before, `valid=true` after. Three same-class divergences were
measured while deciding and stay open (ADR-0014 → *Explicitly not decided here*): `replay.ts`'s
correction path writes `state: 'active'` for a retracted claim (`t_ef77c695`), replay mints ids the
published `ClaimId`/`TombstoneId` patterns reject (`t_0b079fbf`), and `insertClaim` drops a
caller-supplied demotion pointer on the forgotten path (`t_8098b097`). No other suite changed.

Verified 2026-09-15 on Node v26.5.1 for the **claim record's extraction materialization block**
(`wip/tech-head/claim-record-semantic`, kanban `t_229601e4`, ADR-0011 — schema/contract accuracy, not a
protocol change): **532 tests across 73 files**, 31 schema files, `claim.schema.json` the only schema
file changed (`SHA256SUMS` regenerated; the required set and every other property are unchanged). The
delta over the entry below is 2 tests — a fixture group in `test/schemas-v0.5.0.test.ts` (the block is
optional, closed, and required-field-complete when present) and one test in
`test/semantic-materialization.test.ts` (the records `reflect.auto` appends validate against the
published schema) — plus the pinned `test/layer1/legacy-operation-id.test.ts`, which now asserts the
**whole** Ajv error list of the record `insertClaim` appends is empty instead of pinning the known
divergence (same test count). `claim.schema.json` now enumerates the optional `semantic` block, so an
active record the reference implementation writes is accepted by the contract it publishes; the block
is optional, not a wire field, and no conformance claim depends on it. Two same-class divergences were
measured while deciding this and stay open, carded with their evidence (ADR-0011 → *Known
divergences*): `insertClaim`'s forgotten path omits `supersedes` that the schema's forgotten branch
requires (`t_3ba3ee39`), and pod-profile records carry `pod/<pod>/<lane>` scopes and
`substrate:<ULID>` actor ids the v0.5.0 `Scope`/`ActorId` patterns reject (`t_9a700aed`). No other
suite changed.

Verified 2026-09-15 on Node v26.5.1 for the **L1 record writer's legacy OperationId**
(`wip/smarty/l1-legacy-op-id`, kanban `t_85817375`): **530 tests across 73 files**, 31 schema files, no
schema file changed (the `OperationId` pattern is unchanged). The delta over the baseline below is 6
tests in one new file (`test/layer1/legacy-operation-id.test.ts`), which drives `insertClaim` with no
caller operation id, validates the written L1 record against `schemas/v0.5.0/claim.schema.json`, and
pins the marker to the one `src/layer1/tombstone-backfill.ts` stamps on a pre-A3 row's tombstone (the
writer fixed in the entry below). The placeholder was `op_LEGACY00000000000000000000`, whose `L` the
published Crockford-base32 pattern rejects; it is now `op_000000000000000000000000A3`. One Ajv error
remains on such a record — the internal `semantic` block the published claim schema does not enumerate
— measured, unchanged by this fix, and carded separately; **that second half was decided in the entry
above** (the schema enumerates the block, and the pinned test now asserts an empty error list). No
other suite changed.

Verified 2026-09-15 on Node v26.5.1 for the **tombstone backfill writer**
(`wip/neo/tombstone-backfill-writer`, kanban `t_9e124fe6`): **524 tests across 72 files**, 31 schema
files. The delta over the baseline below is 3 tests in one new file
(`test/layer1/tombstone-backfill.test.ts`), which drives the only in-tree writer of
`wiki/tombstones/*.md` over legacy-shaped rows (`status: 'retracted'`, no `operation_id`/`actor_id`)
and validates the written frontmatter against
`schemas/v0.5.0/tombstone-frontmatter.schema.json`; the writer now emits the snapshot envelope fields
the schema requires (`claim_id`, `state`, `epistemic_owner`, `fingerprint`), buckets `confidence`
through `confidenceToBucket`, stamps schema-valid `operation_id`/`actor_id` placeholders for a pre-A3
row, and carries a mechanical demotion into the snapshot; no other suite changed.

Verified 2026-09-15 on Node v26.5.1 for the tombstone schema's coverage of the claim record
envelope (`wip/smarty/tombstone-snapshot-envelope`, kanban `t_2bba749f` — schema/contract accuracy,
not a protocol change): **521 tests across 71 files**, 31 schema files. The delta over the baseline
below is 2 tests, both in `test/schemas-v0.5.0.test.ts` (a tombstone-frontmatter fixture group for
the demotion/release fields in the `snapshot` block, and a guard that the block mirrors
`claim.schema.json` field-for-field); no other suite changed.

Verified 2026-09-15 on Node v26.5.1 for the mechanical demotion's **release** vocabulary —
`REVISE` with `repick_survivor` (`wip/neo/repick-survivor`, kanban `t_1db21462`, ADR-0003 →
*Releasing a demotion*): **519 tests across 71 files**, 31 schema files. The delta over the
2026-09-14 baseline below is 12 tests — `test/protocol/repick-survivor.test.ts` (11: swap,
stability, rescue, multi-copy demotion, the two rejections, the two crash legs, the torn-set
fail-closed case, plus rebuild-equivalence assertions) and one schema fixture group in
`test/schemas-v0.5.0.test.ts` (the `repick_survivor` form and the demotion record fields) — and no
other suite changed.

Verified 2026-09-14 on Node v26.5.1 for the duplicate-claim-identity change
(`fix/duplicate-claim-recipe`), the demotion-durability fix on top of it
(`wip/neo/demotion-durability`), and the carry-forward of a demotion through the
flows that hand-build a version record (`wip/smarty/demotion-handbuilt-records`),
superseding the 2026-09-10 0.7.0 release-cut baseline (which recorded **446 tests
across 64 files**): **507 tests across 70 files**, 31 schema files. The delta over
the parent commit is 6 tests — `test/layer1/demotion-durability.test.ts` (4:
`REVISE`, `FORGET` → `REVIVE`, endorsement, consolidation), plus one each in
`test/protocol/forget-scope.test.ts` (offboarding) and
`test/protocol/retention-expire.test.ts` (expiry) — and the eight
demotion-durability tests of the parent change are all still green; no other suite
changed. The retrieval-kernel contract (9/9 scenarios) and the activation contract
(fails closed) were last measured on the parent revision of this baseline; this
change touches Layer 1 version records only, not the retrieval kernel or the
extractor. (CI re-runs the same gate via `npm ci` from `package-lock.json` on Node
22 and 24, so the two runtime lines are verified by CI rather than by this local
run.)

- The TypeScript package builds cleanly (`tsc`; npm run build, no errors).
- All 16 v0.5.0 schemas compile and match the committed checksum manifest
  (`npm run verify:schemas`: 16 v0.5.0 files OK); the retained v0.4.2 set
  (15 files) still verifies.
- The standalone suite passes **524 tests across 72 files** with no skips.
- The fact-identity suite (`test/layer1/fact-identity.test.ts`, 22 tests) pins the
  claim write-path identity contract documented in the integration guide §1e:
  `ClaimStore.findActiveFactMatches` returns **every** active claim asserting a
  fact (survivor order — lexicographically smallest claim id, i.e. earliest-minted
  ULID first) and `resolveFactMatches` folds duplicates into that survivor by
  unioning the losers' `supporting_evidence`, demoting them (`status:
  'superseded'`, `superseded_by`, timestamped, never deleted), recomputing
  confidence with the library formula, and reporting `ambiguous_matches` /
  `superseded_claims`. Both insertion orders of a duplicate pair yield the same
  survivor; a demoted duplicate is no longer matched, **and the demotion is
  durable**: it is recorded in the demoted claim's own canonical version records
  (`superseded_by`, `superseded_at`) and every materialisation derives the row
  from them, so a compile-path row sync and a full canonical replay both keep the
  duplicate out of the recall-eligible set (previously projection-only — measured
  in `t_15bb0cd0` `evidence/23-demotion-durability.txt`; pinned by
  `test/layer1/demotion-durability.test.ts`, which now also covers the flows that
  hand-build a version record). The same 6 fixtures as the
  host-side pilot reference implementation are reproduced 1:1, so the pilot's
  deterministic suite remains a valid cross-check.
- The same suite pins the **known divergence between that write-path identity and
  the pre-existing structured claim fingerprint** (`computeStructuredClaimFingerprint`,
  `reflect.auto` idempotency, spec §193/§238) in both measured directions: two
  active rows differing only in `claim_type` are one fact to the write path and two
  to the fingerprint, while two rows differing only in text case are the reverse.
  The divergence is recorded, unreconciled, with a reversal trigger in
  [ADR-0003](adr/0003-claim-fact-identity.md) → *Known divergence*; reconciliation
  needs owner sign-off. Re-closing it silently fails the suite (measured: dropping
  `claim_type` from the fingerprint fails 1 test, case-folding a text value in
  `normaliseValue` fails 3, making fact identity depend on `claim_type` fails 1).
- `npm run verify:saas` (public-API smoke) exercises the same contract end to end
  against the packaged surface: a store seeded with two active claims for one fact
  answers **2** recall results for that fact and **1** after
  `resolveFactMatches`, with the duplicate superseded, its evidence unioned
  (2→3 refs), survivor confidence formula-consistent, and the two rows for the one
  fact shown to carry two different `canonicalKey`s (the key includes
  `validity_from`, so it is not the fact identity).
- The G3 provenance-rendering contract suite (`test/render/provenance-rendering.test.ts`,
  33 tests) asserts the spec §10d wording table verbatim — flagship
  "Learned from Maya, May 12; corrected by owner May 13.", badge set
  (New / Not verified / May be stale / Conflict / Unconfirmed), the
  default-on predicate truth table (unverified/FAILED freshness, failed
  compile state, stale/contested/low-confidence, consequential
  types/tags, 14d recency + 30d correction windows, custom-window
  honoring), the client-facing denial matrix (only client-owned
  "From your messages" citation; no staff identity, badges, or
  why-panel), and UTC date determinism — via the reference renderer
  `src/render/provenance.ts` (importable as `smartware/render`).
- The G2 v0.5.0 conformance suite (`test/conformance/v050-rebuild-forget-provenance.test.ts`,
  14 tests) asserts, against wipe-clean REBUILT indexes: (a) byte-level
  rebuild-equivalence — canonical JSONL (evidence/claims/operations) is
  bit-identical after wipe-and-rebuild and every canonical line round-trips
  its own bytes; (b) FORGET.SCOPE `erasure` yields zero results in every lane
  (claim FTS, page FTS, raw-observation window, recall, vector store, graph)
  and stale-FTS "ghost" rows are eliminated by regeneration, not merely
  hidden; (c) erasure vs offboarding semantics — exact pre-mutation counts,
  one ops entry, same-commit grant revocation, non-reusable `client:<id>#n`
  markers; (d) provenance integrity — every recall hit resolves its source
  observation + ops entry, superseded claims never satisfy recall/get, and
  multi-version history is order-correct, including on rebuilt state.
- The Coffee company-brain e2e suite (`test/conformance/coffee-company-brain.test.ts`,
  3 tests) proves the multi-actor product flow on the real core (spec §10b/§10c/§25):
  one business = one tenant; owner admin; clients as scopes under `workspace`
  with `visibility_default: 'scope'`; staff granted per exact client cluster;
  a staff member builds a client's company brain and the owner recalls it scoped
  to that client with no cross-client leakage; EXACT grant clusters (Gigi → Acme,
  never Bcau/Gate/`*`; owner bypasses grants); and EXPORT.SCOPE is exactly one
  client — `scope_exclusive: true`, zero cross-client ids in the package, per-client
  packages distinct, and idempotent by `operation_id`.
- Tests exercise OBSERVE, RECALL, REFLECT, REVISE, FORGET, REVIVE, ENDORSE,
  FORGET.SCOPE (erasure and offboarding lanes, owner-only enforcement,
  same-commit grant revocation, exact retraction counts, idempotent retry,
  crash recovery), access control, sessions, context delivery, retrieval
  eligibility, reflection receipts, recovery behavior, and the v0.5.0 schema
  surface (widened Scope pattern, `forget.scope` ops entries,
  forget-scope-request payloads).
- The packed package exposes the embedded `SmartwareCore`, the side-effect-free
  MCP adapter (including `smartware_forget_scope` and `smartware_export_scope`),
  the CLI, and both frozen
  schema sets (v0.5.0 current; v0.4.2 retained).
- The stdio MCP transport is exercised end to end.
- The nine-scenario retrieval-kernel contract passes with zero forbidden hits.
- The activation contract fails closed on public development evidence, as
  required.
- `npm audit --omit=dev` reports zero production dependency vulnerabilities.
  This required a lock refresh in the 0.7.0 cut: the pre-0.7.0 lock still
  resolved `fast-uri@3.1.5`, `hono@4.13.0`, and `qs@6.15.3`, each covered by
  published advisories (1 high, 2 moderate). The fix moved exactly those three
  transitive packages to `3.1.7`, `4.13.7`, and `6.16.0` within their parents'
  existing semver ranges — no direct dependency, protocol, or source change.

Host products must separately test their adapters, transports, persistence,
and user-facing authorization against the exact Smartware version they ship.

## Crash-recovery boundary

Operation-ID-backed OBSERVE, REVISE, FORGET, REVIVE, ENDORSE, automatic
REFLECT, and FORGET.SCOPE claim writes persist a content-free expected-artifact
intent before canonical mutation. (A `REVISE` re-pick commits **two** version
records — the release and the demotion(s) — as one exact artifact set: the
records land in one append, and recovery commits only when every named artifact
is present.)

Startup recovery:

- commits only a complete, exact, hash-valid artifact set;
- leaves exact partial client operations resumable;
- safely discards an unmaterialized internal `reflect.auto` intent so
  deterministic reflection can retry;
- removes stale intents after finding their exact commit;
- leaves every mismatch untouched in `requiresManualReview`;
- finalizes a FORGET.SCOPE only when its L0 audit marker exists — the marker is
  written LAST, after every physical mutation and the config save, so a crash
  before it leaves the operation pending (the retry re-runs the idempotent
  purge) and a crash after it proves the purge already happened.

The exact ordering and recovery state table are documented in
[atomicity.md](atomicity.md).

## Remaining limits

- Legacy direct calls without an operation ID are outside the recovery
  guarantee.
- Automatic quarantine is not implemented; ambiguous append-only artifacts
  remain available for manual review.
- Duplicate-claim convergence is **host-triggered**, not automatic: an existing
  store keeps two active claims for one fact until a write touching that fact
  resolves them or the host sweeps the scope (`ClaimStore.findActiveFactMatches`
  + `resolveFactMatches`, integration guide §1e). Identity is
  `(subject, predicate, scope, object value)` — the same fact asserted in two
  different scopes is never merged.
- The demotion is durable **from the version that records it** — a compile-path
  row sync and a canonical replay both reconstruct it from the claim's canonical
  version records — and **every flow that hand-builds a claim's next version
  record carries it forward**: user `REVISE` (whose result reports `superseded_by`,
  because a revise changes metadata and not the asserted fact), the `FORGET`
  tombstone, `REVIVE`'s restore from the snapshot, the endorsement cascade,
  consolidation's input tombstones, scope offboarding and retention expiry. One
  limit remains: a demotion written before this fix was projection-only and is not
  reconstructible from canonical data. **Releasing a demotion is a user-only
  re-pick** — `REVISE` with `repick_survivor: true` on the demoted duplicate
  (protocol v0.5.0, shipped 2026-09-15): one atomic commit releases the duplicate
  and demotes the fact's current active copy with a `superseded_by_origin: 'user'`
  warrant, so exactly one copy stays recall-eligible. It works in swap mode (the
  survivor is active) and rescue mode (the survivor is already forgotten — the
  fact returns from audit-only visibility). `invalidate_relations` still cannot
  release one (the demotion is deliberately not an edge); a bare release remains
  unstable and is rejected as a design. Reasoning and the rejected alternatives:
  [ADR-0003](adr/0003-claim-fact-identity.md) → *Carry-forward across hand-built
  version records* and *Releasing a demotion*.
- **Two identity rules over the claim table are unreconciled.** The write-path
  identity above governs the host write path; the autonomous-creation path
  (`reflect.auto`) is idempotent on the structured claim fingerprint instead
  (`claim_type` included, text lowercased), and `insertClaim` stamps that
  fingerprint on every claim when the store has a `data_dir`. A host running both
  surfaces over one store can therefore end up with a duplicate the write path would
  have merged, or a merge the compile path does not see. Recorded with the measured
  cases and a reversal trigger in
  [ADR-0003](adr/0003-claim-fact-identity.md) → *Known divergence*; reconciling the
  two is protocol identity semantics and needs owner sign-off. One legacy artifact
  picks a side rather than inventing a third rule: a backfilled tombstone snapshot
  (`wiki/tombstones/*.md` for a pre-A3 `status: retracted` row) recomputes the
  **content form** — the snapshot block enumerates neither the structured assertion
  nor `semantic`, so a structured value would not be re-derivable from the artifact
  whose purpose is reconstruction (`t_9e124fe6`, ADR-0003 → *Known divergence*).
  (Deliberately unchanged by `t_229601e4` / ADR-0011: the L1 *record* now enumerates
  the materialization block, the snapshot block still does not — its promise is the
  claim schema's **required** fields, and the block is optional.)
- The suite does not prove concurrent multi-writer serialization or universal
  sudden-power-loss durability.
- REFLECT page output and search databases are rerunnable projections rather
  than one transaction spanning the entire compilation run.
- FORGET.SCOPE `erasure` is unrecoverable by design (physical purge); the
  owner-approved non-PII pointer path exists only for `offboarding`.
- Passing schemas and behavioral invariants is not an exhaustive
  requirement-by-requirement proof of Specification v1.6.16.

## Accurate release claim

The tested beta boundary is:

> Idempotent, crash-consistent local mutation commits that recover after one
> process terminates and the operation is retried, plus reason-aware scope
> erasure/offboarding with lane-exhaustive purge, same-commit grant
> revocation, and exact-count audit on the scope boundary.

Smartware must not be described as providing general ACID filesystem
transactions, automatic repair of ambiguous memory, concurrent multi-writer
safety, a single fact-identity rule across its write and compile paths, or full
Specification v1.6.16 conformance.
