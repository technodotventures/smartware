# Smartware Company-Brain Substrate Specification

**Status:** v0.15 NORMATIVE · 2026-09-15 · G0 verdict + compile-latency evidence baked in (§11); Coffee tenant config shape bound (§10b) incl. grant granularity at the FORGET.SCOPE boundary (ADR-0012); client-scope flows + provenance-rendering contract bound (§10c/§10d), F1 export surface SHIPPED (G3.1); all §9 decisions closed; supersedes DRAFT (v0.3–v0.6)
**Companion:** `mem0-gap-analysis.md` (this doc is the design contract; that doc is the analysis).
**Product frame:** Coffee — end-to-end SaaS for small service businesses. The mem0-compat surface (§2–§8) is optional ecosystem reach; the Coffee tenant model (§10) is the primary product frame.
**Binding decisions — all CLOSED 2026-08-29:** provenance-at-query-time (spike, §7); client-scope erasure semantics (§10); Coffee client-scope flows - export/erasure/offboarding/return/attribution (§10c); provenance-rendering contract (§10d); `add` compile-default (§9.1); FORGET.SCOPE versioning (§9.2); MCP namespace / CLI scope / adapter hosting (§9.3–9.5). G0 spike evidence: §11.

---

## 1. Goals / non-goals

- **Primary goal (Coffee frame):** one small business = one tenant; client-scoped memory the owner can trust and explain (see §10). This is what ships first.
- **Reach goal (optional):** Smartware speaks the mem0 v3 callable surface faithfully enough that a mem0 client (SDK, REST, CLI, MCP) can repoint without code changes — while the substrate underneath remains append-only, bitemporal, and provenance-true.
- **Non-goals:** reimplementing mem0's fuse pipeline, matching its score scale, importing its stored vectors/embeddings, or cloning hosted-only features (orgs/projects dashboard, webhooks, events API) in v0.1.

## 2. Compatibility surfaces (normative set for v0.1)

1. REST, OSS shape (no `/v1/` prefix): `POST /memories`, `GET /memories`, `GET /memories/{memory_id}`, `PUT /memories/{memory_id}`, `DELETE /memories/{memory_id}`, `DELETE /memories`, `GET /memories/{memory_id}/history`, `POST /search`, `POST /reset`, `GET|POST /configure`, `GET /entities`, `DELETE /entities/{entity_type}/{entity_id}`.
2. REST, hosted shape: same routes prefixed `/v1/`.
3. SDK method surface (names + params): `add`, `search`, `get`, `get_all`, `update`, `delete`, `history`, `reset`, `configure`. Params honor: `user_id`, `agent_id`, `run_id`, `app_id`, `filters`, `top_k`, `threshold`, `rerank`, `explain`.
4. CLI: mem0 subcommand names mirroring §3.
5. MCP: mem0 tool names, option-gated namespace on the existing Smartware MCP server.

Auth: accept `Authorization: Token <key>` (hosted shape) and `X-API-Key: <key>` (OSS shape, admins/per-user); map to Smartware's existing grant middleware. `AUTH_DISABLED=true` is never honored (security posture); local dev only.

## 3. The memory identity model (binding)

- A **memory** in compat mode = a **lineage root**, identified by a stable `memory_id` (minted once; never renames, never reused).
- Each content revision = a new L1 **claim** version joined to the root by `supersedes`; the **effective-current leaf** is the claim that is active, not superseded, not corrected, not forgotten.
- Compat reads (`get`, `get_all`, `search` results) resolve to the **effective-current leaf** only. Client-visible `memory_id` is ALWAYS the root.
- History endpoints enumerate the full revision chain under the root, in `version_at` order.

**Byte-clean rule (binding):** superseded, corrected, forgotten, or stale claims MUST NOT appear in `search` / `get` / `get_all` results — this is a *view* rule over the append-only mantle. The mantle never physically deletes; the view never resurrects. (Re-delete-after-update is still a delete; tombstone-on-latest, not tombstone-on-all-versions.) `delete_linked` also retracts derived relation edges (tombstone, not remove) so graph traversal stays evidence-true; the counted set is lineage claims + relations.

## 4. Operation semantics (binding mappings)

| mem0 op | Smartware behavior | Notes |
|---|---|---|
| `add(messages)` | `OBSERVE` raw input; adapter MAY trigger `REFLECT`/`compile` per configuration (`x-compile: on\|off` header / config), default `off` for latency parity | mem0 `add` returns per-extracted-memory ids; our response returns per-claim roots after compile, or a single observation root if compile off |
| `add(raw, content)` | `OBSERVE` with explicit content | body `content` supported (mem0 `content` field) |
| `search` | `RECALL` via L3 hybrid; eligibility filter set runs BEFORE ranking; `filters` map to claim entity-type/attribute filters | `rerank=true` → accepted but degraded (re-rank uses own reranker or documented no-op with `explain` hint); `threshold` accepted, applied on our calibrated score only |
| `update` | `REVISE`: append supersede claim | conflict policy: if old content unchanged, return success idempotently; if `memory_id` unknown → `not_found` |
| `delete` | `FORGET`: retract via tombstone on the current leaf chain root | never physical; history keeps the DELETE event |
| `delete` (with `delete_linked=true`, hosted) | `FORGET`: retract lineage root + **tombstone derived graph links** (relations), never physical removal; report `cascade_count` = exactly the number of retracted lineage claims + retracted links, never silently expanded | `delete_linked` count must be faithful, or clients trusting it mispredict blast radius |
| `delete all` | scope-wide retraction with cascade ack | requires `cascade_required_ack` (protocol); refused without it |
| `history` | materialize {`input` from L0, `old_memory`/`new_memory` from chain, `event` from ops-log, `categories`/`metadata` from claim metadata}; `previous_embedding_*`/`embedding_*` → `null` | never leak vectors |
| `reset` | **LOCKED: snapshot + quarantine** (flag-gated; default denies with `forbidden` unless explicit `x-allow-reset`; snapshot remains restorable) | precedent: mem0 OSS issue #3928 — `delete_all()` called `vector_store.reset()` and wiped *all* users' memories; fixed PR #4349 (merged 2026-03-16). OSS `reset()` is a distinct call from `delete_all()`; both get explicit flag-gated mappings. GTM: "the call mem0 shipped a data-loss bug in is the one we make reversible." |
| `configure` | store provider config for our embed/LLM adapters; unknown providers → `400` with clear code (same as mem0 rejecting unbundled) | `/configure/providers` lists ours only |
| `entities` | derived from Smartware scope/actor registries | cascade delete = scope retraction |

## 5. Scope mapping (binding)

- `user_id` → `user:<id>`; `agent_id` → `agent:<id>`; `run_id` → run/session scope; `app_id` → `workspace`/`project:<id>`.
- Resolution happens at adapter boundary; raw mapping recorded in provenance. One mem0 id may map to multiple Smartware scopes — always resolve explicitly, never default.
- Filter entity IDs (v3 `filters`) → claim entity types + attributes, intersected with caller grants, never widened.

## 6. Scoring contract (binding)

- Scores reported to compat clients are **Smartware's own** (RRF fusion output), normalized; never claimed to equal mem0's fused score.
- `explain=true` supported: return our score components (lexical, semantic, fused) with labels — mem0 clients using `explain` get *more* transparency, same shape.
- **Benchmark reads rank/relevance, not absolute scores** (protocol, already agreed).

## 7. Provenance envelope (CLOSED — spike 2026-08-29)

- **Result @50k claims/obs/ops:** envelope materialization p95 = 0.195ms vs 0.146ms baseline `getClaim` → **+~0.05ms; the envelope is effectively free** (all four fields already inline on the claim row). Default-ON passes; decision gate closes.
- **Landmine (storage note, binding):** resolving ops entry *content* via today's JSONL full scan is p95 ~107ms at 50k ops (~12,800x slower than SQLite PK at 0.008ms). Envelope stays cheap only because `operation_id` is inline. **A derived SQLite ops index (same pattern as Layer0Index over JSONL) is REQUIRED** before `history`/`explain` resolve ops payloads. Tracked as a build prerequisite.
- Payload shape: per-hit `{claim_id, observation_ids, ops_entry_id, version_at}` inside `metadata.origin` (extension field; does not break mem0 clients that ignore unknown metadata).
- Storage delta: provenance is already canonical (no duplication); envelope is a read-path cost, not a write cost.

## 8. Verification plan

