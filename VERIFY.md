# VERIFY.md — acceptance criteria and the definition of completion

> **Nothing is done because it was attempted, or because the agent that did the
> work says it succeeded.** A change is accepted when the gate below passes and
> its output is recorded in `docs/journal/` (or attached to the kanban task).

## The gate

Run from a clean tree. Every line must pass; quote the real output, not a summary.

| # | Command | What it proves | Fails when |
|---|---|---|---|
| 1 | `npm ci` | Locked dependency graph resolves | `package.json` / `package-lock.json` disagree |
| 2 | `npm audit --omit=dev --audit-level=low` | No production advisories | Any prod vulnerability at `low`+ |
| 3 | `npm run build` (`tsc`) | Types are sound across the tree | Type errors |
| 4 | `npm test` (`vitest run`) | Behavioural suite passes | Any test failure |
| 5 | `npm run verify:schemas` | Schema checksums match the published contracts | A schema was edited without regenerating its checksum |
| 6 | `npm run verify:saas` | SaaS integration smoke (build + end-to-end integration path) | Integration surface broke |
| 7 | `npm run status:check` | `docs/STATUS.md` is not a stale projection | Substrate state changed without regenerating the status |

Rows 1–5 run in CI (`.github/workflows/ci.yml`) on the **Node 22 and 24 matrix** for
every pull request and every push to `main` — CI is the authoritative acceptance
gate for this repository, and a local pass that CI rejects is not a pass.

