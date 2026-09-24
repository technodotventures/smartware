# ADR-0012 — The revocation boundary is the grant row: one row per (actor, client), and the re-grant requirement

- **Date:** 2026-09-15
- **Status:** Accepted
- **Deciders:** tech-head (substrate/decision lane) — card `t_f2b584dc`, confirming
  the finding recorded by ADR-0011 §6 (`grant-row-revocation`) from the gate run
  card `t_9740ae98`
- **Supersedes:** — (amends the Coffee tenant binding in spec §10b.1/§10b.2; the
  protocol surface is untouched)

## Context

Two bindings compose into a silent production incident.

- **§10b.3 (Lifecycle in config terms, binding):** a `FORGET.SCOPE` for
  `reason: offboarding|erasure` sets `status: 'revoked'` on *grants referencing
  the client scope*, in the same commit as the mutation
  (`src/protocol/forget_scope.ts` → `grantsReferencingScope()` → every capability
  array that literally lists the scope). A row is revoked **whole**.
- **§10b.1/§10b.2 (Staff grants, binding):** *one* `Grant` per staff actor, with
  exact scope-id lists ("the cluster IS the exact scope-id list") per capability
  (amended by this ADR; see §5).

Composed, the revocation boundary is the **row**, not the client the operation is
about. Multi-client staff are the normal case in the Coffee tenant model — the
§10b.4 worked example has `user:gigi` on `client:acme#1` + `client:bcau#1` — and
client churn (offboarding, erasure, DSR) is routine. So offboarding **one** client
silently removes a colleague's access to a **different** client's memory.

Measured by the ADR-0011 gate (the revocations themselves land in checks 6a
offboarding and 6c erasure; the cost to a **second** client is measured in 6n; staff
provisioned at `scripts/coffee-company-brain-fixture.mjs` line 151): ember-group
staff `user:sam` held `[client:meridian#1, client:arcadia#1]` in one row; erasing
`arcadia` flipped his `meridian` recall from `ok` to `403 insufficient_permission`
until he was re-provisioned. Re-measured for this decision on the public adapter
surface (evidence below, S1): the row is `status:'revoked'` with both scopes still
in its arrays, `grants_revoked: 1`.

Why this is a defect and not a choice: the operation is scoped — `FORGET.SCOPE` for
`client:arcadia#1` — and its receipt counts what it revoked, but nothing tells the
operator that a *different* client's access died. The failure is silent, it lands
on a client that was never the subject of the operation, and the adapter offers no
re-grant call to undo it. The erasure is correct; the blast radius is not.

Three options were on the table (the card's (a)/(b)/(c)). The obvious structural
fix — narrow the revocation in the substrate — was rejected as the wrong change at
the wrong moment, for the reasons in *Alternatives considered*.

## Decision

**1. The substrate keeps row-scoped revocation, exactly as released.** §10b.3 and
protocol v0.5.0 §FORGET.SCOPE are unchanged: grants referencing the scope are
revoked in the same commit, the receipt's `grants_revoked` is the set of revoked
row ids, offboarding stays revocable, erasure stays terminal. The v0.5.0
conformance surface is not touched, and the 79/79 gate result stands.

**2. Provisioning issues one grant row per (staff actor, client scope).** A row
carries that actor's capability cluster for **one** client (`observe`/`query`/`read`
for it, `correct`/`forget` only where the business grants them). The revocation
boundary then equals the intended access boundary, by construction: forgetting
client A can only revoke A's row. Measured (S2): erase `arcadia` → `grants_revoked`
lists exactly the arcadia row, the meridian row stays `active`, and the actor's
meridian recall answers with no re-provisioning.

**3. The re-grant requirement is part of the contract (binding on the host).** A
host that provisions a multi-client row (§10b.2 permits it) gets row-scoped
revocation for that row: offboarding or erasing any one of its clients removes the
actor's access to all of them. Such a host MUST re-provision the actor's remaining
**live** scope ids as part of the same offboarding/erasure step. Re-granting is a
**config provision** — write `config.json` (mode 0600); the adapter reads it on the
next operation. There is no protocol call and no adapter method for it. Measured
(R3): the file is authoritative — an in-memory tenant change re-grants nothing —
and a freshly written row restores the owner/brain path and the standby degraded
path on the next operation, with no restart.

