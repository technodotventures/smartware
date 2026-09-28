# Procedure record with receipts: decision spike

Status: NON-NORMATIVE decision spike. No public verb is defined. No released
schema is changed. The fixture at `docs/competitive/procedure-record.fixture.json`
carries `normative: false` and lives outside `schemas/`, `docs/spec/`, and
`docs/protocol/`, which remain authoritative.

## Verdict

Trial. Confidence: medium-high. The shape is proven against the existing claim
and observation models, but it is not yet measured end to end. That measurement
belongs to the dependent benchmark card, not this spike.

## Decision: placement

A procedure record is a typed L1 claim paired with an L0 observation receipt, in
a reference implementation / adapter layer. It is not a new protocol capability
and not a product (Pod or Coffee) surface.

Rationale (one line): the smallest representation that preserves provenance,
correction, and erasure reuses the existing claim and observation models
verbatim, so no normative change is required.

Alternatives considered: A new protocol capability, B reference implementation,
C Pod/Coffee product, D adapter. Chosen: a B/D hybrid, matching the field map
below. Consequence: the fixture is inert reference data, never an executable
surface.

## Field map: procedure record to existing Smartware lanes

| Procedure field | Existing lane |
|---|---|
| identity and version | claim_id, version on the L1 claim |
| task signature | content (inert markdown heading) |
| ordered steps | content (inert markdown list) |
| pre/postconditions | content (declarative markdown, not imperative) |
| expected artifacts | content plus the receipt output |
| verification check + real outcome | the L0 receipt: content.command, content.exit, content.output |
| integrity hash | receipt metadata.integrity: hash, sequence, previous_hash |
| source trace | receipt.source and the claim derived_from |
| actor | actor_id on both the claim and the receipt |
| derivation | claim.derived_from pointing at the receipt observation_id |
| scope | scope on both the claim and the receipt |
| policy and sensitivity | scope plus sensitivity; policy-first retrieval stays binding |
| valid/known time | created_at, version_at (bitemporal); receipt metadata.timestamp = source.observed_at |
| confidence | confidence (ConfidenceBucket) |
| supersession | supersedes (prior version) and superseded_by (surviving claim) |
| retention / FORGET | state, tombstone_id, forgotten_at, forgotten_by |
| L0/L1/L2/L4 boundary | observation = L0; procedure claim = L1; rendered page = L2; injected reused steps = L4 working context, composed on demand, never canonical |

`claim_type: constraint` with `tags: ["procedure"]` is used because the released
ClaimType enum has no `procedure` value. Adding one would be a normative change
and is out of scope here.

## Threat model

| Threat | Mitigation or accept-risk |
|---|---|
| stale or poisoned procedure | the receipt re-verifies the command and output on every replay; a mismatch is surfaced, not executed |
| unsafe commands | the fixture is inert data, never executed by this spike; execution wiring is explicitly out of scope |
| prompt injection in step text | steps are declared as data and rendered inert; nothing interprets them as instructions |
| compromised extraction provider | the receipt hash is recorded at verification time and checked on replay; provider output cannot silently substitute |
| cross-client leakage | scope is set on both claim and receipt; FORGET.SCOPE purges across lanes |
| false-success outcome | the receipt stores the real exit code and output, not an asserted success; the verification command must actually gate success |

## Revision safety

A worse attempt cannot destroy a proven revision: version 2 links back with
`supersedes: 1` and the prior version stays retained in the fixture. An
active-revision change is explainable and reversible through the same supersede
surface and the retained prior version.

## Rollback and falsifier

Rollback: revert this PR; the fixture and brief are additive doc-only changes
with no runtime surface. Smallest falsifier: a benchmark replay that observes
stored step text being executed as an instruction, or a receipt whose hash does
not match the recorded outcome.

## STOP condition

No production procedure engine is implemented on this card. Only the brief and
the non-normative fixture exist. Wiring Expresso to execute procedures is out of
scope.

## Approvals required before any normative change

Adding a `procedure` ClaimType value, changing a released schema, a protocol
verb, or any trust boundary requires explicit owner approval. This spike makes
none of those changes.
