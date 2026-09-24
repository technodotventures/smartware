# Coffee reference adapter — drop-in contract

**Status:** reference integration, executable (not yet exercised against Coffee
staging — staging is unavailable; the deterministic local proof below is the
accepted evidence for this cut).

**Audience:** the engineer wiring Smartware into Coffee. You keep your own
extraction pipeline, your Redis, your auth; this document is the contract your
code calls and the contract you must satisfy.

| Piece | Where |
|---|---|
| Adapter implementation | [`examples/coffee-adapter/adapter.mjs`](../../examples/coffee-adapter/adapter.mjs) |
| Provisioning template | [`examples/coffee-adapter/config.template.json`](../../examples/coffee-adapter/config.template.json) |
| Executable proof | [`scripts/coffee-adapter-smoke.mjs`](../../scripts/coffee-adapter-smoke.mjs) — `npm run verify:coffee-adapter` |
| Acceptance gate (packaged artifact) | [`scripts/coffee-company-brain-fixture.mjs`](../../scripts/coffee-company-brain-fixture.mjs) + [`scripts/coffee-company-brain-gate.mjs`](../../scripts/coffee-company-brain-gate.mjs) — `npm run verify:coffee-gate` (ADR-0011) |
| Resilient host patterns it productizes | [saas-integration.md §1h](saas-integration.md) (lease, guard, fence, app-store outage) |
| Fence semantics it relies on | [ADR-0007](../adr/0007-fencing-token-at-the-mutation-boundary.md) |
| Health surface it embeds | [observability.md](observability.md), ADR-0008 |

Verdict labels used below: **VERIFIED** = measured in the smoke run;
**DESIGNED** = specified here, not measured; **NOT YET PROVEN** = explicitly
open.

---

## 1. The shape

```
Coffee replica (1..N)                         Smartware brain (single writer)
┌───────────────────────────┐                 ┌──────────────────────────────┐
│ HTTP/WS handlers          │                 │ <brain_dir>/smartware.db     │
│  └─ CoffeeBrainAdapter ───┼── ports ───┐    │ <brain_dir>/config.json      │
│       │  extract claims   │            │    │ <brain_dir>/evidence/*.jsonl │
│       │  (your pipeline)  │            │    └──────────────────────────────┘
│       └─ app store (Redis)│            │          ▲  SmartwareCore
└───────────────────────────┘            │          │  ClaimStore/SearchIndex
        authoritative for app data       └── arbiter┘  (public package paths)
                                              = Redis lease + epoch
```

- **One business = one brain** (one `data_dir`, one `config.json`). The brain is
  a *resource* per business, not per request and not per replica.
- **A client is a scope** (`client:<id>#<n>`, non-reusable). Staff and agents
  are **grants** on exact scope ids — one grant row per (actor, client)
  (ADR-0012; §7 *Provisioning grants*).
- **Redis stays authoritative** for application data. The brain is additive: if
  it is unavailable, Coffee still works (degraded) and says so.
- **Single writer.** A replica writes the brain only while it holds the lease;
  the brain's fence (ADR-0007) is the second line for a writer that stalls
  between its guard and its mutation.
- **No model credential.** Extraction is Coffee's; the adapter persists the
  claims you already extract (`llm.provider: 'none'`). VERIFIED: the smoke runs
  with no provider configured.

## 2. What you implement (two ports)

The adapter owns no Redis dependency: you inject the two ports. The commands
shown are the exact Redis operations the pilot measured; implement them however
your client library is shaped.

### `ports.appStore` — authoritative application store

| Method | Contract | Redis sketch |
|---|---|---|
| `appendOnce({key, operationId, record}) -> {written, len}` | Idempotent **when `operationId` is provided** (same op ⇒ `written:false`, nothing appended); a missing op id is not deduplicated | `SET <key>:op:<opid> 1 NX` then `LPUSH <key> <json>`; else append only |
| `list({key}) -> record[]` | All records for one scope, oldest first | `LRANGE <key> 0 -1` |
| `getDrift({key}) -> number` | Recorded app-side/brain divergences for a scope | `GET <key>` |
| `incrDrift({key}) -> number` | Increment and return | `INCR <key>` |

Keep the record shape the adapter writes (`{ts, actor, scope, text, source_ref,
operation_id}`) — the degraded read returns these rows verbatim, so the host
renders its own UI from its own schema.