- **Compat conformance suite:** golden-response tests per endpoint against a fixture mem0 client (typescript & python SDKs) — responses schema-valid for mem0 clients without edits.
- **Byte-clean tests:** update → search excludes old content; delete → search excludes; history still shows events; re-add after delete creates NEW root, old chain intact; `delete_linked` → `cascade_count` equals actual retracted lineage claims + relations (fidelity, never expansion); relations tombstoned, not removed; `reset` → snapshot restorable, quarantine visible, default deny without `x-allow-reset`.
- **FORGET.SCOPE tests (Coffee §10):** `erasure` → zero `RECALL` results for the client in EVERY lane (vector, BM25, graph); claim rows purged, vector entries removed, derived summaries flagged for re-derivation; ops-log entry carries exact `claims_retracted`/`observations_retracted` counts; grant revocation same commit. `offboarding` → tombstone+revoke, reversible; owner-approved non-PII pointer may carry into `client:<id>#2`; re-opened scope never inherits tombstoned history; `client:<id>#1` marker non-reusable.
- **Provenance-integrity tests:** every search hit can reproduce source observation + ops entry; superseded claims never satisfy `get`; timestamp order correct after multi-version history.
- **Benchmark:** recall@k on curated company-brain query set + provenance-integrity + p95; pin `threshold=0.0`, `rerank=false`, same `top_k`, same embedding model; temporal axis separate (see gap analysis §6.4 — with the correction in §11.1: mem0 OSS as-of is platform-only, so our bitemporal axis is structural, not score-comparable). Concrete G0 suite + numbers recorded in `mem0-h2h-recall.md` (harnesses `scripts/mem0-h2h-{smartware,mem0,compare}.*`, query set `benchmarks/retrieval/mem0-h2h-companybrain-v1.json`).
- **Error-envelope mapping:** mem0 HTTP codes (404/400/401/409/500) ↔ Smartware error codes with `details` preserved.

## 9. Open decisions — ALL CLOSED (history retained; none blocking)

1. `add` compile-default: **RESOLVED 2026-08-29 — sync-raw + async-compile** (smarty-pants, accepted by tech-head). Observations written synchronously to L0 (deterministic, sub-100ms, no LLM on write path); claims compiled in background on a durable queue; retrieval includes raw observations for a state-based window with an `unverified` flag in the RECALL payload contract — "learning in progress", never silent omission; FAILED compile → stays raw-searchable forever with `unverified` (an LLM outage must not age out a memory); EXTRACTED → claim ranks above, obs kept as evidence. Metrics: sync write p95 <100ms; compile p95 ≤5s @50k claims. Compile latency axis added to the spike.
2. FORGET.SCOPE protocol versioning: **RESOLVED 2026-08-29 — protocol v0.5.0 core intent** (smarty-pants): changes conformance semantics (atomicity + same-commit grant revocation + lane-exhaustive purge), so it is NOT an extension slot. v0.4.x servers: backward-compatible on the five verbs, non-conformant on scope-erasure — migration note, not a break (anti-pattern cited: mem0 v2→v3 churn). Protocol doc note: v0.5.0 schemas + contract ship together; mismatch blocks conformance.
3. MCP namespace strategy: **RESOLVED 2026-08-29 — option-gated namespace on the existing Smartware MCP server** (default off; mem0 tool names under a `mem0_`-prefixed namespace when enabled). Avoids a second MCP server's tool-collision + double-init cost; compat is a mode, not a surface.
4. CLI surface scope for v0.1: **RESOLVED 2026-08-29 — mem0-compat CLI ships in the compat package only** (`smartware-mem0-compat`), mirroring SDK subcommands (`add/search/get/get_all/update/delete/history` + flag-guarded `reset`); core `smartware` CLI untouched.
5. Adapter hosting: **RESOLVED 2026-08-29 — separate package `smartware-mem0-compat`** with its own semver, decoupled from protocol (a mem0 surface drift never bumps protocol). Core library stays lean.
6. QM hosting appendix (deferred): `MemoryService` is 11 methods (5 required + 6 optional incl. `readHead?` = sha256-revision CAS); drop-in feasible — capture→OBSERVE/REFLECT, query→RECALL, replace→REVISE; intercept `MAX_FACTS=300` oldest-first eviction at the render layer; QM's `memory/MEMORY.md` file-backed store note (tool-only warning is about agent-side authorship, not storage).

## 10. Coffee tenant binding: client scopes · FORGET.SCOPE (PRIMARY — binding)

**Context:** Coffee = end-to-end SaaS for small service businesses. One business = one tenant. Owner = `owner_id`; staff = `Grant` capabilities over `workspace` (grant middleware already exists). Verified: **zero protocol changes needed — only config shape** (tech-head, code-verified).

- **Clients as scopes, not subjects (binding):** `client:<id>` scopes under `workspace` with `visibility_default: 'scope'`. Scope-boundary leak is fixable by construction; subject+filter isolation is a discipline bug that resurfaces in every new retrieval path. Staff hold grants per client-scope cluster. (Also: 2–10 staff means the §5a promote/demote is a weekly event, not quarterly.)
- **Erasure boundary = client scope (binding):** one audited operation for "client left / disputed / erasure request"; "export everything about Acme" = one scope (portability + data rights + trust).
- **FORGET.SCOPE (substrate-required op, binding):** intent-backed mutation `FORGET.SCOPE { scope: client:<id>, reason: erasure | offboarding }`:
  - plans retraction over all claims + observations in the scope;
  - **one ops-log entry** carrying `claims_retracted` / `observations_retracted` counts;
  - executes **grant revocation in the same commit** (atomic);
  - **reason semantics DETERMINE behavior:** `erasure` (legal/PII) = content purge — claim rows purged, **vector entries removed, derived summaries flagged for re-derivation** (embeddings/summaries are part of the leak surface; post-erasure conformance = zero `RECALL` results in EVERY lane: vector, BM25, graph); `offboarding` = tombstone + grant revoke, auditably reversible;
  - **Non-reusable scope marker:** scope ids are versioned — `client:<id>#1`, `client:<id>#2` — so a returned client can never inherit tombstoned history. `erasure` → fresh scope inherits nothing; `offboarding` → may carry an explicit owner-approved non-PII pointer ("client since 2023, 4 jobs, no disputes") into `#2` — an audited choice, never silent resurrection.
- **Provenance rendering rule (Coffee UI, product rule):** the envelope (≈0.05ms, §7) makes per-answer attribution free. Attribution renders **staff-facing only** — never client-facing UI ("Maya said" is a loyalty liability) — and **by default for consequential or recently-changed facts**, with a "why this answer?" toggle for everything else (avoid footnote-wall).

## 10a. v0.5.0 cut scope — indexes and freshness contract (binding)

- **Observation FTS index (v0.5.0-bound):** `searchObservations` is a full JSONL scan + substring match today (core.ts:665) — the sync-raw freshness promise ("raw is searchable") would cost ~O(N) per RECALL and blow the §7 p95 budget. Raw-searchability requires an observations index (existing claims-FTS + Layer0Index patterns apply).
- **Derived SQLite ops index (v0.5.0-bound, not just a history nicety):** the compile worker's intent matching (`reflect.ts` → `readAllOpLogEntries`) hits the same JSONL scan measured at 107ms p95 @50k ops → compile p95 ≤5s (and history/explain) require it. Both indexes are in the v0.5.0 cut, not follow-up.
- **Compile-queue fingerprint index + batched appends (v0.5.0-bound, G0-backed):** compile @50k measured 9,468,298ms (1,893.7× over the 5s budget; §11.2) — root cause is O(N²) per-claim fingerprint dedup plus per-claim fsync JSONL appends. The compile-queue build MUST also land (a) a fingerprint→claim_id index (hash/SQLite) so dedup is O(1) per claim, and (b) batched claim/ops appends (one fsync per N); then **re-run the compile-latency spike** against the ≤5s @50k budget before G2 closes. §11.2 makes the ±0.1ms/claim arithmetic concrete: 5s / 50k = 0.1ms of work per claim.
- **Freshness payload labels (binding):** the runtime currently does not emit literal `unverified`/`EXTRACTED`/`FAILED` labels — the compile/recall payload contract MUST expose them explicitly (per §9.1) so clients assert state instead of inferring it from search behavior. (§11.2, freshness-validation section.)
- **Freshness window is STATE-based, never time-based:** observation stays raw-searchable (`unverified`) until its compile job resolves — EXTRACTED → claim ranks above it, obs retained as evidence; FAILED → searchable forever with `unverified`; job never resolves → flagged, not hidden. A time window would desync from what durably happened.
- **Rebuild-equivalence conformance (consistency contract):** FTS and ops indexes are regenerable artifacts. v0.5.0 conformance includes wipe-and-rebuild from JSONL asserting byte-level equivalence with the canonical log, and **FORGET.SCOPE "zero results in every lane" is asserted against a REBUILT index** — stale FTS entry = the ghost that resurfaces a purged client; purge proof is only valid if index regeneration is part of the conformance suite.

## 10b. Coffee tenant config shape — `config.json` (binding, v0.5.0 cut)

**One business = one tenant = one Pod = one instance** (`instance_id` + `data_dir` + `config.json`). There is no separate tenant record: the tenant IS the Smartware instance. Provisioned once at onboarding; thereafter `scopes` + `grants` are the tenant's operational surface. **Zero code changes required for the shape itself** — verified against dist v0.6.3 (see code-verified notes). The only non-config touch is the schema-set `Scope` pattern (see Schema seam below).

### 10b.1 Field bindings