**The procedure carries §10b.2's retired-marker rule, because the obvious edit is
the trap.** Re-provisioning after an **erasure** covers the actor's remaining
**live** scope ids only, in a **fresh** row: a row that still lists the retired
`client:<id>#n` must not be re-activated (mint a fresh row, or edit the revoked
row's capability arrays down to the live ids first). An erasure leaves the revoked
row still listing the retired id, and re-activating it silently re-authorizes the
purged scope — measured (R4/R5): `checkGrant('user:sam','query','client:arcadia#1')
= true`, `handleWrite` → 201, owner recall `ok:true` (`n:0` before that write, then
`n:1`), standby degraded read `ok:true n:1`; control, with the revoked row left
alone, `403 insufficient_permission`. After an **offboarding**, re-activation is by
contrast the sanctioned revival path: protocol v0.5.0 §FORGET.SCOPE keeps grants
"revoked but re-activatable" and the scope entry remains. Both rules are stated in
`docs/integration/coffee-adapter.md` §7 ("Provisioning grants") and the second is
asserted by C7 of §6. The paragraph at the end of this ADR is carried verbatim into
the Coffee release handoff.

**4. Three companion changes are implied by (2) and are not optional.** All three
are measured, and all three are in card `t_864a5900` (created by this decision;
that card's "the substrate is NOT changed" preamble is amended for the third,
which is the one `src/` call site the shape makes wrong — single-row-invariant,
see below):

- **The adapter's degraded-read precheck must union the actor's active rows.** With
  per-client rows, `#precheck` in `examples/coffee-adapter/adapter.mjs` today takes
  the first row with `Array.find()` and denies a scope the actor *is* granted
  (S3, measured: standby `ok` for the first row's client, `403
  insufficient_permission` for the second). Fail-closed, but wrong: the fallback
  path would report a denial for authorized staff reads.
- **`coffeeTenantConfig` must emit the per-client shape.** Today `grantFor()` emits
  one row per actor listing every scope it was handed (S1, `row_count: 1`). Until
  this lands, the shipped template provisions exactly the shape the finding
  describes, and the contract doc says so explicitly.
- **The session surface must union the actor's active rows**
  (`src/session/policy.ts:96` — `resolveTrust()` → `getGrantForActor()` →
  `resolveCapabilities()`, line 151). It derives `capabilities_granted` **and** the
  trust/quarantine caps from the **first** active row. Measured (S4,
  `measure-session.mjs`, on the public package surface: `SmartwareCore.sessionStart`,
  the path behind the `smartware_session_start` tool): with one row per client, a
  session that requests the **second** row's client returns
  `capabilities_granted: []` — while `checkGrant` authorizes the actor on that very
  scope and the adapter's recall on it answers `ok:true n:1` (measured in the same
  run); and the trust answer follows row **order**, not the actor (the same two
  rows, swapped: untrusted-first → `user_facing`, trusted-first → `verified`). Rule
  for the fix: a capability is granted if **any** active row of the actor covers
  **any** requested scope; trust is the **most restrictive** union across the
  actor's active rows (`trusted` only if every active row is trusted, quarantined
  if any row is). This is identical to today's resolution for a single row, which is
  what keeps the released session surface (protocol contract v0.4.1) unchanged in
  behaviour for conformant configs — the change only makes multi-row resolution
  match `checkGrant`. Hosts MUST in addition keep `trusted`/`quarantine` uniform
  across an actor's per-client rows (contract §7): the restrictive union is a
  fail-safe for divergence, not a licence to mix.

**5. Normative text amended in this change** (`docs/competitive/mem0-substrate-spec-draft.md`):
§10b.1 `grants[]`, §10b.2 (granularity bullet), §10b.3 (row-scoped pointer on the
offboarding/erasure rows + the exact-id revocation-matching note), §10b.4 (note: the worked example still shows the
single-row shape), and a v0.15 drafting-history entry. §10b.4's example, the mirror
`coffee-tenant-config.example.json` and the `scripts/verify-config-shape.mjs`
harness are re-shaped and re-verified by `t_864a5900`, in one change with the
adapter (their §10b.5 "code-verified" claims must move with them).

**6. Test surface this decision implies** (each is a required companion; the
implementation is `t_864a5900`):

| # | Where | Assertion | What it closes |
|---|---|---|---|
| C1 | `scripts/coffee-company-brain-fixture.mjs` (packaged gate, check 6n) | with per-client rows: erasing `arcadia` leaves the staff member's `meridian` recall `ok` **and** refuses `arcadia` — no re-provisioning | the finding itself, on the packaged artifact (today 6n asserts the collateral damage) |
| C2 | same fixture | no active staff/agent row lists two client scopes, and none carries `*` in any capability array (provisioning shape) | the regression the gate could only document; the `*`-row divergence from §10b.3 (a `*` row authorizes a scope it is not revoked with) |
| C3 | `scripts/coffee-adapter-smoke.mjs` | standby/degraded recall allows every scope the actor holds a row for and denies a scope it does not | the S3 first-match defect |
| C4 | `test/protocol/forget-scope.test.ts` | two rows for one actor: forgetting one scope reports exactly that row, the other stays `active`, and the actor's other scope stays authorized | row-scope exactness at unit level |
| C5 | `scripts/verify-config-shape.mjs` + the two example configs | the shipped examples are per-client rows and the §10b.5 facts still hold (union semantics) | keeping "code-verified" claims true |
| C6 | `test/regressions.test.ts` ([Phase-E] session block) | two rows for one actor: `session_start` with `requested_scopes: [<second row's client>]` returns that client's capability cluster — non-empty, and the same scope the operation surface authorizes — with caps/trust resolved across all the actor's active rows; and the trust/quarantine caps take the **most restrictive** union (untrusted row present → `user_facing`; quarantined row present → `background_agent`), independent of row order | the S4 first-row derivation in `src/session/policy.ts:96` (live on the public MCP tool `smartware_session_start`) |
| C7 | `scripts/coffee-company-brain-fixture.mjs` (packaged gate — the `ember-group` block, in the erasure **check 6n** performs, `client:arcadia#1`) | **no active row references the removed scope id**: after that erasure, the **persisted `config.json`'s `scopes`** no longer lists `client:arcadia#1`, and no row with `status:'active'` lists it in any capability array (provisioning hygiene, the same class as C2 — a row left `active` on the retired id must never be the re-grant). Read the **file**, not the in-memory tenant object the fixture hands the adapter: the erasure prunes the file's scope registry and leaves the object's `scopes` untouched (measured, round 4) | the retired-marker case of §3: the row an erasure leaves behind still names the retired id, and re-activating it re-authorizes the purged scope (measured R4/R5) — the case fails open today |

**C7 hangs off check 6n, not 6c** — measured with the fixture's own provisioning
(`measure-c7-geography.mjs`, round 4): check 6c's erasure retires `bob-studio`'s
`client:meridian#1`, and **no grant row in that tenant references it**
(`grants_revoked: []` — `user:lina` carries `client:acme#1`, and the owner is
authorized by `config.owner_id`, not by a row), so an assertion placed there guards
nothing; the id an `erasure` retires **while a grant row still lists it** is
`client:arcadia#1`, retired by check 6n in the `ember-group` tenant, where the
fixture's multi-client row lives (`user:sam`,
`scripts/coffee-company-brain-fixture.mjs:151`). C2 is the shape half of the same
hygiene (no active row lists two client scopes); C7 is the retired-marker half.

## Consequences

- **What becomes true.** The revocation boundary is the grant row, and the row is
  per client: offboarding or erasing one client cannot touch another client's
  access. Hosts that follow the shape carry no re-grant bookkeeping on the
  offboarding write path. The released protocol semantics — and the evidence
  behind them — stay exactly as verified.
- **What becomes harder.** Grant rows multiply (staff × clients), so anything that
  reasons about "the actor's grant" must union rows. `getGrantForActor()` is safe
  for existence checks and denial *reason* text only — never for authorization.
  The sites, and what this decision does about each:
  - **`src/session/policy.ts:96` (`resolveTrust()` → `resolveCapabilities()`, line 151)
    is a live violation, measured, and it is on the public surface.**
    `smartware_session_start` (`src/mcp.ts:560`) / `SmartwareCore.sessionStart`
    derives `capabilities_granted` and the trust/quarantine caps from the first
    active row: under the shape this ADR makes binding, a session for a client held
    in a **second** row reports `capabilities_granted: []` while `checkGrant`
    authorizes the actor and the adapter's recall on that scope answers `ok:true n:1`
    (S4), and the trust answer follows row *order* (S4D/S4D2). It is not left
    implicit: it is companion change 3 of §4 and C6 of §6, folded into
    `t_864a5900` (§4's preamble note explains the scope amendment). The fix is
    single-row-invariant, so the released session behaviour for conformant configs
    does not move.
  - `evaluateAccess()`'s deny *reason* (`src/auth/middleware.ts:124`) is derived from
    the first row and can name the wrong row; the allow/deny decision itself is
    `checkGrant`, which unions. Read from the source, not measured.
  - `SmartwareCore.ensureTrustedClientGrant()` (`src/core.ts:637`) rewrites the
    **first** row's capabilities and must not be used to provision per-client rows
    (it would collapse them). Read from the source, not measured.
  - `grantsReferencingScope()` matches the scope by exact `list.includes()`, while
    authorization uses `scopeMatches()` (exact id, `*`, `prefix/*`): a row carrying
    `*` authorizes a client scope but is **not** revoked by forgetting it. §10b.2
    already forbids `*` for staff and agents; §10b.3 now states the revocation
    consequence explicitly and C2 asserts it. Left as-is in the substrate — a `*`
    row is a provisioning error, not a supported shape, and narrowing this would
    touch a released conformance surface for an unreachable case.
- **Auditability is preserved.** Revocation is still `status: 'revoked'` on a row,
  counted in the ops entry, reversible on offboarding — the substrate's audit story
  is unchanged. That is the main reason the substrate behaviour was not narrowed
  (see alternatives).
- **Foreclosed / not decided here.** Hosts may still provision wide rows; the
  decision does not forbid the shape, it prices it (re-grant requirement). Nothing
  about the protocol surface changes.
- **Reversal trigger.** Revisit if a host legitimately needs a multi-client row
  (for example a future cluster-wide capability model) and cannot re-grant reliably
  — then the alternative is option (b) below, which changes a **released**
  conformance surface and is therefore an owner escalation, not a builder call.
- **Evidence.** `S1`/`S2`/`S3` in `/opt/data/workspaces/brain-pilot-evidence/adr-0012-20260915T110900Z/`
  (`measure.mjs` + `measure.json` + `raw.log` + `NOTES.md`), re-deriving gate check 6n and measuring
  the decision's mechanism on the public adapter surface at base `e937fab`.
- **Evidence, round 2 (`S4` — the session surface).** Same directory,
  `measure-session.mjs` + `measure-session.json` + `session-raw.log`:
  24 facts, reproduced identically on a second run (only fresh ULIDs differ), run
  at `e7475c5` on the **public package surface** (`SmartwareCore.sessionStart`,
  the path behind the `smartware_session_start` MCP tool) plus the provisioning
  template and the adapter's own recall for the authorization contrast. It
  measures: the shipped single-row shape resolving correctly (S4A); the
  per-client-row shape returning `capabilities_granted: []` for the second row's
  client while the adapter's recall on it answers `ok:true n:1` (S4B); the
  single-row control (S4C); trust following row order rather than the actor
  (S4D/S4D2); and a `quarantine: true` row granting the requested scope being
  ignored by the session (S4E). Two honest limits are recorded with it: omitting
  `requested_scopes` masks the S4B defect (the default is every config scope, so
  the first row usually covers one), and the Coffee adapter never calls the
  session surface — the affected consumer is any host using
  `smartware_session_start` with per-client rows.

- **Evidence, round 3 (`R2`–`R5` — the re-grant procedure and the retired-marker
  trap).** Same directory: `measure-regrant.mjs` + `regrant-raw.log` +
  `regrant-facts.json` (17 facts, R2/R3/R4) and `measure-hazard.mjs` +
  `hazard-raw.log` + `hazard-facts.json` (7 facts, R5) — the review card's own
  probes (`t_102f3dfa`), re-run first-party at `55fc628` (the branch is docs-only
  against that review's base `e937fab`, so the code under test is the same tree);
  every fact is identical to the review's raw logs after id/ULID normalization
  (`normalize_check_round3.py` → `normalize-report-round3.txt`). They measure the
  two halves §3 now states: the file is authoritative and a written row re-grants on
  the next operation (R2: an in-memory tenant change re-grants nothing, R3: owner
  `ok n:1` `brain` **and** standby `ok n:1` `app-store-fallback`, plus
  hand-revocation in the file taking effect), and the trap — an erased row restored
  verbatim re-authorizes the purged scope while `config.scopes` no longer contains
  it (R4/R5: `checkGrant` true, write `201`, owner recall `ok:true` `n:0`→`n:1`,
  standby degraded `ok:true n:1`; control `403`). Two honest limits: the probes
  drive the adapter in-process with stub `arbiter`/`appStore` ports (no Redis, no
  second host, no packaged artifact), and C7 does not exist in the fixture yet — the
  assertion is specified in §6 and lands with `t_864a5900`.

## Alternatives considered

- **(b) Narrow the revocation: strip the forgotten scope from the capability
  arrays, leave the row active — lost on three counts.** (i) It changes a *released*
  conformance surface: protocol v0.5.0 §FORGET.SCOPE's "same-commit grant
  revocation", the meaning of the `grants_revoked` receipt, the ops-log intent
  validation (`src/ops_log/intent.ts`), ≥3 test files
  (`test/protocol/forget-scope.test.ts`, `test/conformance/v050-rebuild-forget-provenance.test.ts`,
  `test/schemas-v0.5.0.test.ts`), the gate fixture and the contract doc — that is a
  protocol version bump and a re-gate, straight into the Coffee handoff. (ii) It
  weakens the audit shape: "revoked, re-activatable" (offboarding) becomes
  "arrays edited", so "who lost access when" has to be reconstructed from a receipt
  field that does not exist yet. (iii) It is an owner escalation
  (`AGENTS.md`: escalate before changing a released protocol surface), so it cannot
  be decided by a builder card at all.
- **(c) Add an adapter re-grant path.** Keeps the coupling and turns it into a
  step on the write path: the host must notice, and until it acts the staff member
  is locked out of an unrelated client — the same silent window, now with a
  documented remedy. Strictly worse than (2): the structural fix removes the window
  and needs no bookkeeping.
- **(d) Documentation only, no provisioning change.** Rejected: it leaves the
  reference template provisioning the footgun, so the failure would still be
  discovered in production — the one outcome the card rules out.
- **Splitting rows per *capability* per client.** Considered and rejected as
  unnecessary: one row per (actor, client) carrying that actor's capability cluster
  for the client is the minimal unit that makes the revocation boundary right;
  more rows add no authority precision and multiply the config surface.

## Handoff text — verbatim for the Coffee release handoff (card `t_46c4acce`)

> **Grant revocation is row-scoped.** `FORGET.SCOPE` revokes every grant row whose
> capability arrays reference that client scope (spec §10b.3), and the reference
> adapter is being changed to provision one grant row per (staff member, client) —
> one row per actor until card `t_864a5900` lands (measured `row_count: 1` in the
> shipped template, and contract §7 says which shape is shipped today). Once it
> lands, offboarding or erasing one client cannot touch that staff member's other
> clients. If your provisioning instead lists several clients in one grant row (the
> §10b.4 worked example shape), that row is revoked whole and the staff member
> loses **every** client in it — the adapter exposes no re-grant call. Re-provision
> the actor's remaining **live** scope ids as part of the same offboarding/erasure
> step: re-granting is a config provision (`config.json`, mode 0600, read on the
> next operation), never a protocol call. Do **not** re-activate the revoked row an
> `erasure` leaves behind — it still lists the retired `client:<id>#n`, and
> re-activating it re-authorizes the purged scope (measured: `checkGrant` returns
> true, a write is accepted `201`, recall answers `ok:true`); after an
> `offboarding`, re-activation *is* the sanctioned revival path (protocol v0.5.0
> §FORGET.SCOPE: revoked but re-activatable). Measured before the fix: a `meridian`
> recall that answered `ok` before an `erasure` of `arcadia` answered `403
> insufficient_permission` after it.