### `ports.arbiter` — ownership arbiter (Redis lease + monotonic epoch)

| Method | Contract | Redis sketch (exact) |
|---|---|---|
| `tryAcquire({key, holder, ttlMs}) -> boolean` | Acquire if free; true if already ours | `SET <key> <holder> NX PX <ttl>` |
| `renewIfHeld({key, holder, ttlMs}) -> boolean` | **Compare-and-extend.** Renew only if the value is still ours | Lua: `if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('pexpire',KEYS[1],ARGV[2]) else return 0 end` |
| `releaseIfHeld({key, holder}) -> boolean` | Release only if ours (fast failover path) | Lua compare + `DEL` |
| `holder(key) -> string \| null` | Who holds it now | `GET` |
| `nextEpoch(key) -> number` | Monotonic, one per ownership acquisition | `INCR <key>` |

> **`SET key me XX PX ttl` is not a renewal.** `XX` checks existence, not
> identity: a replica whose lease was already handed over takes it *back*, and
> two replicas believe they own the brain. The smoke reproduces exactly that
> hazard and shows the compare-and-extend contract refusing it
> (`17a`/`17b`, VERIFIED).

Derive the keys from the brain's own provisioning surface, never from the mount
path — the adapter exports the functions:

```js
import { brainIdentity, leaseKeyFor, fenceEpochKeyFor, messagesKeyFor, driftKeyFor } from './coffee-adapter/adapter.mjs';

const identity = brainIdentity({ brainDir, tenant });   // "<workspace>.<instance>" from config.json
// coffee:brainlease:ava-consulting.smartware_ava
// coffee:brainfence:ava-consulting.smartware_ava
// coffee:ava-consulting.smartware_ava:messages:client:acme#1
// coffee:ava-consulting.smartware_ava:drift:client:acme#1
```

Identity resolution order: the brain's `config.json` (authoritative once the
brain exists), else the provisioning object you are about to write, else **fail
closed** (no path-derived identity — a mount point is not an identity, and two
businesses with a client named "acme" must never share a key).

## 3. Lifecycle

```js
import { CoffeeBrainAdapter, coffeeTenantConfig, newOperationId } from './coffee-adapter/adapter.mjs';

const adapter = new CoffeeBrainAdapter({
  tenant,                    // provisioning object (see config.template.json)
  brainDir: '/var/lib/smartware/<workspace>/<instance>',
  instanceId: `inst_${process.pid}`,   // the lease holder identity
  ports: { appStore, arbiter },
  leaseTtlMs: 8000,
});
await adapter.start();       // provisions config.json if absent, then acquires or stands by
// ... poll ownership on a timer (ttl/3):  await adapter.refreshOwnership();
await adapter.stop();        // releases the lease (fast failover) and closes the brain handle
```

- `start()` writes `config.json` (mode 0600) only when it does not exist; the
  brain's own config is the identity source from then on.
- `refreshOwnership()` renews when owner, demotes when the renewal fails, and
  re-acquires in the same poll when the lease merely lapsed. A host that forgets
  the timer still fails closed: every write re-checks the lease itself.
- Shutdown releases the lease so a standby takes over immediately instead of
  waiting out the TTL (measured in the pilot at ~1 s; the smoke asserts the
  release path).

## 4. Write path (`adapter.handleWrite(...)`)

```js
const result = await adapter.handleWrite({
  actor: { type: 'person', id: 'user:gigi', display_name: 'Gigi' },  // YOUR authenticated identity
  client: 'acme',                       // or scope: 'client:acme#2'
  text: 'Acme renewal date is 2026-11-02',
  source_ref: 'src_gmail_ava',          // optional: a registered source (provenance)
  operation_id: newOperationId(),       // `op_<ULID>` — supply it; it is the retry key
  claims: [{
    subject: { name: 'Acme', type: 'organization' },   // or { id: 'entity_...' }
    predicate: 'renewal_date',
    object: { type: 'text', value: '2026-11-02' },
    validity_from: '2026-08-01T00:00:00.000Z',         // the FACT's validity start
  }],
});
```

Order of operations (each step is load-bearing):

1. **Validate shape, fail closed**: actor id/type, scope shape, non-empty text,
   `op_`+ULID operation id, claim shape. Nothing is written on failure.
2. **Settle ownership before either store** — not the holder ⇒ refusal with the
   holder's name and `partial_write: false`.