Row 7 is an **agent/operator gate, not a CI job**: it compares `docs/STATUS.md`
against a live regeneration, which needs local git refs and a readable kanban
board. Run it before you close a session and whenever you change anything
`docs/STATUS.md` derives from (`npm run status` first, then `status:check`).
Environmental facts (the working tree's cleanliness at generation time) are
ignored by the comparison; everything else must match.

### Task-specific gates (run when the change touches them)

| Area | Command | Contract |
|---|---|---|
| Coffee reference adapter | `npm run verify:coffee-adapter` | Drop-in contract in `docs/integration/coffee-adapter.md` (53 deterministic checks: routing, retries, degraded reads, fencing, isolation, attribution) |
| Coffee company-brain acceptance | `npm run verify:coffee-gate` | ADR-0011 + the contract doc: packs and installs the artifact into a scratch app, then 79 checks on six businesses (lifecycle, corrections, erasure/retention, export/restore, failover + degraded reads, guard/fence drills, restart during load, ~180 unauthorized fuzz requests, soak with p50/p95 + resource accounting). **A partial pass is a failed release gate** |
| Retrieval kernel | `npm run benchmark:retrieval-kernel` | `benchmarks/retrieval/kernel-contract-v1.json` |
| Retrieval arena | `npm run benchmark:retrieval-arena` | `benchmarks/retrieval/arena-contract-v1.json` |
| Retrieval activation | `npm run benchmark:retrieval-activation-contract` | Fails **closed** unless held-out evidence clears the gates (`--expect-hold`) |

Benchmark results are evidence, not decoration: a change that moves retrieval
quality or latency must record the previous and new numbers, the machine, and the
run count. "It feels faster" is not evidence.

## Definition of completion

A change is complete when **all** of these are true:

1. The full gate above passes (or the specific failure is named, with its output, and accepted by the operator).
2. The change is attributable — a commit, on a branch, with a subject that says what it changes.
3. The state delta is recorded: `docs/journal/` entry or kanban completion summary containing intent → evidence → resulting state → unresolved implications.
4. Any normative text the change affects (`docs/spec/`, `docs/protocol/`, `schemas/`) is updated **in the same change**, or the divergence is written down as an open question.
5. `npm run status` has been run if the change alters anything `docs/STATUS.md` derives from.
6. Any consequential decision made along the way exists as an ADR, not as a comment.

## Verification independence

- The producer of a change must not be its only verifier when the change touches a
  **released protocol surface**, **acceptance criteria**, or **verification logic
  itself**. Those require a second agent or a human who did not write the change.
- A verifier may not mutate the artifact it verifies. Its output is evidence
  attached to the state transition.
- Self-reported results (agent summaries, generated status text, "tests pass") are
  leads, not proof. Re-run the command or read the artifact.

## Recording evidence

Evidence goes in the session's journal entry in this shape:

```
Gate:   npm run build && npm test        → tsc clean; 443/443 vitest passed
Run:    2026-08-30, 2 vCPU, Node 22.18, n=4
Notes:  <what the numbers mean, what did NOT pass, what remains unproven>
```

If a gate was **not** run, say so explicitly. An unstated unrun gate is the most
common way a multi-agent project accumulates fiction.

## Last recorded full-gate result

| Date | Result | Source |
|---|---|---|
| 2026-08-30 | `tsc` clean · **443/443** vitest passed · full-pipeline compile budget 9,493.5 ms @50k claims (n=4 clean runs, 9,445–9,682 ms) | kanban task `t_b5392e58`, recorded in spec §11.2c |
| 2026-09-14 | `tsc` clean · **515/515** vitest across 71 files · `verify:schemas` 31 OK · `verify:saas` SMOKE_OUTCOME=pass (sources, ingestion replay/dedup, sync status, federated grants) · `status:check` current | kanban task `t_868d9680` (sources/ingestion/federation), evidence in its journal entry |
| 2026-09-14 | `tsc` clean · **520/520** vitest across 72 files · `verify:schemas` 31 OK · `verify:saas` SMOKE_OUTCOME=pass · resilience gauntlet **16/16 drills pass** on the throwaway Coffee-shaped deployment (SIGKILL→TTL takeover 11.8 s ≤ TTL+poll; **0 acknowledged lost writes**; **0 dual-owner samples**; lease-loss guard 9 ms; Redis-outage recovery 2.8 s; index wipe→rebuild 399 ms; backup/restore and export→restore equivalence) | kanban task `t_00a9df88` (resilience gauntlet + ADR-0006 restore), evidence `/opt/data/workspaces/brain-pilot-evidence/gauntlet-postfix2-20260914T165142Z/` |
| 2026-09-15 | `tsc` clean · **526/526** vitest across 73 files · `verify:schemas` 31 OK · `verify:saas` SMOKE_OUTCOME=pass · `status:check` current · fencing at the brain mutation boundary (ADR-0007): 6 new tests fail 6/6 before the implementation and pass 6/6 after; before/after gauntlet pair on the deterministic post-guard stall (unfenced `9cbe1fb` — the stalled write **commits** after the handoff; fenced `c65b302` — refused `fencing_token_stale` before any canonical artifact, zero acknowledged writes after the handoff, 0 dual-writer samples); same-session alternated latency A/B (5 pairs) shows no regression distinguishable from run-to-run variance | kanban task `t_9ee8bd54` (ADR-0007), evidence `/opt/data/workspaces/brain-pilot-evidence/gauntlet-fencing-before-20260915T083255Z/`, `.../gauntlet-fencing-after-20260915T083414Z/`, `.../latency-ab-20260915T083526Z/`, `.../fencing-tdd-20260915T083124Z/` |
| 2026-09-15 | `tsc` clean · **550/550** vitest across 77 files · `verify:schemas` 31 OK · `verify:saas` SMOKE_OUTCOME=pass · `status:check` current · new host-facing health contract + Coffee-trial SLOs (`health`, MCP `smartware_health`), `STATUS.layer3.indexed` replaced by four named lanes; TDD red-first per feature group (23 new observability tests + MCP transport assertion) | kanban task `t_65569b9e` (ADR-0008; field docs in `docs/integration/observability.md`), branch `wt/t_65569b9e` @ `847fe10` |
| 2026-09-15 | `tsc` clean · **550/550** vitest across 77 files · `verify:schemas` 31 OK · `verify:saas` SMOKE_OUTCOME=pass · `status` regenerated (the `status:check` comparison drifts by exactly one live board-event-counter line while other lanes emit board events; no other line differs) · **`verify:coffee-adapter` 53/53 checks pass** (drop-in Coffee adapter: standby refusals write nothing, retry semantics, corroboration/contested facts, degraded reads that never invent provenance, guard + brain-fence refusal of a stalled stale writer, cross-business key isolation, scope-exclusive export, health without tenant content) · `npm audit --omit=dev` 0 vulnerabilities | kanban task `t_ba868906` (ADR-0010; contract in `docs/integration/coffee-adapter.md`), branch `wt/t_ba868906`, smoke alone 12.2 s on 2 vCPU / Node v26.5.1 |
| 2026-09-15 | `tsc` clean · **551/551** vitest across 77 files · `verify:schemas` 31 OK · `verify:saas` SMOKE_OUTCOME=pass · `verify:coffee-adapter` 53/53 · **`verify:coffee-gate` 79/79 checks pass** against the packed artifact (`smartware-0.7.0.tgz` sha256 `eed3a305…b2827`, installed into a scratch app; six businesses: lifecycle, corrections, erasure/retention, export/restore, failover + degraded reads, guard/fence drills, restart during load, ~180 unauthorized fuzz requests, soak with p50/p95 + resource accounting) · one in-lane defect found by the gate and fixed (FORGET.SCOPE left the raw-observation projection out of sync — health drift + SLO breach after every forget until restart; regression test `S7` red→green) and one adapter contract fix (`client:<id>#0` rejected) · `npm audit --omit=dev` 0 vulnerabilities | kanban task `t_9740ae98` (ADR-0011; evidence `/opt/data/workspaces/brain-pilot-evidence/coffee-gate-20260915T104115Z/` incl. `NOTES.md`) |
| 2026-09-15 | `tsc` clean · **551/551** vitest across 77 files · `verify:schemas` 31 OK · `verify:saas` SMOKE_OUTCOME=pass · `verify:coffee-adapter` 53/53 · `status:check` current · `journal:sync` 64 completed tasks (5 entries written) · **no code, schema or protocol change** — decision + normative §10b grant granularity + adapter contract §7 (`verify:coffee-gate` deliberately not run: docs-only change, and its check `6n` asserts the finding this card decides, inverted by card `t_864a5900`) | kanban task `t_f2b584dc` (ADR-0012 — one grant row per (actor, client) at the FORGET.SCOPE boundary), branch `wt/t_f2b584dc`, evidence `/opt/data/workspaces/brain-pilot-evidence/adr-0012-20260915T110900Z/` |
| 2026-09-15 | Round 2 of the same card (review verdict `changes_requested`): `tsc` clean · `verify:schemas` 31 OK · `verify:saas` SMOKE_OUTCOME=pass · `verify:coffee-adapter` 53/53 · `npm audit --omit=dev` 0 vulnerabilities · `status:check` current immediately after regeneration (it drifts on the live board-event counter alone: 4230→4253 events in ~3 min) · **markdown only — no code, schema or protocol change** · `npm test` red only in load-sensitive stdio hooks while this shared 2-vCPU box ran at load average 25–32 (`test/conformance/mcp_smoke.test.ts` 10s hook timeouts, one fixture hook in `test/regressions.test.ts`) — those files pass **51/51 in isolation** on the same tree; a full-suite run on a quiet box is still owed · new measurement **S4** (24 facts, reproduced twice, public package surface): session resolution reads the FIRST grant row, so a session for a client held in a second row reports `capabilities_granted: []` while `checkGrant` authorizes it | kanban task `t_f2b584dc` round 2 (review card `t_102f3dfa`; the fix is folded into `t_864a5900` as companion change 3 / C6), branch `wt/t_f2b584dc` @ `054e60f`, PR #16, evidence `/opt/data/workspaces/brain-pilot-evidence/adr-0012-20260915T110900Z/` (`measure-session.mjs`, `measure-session.json`, `session-raw*.log`) |

| 2026-09-16 | Round 3 of the same card (review round 2 verdict `changes_requested`): `tsc` clean · **551/551** vitest across 77 files in 83.8 s — the quiet-box full-suite run rounds 1–2 left owed, whose load-induced stdio-hook reds do not reproduce · `verify:schemas` 31 OK · `verify:saas` SMOKE_OUTCOME=pass · `verify:coffee-adapter` 53/53 · `npm audit --omit=dev` 0 vulnerabilities · `status:check` **current immediately after regeneration** (14:06; every later check drifts on exactly one line — the live board-event counter, 8202→8205→8207 events, task count unchanged at 96 — the mechanism round 2 identified and re-measured here) · **markdown only — no code, schema or protocol change** · the review's R-probes re-run first-party at `55fc628` (17/17 and 7/7 facts identical after id/ULID normalization), and spec §10b.2's retired-marker rule is now carried into contract §7's re-grant procedure and ADR-0012 §3 with the new §6 assertion **C7** — `verify:coffee-gate` deliberately not run (docs-only; its check `6n` still asserts the finding this card decides) | kanban task `t_ff85ae12` (round 3 of `t_f2b584dc`; the amendments' implementation, C1–C7, is `t_864a5900`), branch `wt/t_f2b584dc`, evidence `/opt/data/workspaces/brain-pilot-evidence/adr-0012-20260915T110900Z/` (`measure-regrant.mjs`, `regrant-raw.log`, `measure-hazard.mjs`, `hazard-raw.log`, `normalize-report-round3.txt`) |
| 2026-09-16 | Round 4 of the same card (review round 1 on round 3 → `changes_requested`, one blocking text correction **R1** on the §6 **C7** row): `tsc` clean · **551/551** vitest across 77 files in 73.51 s · `verify:schemas` 31 OK · `verify:saas` SMOKE_OUTCOME=pass · `verify:coffee-adapter` 53/53 · `npm audit --omit=dev` 0 vulnerabilities · `status:check` **current immediately after regeneration** · **markdown only — no code, schema or protocol change** · R1 was a citation defect: C7 said "after the erasure check 6c already performs" while asserting on `client:arcadia#1`, but check 6c erases `bob-studio`'s `client:meridian#1` and **no grant row in that tenant references it** (`grants_revoked: []`, measured first-party with the fixture's own provisioning — `measure-c7-geography.mjs`), so the check now hangs off the erasure **check 6n** performs (`ember-group`, `client:arcadia#1`) and names the surface (**the persisted `config.json`'s `scopes`** — the erasure prunes the file and leaves the in-memory tenant object untouched, both measured); the same citation is corrected in contract §7 and on the board (`t_46c4acce` comment, plus notes on `t_864a5900` / `t_e19e8f19`) · `verify:coffee-gate` deliberately not run (docs-only; its check `6n` still asserts the finding this card decides) | kanban task `t_ff85ae12` (round 4; the amendments' implementation, C1–C7, is `t_864a5900`), branch `wt/t_f2b584dc`, evidence `/opt/data/workspaces/brain-pilot-evidence/adr-0012-20260915T110900Z/` (`measure-c7-geography.mjs`, `c7-geography-raw.log`, `round4-summary.md`) |

_This table is a projection of the journal. If it disagrees with
`docs/journal/`, the journal wins — and this table is a bug._
