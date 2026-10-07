# Open Recall Protocol (ORP) 0.1

**Status:** Draft for review · **Editor:** Steve Ghiassi, Techno Ventures · **Date:** October 2026
**Working name.** "Open Recall Protocol" is a placeholder until a name is chosen.

ORP is an open protocol for how AI agents and SaaS platforms **store, govern and recall** memory and context. It defines a memory record, a small set of verbs, normative recall semantics, an access-control model that holds up in multi-tenant products, transport bindings for MCP and HTTP, and a conformance and evaluation suite. A reference engine and benchmark harness ship with the spec.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are used as in RFC 2119.

---

## 0. Why this exists

We read the source of 15 leading open-source memory and "brain" projects (Graphiti/Zep, Hindsight, mem0, Letta, Supermemory and its Company Brain, GBrain, Onyx, Cognee, Basic Memory, Sylph, the MCP reference memory server, MemOS, LangMem, MemoryOS) and benchmarked faithful reimplementations of their recall paths under identical conditions. The findings that shape this spec:

1. **There is no shared contract.** Every project invents its own record, scope model and search API. Memory written by one agent stack cannot be read, governed or audited by another.
2. **Time is mostly missing.** Only Graphiti models validity windows; most store one timestamp and push temporal reasoning onto the LLM. Several compute recency from wall-clock "now", so the same query gives different answers on different days.
3. **Supersession is invisible.** Contradiction handling is either absent (mem0 OSS drops the LLM's links; Cognee writes validity tags no retriever reads) or decided by similarity thresholds the caller never sees.
4. **Access control is bolted on, often after ranking.** We found post-limit filtering (Letta tags, Hindsight graph arm, Supermemory list), cross-tenant reads by id (mem0 REST, MemOS `/product`), shared assistant stores (MemoryOS) and no defined rule for summaries built from private sources.
5. **Recall returns lists, not context.** Few projects budget tokens, explain why an item was returned, or tell the agent when nothing relevant exists.
6. **Knobs lie.** We found accepted-but-ignored parameters in at least six projects (`hybrid_alpha`, `reference_time`, `visibility`, recency multipliers, MCP `filter`).
7. **Benchmarks are not reproducible.** Headline LoCoMo/LongMemEval numbers mostly come from hosted engines, unpinned external harnesses, generous judges or tuning on the scored set.

ORP turns the best mechanisms we found into requirements, and turns each failure into a MUST NOT.

## 1. Design principles

| # | Principle | Learned from |
|---|---|---|
| P1 | **Evidence first.** The raw tier (what was actually said or written) is lossless and always retrievable. Derived memories (facts, observations, summaries, profiles) point to their evidence. | MemOS deleting raw windows; MemoryOS dropping turns; Supermemory two-layer results |
| P2 | **Three clocks.** Every record separates when it was *observed*, when the thing *happened*, and when the assertion is *valid*. Queries carry an `as_of`. | Graphiti bi-temporal edges; Hindsight `mentioned_at`; Basic Memory `@effective` |
| P3 | **Access before ranking, closed under provenance.** Admissibility is decided before any candidate is scored, and a derived memory is never more visible than its least visible source. | Onyx ACL pre-filter; Company Brain containers; leaks listed above |
| P4 | **Recall returns a budgeted, explained ContextPack.** | GBrain context budgets; Hindsight token budgets; Letta score provenance |
| P5 | **Every parameter is honoured or rejected.** Capabilities are discoverable. | Ignored knobs listed above |
| P6 | **Writes are idempotent, versioned and concurrency-safe.** | Letta shared-block lost updates; MemOS duplicate retries; Basic Memory checksums |
| P7 | **Supersession is explicit, queryable and reversible.** | Graphiti invalidation; Supermemory version chains |
| P8 | **Portable and human-readable.** A lossless export bundle and a Markdown rendering are part of the protocol. | Letta `.af` gaps; Basic Memory / Sylph files-as-memory |
| P9 | **Evaluation is part of the protocol.** Conformance tests and a pinned benchmark ship with the spec. | Non-reproducible claims across the field |

## 2. Terminology

- **Memory**: one ORP record (§3).
- **Unit**: the smallest addressable piece of evidence (a message, a turn, a document section).
- **Thread**: an ordered container of units (a conversation, session, channel, document).
- **Principal**: the identity on whose behalf a request is made (user, agent, service), with group memberships.
- **Access token**: a string a principal holds (`principal:u7`, `group:sales`, `channel:C123`, `public`).
- **Tenant**: the hard isolation boundary (an organisation or customer). Nothing crosses tenants.
- **Scope**: a hierarchical path inside a tenant (`org/acme/team/sales/user/u7/agent/support-bot`), used for routing, quotas and soft preference, never as the only access control.
- **ContextPack**: the response to a recall: budgeted, ordered, cited context ready to place in a model's window.
- **Engine**: an implementation of ORP. **Client**: an agent, app or connector calling it.

## 3. Data model

### 3.1 The Memory record

| Field | Type | Req | Meaning |
|---|---|---|---|
| `id` | string | MUST | Stable, engine-assigned or client-supplied, unique per tenant. Never a display name. |
| `tenant` | string | MUST | Hard isolation boundary. |
| `kind` | enum | MUST | See §3.2. |
| `content` | string | MUST | Human-readable text. For structured kinds, a faithful rendering of `claim`. |
| `claim` | object | MAY | `{subject, predicate, object, cardinality: one\|many}` for facts and preferences. |
| `scope` | string | SHOULD | Hierarchical path (§2). |
| `readers` | string[] \| null | MUST | Access tokens that may read. `null` = every principal in the tenant. |
| `writers` | string[] \| null | SHOULD | Tokens that may modify or forget. Defaults to the creator. |
| `actor` | string | SHOULD | Who said or asserted it (speaker, agent id, connector). |
| `role` | enum | SHOULD | `user \| assistant \| system \| tool \| human \| connector`. |
| `origin` | enum | MUST | `observed` (raw evidence) · `extracted` (derived by a model or rule from evidence) · `inferred` (concluded, not stated) · `human` (written or approved by a person) · `imported` · `seed`. |
| `thread`, `seq` | string, int | SHOULD | Container and position, so neighbours can be fetched. |
| `observed_at` | timestamp | MUST | Transaction time: when the engine learned it. Engine-set, never authored. |
| `event_time` | interval | SHOULD | When the described thing happened: `{start, end, granularity}`. |
| `valid_from`, `valid_to` | timestamp | SHOULD | World-time validity of the assertion. Open-ended `valid_to` = still valid. |
| `expires_at` | timestamp | MAY | Retention limit. After it the record is `expired`. |
| `status` | enum | MUST | §3.3. |
| `supersedes`, `superseded_by` | id[], id | MUST when applicable | Explicit version links (§5.6). |
| `version` | int | MUST | Increments on every change; used for `if_version` concurrency. |
| `sources` | Source[] | MUST for non-`observed` | `{uri, span, hash, observed_at}`: where the evidence lives (`slack://C1/p17`, `file://notes/a.md#L10-14`, `orp://t/mem_x`). |
| `derived_from` | id[] | MUST for `extracted`/`inferred` | Memories this was derived from. Drives ACL closure and cascade. |
| `support` | int | MAY | Count of independent sources agreeing. |
| `confidence` | number 0..1 | MAY | Engine's or author's confidence. Declared scale. |
| `labels` | string[] | MAY | Sensitivity (`pii`, `secret`, `health`, `legal`) and topical labels. |
| `load` | enum | MAY | `always \| on_scope \| on_query` (default). Profiles, standing rules and identity are `always`. |
| `meta` | object | MAY | Free-form, namespaced keys (`x-acme-...`). Engines MUST round-trip it. |

Embeddings and index structures are **not** part of the record. They are engine internals and MUST be rebuildable from records.

### 3.2 Kinds

`event` (raw unit: message, turn, edit, tool call) · `document` (raw section of a file or page) · `fact` · `preference` · `procedure` (standing rule, skill, instruction) · `observation` (consolidated pattern over many sources) · `summary` (compression of a thread or scope) · `profile` (query-independent context about a subject) · `entity` (a person, project, product, place, with aliases).

Engines MAY add kinds as `x-<name>`. Recall requests MAY set per-kind quotas (§5.4).

### 3.3 Status state machine

```
draft ──approve──► active ──newer valid claim──► superseded
  │                  │  ▲                              │
  └──reject──► (gone)│  └──────────restore─────────────┘
                     ├──forget(retract)──► retracted  (hidden, auditable, blocks re-ingest)
                     ├──forget(redact)───► retracted + content erased
                     ├──forget(purge)────► removed (tombstone id kept)
                     └──expires_at──────► expired
```

`draft` memories (agent proposals awaiting human approval) are never returned by recall unless the request asks for drafts and the principal may approve them.

### 3.4 Entities and edges

Entities are memories of kind `entity` with `aliases[]` in `meta.aliases` and a normalised match key. Typed edges are lightweight records `{id, type, from, to, valid_from, valid_to, sources}` with `type ∈ {about, mentions, part_of, follows, causes, supports, contradicts, supersedes, derived_from, relates}` plus `x-` extensions. Edges carry their own validity and ACL (closure rule in §6.3 applies: an edge is readable only if both endpoints are).

### 3.5 Threads

A thread is identified by `thread` on its units. Engines MUST be able to return a unit's neighbours (`before`/`after` cursors) and whole threads, because answers in conversation often sit in the turn after the most "relevant" one (§5.5).

## 4. Verbs

All verbs take a `principal` (bound server-side from credentials, never from model-supplied arguments) and a `tenant`. All responses carry `degraded[]` (§5.8) and `request_id`.

| Verb | Purpose | Idempotent | Destructive |
|---|---|---|---|
| `capabilities` | Discover versions, kinds, channels, ranking parameters honoured, limits | yes | no |
| `remember` | Write one or many memories | with `idempotency_key` | no |
| `recall` | Get a ContextPack for a query | yes (same `snapshot`) | no |
| `profile` | Get `load: always` context for a subject, budgeted | yes | no |
| `get` / `get_many` | Read records by id (ACL enforced) | yes | no |
| `neighbours` | Units before/after a unit in its thread | yes | no |
| `history` | Version chain of a claim key or memory | yes | no |
| `link` | Create or end a typed edge | with key | no |
| `forget` | Retract, redact or purge, with cascade and `dry_run` | yes | **yes** |
| `feedback` | Report use, correctness, or a draft→final edit | with key | no |
| `reflect` | Start a consolidation job (observations, summaries, profiles, rule candidates) | with key | no |
| `job` | Status of async work (ingest, reflect, re-index) | yes | no |
| `changes` | Ordered change feed after a cursor (sync, subscriptions) | yes | no |
| `export` / `import` | Lossless bundle (§8.3) | yes | import may be |

### 4.1 remember

```json
{
  "memories": [{
    "kind": "event", "content": "I moved to Lisbon last month.",
    "actor": "Maya", "role": "user", "thread": "slack:C07/1712345", "seq": 41,
    "event_time": null, "readers": ["channel:C07"], "origin": "observed",
    "sources": [{"uri": "slack://C07/p1712345000041", "hash": "sha256:..."}]
  }],
  "mode": "append",
  "idempotency_key": "slack:C07:p1712345000041",
  "if_version": null,
  "wait": "indexed"
}
```

- `mode`: `append` (default) · `upsert` (by client id, requires `if_version` when the record exists) · `supersede` (explicitly replaces `supersedes[]`) · `propose` (creates `draft` for approval).
- Engines MUST be idempotent on `idempotency_key` (or, absent one, on `(tenant, thread, actor, content-hash)` within a declared window) and MUST return the original ids on replay.
- Engines MUST reject unknown top-level fields and unsupported enum values with `unsupported_parameter`, not ignore them (P5).
- `wait`: `accepted \| stored \| indexed`. The response states which stage was reached, so clients get read-your-writes when they need it.
- Engines SHOULD resolve relative time expressions against `observed_at` at write time and store the absolute value (e.g. "last month" observed on 2026-05-12 → `event_time ≈ 2026-04`), keeping the original text.
- `origin: observed` records are evidence and MUST NOT be rewritten by consolidation.

### 4.2 recall

Request:

```json
{
  "query": "Where does Maya live now?",
  "queries": [{"text": "Maya address city", "weight": 0.5}],
  "as_of": "2026-06-30T00:00:00Z",
  "known_at": null,
  "scope": {"prefer": ["org/acme/team/growth"], "within": "org/acme"},
  "kinds": {"fact": 10, "event": 30, "observation": 5},
  "budget": {"tokens": 1500, "items": 50},
  "granularity": "auto",
  "include": {"history": false, "drafts": false, "neighbours": "auto", "explain": true},
  "time": {"window": null, "order": null},
  "mode": "balanced",
  "min_coverage": null,
  "cursor": null
}
```

- `as_of` defaults to the request time. It sets the validity view (§5.2) **and** the "now" for all relative reasoning and recency. Engines MUST NOT use wall-clock time when `as_of` is given.
- `known_at` answers "what did we know then?" by excluding records observed after it.
- `scope.within` narrows admissibility; `scope.prefer` is a soft prior that MUST NOT filter.
- `kinds` is a per-kind quota map or list.
- `budget.tokens` is a hard cap on the rendered pack. Engines MUST report the tokeniser they count with.
- `granularity`: `unit \| span \| thread \| auto`. `auto` lets the engine return dialogue spans (§5.5).
- `mode`: `fast` (no second pass), `balanced` (default), `deep` (may use model-based reranking or query decomposition; MUST declare it in `explain`).

Response (ContextPack):

```json
{
  "items": [{
    "ref": "orp://acme/mem_9f..",
    "memory_ids": ["mem_9f..", "mem_a0.."],
    "kind": "event",
    "text": "## 2026-05-12 (Tue) · #growth\nMaya: I moved to Lisbon last month. (last month = April 2026)",
    "tokens": 31,
    "score": 0.83,
    "why": {"lexical": 0.016, "dense": 0.011, "thread": 0.006, "entity": 0.004, "features": {"coverage": 0.67, "actor": 1}},
    "validity": "current",
    "event_time": {"start": "2026-04-01", "end": "2026-04-30", "granularity": "month"},
    "citations": ["slack://C07/p1712345000041"],
    "superseded": []
  }],
  "tokens": 1432,
  "budget": {"tokens": 1500},
  "coverage": 0.71,
  "abstain_hint": false,
  "snapshot": "snap_7c1e..",
  "degraded": [],
  "notes": ["time window 2026-04-01..2026-04-30 inferred from 'last month'"]
}
```

### 4.3 profile

Returns `load: always` memories for a subject (user, agent, team) within a token budget, ordered by kind priority (`procedure` → `preference` → `profile` → `fact`), each line individually citable and forgettable. Unbounded ambient injection is not allowed: `budget.tokens` is REQUIRED.

### 4.4 forget

```json
{"ids": ["mem_9f.."], "mode": "retract|redact|purge", "cascade": true, "dry_run": true, "reason": "user request"}
```

- `dry_run: true` MUST return the full set of ids that would be affected (including cascaded derived memories) and change nothing.
- `cascade` retracts or re-derives every memory whose `derived_from` includes an affected id, transitively. A summary MUST NOT keep facts its sources no longer support.
- `retract` blocks re-ingestion of the same content hash unless the request sets `allow_reingest`.
- Forget on records readable by others REQUIRES `writers` permission, not just read permission.
- Over MCP, engines SHOULD use elicitation to confirm destructive forgets (§8.1).

### 4.5 feedback

```json
{"target": "mem_9f..", "signal": "used|helpful|wrong|outdated|draft_final",
 "draft": "...", "final": "...", "actor": "u7", "note": "..."}
```

`draft_final` pairs are stored server-side as evidence. Engines MAY derive `procedure` rule candidates from them, which MUST be created as `draft` and require approval before `active`. Feedback MUST NOT silently change the ranking algorithm; its effect is a declared, versioned ranking feature (§5.4).

### 4.6 reflect

Starts an async job that produces derived memories (`observation`, `summary`, `profile`, rule candidates). Requirements: idempotent per `(scope, cursor)`; outputs carry `derived_from` and `origin: extracted|inferred`; outputs inherit closed ACLs (§6.3); a job MUST NOT delete or rewrite `observed` evidence; results are versioned, so a re-run supersedes the previous version rather than overwriting it. Engines MAY use the client's model through MCP sampling (§8.1), so a server needs no model key.

### 4.7 changes and subscriptions

`changes(cursor)` returns an ordered, gap-free feed `{cursor, op, id, version, status}` per tenant. Subscriptions are per session, fire only on real change, and never deliver records the subscriber cannot read.

## 5. Recall semantics (normative)

### 5.1 Admissibility comes first

Before any channel scores a candidate, the engine MUST compute the admissible set: same tenant; `readers ∩ principal.tokens ≠ ∅` (or `readers = null`); status admissible under §5.2; `observed_at ≤ known_at` if given; within `scope.within`; matching `kinds`. Every channel (lexical, dense, graph, temporal, entity, neighbour expansion, profile) MUST draw only from the admissible set. Post-ranking filtering is non-conformant because it silently loses recall and is the usual source of leaks.

### 5.2 Validity view

By default recall returns only memories that are `active` and valid at `as_of` (`valid_from ≤ as_of < valid_to`, open bounds allowed). With `include.history: true`, superseded versions MAY be returned and MUST be labelled `validity: superseded` with a link to the current version. A memory that was current at `as_of` (time travel) is returned as current for that request. `event` and `document` records are evidence and are never "superseded"; facts derived from them are.

### 5.3 Channels

A conforming engine MUST provide at least a lexical channel with IDF and length normalisation, and a dense channel. Each channel MUST be able to nominate candidates independently (no channel may only re-rank another channel's pool). Engines SHOULD provide: sentence- or span-level max-pooling for long units, thread-level relevance, entity match, temporal windows, and pseudo-relevance feedback. Fusion MUST be declared in `capabilities` (e.g. `rrf(k=60)` with weights). Scores returned to clients MUST be higher-is-better with a declared scale.

### 5.4 Ranking features

Engines MAY apply declared features after fusion: query coverage, actor/subject match, role match ("what did you recommend" → assistant), temporal fit, order intent (earliest/latest), current-state intent ("now", "still", "currently" → latest valid), feedback, support count. Every applied feature MUST appear in `why`. Recency and popularity boosts MUST be gated on independent relevance evidence, never applied to everything.

### 5.5 Packing

- The engine MUST NOT exceed `budget.tokens`.
- Dialogue integrity: with `granularity: span`, when a unit is a question the engine SHOULD include its reply, and when a unit is a short reply it SHOULD include the prompt it answers; these spans count as one item. With `auto`, the engine decides (the reference engine's learned ranker returns units, because it already ranks answer turns directly and spans cost budget).
- Thread headers (date, thread label) SHOULD be emitted once per thread per pack, not per line.
- Relative time expressions SHOULD be rendered with their resolved absolute value.
- Long units MAY be snippeted to their most relevant sentences; snippets MUST be marked with `…` and keep `memory_ids` of the full unit so the client can fetch it.
- Superseded versions, when included, MUST be marked.

### 5.6 Supersession

When two active `fact`/`preference` memories share a claim key `(subject, predicate)` with `cardinality: one` and different objects, the one with the later `valid_from` (then later `observed_at`) is current; the other becomes `superseded` with `valid_to` set to the newer `valid_from`. Ordering MUST use event/validity time, not ingestion order, so backfilled old data never overrides newer facts. Engines MUST NOT supersede on similarity alone without exposing the decision (`supersedes` link + `why.superseded_by_rule`). `cardinality: many` claims never supersede each other.

### 5.7 Coverage and abstention

Every pack carries `coverage` (0..1, declared method) estimating whether memory contains an answer. Clients MAY set `min_coverage`; below it the engine returns an empty pack with `abstain_hint: true` rather than padding with noise.

### 5.8 Degradation

If any channel, index or model is unavailable, the response MUST list it in `degraded[]` (`dense_unavailable`, `index_stale`, `reranker_skipped`, `partial_scope`, `budget_truncated`). An engine MUST NOT return an empty success when a scorer failed.

### 5.9 Determinism

Given the same request and `snapshot`, an engine MUST return the same pack. `snapshot` identifies the index state used.

### 5.10 Declared rankers

If an engine uses a learned or model-based ranker, `capabilities` MUST name it with a version (`ranker: "orp-gbdt-1"`), list its input features, and state what data it was fitted on. Engines SHOULD also offer an unlearned mode (`mode: fast`) so clients can compare. Learned rankers MUST NOT use features that bypass admissibility (e.g. statistics computed over inadmissible memories).

### 5.11 Multi-scope recall (SaaS)

A single recall MAY span several scopes the principal can read (organisation, team, channel, user, agent). Engines MUST rank all admissible candidates on one scale rather than merging per-scope result lists by raw score, MUST return each item's `scope`, and SHOULD honour `scope.prefer` as a soft prior. Per-scope quotas, when needed, are expressed with `kinds`-style quota maps on scopes.

## 6. Access control and governance

### 6.1 Principals and tokens

Credentials bind to a principal server-side. The engine computes `principal.tokens` (`principal:<id>`, `group:<g>`, `channel:<c>`, `role:<r>`, `public`). Clients MUST NOT be able to assert tokens through tool arguments. Connectors MUST write source-system ACLs into `readers` at ingest and keep them in sync through `remember(mode: upsert)` with metadata-only updates (no re-embedding).

### 6.2 Read and write are separate

`readers` controls recall, get, neighbours, profile, export. `writers` controls upsert, supersede, forget, approve. Get-by-id, neighbours and history MUST check `readers` exactly like recall.

### 6.3 Provenance-closed access (the laundering rule)

For any derived memory `d` with sources `S = derived_from(d)`:

```
readers(d) ⊆ ⋂_{s ∈ S} readers(s)
```

with `null` treated as the universal set. Engines MUST enforce this at write time (narrowing `readers(d)`) and on every source ACL change (re-narrowing or retracting `d`). A summary, profile, observation, edge or entity description built from a private DM can therefore never surface in a shared channel. No project we studied enforces this.

### 6.4 Labels and redaction

Memories labelled `secret` MUST NOT be returned unless the request includes the label in `include.labels` and the principal holds `label:secret`. `redact` erases content in the record and every derived record and index, keeping an auditable tombstone.

### 6.5 Audit

Every write, forget, approval and ACL change is appended to a tenant audit log `{at, principal, op, ids, reason}` retrievable through `changes` by principals with `role:auditor`.

## 7. Lifecycle

- **Tiers.** Raw evidence (`event`, `document`) is lossless and never rewritten. Derived tiers (`fact`, `preference`, `procedure`, `observation`, `summary`, `profile`) are rebuildable.
- **Consolidation** runs as `reflect` jobs with declared triggers (`every N units`, `on thread close`, `nightly`, `on compaction`), never inline in `remember` unless `wait: indexed` asks for it.
- **Forgetting** is explicit (`forget`), time-based (`expires_at`) or policy-based (declared per scope). Silent FIFO or LFU deletion is non-conformant. Reads MUST NOT mutate state (no heat counters on read); usage signals go through `feedback(used)`.
- **Procedures** (standing rules, skills) are versioned memories with `load: always|on_scope`, created through `propose` and activated by approval.

## 8. Bindings

### 8.1 MCP binding

ORP over MCP maps verbs to tools with JSON Schema `inputSchema` and `outputSchema` returning `structuredContent` plus a text rendering:

| Tool | Annotations |
|---|---|
| `orp_recall` | `readOnlyHint, idempotentHint` |
| `orp_profile` | `readOnlyHint, idempotentHint` |
| `orp_remember` | `idempotentHint` |
| `orp_forget` | `destructiveHint` (engine SHOULD confirm through elicitation) |
| `orp_feedback`, `orp_link`, `orp_reflect` | — |
| `orp_history`, `orp_get`, `orp_neighbours` | `readOnlyHint` |

- Resources: `orp://{tenant}/mem/{id}`, `orp://{tenant}/thread/{thread}`, `orp://{tenant}/entity/{key}`, `orp://{tenant}/profile/{subject}` with resource templates and completions (entity names). Recall MAY return `resource_link` blocks instead of full bodies when `granularity: thread` would exceed the budget.
- Content annotations: `priority` = normalised score, `lastModified` = `observed_at`, `audience: ["assistant"]`.
- Subscriptions: per MCP session, `notifications/resources/updated` only on real change, ACL-filtered.
- Sampling: engines MAY ask the client's model to extract facts or write observations (`reflect`), so no server-side model key is needed. Outputs are `origin: extracted` with `derived_from`.
- Tasks: `remember` with `wait: accepted`, `reflect` and `import` return MCP tasks with progress notifications.
- Server `instructions` carry the memory policy (when to recall, what to store, what never to store) instead of a README.
- Principal binding comes from the authenticated MCP session (Streamable HTTP + OAuth), never from tool arguments.

### 8.2 HTTP binding

`POST /v1/{tenant}/recall`, `/remember`, `/forget`, `/feedback`, `/reflect`, `/profile`, `/link`; `GET /v1/{tenant}/memories/{id}`, `/memories/{id}/history`, `/threads/{thread}/neighbours`, `/changes?cursor=`, `/jobs/{id}`, `/capabilities`. JSON bodies as in §4. `ETag`/`If-Match` carry `version`. Errors use `{code, message, details}` with codes `unsupported_parameter`, `version_conflict`, `forbidden`, `not_found`, `budget_too_small`, `degraded`, `rate_limited`.

### 8.3 Bundle format

An ORP bundle is a directory or `.orp` zip containing `manifest.json` (spec version, tenant, counts, hashes, engine capabilities, consolidation config), `memories.jsonl` (records, all tiers, all statuses including tombstones), `edges.jsonl`, `audit.jsonl` (optional) and `render/` (optional Markdown rendering: one file per thread or entity, frontmatter = record fields). Import MUST be lossless: `export → import → export` yields identical `memories.jsonl` modulo engine-assigned ids, which are remapped through `manifest.id_map`. Secrets in `meta` are scrubbed unless the exporter holds `label:secret`.

## 9. Conformance

| Level | Name | Requires |
|---|---|---|
| L1 | Core | Record model, `remember`, `recall` (lexical + dense, independent nomination), `get`, `forget(retract)`, idempotency, budget cap, `capabilities`, unknown-parameter rejection |
| L2 | Temporal | `observed_at`/`event_time`/validity, `as_of`, `known_at`, explicit supersession with links, `history`, relative-time resolution |
| L3 | Governed | Pre-ranking admissibility on every channel and verb, provenance-closed ACL, read/write separation, redact with cascade, `dry_run`, audit, per-session subscriptions |
| L4 | Adaptive | `reflect` jobs with lineage, `feedback` incl. draft→final, `profile` with budget, rule proposals via `draft`, bundle export/import round-trip |

The conformance suite (`tests/conformance/`) tests each MUST in this document. An engine claims a level only if it passes every test at that level.

## 10. Evaluation (ORP-Bench)

Recall quality is measured by **evidence recall inside a fixed token budget**: the fraction of gold evidence units contained in the context an agent would actually receive at 500, 1,000, 2,000 and 4,000 tokens, plus answer-token recall (does the packed context contain the answer's content words), session recall (LongMemEval `recall_all@k`), MRR and latency. Charging every item its rendered token cost makes systems of different granularity (facts, turns, chunks, pages) comparable.

Rules: pinned dataset versions; dev/test splits with all tuning on dev; per-category n; every system run with the same encoders; LLM-free retrieval numbers reported separately from LLM-in-the-loop QA numbers; the harness, configs and raw per-query results published with any claim.

---

## Appendix A. How ORP maps to the 15 projects

| Project | Closest ORP concepts it already has | What ORP adds for it |
|---|---|---|
| Graphiti / Zep | bi-temporal edges, invalidation by event time | explicit `superseded_by` links, ACL closure, budgets, `as_of` default view |
| Hindsight | kinds (world/experience/observation), token budget, temporal arm | pre-ranking scope in every arm, honest fusion, validity |
| mem0 | scope tuple, entity index | supersession links (dropped today), event time, independent channel nomination |
| Letta | blocks (`load: always`), compaction | ACL before limit, enforced limits, lossless export |
| Supermemory + Company Brain | versions, latest flag, containers as permission graph | event time, `as_of`, calibrated multi-scope fusion, write ACL |
| GBrain | page/chunk split, budgets, dream cycle | `as_of` instead of wall-clock, searchable facts, scope |
| Onyx | principal ACL pre-filter, connectors | validity, supersession, memory kinds |
| Cognee | typed DataPoints, lineage | retrieval that honours validity, ACL closure |
| Basic Memory | human-readable files, `@effective` validity | supersession, ACL, recall budgets |
| Sylph | draft→final learning, files-as-memory | enforced ACL, record model, search |
| MCP memory server | entities/relations/observations over MCP | ids, time, search, scope, pagination |
| MemOS | memory states, scheduler | enforced tenancy, no FIFO deletion |
| LangMem | typed kinds, procedural memory | lexical channel, time, history |
| MemoryOS | tiers and promotion | side-effect-free reads, scope, lossless raw tier |

## Appendix B. Reference engine recall algorithm

The reference engine (`orp/engine.py`) implements §5 without any language model. It ships two declared rankers: `rrf` (weighted reciprocal-rank fusion plus multiplicative features, hand-tuned on public dev splits) and `gbdt` (a gradient-boosted tree ensemble, exported as JSON, over the 39 declared features in `Engine.FEATURES`, fitted only on public dev splits). Steps:

1. Admissibility mask (§5.1, §5.2).
2. Query analysis: content terms, named entities resolved against known actors and entities, temporal intent (window, order, current, when, count, span) relative to `as_of`, role intent.
3. Channels, each nominating its own top 200: BM25 over units; BM25 over three-unit windows; BM25 over two-sentence segments max-pooled to units; dense over units, windows and segments; thread relevance (BM25 + dense over whole threads, rank-fused); entity match with hub damping; temporal window; RM3 pseudo-relevance feedback.
4. Weighted reciprocal-rank fusion (declared weights, `k` declared) to form a 300-candidate pool.
5. Ranking over the pool: either multiplicative features (`rrf`) or the learned model (`gbdt`) over per-channel reciprocal ranks, normalised lexical and dense scores, query coverage of the unit and its neighbours, actor and role match, temporal window and order position, dialogue cues (question, reply, prompt), answer-type cues (a "when" question and a unit with a time expression; a "how many" question and a unit with a number; a "where" question and a unit naming a place) and query-type flags.
6. Thread interleaving for count/span questions (aggregation needs breadth).
7. Packing per §5.5: dialogue spans, once-per-thread headers, resolved dates, snippeted long units, hard budget.

All weights are tuned on development splits only and published with the engine version.