3. **Renew the lease (compare-and-extend)**. A lost lease here ⇒ clean refusal,
   nothing written, safe to retry elsewhere.
4. **Append to the app store** (authoritative, idempotent per `operation_id`).
5. *(drill seam: `hooks.afterGuard` — used by the smoke to model the stall the
   guard cannot cover.)*
6. **Write the brain** — `observe` under the current fence epoch, then persist
   your claims through the public conflict seam (`admitClaim`: inserted /
   corroborated / contested / superseded — never a twin, never last-write-wins),
   then `syncSearchFromClaims` so recall sees them.
7. On any brain failure, increment the drift counter and report the divergence
   in the response.

### Response and retry semantics (VERIFIED in the smoke)

| code | status | partial_write | retryable | What the caller does |
|---|---|---|---|---|
| — | 201 | false | — | Accepted; `claims.outcomes` says inserted/corroborated/contested/superseded |
| — | 200 (`replayed: true`) | false | — | Same operation already completed; nothing changed |
| `invalid_actor` / `invalid_scope` / `invalid_text` / `invalid_operation_id` / `invalid_claims` | 400/401 | false | false | Fix the caller; nothing was written |
| `lease_not_holder` | 503 | false | **true** | Route to `holder`, or retry after failover — nothing written |
| `lease_lost` | 503 | false | **true** | Ownership moved mid-request; nothing written — retry against the current holder |
| `fencing_token_stale` / `fencing_token_missing` | 503 | **true** | **true** | The brain refused a stale epoch before any artifact. The app record stands; retry the same `operation_id` against the new holder (app dedupes, brain completes) |
| `insufficient_permission` / `actor_unregistered` / other denials | 403 | **true** | false | The brain refused this actor. Fix the grant or the caller; reconcile the app-side row |
| `brain_unavailable` | 502 | **true** | true | Replay the brain half with the same `operation_id`; do not re-push the app record |
| `conflict` | 409 | true | false | The operation id was reused with a different payload — a caller bug |

The `hint` field on every refusal repeats the relevant instruction; `drift_total`
tells you how much reconciliation this scope has accumulated.

**Why app store first.** It is Coffee's own data: a message the product accepted
must not disappear because the memory layer was down. The cost is real and
stated: a brain refusal after the app write is a divergence you must reconcile,
counted per scope and reported in the response — never hidden.

## 5. Read path (`adapter.handleRecall(...)`)

```js
const read = await adapter.handleRecall({ actor, client: 'acme', query: 'Acme renewal', limit: 10 });
```

| State | Result |
|---|---|
| Lease holder, brain answers | `{ok:true, source:'brain', degraded:false, provenance:'brain', results:[…claim-bearing rows…]}` |
| Lease holder, brain **denies** | `{ok:false, status:403, code:<substrate code>, denied:true}` — a denial is never answered from the fallback |
| Lease holder, brain fails | Degraded fallback (below) with the brain error attached |
| Standby | Degraded fallback (below), `reason` names the holder |
| Standby, actor not granted | `{ok:false, status:403, code:'actor_unregistered' \| 'insufficient_permission'}` — the degraded path applies the config-derived precheck (fail closed; labelled in `authorization`) |

Degraded fallback (VERIFIED): `source:'app-store-fallback'`, `degraded:true`,
`provenance:'unavailable'`, the app-store rows for that business+scope filtered
by a substring match, and a `note` stating that provenance, claims, conflicts and
lifecycle require the brain. **No provenance is ever invented** on this path, and
no brain write happens.

## 6. Attribution (`describeProvenance` + `attribution`)

```js
const provenance = await adapter.describeProvenance({ actor, observation_id });
// { actor: {id, type, display_name}, observed_at, source: {app, source_id, source_ref}, ... }

const { show, line, why } = adapter.attribution({ ...provenance.render, surface: 'staff' });
```

`describeProvenance` reads the canonical evidence record (`readObservationEvidence`)
so the actor and the **registered source** come from the brain, not from your
request. Rendering itself goes through `smartware/render`: staff-facing
attribution defaults on for corrections, conflicts, unverified/failed freshness
and recent changes; client-facing surfaces never show sources or staff names.
VERIFIED: the smoke asserts the actor line renders for `surface:'staff'` and
never for `surface:'client'`.