| Field | Shape | Binding rule |
|---|---|---|
| `instance_id` | `smartware_<ulid>` | Pod identity; one per business. Never reused after a business closes. |
| `owner_id` | spec-conformant ActorId — `user:<slug>` (e.g. `user:ava`) | The business owner. Owner bypasses ALL grant checks (`grants.ts isOwner`); never assign staff here. Legacy ids (`person_owner`) still accepted in migration window but NOT used for new Coffee tenants. `user:<slug>` verified conformant to `common.schema.json` `ActorId` (`^(user|agent|sidecar|substrate):[a-z0-9-]+$`); `person_owner` is not. |
| `writer_id` | `writer_local_<ulid>` | Pod writer; untouched by tenant model. |
| `version` / `data_dir` | as scaffolded | untouched by tenant model. |
| `scopes[]` | `ScopeEntry` | `self` (private, parent null) and `workspace` (workspace, parent null) remain from the scaffold. `project:default` is replaced by the client scopes in the Coffee frame. **Each client = one scope entry** `client:<id>` with `parent: 'workspace'`, `visibility_default: 'scope'` — clients are scopes, never subjects (see §10). |
| scope id versioning | `client:<id>#n` where n ≥ 1 | **Non-reusable marker (binding):** initial onboarding mints `client:<id>#1`; a returned client after erasure/offboarding mints `#2` (fresh scope inherits nothing — §10). `#1` is permanently retired and its entry removed from `scopes` on `reason=erasure`. Scope ids are plain strings to the runtime — verified no id validation rejects `:` or `#` (round-trip test, §10b.5). |
| `grants[]` | `Grant` | **One grant row per (staff actor, client scope)** — a row per client carrying that actor's capability cluster for it. The row is the revocation boundary (§10b.3 rowwise revocation), so one row per client makes revocation scope-exact; see 10b.2 grant granularity. |
| `llm` / `staleness` / `entity_resolution` | as scaffolded | `staleness.scope_overrides` is declared in the config contract but **not consumed by the runtime today** (verified: only `default_half_life_days`/`stale_threshold` are read, `manifest.ts`). Set staleness at Pod level; do not rely on per-client overrides until a future staleness layer consumes them. |

### 10b.2 Staff grants — per client-scope cluster (binding)

- **One `Grant` row per (staff actor, client scope)** (ADR-0012); `actor_type` legacy enum `person|agent|system` with **conformant `actor_id`** (`user:<slug>` for staff, `agent:<slug>` for Coffee agents/teammates). Legacy enum ↔ spec kinds is projected in `registry-md.ts` (person→human); the alias-map PR will tighten.
- Capabilities: `observe` (record), `query` (recall), `read`, `correct`, optionally `compile`; `forget` only where the business wants staff-initiated five-verb FORGET within their cluster — **erasure/offboarding (FORGET.SCOPE) is an owner decision**; staff never invoke it. Grants carry `trusted:false`, `quarantine:false`, `status:'active'`, `expires_at` set for contractors or `null` for permanent staff (expiry is enforced by `isExpired`).
- **The cluster IS the exact scope-id list (binding — verified):** `scopeMatches` supports only (a) exact id, (b) `*` (matches EVERY scope — never issue to staff), (c) `prefix/*` (matches `prefix/…` slash ids **only** — does NOT match colon ids). Verified: `client:*`, `client/*`, and `client:acme#*` ALL return false for `client:acme#2`; only exact `client:acme#2` (or `*`) matches. Therefore: **list every client scope id explicitly** in the capabilities arrays; a "cluster" of clients = the explicit set in one grant's arrays. Rotation = explicit list edit (weekly ops cadence, per §10).
- **Non-reusability by construction:** because no wildcard spans versions, a grant for `client:acme#2` can never authorize `client:acme#1` (verified: grant on `#2` → `checkGrant(query, 'client:acme')` = false). A resurrected marker cannot be reached by any pattern except `*` or an explicit re-list — both are config smells; the rule is: never issue `*`, never re-list a retired marker.
- **Grant granularity is load-bearing, not cosmetic (binding — ADR-0012):** `FORGET.SCOPE` revokes every grant **row** whose capability arrays reference the forgotten scope (§10b.3, same commit). A row that lists several clients is therefore revoked **whole** when any one of them is offboarded or erased, and the staff member loses the others until re-provisioned — measured: a `client:meridian#1` recall answering `ok` before an `erasure` of `client:arcadia#1` answered `403 insufficient_permission` after it (ADR-0012 evidence S1; gate check 6n). Issue **one row per client**, so the revocation boundary equals the intended access boundary (measured S2: the other row stays `active`, recall unchanged, no re-grant). A host that nevertheless keeps multi-client rows MUST re-provision the actor's remaining scopes in the same offboarding/erasure step: re-granting is a config edit (`docs/integration/coffee-adapter.md` §7), never a protocol call. Authorization unions active rows (`checkGrant`/`getAuthorizingGrants`), so multiple rows per actor are valid; first-match lookups (`getGrantForActor`) are existence/reason lookups only, never an authorization decision — **with one live exception, now named and carded:** session capability/trust resolution (`src/session/policy.ts:96`, behind the `smartware_session_start` tool) still reads the first row and reports **no** capabilities for a client held in a second row (measured: ADR-0012 S4) although the operation surface authorizes the actor on that scope. It is companion change 3 of ADR-0012 §4 and test C6 there, and lands with card `t_864a5900` before the release candidate is packed.
- Minimal cluster: one grant `query:[client:<id>]` + `observe:[client:<id>]` is sufficient for a staff member serving one client; empty arrays for unheld operations (default `getActiveGrants`/`checkGrant` semantics treat them as absent).

### 10b.3 Lifecycle in config terms

| Event | Config effect (same commit as the FORGET.SCOPE mutation) |
|---|---|
| Client onboard | add `client:<id>#1` scope entry; add/extend grants for serving staff (after GRANT op or config edit — scope entries have no protocol op today; they are config-provisioned). |
| Staff reassignment | edit capability lists to add/remove exact client ids (weekly, audited). |
| Client offboarding | `FORGET.SCOPE{reason:offboarding}` → grant **rows** referencing the client scope set `status:'revoked'` (row-scoped — §10b.2 grant granularity; auditable, reversible); scope entry retained (tombstone is data-side). Reopen → `client:<id>#2` + repointed grants; optional owner-approved non-PII pointer (§10). |
| Client erasure | `FORGET.SCOPE{reason:erasure}` → grant rows revoked (same commit; row-scoped — §10b.2 grant granularity); **scope entry removed**; `#1` marker permanently retired. Reopen → `#2` inherits nothing. |
| Business closes | suspend Pod; never reuse `instance_id`; `owner_id` transfer is a config edit + audit. |

**Revocation matching is exact-id (binding — ADR-0012).** Row revocation tests the scope id literally against the capability arrays (`src/protocol/forget_scope.ts` → `grantsReferencingScope()` → `Array.includes(scope)`), whereas authorization goes through `scopeMatches()` (exact id, `*`, `prefix/*`). Consequence: a row carrying `*` in a capability array **authorizes** a client scope but is **not** revoked by a `FORGET.SCOPE` on it. §10b.2 already requires exact-id lists and forbids `*` for staff and agents, so this is unreachable for a conformant config — the note exists so it is not discovered as a surprise, and row `C2` of ADR-0012 §6 asserts that no active row carries `*`. Substrate behaviour is unchanged (no narrowing of a released surface for a provisioning error).

### 10b.4 Worked example (tenant "Harbor & Lane", 2 staff + 1 agent, 3 clients)

```json
{
  "instance_id": "smartware_01kxw9f2v3",
  "owner_id": "user:ava",
  "writer_id": "writer_local_01kxw9f2v4",
  "version": "0.6.3",
  "data_dir": "/var/lib/smartware/harbor-lane",
  "scopes": [
    { "id": "self", "parent": null, "visibility_default": "private" },
    { "id": "workspace", "parent": null, "visibility_default": "workspace" },
    { "id": "client:acme#1", "parent": "workspace", "visibility_default": "scope" },
    { "id": "client:bcau#1", "parent": "workspace", "visibility_default": "scope" },
    { "id": "client:gate#2", "parent": "workspace", "visibility_default": "scope" }
  ],
  "grants": [
    { "id": "grant_01kxw9f2v5", "actor_type": "person", "actor_id": "user:gigi",
      "capabilities": { "observe": ["client:acme#1", "client:bcau#1"], "query": ["client:acme#1", "client:bcau#1"], "compile": [], "correct": ["client:acme#1"], "forget": [], "read": ["client:acme#1", "client:bcau#1"] },
      "trusted": false, "quarantine": false, "created_at": "2026-08-29T09:00:00.000Z", "expires_at": null, "status": "active" },
    { "id": "grant_01kxw9f2v6", "actor_type": "person", "actor_id": "user:noah",
      "capabilities": { "observe": ["client:acme#1", "client:gate#2"], "query": ["client:acme#1", "client:gate#2"], "compile": [], "correct": [], "forget": [], "read": ["client:gate#2"] },
      "trusted": false, "quarantine": false, "created_at": "2026-08-29T09:00:00.000Z", "expires_at": "2027-01-01T00:00:00.000Z", "status": "active" },
    { "id": "grant_01kxw9f2v7", "actor_type": "agent", "actor_id": "agent:coffee-assistant",
      "capabilities": { "observe": ["client:gate#2"], "query": ["client:gate#2"], "compile": [], "correct": [], "forget": [], "read": [] },
      "trusted": false, "quarantine": false, "created_at": "2026-08-29T09:00:00.000Z", "expires_at": null, "status": "active" }
  ],
  "llm": { "provider": "anthropic", "model": "claude-sonnet-4-5" },
  "staleness": { "default_half_life_days": 90, "scope_overrides": {}, "stale_threshold": 0.3 }
}
```

