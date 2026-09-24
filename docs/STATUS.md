<!-- GENERATED FILE — DO NOT EDIT BY HAND.
     Regenerate: npm run status   (node scripts/substrate-status.mjs)
     Staleness:  npm run status:check (fails when this file is stale — run in a
     working checkout that has git refs; it is not a CI job for that reason)
     Derived from: git, package.json, docs/spec, docs/protocol, docs/adr,
     docs/journal, docs/substrate.json and (where reachable) the kanban board. -->

# STATUS — current operational state

> This is a *projection*, not a source of truth. Every line is regenerated from
> canonical state; if it disagrees with a canonical source, this file is wrong.
> A fresh agent should be able to read this file alone and know where the project is.

**Branch:** `wt/t_f2b584dc` · **Trunk:** `main` (trunk has moves this tree does not — run `git log main..HEAD` / `git log HEAD..main`)
**Version:** 0.7.0 · **Spec:** smartware-spec-v1.6.16.md · **Protocol:** smartware-protocol-v0.5.0.md · **Schemas:** v0.4.2, v0.5.0

## Declared (human-owned; the only non-derived block)

- **Mission:** One memory for every app and agent: an open, user-owned memory protocol with provenance, bitemporal truth, policy-first retrieval, and deterministic fallback. Reference implementation in TypeScript, local-first, SQLite + JSONL + compiled Markdown.
- **Phase:** 0.7.0 release cut on release/coffee-v050-surface (PR #3, open, pending human merge). Retention + consolidation landed via #4, closing the ADR-0001 / ADR-0002 archive loop. feature/retention-expiry carries 6 further commits (owner sign-off freezing Tier-1 invariants, HQ competitive corpus entry) awaiting their own PR.
- **Owner:** Stevie G (Techno Ventures)
- **Next action (declared):** Merge PR #3 (0.7.0 cut) to main, then land the project-substrate change (#6). Then open a PR for the 6 outstanding commits on feature/retention-expiry.
- Declared: 2026-09-11 · source: `docs/substrate.json`

## Latest material change

- **Journal:** [`2026-09-16-t_804449f1.md`](journal/2026-09-16-t_804449f1.md) — DECIDE (owner): host-registered lanes in the next protocol revision, or hosts migrate onto the Scope vocabulary (ADR-0012 fork) (2026-09-16)
- **Journal entries:** 78 · tasks completed on board `smartware`: 78
- Commit-level history is deliberately NOT duplicated here — see `git log`. This projection tracks operational state, not the commit stream.

## In flight

| Branch | Ahead | Last commit | Subject |
|---|---|---|---|
| `wip/smarty/canonical-schema-boundary` | 67 | 2026-09-16 | docs: rebase onto wip/neo/host-lane-identity @ 9fcfff7 — regenerate STATUS projection, refresh the ADR-0012 -> 0015 labels (t_74faf12d) |
| `fix/l1-replay-correction-state` | 64 | 2026-09-15 | test(layer1): a 30s hook timeout for the correction-record file — opening a real pod exceeds the 10s default on a loaded box (t_ef77c695) |
| `wip/neo/host-lane-identity` | 61 | 2026-09-15 | docs(journal): fix the dead card id in t_9a700aed, record the gate re-run (t_ad84d246) |
| `wip/smarty/l1-forgotten-supersedes` | 60 | 2026-09-15 | docs(adr): refile this lane's ADR 0012 -> 0014 before merge (t_201cdca8) |
| `wip/tech-head/l1-forgotten-carry-superseded` | 60 | 2026-09-15 | docs(adr): refile this lane's ADR 0012 -> 0014 before merge (t_201cdca8) |
| `wip/tech-head/l1-replay-crockford-claim-id` | 59 | 2026-09-15 | docs: regenerate STATUS projection (kanban t_3ba3ee39; board counters moved while closing) |
| `fix/status-projection-adr-bullet` | 58 | 2026-09-15 | fix(status): the ADR reader accepts the template's `- **Status:**` bullet |
| `wip/neo/optype-derivation-guard` | 58 | 2026-09-15 | docs: STATUS projection refresh for t_0e732b96 (status:check current at commit time) |
| `wip/smarty/retention-cross-op-id` | 57 | 2026-09-15 | fix(protocol): an OperationId spent on another op conflicts with the retention sweep |
| `wip/tech-head/claim-record-semantic` | 57 | 2026-09-15 | docs: regenerate STATUS projection (board event counter moved while closing t_229601e4) |
| `wip/smarty/retention-payload-identity` | 56 | 2026-09-15 | fix(protocol): the retention sweep matches the OperationId payload, not just the id |
| `wip/smarty/retention-op-id` | 55 | 2026-09-15 | fix(protocol): the retention sweep mints a contract-valid OperationId |
| `wip/smarty/l1-legacy-op-id` | 54 | 2026-09-15 | fix(layer1): the L1 record writer stamps a Crockford-valid legacy OperationId |
| `wt/t_9740ae98` | 54 | 2026-09-15 | docs: regenerate STATUS projection (board counters and branch tips moved while this lane ran; no content lines differ) |
| `wip/neo/ops-enum-symmetry` | 53 | 2026-09-15 | docs: state delta for t_0e3989eb (gate evidence, conformance counts, STATUS projection) |
| `wip/neo/tombstone-backfill-writer` | 53 | 2026-09-15 | docs: regenerate STATUS projection (board counters moved while closing t_9e124fe6) |
| `wt/t_ba868906` | 52 | 2026-09-15 | docs: record the exact status:check outcome (board-counter drift) for the adapter cut |
| `wip/neo/legal-hold-findings` | 51 | 2026-09-15 | docs: state delta for t_7a64ded2 (gate evidence, conformance counts, STATUS projection) |
| `wip/smarty/tombstone-snapshot-envelope` | 51 | 2026-09-15 | fix(schemas): tombstone snapshot block enumerates the claim record envelope |
| `wip/neo/repick-survivor` | 50 | 2026-09-15 | docs: regenerate STATUS (board counters; drift is projection-only) |
| `wip/neo/storage-fencing` | 50 | 2026-09-15 | docs: regenerate STATUS projection (board counters moved while closing t_695656d8) |
| `wip/neo/legal-hold-marker` | 49 | 2026-09-15 | docs: state delta for t_463c1ff9 (legal-hold marker) + journal/STATUS projections |
| `wip/neo/f1b-demoted-fingerprint` | 48 | 2026-09-15 | fix: a fingerprint hit on a demoted duplicate routes corroboration to the fact's survivor (ADR-0005 F1b) |
| `wip/tech-head/retention-sweep-audit` | 48 | 2026-09-15 | docs: regenerate STATUS projection (board counters and other branch tips moved) |
| `falsifier/t_e8bd6747` | 47 | 2026-09-15 | test-tree: compose F1 + F2 onto one tree for the ADR-0005 steady-state falsifier |
| `wt/t_c5c999ba` | 47 | 2026-09-14 | docs: decide legal hold — v1 composition stands, marker not built (ADR-0008) |
| `feat/deepseek-provider` | 46 | 2026-09-15 | feat(extraction): add deepseek provider (OpenAI-compatible, api.deepseek.com) |
| `wip/neo/fencing-token` | 46 | 2026-09-15 | docs: fencing evidence — before/after gauntlet pair, measured cost, guard-list fix |
| `wip/smarty/demotion-handbuilt-records` | 46 | 2026-09-14 | docs: regenerate STATUS (board counters after the t_742e31f9 evidence comment) |
| `wt/t_65569b9e` | 46 | 2026-09-15 | health: host-facing health/metrics contract, lane-explicit counts, Coffee-trial SLOs (ADR-0008) |
| `wt/t_ad51d0e2` | 46 | 2026-09-14 | docs: regenerate STATUS projection (lifecycle composition lane) |
| `wip/neo/f1-reflect-auto-identity` | 44 | 2026-09-15 | docs(journal): record the pilot cross-check and gate evidence for t_2996a3ab |
| `wip/neo/p0-sources-ingestion` | 44 | 2026-09-14 | docs: regenerate journal + STATUS projections after the resilience gauntlet |
| `wip/neo/demotion-durability` | 43 | 2026-09-14 | docs(journal): state delta for t_01ef0ede (demotion durability) + STATUS regen |
| `docs/protocol-identity-adr` | 41 | 2026-09-15 | docs: accept ADR-0005 — protocol claim identity decided; F1 released, F2 fixed on branches |
| `fix/duplicate-claim-recipe` | 39 | 2026-09-14 | docs: state the measured numbers, not the remembered ones |
| `wip/neo/p0-contradiction-temporal` | 38 | 2026-09-14 | contradiction: deterministic admission, contested recall, bi-temporal closure (P0-2/P0-4) |
| `docs/adr-numbering-registry` | 37 | 2026-09-15 | docs(adr): number registry on main + the claiming/renumbering rule (t_201cdca8) |
| `docs/saas-retention-ops-entry` | 37 | 2026-09-15 | docs: the retention sweep's ops entry is caller-supplied only (measured) |
| `wip/neo/adr-numbering-rule` | 37 | 2026-09-15 | docs(adr): number registry on main + the claiming/renumbering rule (t_201cdca8) |
| `wip/neo/p0-isolation-conformance` | 37 | 2026-09-14 | isolation: actor-bound raw window + activity lanes, explicit denials (P0-5/P0-7) |
| `wip/tech-head/gate-under-load-policy` | 37 | 2026-09-15 | docs(adr): number registry on main + the claiming/renumbering rule (t_201cdca8) |
| `feat/corroboration-reachable` | 33 | 2026-09-12 | feat: make corroboration reachable, and demonstrate it in the reference example |
| `fix/concurrent-schema-migration` | 32 | 2026-09-11 | fix: make column migrations idempotent when two processes open one brain |
| `fix/public-surface-and-docs` | 31 | 2026-09-11 | fix: make the public surface sufficient for host-side claim persistence |
| `docs/node-requirement` | 29 | 2026-09-11 | docs: correct the Node requirement in the integration guide (>=20 -> >=22) |
| `ci/tag-publish` | 25 | 2026-09-11 | ci: publish on version tags, with provenance and the gate in front of it |
| `docs/v070-handoff-accuracy` | 25 | 2026-09-11 | docs: point the SaaS integration guide at the release that carries the surface |
| `docs/project-substrate` | 15 | 2026-09-11 | docs: regenerate STATUS projection after the release-line rebase |
| `feature/retention-expiry` | 14 | 2026-09-11 | adr: owner sign-off — freeze Tier-1 invariants (retention + consolidation) |
| `release/coffee-v050-surface` | 8 | 2026-09-10 | release: 0.7.0 — v0.5.0 protocol surface cut on the upstream Node 22/24 gate |
| `agent/hermes-onboarding` | 3 | 2026-08-25 | [verified] docs: name complete retrieval CI gate |

Unmerged work — read the branch before assuming this tree is current.

## Queued / next up

- `t_e19e8f19` [todo] REVIEW (independent, round 3 text check): ADR-0012 retired-marker rule in the re-grant procedure, §6 C7, and the qualified handoff (wt/t_f2b584dc @ ce9005d, PR #16) (created 2026-09-16, assignee smarty-pants)

## Blockers and stale work

- `t_66f1dd7d` GATE review: independent Coffee company-brain release verdict
- `t_dc609143` VERIFY (independent): retention sweep OperationId writer — branch wip/smarty/retention-op-id @ 37c914c (base 0a68482)

## Decisions

| ADR | Title | Status | Date |
|---|---|---|---|
| [0001](adr/0001-retention-expiry-archival.md) | ADR-0001 — Retention, Expiry & Archival | Approved (owner sign-off 2026-09-10 — Tier-1 invariants §2.2 frozen; `staleness` block deprecated §2.4) | 2026-09-10 |
| [0002](adr/0002-consolidation.md) | ADR-0002 — Consolidation of claim clusters | Approved (owner sign-off 2026-09-10 — Tier-1 invariant §2.2 frozen) | 2026-09-10 |
| [0004](adr/0004-contradiction-and-bi-temporal-lifecycle.md) | ADR-0004 — Contradiction and bi-temporal lifecycle | (no status line) |  |
| [0005](adr/0005-sources-ingestion-and-federation.md) | ADR-0005 — Sources, connector ingestion and federated reads | (no status line) |  |
| [0006](adr/0006-export-restore-return-path.md) | ADR-0006 — An export must have a return path: RESTORE.SCOPE | (no status line) |  |
| [0007](adr/0007-fencing-token-at-the-mutation-boundary.md) | ADR-0007 — A fencing token validated at the brain mutation boundary | (no status line) |  |
| [0008](adr/0008-host-facing-health-contract.md) | ADR-0008 — A host-facing health contract with a three-state SLO verdict | (no status line) |  |
| [0010](adr/0010-coffee-reference-adapter.md) | ADR-0010 — Coffee reference adapter: public-surface ports, settle-before-write, and a deterministic local proof | (no status line) |  |
| [0011](adr/0011-coffee-company-brain-acceptance-gate.md) | ADR-0011 — Coffee company-brain acceptance gate: the packaged artifact, a six-business fixture, and the surface it forced | (no status line) |  |
| [0012](adr/0012-grant-granularity-at-the-forget-scope-boundary.md) | ADR-0012 — The revocation boundary is the grant row: one row per (actor, client), and the re-grant requirement | (no status line) |  |

## Verification state

- **Acceptance criteria + exact commands:** [`VERIFY.md`](../VERIFY.md) — the definition of done for this repo.
- **CI gate:** `.github/workflows/ci.yml` (Node 22 + 24 matrix; `npm ci`, prod-vulnerability rejection, build, tests).
- **Adjacent status projections:** [`conformance-status.md`](conformance-status.md) · [`dream-status.md`](dream-status.md) · [`security-audit.md`](security-audit.md)
- **Last recorded gate evidence:** see the newest journal entry (each entry carries the commands run and their output).

## Health

- ✅ declared block fresh (5d old)
- ✅ kanban board readable (97 tasks, 8247 events)
- ✅ every completed task has a journal entry
- ⚠️ 2 blocked task(s)

## Canonical index

| Question | Source |
|---|---|
| What is this project for? | [`README.md`](../README.md) |
| What must the code do (normative)? | [`docs/spec/smartware-spec-v1.6.16.md`](spec/smartware-spec-v1.6.16.md) |
| What is the wire contract? | [`docs/protocol/smartware-protocol-v0.5.0.md`](protocol/smartware-protocol-v0.5.0.md) |
| Why is it built this way? | [`docs/adr/`](adr/) |
| What work happened? | [`docs/journal/`](journal/) |
| How do I know a change is acceptable? | [`VERIFY.md`](../VERIFY.md) |
| What is queued or blocked? | kanban board `smartware` (operational record, outside this repo) |
| How do I operate in this repo? | [`AGENTS.md`](../AGENTS.md) |

_Projection generated by `scripts/substrate-status.mjs` from the canonical sources listed under "Canonical index"._