## 7. Owner and lifecycle operations, lease-routed

`registerSource`, `addClient` (new client ⇒ new `#N` scope via `ensureScopes`),
`exportClientScope`, `forgetClientScope`, `expireRetention`, `restoreScope` and
`correctClaim` are pass-throughs that refuse on a standby (503 + holder) and
surface the substrate's codes verbatim on the holder.

| Method | Who may call | What it runs |
|---|---|---|
| `exportClientScope({ actor, client \| scope, operation_id? })` | owner | Scope-exclusive EXPORT.SCOPE package (ADR-0006); idempotent per `operation_id` |
| `forgetClientScope({ actor, client \| scope, reason, export_id?, owner_pointer?, operation_id? })` | owner | `offboarding` tombstones, revokes grants and keeps the scope entry so a future `#n` can be minted; `erasure` purges every claimed lane and retires the id. Grant revocation is **row-scoped** (spec §10b.3): the row is revoked whole, so a row must carry exactly one client — see *Provisioning grants* below |
| `expireRetention({ actor, client \| scope, as_of?, operation_id? })` | owner, or a `forget` grant on the scope | ADR-0001 sweep: tombstones elapsed `duration`-policy observations and retracts claims whose only evidence they were; idempotent by nature and per `operation_id` |
| `restoreScope({ actor, package_dir, operation_id? })` | owner | ADR-0006 return path: verifies manifest checksums and refuses a non-empty target scope, a tampered package or a package crossing its scope boundary **before writing anything** |
| `correctClaim({ actor, target_claim_id, corrected_predicate?, corrected_object?, corrected_validity?, change_time?, reason })` | a `correct` grant on the claim's scope | Spec verb CORRECT/REVISE: appends a correction observation and spawns a new claim version. **One-shot** — the verb carries no `operation_id`, so a blind retry after a timeout creates a second correction version; settle ambiguity by reading the brain, not by retrying the write |

Provisioning issues the capabilities:
`coffeeTenantConfig({ staff: [{ actorId, scopes, correct, forget }], agents: [...] })`.
Each is an exact-scope list (`forget` used to be hard-wired empty, which made the
retention sweep unreachable for anyone but the owner). A capability granted with
no adapter method — and a method callable by nobody — are both contract holes;
the acceptance gate exercises each path with a granted and an ungranted actor.

### Provisioning grants — one row per (staff member, client) (ADR-0012)

Provision **one grant row per (actor, client scope)**: a row carrying that actor's
capability cluster for that one client (`observe`/`query`/`read` for it,
`correct`/`forget` only where the business grants them). This is what makes the
revocation boundary equal the intended access boundary — spec §10b.2 grant
granularity, decision in ADR-0012. Measured (ADR-0012 evidence, S2): with one row
per client, `forgetClientScope` on one client reports exactly that row in
`grants_revoked`, the actor's other rows stay `active`, and recall on the other
client answers unchanged with **no re-provisioning**.

**The re-grant requirement (binding on the host).** If your provisioning lists
several clients in one row (§10b.2 permits the shape; the §10b.4 worked example
shows it), offboarding or erasing **any one** of those clients revokes the row and
the staff member loses **every** client in it. Measured (ADR-0012 evidence, S1, and
gate check `6n`): a `meridian` recall that answered `ok` before an `erasure` of
`arcadia` answered `403 insufficient_permission` after it. The host MUST
re-provision that actor's remaining **live** scope ids as part of the same
offboarding/erasure step. Re-granting is a **config provision**: write
`config.json` (mode 0600); the adapter reads it on the next operation. There is no
re-grant protocol call and no adapter method for it. Measured (ADR-0012 evidence,
R3): the file is authoritative — an in-memory tenant change re-grants nothing — and
a freshly written row restores the owner/brain path (`ok n:1`, `brain`) and the
standby degraded path (`ok n:1`, `app-store-fallback`) on the next operation, with
no restart.