(`client:gate` left via erasure earlier → mints `#2` only; `#1` is absent from `scopes` and permanently retired. `user:ava` has no grant — owner bypasses. Mirror/companion file: `docs/competitive/coffee-tenant-config.example.json`.)

**Grant shape note (ADR-0012):** the example above still shows the
single-row-per-actor shape (`user:gigi` holds acme + bcau in one row). That shape is
permitted but **revocable-whole** — offboarding or erasing either client revokes the
row and `#2`-repointing loses the other client until re-provisioned (§10b.2 grant
granularity). The reference adapter issues **one row per client**; this example, the
mirror file and the config-shape harness are re-shaped and re-verified together with
the adapter by card `t_864a5900` (the §10b.5 "code-verified" facts move with them).

### 10b.5 Code-verified facts (dist v0.6.3, Node v26.5.1; harness `scripts/verify-config-shape.mjs`)

- `loadConfig`/`saveConfig` round-trip a Coffee shape unchanged (scopes incl. `client:<id>#n`); no format validation on scope ids in the runtime.
- `ScopeRegistry.getAncestors('client:acme#2')` → `client:acme#2 > workspace` — hierarchy holds as configured.
- `checkGrant('user:gigi','query','client:acme#2')` = true; `client:acme` (#1 orphan) = false; `client:*` / `client/*` / `client:acme#*` = false; `*` = true (therefore banned for staff).
- `isOwner('user:ava')` = true → staff/agent rows are grant-only; owner row appears in `agents/registry.md` (human|self|all).
- `getVisibilityDefault('client:acme#2')` = `'scope'` (accessor exists; the write path defaults observation visibility to literal `'scope'` today — observe.ts — which coincides with this binding; no runtime mismatch).

### 10b.6 Schema seam (the ONE non-config touch, v0.5.0 schema set)

`common.schema.json` `$defs/Scope` currently restricts ids to `^(self|workspace|project:[a-z0-9-]+|agent:[a-z0-9-]+)$` — it **rejects** `client:<id>` and `client:<id>#n`. It is referenced by `observation`, `claim`, `recall-request`, `context-request`, `context-bundle`, `page-frontmatter`, `tombstone-frontmatter`, `watch-event`, `agent-registry-entry`. The v0.5.0 schema set (released with the cut, per §9.2 "schemas + contract ship together") must widen it:

```
^(self|workspace|project:[a-z0-9-]+|agent:[a-z0-9-]+|client:[a-z0-9-]+(?:#[1-9][0-9]*)?)$
```

Shipped pattern (v0.5.0, 2026-08-29): the widened pattern is released with the
strict no-leading-zero form above — `#[0-9]+` would admit `#0`/`#01`, which
violates the binding rule "n ≥ 1" (§10b.1). Markers are minted starting at `#1`,
so this pattern admits every legal marker and rejects no valid id. Grant
wildcards (`client:*`, `client/*`, `client:acme#*`) remain config patterns and
are rejected as scope ids.

This is a schema-pattern widening, NOT a protocol-semantics change (no verbs, contracts, or conformance semantics change); v0.4.x artifacts keep the old pattern (migration note per §9.2). Impl card (t_357cd5fb) and conformance card (t_58b66030): the Coffee-tenant config fixture above is the seed fixture for config-shape conformance and for the widened `Scope` tests.

## 10c. Coffee client-scope flows — UX on the FORGET.SCOPE substrate (binding, G3)

**Context:** G2 shipped the substrate half: FORGET.SCOPE (erasure | offboarding), the owner-approved non-PII pointer bound into offboarding (`owner_pointer`; rejected on erasure — verified t_357cd5fb), same-commit grant revocation, non-reusable `client:<id>#n` markers, §10a indexes, §10b config shape. §10c binds the five Coffee-facing flows that sit on it. **Dates are an @user decision** (plan G3: "no dates until Coffee's window is known") — this section binds flow semantics and sequencing, never the calendar.

### 10c.1 Flow inventory and triggers

| # | Flow | Coffee trigger | Substrate surface | State |
|---|------|-----------------|-------------------|-------|
| F1 | Export one client | Owner: "Export everything about Acme"; data-rights request (client-initiated, owner-mediated) | `smartware_export_scope` (owner-only; §10c.4) | SHIPPED (G3.1) |
| F2 | Offboarding | Client churned / paused / migrated; dispute-hold lane | FORGET.SCOPE{reason:offboarding} + optional `owner_pointer` | SHIPPED v0.5.0 |
| F3 | Erasure | Client fully gone + no dispute; DSR erasure; dispute resolved | FORGET.SCOPE{reason:erasure} | SHIPPED v0.5.0 |
| F4 | Return | Same legal entity returns | config mint `client:<id>#N` + grant repoint; pointer = OBSERVE into #2 | config-backed (F4 pointer flow designed §10c.5) |
| F5 | Attribution | Any staff-facing answer with provenance | read-only: provenance envelope (§7, §10 rendering rule) | SHIPPED (provenance); UI rule §10c.6 |

### 10c.2 Owner-only gates (binding)

- FORGET.SCOPE and export are **owner-only** (substrate enforces `requireOwner`, t_357cd5fb); the staff capability surface NEVER includes them (§10b.2: staff `forget` defaults to `[]`; scope erasure/offboarding is an owner decision). Staff/agent flows are read-side only (observe/query/read/correct within their exact client scope ids).
- Coffee: the owner gate is an owner-verification modal; staff see no affordance for either operation — not hidden, absent.
- **Receipt (binding):** every mutation returns the ops entry (operation_id, exact counts, grants_revoked, timestamp); Coffee renders it in the operation/view history (history/explain consume the derived ops index, G2). No mutation is presented as "soft" — offboarding is reversible ON PURPOSE, erasure is terminal ON PURPOSE; the UI states which.

### 10c.3 Erasure vs legal hold (binding rule)

- `reason=erasure` is terminal and irreversible (`erased` status, `#1` retired, marker-last, zero-results-in-every-lane against rebuilt indexes — §10/§10a, conformance t_58b66030). **Erasure destroys the evidence needed to defend a dispute.**
- **Binding: a dispute / legal-hold trigger NEVER executes erasure.** It executes the hold lane: offboarding (tombstone + grant revoke, data retained) + F1 export snapshot held in the Pod. Erasure runs only when the hold releases — the Coffee flow requires an owner attestation ("no pending dispute / verified request / hold released"), recorded in the ops entry; the snapshot is the defense record.
- DSR erasure (client data-subject request): erasure lane, no snapshot (no allowed retention); the audit marker IS the deletion certificate (operation_id + exact counts + hash chain), rendered by Coffee as proof-of-erasure.

### 10c.4 Export contract — "export everything about Acme" = one scope (binding; impl G3.1)

- **Surface:** owner-only MCP tool `smartware_export_scope { actor_id, scope, operation_id? }` → `export_id` (`exp_<ulid>`), package path (`exports/<export_id>/`), manifest summary. **Not a new protocol verb in v0.5.0** — a substrate tool over canonical records; a verb is a G4 decision if an adapter surface needs it.
- **One scope = exactly one boundary (binding):** every content record in the package has `scope = <target>`. The only cross-scope records are (a) ops entries in the operation-closure of exported records and (b) `forget.scope` audit markers for the target — which live in the POD scope by design (they reference the scope id; content is non-PII). Manifest asserts `scope_exclusive: true` + per-source counts; the acceptance test is re-import equivalence (G4).
- **Content (canonical records only; derived indexes — FTS, vector, pages, ops-index SQLite — are regenerable and excluded):**
  - observations: all rows with `scope = target` (any status), canonical shape;
  - claim version records: every version (incl. forgotten/tombstoned — history is portable), canonical shape;
  - evidence items referenced by those claims (`supporting_evidence` id closure);
  - ops entries: the operation closure (every operation_id referenced by the above + `forget.scope` audits for the target);
  - entity rows for the scope, marked **non-canonical** (entity identity is a derived projection — G2 finding; an importer MUST re-resolve entities).
- **Format:** `manifest.json` (protocol v0.5.0, schemas v0.5.0, export_id, scope, exported_at, actor_id, per-file counts + sha256 + aggregate) plus `observations.jsonl` / `claims.jsonl` / `evidence.jsonl` / `operations.jsonl` / `entities.jsonl` — canonical record shapes, one record per line.
- **Export-before-erasure (binding default):** the erasure flow runs F1 first — the snapshot is both the defensible record and the client's own data-rights copy; the export_id is linked in the erasure ops entry (`details.export_id`). DSR lane skips the snapshot (no retention allowed); audit marker alone.
- **Post-erasure export** of the erased scope returns an empty package + the deletion-certificate reference (marker id + operation_id) — proof-of-erasure is itself portable.
- **Portability forward-safety (binding):** the format is chosen FOR import. The G4 import verb's acceptance test: fresh Pod ← export → per-file counts equal + scope-visible recall equivalence (modulo entity re-resolution, G2 note). No import until that test exists.

### 10c.5 Return flow — fresh scope #2 + optional owner-approved pointer (binding)

- **Reopen = config provision:** mint `client:<id>#N` (N = max existing `#n` for that id + 1, n ≥ 1 — distinctness is structural, §10b.1) + repoint grants to the new exact ids. No protocol op (config-provisioned, per §10b.3); #N inherits nothing.
- **Pointer source = the offboarding audit marker** (pod-scope observation; `content.body.owner_pointer` — persisted, and rejected on erasure: t_357cd5fb). Never automatic: Coffee reads the marker, the owner reviews/edits the pointer, then seeds it as owner OBSERVE claims into #2 with `provenance.parent_ids = [audit marker obs id]`, `context = 'client-return-pointer'`; UI labels "carried from offboarding ⟨op id⟩ ⟨date⟩ — owner-approved ⟨ts⟩".
- **Pointer content rule (binding):** non-PII categorical ONLY — relationship length, job categories/count, satisfaction summary, disputes: none, preferred contact-free channels. No contact data, no documents, no raw conversation text, no descriptive free-text PII. Enforced in Coffee's pointer builder (typed fields); the substrate enforces scope only.
- **Post-erasure return:** #N minted; NO pointer, no history — UI states "no prior history — erased ⟨op id⟩ ⟨date⟩".

### 10c.6 Attribution rendering (F5; product rule — extends §10)

- Staff-facing only; never client-facing ("Maya said" is a loyalty liability, §10).
- Default ON: correctness events (corrections/revises/conflicts/superseded by a later version); freshness ≠ EXTRACTED (raw or FAILED — evidence is live, decision-relevant); version_at within the UI recency window (≤14d, presentation-level — §10a's state-based rule governs searchability, not presentation; no conflict).
- Default OFF (toggle): everything else.
- Render shape: "learned from ⟨source type⟩ ⟨date⟩; corrected ⟨date⟩" + one-line reason (freshness / correction / conflict).
- **Detailed contract (§10d):** exact defaulting predicate (consequential types/tags, stale/contested/low-confidence, 14d recency cap, 30d correction window), the normative wording table for unverified / EXTRACTED / FAILED, the client-facing denial matrix, and the reference renderer `src/render/provenance.ts` + exact-string oracle tests (33) — Coffee MUST render through it.

### 10c.7 Slotting — sequencing (dates = @user)

| Lane | Piece | State | Owner | Blocks / depends |
|------|-------|-------|-------|------------------|
| Substrate | FORGET.SCOPE, owner_pointer, revocation, markers, audits | SHIPPED (v0.5.0, G2) | tech-head | none |
| Substrate | `smartware_export_scope` + manifest | SHIPPED (G3.1) — owner-only, one-scope boundary, canonical package + manifest, idempotent per operation_id, export-before-erasure link (`details.export_id`) | tech-head | none — can ship before Coffee window |
| Substrate | import/restore verb (portability round-trip) | CONTRACT defined; impl = G4 decision | tech-head | after export impl; acceptance test defined |
| Substrate | explicit legal-hold marker | v1 = composition (offboarding + export + attestation); optional explicit marker = G4 decision | @user / tech-head | G4 |
| Coffee | F1–F5 UX (screens, gates, receipts, pointer builder, attribution) | DESIGNED; unimplemented (Coffee repo) | Coffee team | Coffee release window — @user dates |
| Coffee | client data-rights request intake (owner-mediated v1) | DESIGNED | Coffee team | Coffee window |

**Open @user decisions (surfaced, not decided here):** (1) Coffee release window per plan G3; (2) which flows ship in the launch cut vs later; (3) whether G3.1 export tool ships substrate-side now (recommended — cheapest, unblocks F1/F3 for any release); (4) legal-hold marker vs v1 composition.

## 10d. Provenance-rendering contract — attribution wording · why-toggle · freshness states (PRODUCT RULE — binding; F5 refinement of §10c.6)

**Context:** §7 closed the envelope at ≈0.05ms p95 — attribution is free at answer time, so it is a **Coffee product decision, not a perf constraint**. §10c.6 states the F5 rule (staff-facing only; default-on for correctness events, non-EXTRACTED freshness, ≤14d recency; toggle otherwise; "learned from ⟨source⟩ ⟨date⟩; corrected ⟨date⟩" shape). This section makes that rule **implementable and testable**: the exact defaulting predicate, the exact wording for `unverified` / `EXTRACTED` / `FAILED` (the three state-based freshness labels, §10a), the provenance states' punctuation-verified strings, and the client-facing denial matrix. Where they overlap, §10d governs detail (wording/predicate); §10c.6 governs the flow surface. **Executable form:** `src/render/provenance.ts` + `test/render/provenance-rendering.test.ts` (33 tests, exact-string oracle) — Coffee's rendering pipeline MUST use this module (dependency-free, importable as `smartware/render`); rendering through it is what keeps "Maya, May 12 → corrected by owner May 13" free from drift.

### 10d.1 Rendering authority — surface rules

- **Staff-facing (eligible):** recipient holds a Grant over the answer's scope, or is the owner. Full provenance MAY render (staff app, owner dashboard, internal search, handover notes, staff/owner exports).
- **Client-facing (never provenance chrome):** client portal, client-facing chat/bot, client email/SMS, exported client artifacts. **Staff/agent identity, staff statement dates, confidence, epistemic labels, freshness badges, correction history, and "why this answer?" MUST NEVER render** — "Maya said" is a loyalty liability, not a feature.
  - **Client-owned exception (MAY):** the client's *own* content may be cited as "From your messages, {date}" when it resolves ambiguity. Opt-in per answer type; never staff/agent attribution.
- **Exports:** staff/owner exports MAY carry full provenance. Client-facing data-rights export = client's own content + factual history **without internal staff actor metadata** (staff identity is internal; jurisdiction-specific disclosure = Coffee counsel item, flagged not resolved here).

### 10d.2 Defaulting — "default on for consequential/recently-changed; 'why this answer?' otherwise"

The "why this answer?" affordance exists on **every** staff-facing answer (envelope is free, §7). Auto-render (visible without the toggle) if **ANY**:

| # | Predicate (binding) | Rationale |
|---|---|---|
| 1 | `freshness ≠ extracted` (unverified/failed) | State-based freshness (§10a); never silent omission |
| 2 | `compileState = failed` | Never dressed as verified (§11.2) |
| 3 | `epistemic_tag ∈ {stale, contested}` | Decision-relevant; danger of trusting stale data |
| 4 | `confidence = low` | Unconfirmed = needs staff judgment |
| 5 | `claim_type ∈ {decision, constraint, handoff, correction}` | Consequential: commits the business |
| 6 | tag ∈ consequential set (`price`, `quote`, `appointment`, `booking`, `contact`, `address`, `payment`, `dispute`, `deadline`, `commitment`) | Client-commitment facts; Coffee MAY extend, never shrink |
| 7 | `version_at` within `recent_days` (default **14**, presentation cap per §10c.6 — may lower, never raise) | Recently-changed = most likely to matter / be wrong |
| 8 | `correctedAt` within `correction_visible_days` (default **30**) — the correctness events of §10c.6 | Trust in the fix: corrections stay visible a month |

Otherwise attribution renders collapsed under the toggle. Tunables are **Coffee product settings** — never substrate `config.json` fields (§10b untouched), never client-facing. Per-hit evaluation; a mixed answer renders the union.

### 10d.3 Wording (NORMATIVE — exact strings; templates, substitution only)

Dates: UTC month + day ("May 12"). Names: resolved display names (first name for persons, "Coffee AI" for agent, "the record" fallback) — **never raw actor ids** (`user:<slug>`) in staff UI. Corrections render the *fact* of correction, never the wrong content, never blame ("corrected", not "Maya was wrong").

| State | Badge | Attribution line | "Why this answer?" panel lead |
|---|---|---|---|
| EXTRACTED · person · fact | — | From Maya, May 12 | Learned from Maya, May 12. |
| **EXTRACTED · person · corrected** (flagship) | — | From Maya, May 12; corrected by owner May 13 | **Learned from Maya, May 12; corrected by owner May 13.** |
| EXTRACTED · agent · fact | — | From Coffee AI, May 12 | Coffee learned this on May 12. |
| EXTRACTED · agent · inference | — | Inferred by Coffee, May 12 | Coffee inferred this on May 12; it wasn't stated directly. |
| EXTRACTED · person · inference | — | From Maya, May 12 — inference | Learned from Maya, May 12 — an inference, not stated directly. |
| EXTRACTED · opinion | — | From Maya, May 12 — preference (agent: "Coffee noted this as a preference on May 12; not a fact.") | Learned from Maya, May 12 — recorded as a preference, not a fact. |
| EXTRACTED · stale | May be stale | From Maya, May 12 | Learned from Maya, May 12. Not updated since {version date}. |
| EXTRACTED · contested | Conflict | From Maya, May 12 | Learned from Maya, May 12. Sources disagree about this. |
| EXTRACTED · low confidence | Unconfirmed | From Maya, May 12 | Learned from Maya, May 12. Low confidence. |
| **unverified** (compile pending) | New | From Maya, May 12 | Learned from Maya, May 12; still being filed — may change. |
| **FAILED** (compile failed) | Not verified | From Maya, May 12 — not verified | Coffee couldn't verify this automatically; shown as received from Maya on May 12. Review before relying. |
| Client-facing · client-owned (MAY) | — | From your messages, May 12 | — (panel never renders client-facing) |

**Canonical demo:** "Learned from Maya, May 12; corrected by owner May 13." — staff-facing rendering of a claim carrying a `corrects`/`supersedes` edge (Maya's May 12 statement corrected by the owner May 13; the correction lineage, never a re-install of the wrong content).

### 10d.4 Honesty rules (anti-verification fraud)

- Unverified/FAILED content NEVER claims verification. FAILED wording is obligatory when `compileState=failed`, even though the RECALL hit label is `unverified` (§11.2 keeps failures raw-searchable with `unverified`; failing-vs-pending lives in compile state — ops receipts/queue status). **If compile state is unavailable, render pending wording — never guess.** (Substrate follow-up, OPEN: expose `compile_state` on raw-window hits; v0.5.1 candidate — needs tech-head + spec note before binding.)
- Derived ≠ verified: `epistemic_tag=inference` always carries inference wording; `opinion` renders as preference, never fact.
- Honest about evidence, never about people: "corrected", "not verified", "sources disagree" — no blame, no character claims.
- Render path = template + format: no LLM, no extra query (envelope inline per §7), no second network hop. Zero-latency stays a UI feature.

### 10d.5 Conformance anchor

`src/render/provenance.ts` + `test/render/provenance-rendering.test.ts` (33 tests) assert: flagship strings verbatim; predicate truth table incl. 14d/30d windows + custom-window honoring + client-surface false; freshness-state wording incl. FAILED-via-`compileState`; epistemic variants (person/agent inference, opinion, stale, contested, low confidence); client-facing denial matrix (client-owned citation only, everything else null); `formatMonthDay` UTC determinism; contract defaults. These tests fail if the product wording changes without a contract change — by design.

## 11. G0 spike evidence — verdicts and bindings (CLOSED 2026-08-29)

### 11.1 mem0 head-to-head (record: `mem0-h2h-recall.md`)

**Verdict (pre-agreed decision rule applied): mem0's fused retriever did NOT win. Fix Smartware hybrid internals — never adopt mem0's engine.**

Pinned protocol honored by both engines: `threshold=0.0`, `rerank=false`, `top_k=10`, same embedding model (BAAI/bge-small-en-v1.5 384d via fastembed — exact vector identity by construction), temporal axis separate. 18 scored queries / 33 memories, 1 tenant; mem0 ran with full BM25 path (spaCy + en_core_web_sm), `infer=False` ingest, warm-up excluded from timing.

| Axis | Smartware | mem0 OSS 2.0.19 |
|---|---|---|
| recall@1 / recall@5 / recall@10 | 0.806 / **1.000** / 1.000 | 0.806 / **1.000** / 1.000 (tie — curated set saturates; discriminates ranking/lifecycle) |
| hit@1 | 0.889 | 0.889 (tie) |
| **MRR** | **0.9444** | 0.9352 (+0.93%) |
| **NDCG@10** | **0.9590** | 0.9517 (+0.73%) |
| **Latency p95** (incl. query embedding; same box) | **131 ms** (engine-only 16 ms) | 308 ms (2.35× faster) |
| Provenance integrity | **200/200** hits → observation + ops entry | **none** (payload = data + metadata only) |
| Safety (staff query, sensitive payroll) | **0** forbidden hits (excluded pre-ranking) | 1 forbidden hit (no sensitivity concept in OSS) |
| Temporal axis (as-of / range / current) | **4/4** correct (incl. superseded history) | **N/A** — `reference_date` raises `ValueError` in OSS; platform-only (source + runtime verified). Range has no OSS surface |
| Per-query rank wins | 2 | 1 (15 ties; abstention neutralized by the `threshold=0.0` pin — recorded, not scored) |

**The one mem0 win (q03-entity-owner) is fully explained by two Smartware internals defects — the "fix internals" ruling is correct, not "their engine is better":**

- **D1 (lexical feed):** the RRF lexical channel is built from canonical RECALL order, which is **entity-aggregated** — exact entity-name match ("Project Aster") ranks above claim-level text match ("owns: Project Aster"). Claim-level FTS `searchClaims` ranks the right claim #1 (9.70 vs 6.66) — verified by measurement.
- **D2 (tiebreak):** `fuseHybridRankings` final tiebreak is `id.localeCompare` (alphabetical); semantic_relevance (0.8365 vs 0.7385) would flip the result, and never harms determinism.

**Fix path — BINDING (hybrid internals; prerequisites of any mem0-compat hybrid claims):**

1. Feed claim-level FTS ranks (`searchClaims`) as the RRF lexical channel (or expose a claim-ordered canonical variant for the hybrid lane).
2. Tiebreak `fuseHybridRankings` by `semantic_relevance` desc before `id`.
3. (Follow-up, not binding) candidate_limit / rrf_k / weights sweep via the activation harness — record only.

Expected outcome after 1+2: q03 → rank 1; MRR/NDCG edge widens. Honest caveat in the record: mem0's fused score-mixing is not worse at this task — our edge must come from score-ordering discipline + provenance/safety/temporal/latency, per the design posture (gap analysis §3c). Also corrected: gap analysis §6.4's "temporal ranking" claim is platform-only in OSS practice; our bitemporal axis is structural (see §8).

**Post-fix verification (2026-08-30, G2-launch run):** D1+D2 landed as bound and re-run on the pinned protocol with the same vectors — **q03 → Smartware rank 1 ✓** (`c_deliverable_aster` wins both channels: lex#1 sem#1, RRF 0.032787 vs 0.032258). Recorded caveat: at n=18 the aggregate MRR/NDCG is **unchanged** (0.9444/0.9590) because `q06-overdue-invoice` flipped to rank 2 — the two channels disagree there (bloom lex#1 sem#2 0.7890 vs incident_2 lex#2 sem#1 0.8063 → exact RRF tie, semantic channel's own leader is the wrong claim). q06's prior rank-1 was alphabetical luck, the same luck that broke q03. So the "edge widens" expectation is NOT demonstrated at this n; the weights sweep (item 3, non-binding) is now the measured next knob for exact-tie cases, not part of this launch.

### 11.2 compile-latency (record: `.spike/compile-latency/README.md`, `report.json`)

| Axis | Result vs target |
|---|---|
| **Sync-raw write** @50k obs | **VALIDATED** — p95 **1.868ms** (p50 1.035, p99 4.829, mean 1.197) vs <100ms; no LLM on write path; raw searchable before compile |
| **Async compile** @50k claims | **INVALIDATED** — **9,468,298ms** vs 5s target = **1,893.7× over** |
| Freshness semantics | **VALIDATED** at 5k/10k/50k and under forced failure (EACCES): raw searchable before compile (unverified), claims queryable after (EXTRACTED), forced failure → zero claims, raw stays searchable (FAILED); correctness asserts `claims_created == N`, `active_claims_after == N` passed at all scales |

- Growth: ms/claim 18.5 @5k → 31.4 @10k → **189.4 @50k** (10.2× for 10× claims = **O(N²)**). The 5s budget implies **≤0.1ms/claim**; current impl is ~3–4 orders of magnitude above — **a data-structure problem, not tuning**.
- Root cause (verified): per-claim fingerprint dedup (`findByFingerprint`/`findSemanticMatch` — scans growing claim log + active-claim set once per claim) plus two fsync-backed JSONL appends per claim.
- Bindings (also in §10a): (a) fingerprint→claim_id index (O(1) dedup), (b) batched appends (one fsync per N), (c) derived ops index is **load-bearing for the compile queue, not optional** — removes per-claim ops/idempotency scans; (d) explicit `unverified`/`EXTRACTED`/`FAILED` labels in the compile/recall payload contract; (e) re-run this spike after the fix before G2 closes.
- Method note: fresh fixture per scale, full `handleCompile` timed (extraction → version writes → L1 sync → L2 compile → search index → manifest), n=1 per scale, 4vCPU/7GB single box, Smartware dist v0.6.3, Node v26.5.1.

### 11.2b Post-fix re-run (G2, 2026-08-29 — record: `.spike/compile-latency/README.md` v2, `report-v2.json`)

| Axis | Result vs target |
|---|---|
| **Fix effectiveness** | Reference per-claim cost 189.4ms → **0.49ms @50k (constant across 5k/10k/50k)** — the O(N²) growth curve is eliminated; 50k compile **24,383ms vs 9,468,298ms = 388x faster** |
| **≤5s @50k budget** | **NOT MET** — 24.38s (4.88x over). Residual is per-item real work (extraction + fingerprint hash + SQLite rows + L3 claim FTS + L2 synthesis + git + evidence parses), no longer a data-structure pathology |
| **Sync-raw write @20k (incl. obs FTS + enqueue)** | **VALIDATED** — p50 1.30ms, p95 2.449ms, p99 7.477ms vs <100ms (was 1.868ms p95 pre-index/enqueue) |
| **Freshness labels (literal)** | **VALIDATED** — `telemetry.freshness {unverified,extracted,failed}` on the compile payload; ops receipts carry `freshness` label; forced failure (EACCES) → raw stays searchable with `unverified`, zero claims |
| **Compile-queue path** | durable SQLite ledger + O(1) fingerprint index + batched append primitives landed; worker drains per-observation with per-job fault isolation (FAILED marked, raw never hidden) |
| **Re-scope exit (2026-08-30, owner @tech-head)** | **Full pipeline-and-wiki synthesis ≤10s @50k = MET: 9,493.5ms** (0.19ms/claim; n=4: 9,445/9,493.5/9,619.7/9,682ms; 443/443 tests, tsc clean). Levers, verification and honest notes in **§11.2c** below. The ≤5s @50k budget remains assigned to the async claim-production path (per-claim 0.49ms, §11.2b first row) |

What landed (bindings a–d of §10a/§11.2, code-verified): (a) fingerprint→claim_id derived index (O(1) dedup), (b) batched appends (`appendOpLogEntries`, `appendClaimVersions`; per-claim reflect intents removed — recovery already treats reflect.auto without artifact as safe-to-recompute), (c) derived ops index wired as the compile-queue dependency (self card t_f0a0702e), (d) explicit `unverified`/`EXTRACTED`/`FAILED` labels in the compile payload contract + RECALL raw-window (`searchObservations` freshness per hit), plus two measured fixes beyond the bindings: freshness moved off the FTS5 row (meta table — FTS5 UPDATE/DELETE cost 2.3–3.6ms/row) and cached statements + single-transaction store sync.

**Where the next 4.9x must come from if the budget is enforced:** defer L2 synthesis + the scoped claim-FTS rebuild out of the synchronous handler (compile queue already does per-observation claim production), shard/parallelize the evidence parse, and batch the L1 store sync with hand-rolled multi-row inserts. Honest verdict: per-claim floor for full pipeline ≈0.3–0.5ms without restructuring; the 5s @50k "≤0.1ms/claim" arithmetic only holds for the async claim-production path, not full pipeline-and-wiki synthesis.

### 11.2c Re-scope verification (G2-sub, 2026-08-30 — owner @tech-head; record: `.spike/compile-latency/README.md` v3, `report-v3.json`, `bench-v3.mjs`, `defer-check.mjs`)

**Recorded re-scope (2026-08-29, owner tech-head):** async claim-production path ≤5s @50k = MET (per-claim 189.4ms → 0.49ms, 388×; §11.2b). Full pipeline-and-wiki synthesis re-scoped to **≤10s @50k** via the levers: parallel claim production, batched fsync, deferred wiki synthesis. This section records the re-run against that target and the final exit.

| Axis | Result vs target |
|---|---|
| **≤10s @50k full pipeline-and-wiki synthesis** | **MET** — 9,493.5ms vs 10s (0.1899ms/claim; 2.57x under the old 24.38s record). n=4 clean runs (2 full bench-v3, 2 stage-profile): 9,445 / 9,493.5 / 9,619.7 / 9,682ms @50k — all under the 10s line (variance ±1s on this 2-vCPU box; full suite 443/443 + tsc clean) |
| **Per-claim curve** | **constant ~0.19–0.23ms** across 5k/10k/50k (1,129.3 / 1,946.4 / 9,493.5ms) — no growth curve; the residual is per-item real work (extraction + fingerprint hash + SQLite rows + L1 store rows + L3 claim FTS + L2 synthesis + git commit) |
| **Sync-raw write @20k (incl. obs FTS + enqueue)** | **VALIDATED** — p50 1.145ms, p95 2.366ms, p99 7.035ms vs <100ms (improved from 2.449ms p95 in v2) |
| **Freshness + forced failure** | **UNCHANGED VALIDATED** — EACCES → raw stays searchable (`unverified`), zero claims; `freshness {unverified:0, extracted:50000, failed:0}` @50k |
| **Deferred synthesis path (`defer_synthesis`)** | **NEW VALIDATED** — handler returns `pages_compiled: 0`, `telemetry.synthesis_deferred: true`, claims 5,000, freshness EXTRACTED, L3 claim FTS rows 5,000 + searchable before the L2 step; deferred L2 step produces the page and git commit. 5k: handler 683ms + synthesis 527ms |

What landed this re-run (the recorded levers, code-verified):

1. **Batched fsync / batched SQLite**: fingerprint-index upserts now accumulate in a batch overlay with same-run dedup semantics and flush in ONE transaction (was 50k autocommit transactions ≈13% of the pipeline). L1 store sync moved to chunked multi-row INSERT (200 rows/stmt, one transaction) with a per-batch entity-resolution memo and elided getClaim/getEntity SELECTs for known-new claim ids — per-row sync was ~20% of the pipeline.
2. **Single evidence read**: the 48MB evidence JSONL was parsed 3× per compile (reflect production, L2 gather, L2 reconcile). It is now parsed once in `handleCompile` and threaded through `reflectAutoCreateClaims`, `compile()` and `replayCatchUp()` — was ~8% of the pipeline.
3. **Statement caches**: `Layer0Index` gained a prepared-statement cache (getEffectiveStatus/insertOrSkip/mutation replay did `db.prepare` per call — 100k+ per compile, the #1 self-time frame at 16.6%); `SearchIndex.replaceClaimIndex` statements cached; effective-status lookups replaced by a single `SELECT id, effective_status` snapshot Map used by gather + replay.
4. **Deferred wiki synthesis** (`CompileParams.defer_synthesis`, `CompileTelemetry.synthesis_deferred`): keeps the queue-worker architecture honest — the synchronous handler contract is claim production + L1/L3 + freshness + manifest; L2 page synthesis + git commit run as a separate step. Verified by `defer-check.mjs`.
5. **Fingerprint dedup snapshot**: batch mode loads the claim_state into memory once, so per-claim `activeByFingerprint` is a Map lookup (fresh-pod compile = zero SELECTs).

Honest scope note: "parallel claim production" as worker-thread parallelism was NOT taken — the residual at 9.5s is not CPU-bound per-claim work extractable by threads on this 2-vCPU box; the measured wins came from batching (fsync/transaction elimination) and single-read restructuring. If another 2-3x is ever required, the next lever is genuinely sharding evidence parse + claim production across cores, then deferring L2 synthesis unconditionally (already available via `defer_synthesis`).

## 12. Drafting history

- v0.1 (2026-08-29): initial draft from gap analysis; shape agreed with tech-head (separate doc) / smarty-pants (history contract, byte-clean rule, benchmark protocol).
- v0.2 (2026-08-29): `reset` LOCKED to snapshot+quarantine (precedent mem0 #3928 / PR #4349 per @smarty-pants); `delete_linked=true` mapping added with strict `cascade_count` fidelity; relation edges tombstoned on linked delete; verification extended.
- v0.3 (2026-08-29): Coffee frame adopted (Stevie) — mem0-compat demoted to reach; §7 provenance envelope CLOSED via tech-head spike (≈0.05ms; derived SQLite ops index required for ops payloads); §10 Coffee tenant binding added: clients-as-scopes, FORGET.SCOPE (erasure vs offboarding, lane-exhaustive purge), non-reusable `client:<id>#n` markers, staff-facing attribution rule.
- v0.4 (2026-08-29): FORGET.SCOPE versioning RESOLVED — protocol v0.5.0 core intent (per smarty-pants conformance-semantics test); single ops-log entry + counts, same-commit grant revocation, lane-exhaustive purge are normative for every implementation; v0.4.x gets a migration note (five-verb backward compat, scope-erasure non-conformant).
- v0.5 (2026-08-29): `add` compile-default RESOLVED — sync-raw + async-compile (per smarty-pants/tech-head); v0.5.0 cut scope bound: observation FTS index + derived ops index (both are compile-queue prerequisites, not follow-up), state-based freshness window (`unverified`/EXTRACTED/FAILED semantics in RECALL payload), rebuild-equivalence conformance incl. FORGET.SCOPE against REBUILT indexes.
- v0.6 (2026-08-29): remaining spec items RESOLVED — MCP namespace = option-gated namespace on existing Smartware MCP server (default off); mem0 CLI ships in compat package only; adapter hosting = separate `smartware-mem0-compat` package with own semver. All §9 items closed except deferred QM appendix.
- v0.7 (2026-08-29): **DRAFT → NORMATIVE.** G0 verdicts baked in (§11): mem0 head-to-head — mem0's fused retriever did NOT win (recall@k ties, Smartware leads MRR/NDCG, latency 2.35×, provenance 200/200, safety, temporal); the single mem0 win (q03) traced to Smartware internals defects D1 (entity-aggregated lexical feed) + D2 (alphabetical tiebreak) with concrete BINDING fix path. Compile-latency: sync-raw write p95 1.868ms @50k VALIDATED, async compile 9,468,298ms @50k INVALIDATED (1,893.7× over 5s; O(N²) fingerprint dedup + per-claim fsync) → fingerprint index + batched appends bound to the v0.5.0 compile-queue cut; explicit `unverified`/`EXTRACTED`/`FAILED` labels bound in the compile/recall payload contract; spike re-run required before G2 closes. Records: `mem0-h2h-recall.md`, `.spike/compile-latency/`.
- v0.8 (2026-08-29): **Coffee tenant config shape bound (§10b).** One business = one tenant = one Pod (`config.json` + scopes + grants); `client:<id>` under `workspace` with `visibility_default: 'scope'`; non-reusable `client:<id>#n` markers; staff = Grant capability clusters expressed as **exact scope-id lists** (verified: `client:*`, `client/*`, `client:acme#*` do NOT match — `scopeMatches` supports exact ids, `*`, and `prefix/*` slash wildcards only); versioned distinctness is structural (grant on `#2` never authorizes `#1`). Code-verified against dist v0.6.3 (harness `scripts/verify-config-shape.mjs`); worked example `docs/competitive/coffee-tenant-config.example.json`. One seam flagged: v0.5.0 schema set must widen `common.schema.json` `Scope` pattern to admit `client:<id>` + `#n` — schema widening only, no protocol-semantics change (§10b.6).
- v0.9 (2026-08-29): **G2 compile-queue closure (§11.2b).** Bindings a–d landed and code-verified: O(1) fingerprint→claim_id derived index, batched claim/ops appends, ops index wired for compile intent matching, literal `unverified`/`EXTRACTED`/`FAILED` labels in the compile + RECALL payload. Re-run: per-claim cost constant at ~0.49ms @50k (was 189.4ms, O(N²) curve eliminated; 50k compile 9,468,298ms → 24,383ms = 388x), sync-raw write p95 2.449ms incl. obs-FTS indexing + durable enqueue (<100ms budget = VALIDATED), forced-failure semantics verified (raw stays searchable, `unverified`, zero claims). ≤5s @50k full-pipeline budget NOT met (4.88x over) — residual is per-item real work; the deferred-path note and next levers are recorded in §11.2b. Observation-index perf structure changed to a meta-table + rowid-delete design (measured FTS5 row updates at 2.3–3.6ms/row; FORGET.SCOPE purge and freshness transitions both stay O(log n)).
- v0.10 (2026-08-29): **Protocol v0.5.0 released (G2 docs card, t_51c0d770).** Conformance surface = five verbs + FORGET.SCOPE; `docs/protocol/smartware-protocol-v0.5.0.md` ships with the change-history migration note (v0.4.x servers backward-compatible on the five verbs, non-conformant on scope-erasure — NOT a break; mem0 v2→v3 churn cited as the anti-pattern); `schemas/v0.5.0/` released with it (contract + schemas ship together, mismatch blocks conformance) — `Scope` widened per §10b.6 in the strict no-leading-zero form (n ≥ 1), `operation-log-entry` op enum gains `forget.scope`, new `forget-scope-request.schema.json`. Conformance-status updated to v0.5.0 target with FORGET.SCOPE evidence; schema checksums verified for both v0.4.2 (retained, five-verb claims) and v0.5.0.
- v0.11 (2026-08-29): **Coffee client-scope flows bound (§10c, G3 card t_cd39a364).** Five flows on the FORGET.SCOPE substrate: F1 export-one-client (`smartware_export_scope` — owner-only, one scope = exactly one boundary, canonical-record package + manifest, export-before-erasure default, post-erasure export returns the deletion certificate; impl = G3.1; import verb = G4 with re-import-equivalence acceptance test); F2 offboarding + F3 erasure (SHIPPED v0.5.0); F4 return (config-mint `#N` + owner-approved non-PII pointer seeded from the offboarding audit marker into #2 with provenance link); F5 staff-facing attribution rendering rule (default-on for correctness events / non-EXTRACTED freshness / ≤14d, toggle otherwise). **Erasure-vs-legal-hold binding:** a dispute never triggers erasure — hold lane = offboarding + export snapshot, erasure only after owner attestation. Dates remain an @user decision (plan G3) — §10c.7 slotting matrix surfaces the four open decisions.
- v0.12 (2026-08-29): **Provenance-rendering contract bound (§10d, G3 card t_bf5839cd — F5 refinement).** §10c.6's rule made implementable: rendering authority matrix (staff-facing only; client-facing denial + client-owned "From your messages, {date}" exception; exports split), exact defaulting predicate (state-based freshness, failed compile, stale/contested/low-confidence, consequential claim types + tag set, 14d recency cap per §10c.6, 30d correction visibility), NORMATIVE wording table (badges New/Not verified/May be stale/Conflict/Unconfirmed; attribution lines; why-sentences incl. the flagship "Learned from Maya, May 12; corrected by owner May 13."), honesty rules (FAILED wording via compileState even when the hit label is unverified — no guessing, never dress failures as verified; inferences/opinions never rendered as fact), and a conformance anchor: `src/render/provenance.ts` + `test/render/provenance-rendering.test.ts` (33 exact-string tests, all passing; tsc clean). OPEN follow-up flagged §10d.4: expose `compile_state` on raw-window RECALL hits (v0.5.1 candidate, needs tech-head).
- v0.13 (2026-08-29): **G3.1 SHIPPED — `smartware_export_scope` implemented (spec §10c.4).** Owner-only MCP tool (mcp.ts + CLI bundle) → `{ export_id (exp_<ulid>), path, counts, manifest }` under `<data_dir>/exports/<export_id>/`; files: observations/claims/evidence/operations/entities .jsonl + manifest.json (protocol v0.5.0, schemas v0.5.0, per-file counts + sha256 + aggregate, scope_exclusive assertion). ONE-boundary enforced: content records are scope-filtered; cross-scope allowed only for operation-closure ops entries + forget.scope audit markers (pod scope). Erased-scope export = empty package + deletion-certificate reference (marker obs id + erasure operation_id); offboarding export = full history (forgotten claim versions included). Idempotent per operation_id (deterministic export_id via SHA-256 → Crockford base32; retry returns same package + stable manifest; conflict on reuse with different scope/actor). Zero-touch verified (evidence/claims/ops/config byte-identical). FORGET.SCOPE additive change: optional `export_id` (erasure lane only) surfaced in ops-entry details. 9 new tests (test/protocol/export-scope.test.ts); full suite 440/440 green, tsc clean.
- v0.14 (2026-08-30): **§11.2c RE-SCOPED BUDGET VERIFIED (G2-sub, owner @tech-head).** Full pipeline-and-wiki synthesis **≤10s @50k = MET: 9,493.5ms** (0.1899ms/claim; n=4 clean runs 9,445/9,493.5/9,619.7/9,682ms — all under 10s; 2.57x under the re-scope; vs §11.2b's 24,383ms). Levers landed: fingerprint upserts in one batch transaction + in-memory dedup snapshot; L1 store sync as chunked multi-row INSERT with entity memo + known-new elision; single evidence parse threaded through reflect/gather/replay (was 3×, ~8%); Layer0 prepared-statement cache + one-query effective-status snapshot (was 100k+ per-call prepares, 16.6% self-time); deferred wiki synthesis (`defer_synthesis`, `synthesis_deferred`) with L3 claim window in the deferred handler contract. Constant per-claim curve ~0.19–0.23ms @5k/10k/50k; sync-raw write p95 2.366ms; forced-failure semantics unchanged. Honest note: worker-thread parallelism NOT taken — residual is per-item real work, not threadable CPU; next lever if more is needed = shard evidence parse/claim production, or defer L2 synthesis unconditionally.
- v0.15 (2026-09-15): **Grant granularity at the FORGET.SCOPE boundary bound (§10b.1/§10b.2, ADR-0012 — card `t_f2b584dc`).** Row-scoped revocation stays exactly as released (§10b.3, protocol v0.5.0 unchanged); provisioning issues **one grant row per (staff actor, client scope)**, so the revocation boundary equals the intended access boundary. Measured on the public adapter surface (evidence `/opt/data/workspaces/brain-pilot-evidence/adr-0012-20260915T110900Z/`): the single-row shape (`user:sam` on meridian + arcadia) → erasing arcadia revoked the row whole and flipped the meridian recall `ok` → `403 insufficient_permission` (re-derives gate check 6n); per-client rows → `grants_revoked` = exactly the arcadia row, the meridian row stays `active`, meridian recall unchanged with **no re-provisioning**; the standby/degraded precheck under the new shape denies a scope the actor holds a second row for (`.find()`, first match). Re-grant requirement stated in the adapter contract (`docs/integration/coffee-adapter.md` §7) and carried verbatim into the Coffee release handoff. Two adapter consequences are named as required companions of the decision — degraded-path precheck must union the actor's active rows, and `coffeeTenantConfig` must emit the per-client shape (plus one substrate call site, see the round-2 amendment below) — implementation card `t_864a5900`, before the release candidate is packed; §10b.4's example, the mirror config and `scripts/verify-config-shape.mjs` are re-shaped and re-verified in that same card. **Round-2 amendment to this entry** (same card `t_f2b584dc`, after the artifact review requested changes): the §10b.2 rule "first-match lookups are never an authorization decision" now names its one **live** exception — session capability/trust resolution (`src/session/policy.ts:96`, behind `smartware_session_start`) reads the first row, measured to report no capabilities for a client held in a second row (S4, same evidence dir) — and that fix is folded into `t_864a5900` as companion change 3 of ADR-0012 §4 with test C6; §10b.3 gained the exact-id revocation-matching note (a `*` row authorizes a scope it is not revoked with; asserted by C2). Both are decision-text changes only; the substrate is untouched by this card.