**Never re-activate a row that still lists a retired marker (spec §10b.2).** An
`erasure` retires the scope id permanently (a returning client mints
`client:<id>#2`), but the row it revoked still lists the retired id in its
capability arrays — and that revoked row is the surface an operator lands on here.
Re-activating it silently re-authorizes the purged scope. Measured (ADR-0012
evidence, R4/R5) on a config whose `scopes` no longer contain `client:arcadia#1`:
`checkGrant('user:sam','query','client:arcadia#1') = true`, a `handleWrite` on
`arcadia` → **201** (new observations under a retired marker), owner recall →
`ok:true` (`n:0` before that write — answered, not refused) and the standby
degraded read → `ok:true n:1` from the app store. Control, with the revoked row
left alone: `403 insufficient_permission`. The runtime performs no scope-id
validation (§10b.5), so only provisioning discipline prevents this. Therefore:
after an **erasure**, re-provision the actor's remaining live scope ids only, in a
**fresh** row (or edit the revoked row's capability arrays down to the live ids
first) — never re-activate the row that still names `client:<id>#n`. After an
**offboarding** the opposite holds: re-activation *is* the sanctioned revival path
— the protocol keeps grants "revoked but re-activatable" (protocol v0.5.0
§FORGET.SCOPE), the scope entry remains, and no id was retired. Asserted in
ADR-0012 §6 as **C7**: in the packaged fixture, after the `erasure` **check 6n**
performs (`ember-group`'s `client:arcadia#1`), the persisted `config.json`'s
`scopes` no longer lists the retired id and no `active` row lists it in any
capability array. (Not check 6c: its erasure retires `bob-studio`'s
`client:meridian#1`, which no grant row in that tenant ever referenced —
`grants_revoked: []`.)

**Degraded reads union the actor's rows.** The config-derived precheck on the
standby/fallback path (see §5) must allow a scope covered by **any** active row of
that actor, not the first row it finds. VERIFIED (ADR-0012 evidence, S3) that the
shipped `.find()` precheck denies a scope the actor holds a second row for; the fix
lands with card `t_864a5900`.

**Sessions union the actor's rows too — and until they do, do not read an empty
`capabilities_granted` as a denial.** `smartware_session_start`
(`SmartwareCore.sessionStart`) resolves `capabilities_granted` and the
trust/quarantine caps from the **first** active row of the actor. VERIFIED
(ADR-0012 evidence, S4): with one row per client, a session requesting the **second**
row's client returns `capabilities_granted: []` while `checkGrant` authorizes the
actor on that scope and a recall on it answers `ok` (`n:1`) — measured in the same
run. Two host obligations follow. (1) Keep `trusted` and `quarantine` **uniform**
across an actor's per-client rows: the resolver reads the first row, so a mixed pair
gives a row-order-dependent trust level (S4D/S4D2 measured `user_facing` vs
`verified` for the same two rows swapped). (2) Treat the session surface's
capability list as advisory for multi-row actors until the union lands with card
`t_864a5900` (test C6 in ADR-0012 §6). The Coffee adapter itself never calls this
surface — its reads and writes go through the substrate's own operation
authorization (`checkGrant`, which unions) — but the tenant's MCP server exposes the
tool, so a host that provisions the per-client shape **and** uses sessions is
affected.

**Status of this shape in the reference adapter.** One row per (actor, client) is
the decision; the shipped `coffeeTenantConfig` still emits one row per actor
listing every scope it was handed (`row_count: 1`, measured S1), and its `#precheck`
is first-match. Both change with card `t_864a5900` **before the release candidate is
packed**; until then the re-grant requirement above is the operative rule.

## 8. Migration (adding the brain to a running Coffee)

The brain is **additive and behind the adapter**, so the rollout is staged and
every stage is reversible:

1. **Provision** (no behaviour change): create one `brain_dir` per business,
   write `config.template.json`-shaped `config.json` with `owner_id`, the
   `workspace`/`self` scopes, one `client:<id>#<n>` per active client, and
   exact-id grants for current staff and agents. `adapter.start()` does this if
   you pass the provisioning object; there is no data migration — the brain
   starts empty.
2. **Shadow writes** (dark launch): call `handleWrite` after your existing write
   path, ignore the result except its `code`/`drift_total`. Watch
   `adapter.health({actor: owner})` and the Coffee-trial SLO verdict. Recall
   stays off.
3. **Owner reads**: enable `handleRecall` for owner-facing/internal surfaces
   only. Read the `source` field: `'app-store-fallback'` means the brain did not
   answer and the host is on the degraded path.
4. **Staff and agent reads**: enable per surface. Attribution goes through
   `adapter.attribution(...)`; the render contract decides what a client sees.
5. **Owner operations**: enable export/offboarding once (2)–(4) are stable.
6. **Retire nothing.** The app store remains authoritative throughout.

Rollback, at any stage:

- **Reads first, writes second**: turn off `handleRecall` per surface, then
  `handleWrite`. Nothing in Coffee's own store changes; the brain simply stops
  being consulted.
- **Stop the adapter**: `adapter.stop()` releases the lease; the `brain_dir`
  stays on disk, intact and inspectable (SQLite + JSONL + config).
- **Revert the package pin** to the previous Coffee release. The brain's data is
  a separate directory and is not touched by a code rollback.
- **Re-entering later**: the brain is still there. Restart the adapter; it
  re-acquires ownership and recall works again. Restore (ADR-0006) is the return
  path after a disaster: `restoreScope` verifies manifest checksums and refuses
  a package that crosses its scope boundary, a non-empty target scope, or a
  tampered package — before writing anything.

## 9. Verification — what to run, and what it proves

```sh
npm run verify:coffee-adapter      # builds, then runs the deterministic smoke (unit proof)
npm run verify:coffee-gate         # packs the artifact, installs it, runs the acceptance fixture
```

53 checks, no network, no Redis, no model key, no sleeps: two businesses with
overlapping client names, two replicas per lease dance, staff + agent + stranger
actors, a contradicting fact, a stalled stale writer (the fence drill), degraded
reads, provenance, export and health. The exact check list is in the script's
header; the run prints one `PASS` line per behaviour and exits non-zero on any
failure.

**VERIFIED by that run** (the full list is the script; highlights): standalone
refusals write nothing; owner writes are recallable with one claim per fact;
restatements corroborate; contradictions stay contested with both sides
surfaced; agents act under their own grants and ungranted scopes are denials;
the standby answers degraded without inventing provenance and fails closed for
unauthorised actors; failover advances the epoch; a stalled stale writer is
refused by the guard (nothing written) and, in the race the guard cannot cover,
by the brain fence before any canonical artifact; two businesses never share a
key; export is scope-exclusive; health carries no tenant content.

### The acceptance gate — the packaged artifact, six businesses (ADR-0011)

`npm run verify:coffee-gate` packs the build, `npm install`s the tarball into a
scratch app, copies this adapter next to the install, and drives the installed
package's own `exports` surface. 79 checks, one `PASS` line each: six businesses
with overlapping client names, staff + agents + strangers, shared and private
sources, duplicates and paraphrases, contradictions, corrections (granted and
un-granted), offboarding, erasure (including a derived-index wipe), retention
expiry, export → disaster → restore equivalence and tamper refusal, replica
failover with degraded reads, the guard/fence stale-writer pair, restart during
load (a replica restarted mid-loop, plus a modelled process death between the two
stores and its replay), ~180 unauthorized fuzz requests, and a soak reporting
p50/p95, per-business resource growth and claim/evidence counts. Every response
is scanned for foreign-business markers; every brain directory is scanned for the
others' bytes.

The run writes `results.json`, `summary.json`, `README.md` and the raw log to an
evidence directory (override with `GATE_EVIDENCE_DIR`), records the artifact
sha256 and the machine, and exits non-zero on any failed check — a partial pass
is a failed gate.

**NOT YET PROVEN / out of scope for this cut:**
- Coffee staging or production traffic (staging unavailable) — the ports above
  are the interface you test against staging.
- Cross-replica behaviour under real Redis (both the smoke and the gate use
  in-memory ports implementing the same contract; the pilot measured the
  Redis-shaped paths in the resilience gauntlet, including SIGKILL→TTL takeover).
- Cross-process crash at the adapter level over real Redis: the gate models the
  crash window in-process (app record stands, no brain artifact, replay
  completes); the primitive itself was proven cross-process in the gauntlet.
- The drift counter counts divergences **this process observed**; reconcile from
  the app store to catch the ones it did not.
- No cross-store transaction exists by design; a crash between steps 4 and 6
  leaves an app record whose brain half is replayable with the same
  `operation_id` and is meanwhile counted as drift.
- L0 raw-content erasure: `erasure` clears every claimed lane (recall, export,
  settled stores, rebuilt indexes), but the append-only evidence JSONL keeps the
  plaintext until the deferred L0 path (spec §16) lands. Word client-facing
  promises with that bound.
- Storage-level fencing for a pause *inside* one mutation (ADR-0007's stated
  residual).
