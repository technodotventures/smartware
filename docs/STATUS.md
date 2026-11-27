# Status against the implementation layer design (11 October 2026)

## Decisions taken (each is the design doc's own recommendation, and easy to reverse)
1. **Stack**: TypeScript product engine on Node, SQLite from the runtime (`node:sqlite`) or Postgres. Python stays as
   the oracle and the benchmark harness.
2. **Engine gaps first**: done (tenant partitions, incremental index, SQLite and Postgres stores, Streamable HTTP and
   OAuth).
3. **Name**: Smartware (Smartware 2.0; packages at 2.0.0). The server and CLI publish as `smartware` on npm (the
   name the earlier library used; 2.0.0 is a new major version, so consumers pinned to the old versions are unaffected), the libraries as `@technodotventures/smartware-*`,
   the Python client as `smartware-client`. The earlier code (v0.8.1 and before) is kept at technodotventures/smartware-legacy.
4. **Licence**: Apache-2.0 for everything in this repository; hosted multi-tenant operations stay closed. Copyright
   Social Money Inc. d/b/a Techno Ventures.
5. **Moving off the earlier Smartware**: not started. The importers below are the pattern an importer for it would follow.

## Build order
| Phase | Exit gate | Status |
|---|---|---|
| 0. Preconditions | Benchmarking complete | Done (v0.1.3 results). Name and licence decided, see above |
| 1. Foundation | L1 conformance; fast-mode packs match the oracle id for id on LoCoMo test | **Met.** 100% identical packs (fast, rrf and the shipped linear ranker); `smartware doctor` L1 passes |
| 2. Temporal and governed | Every L1 to L3 MUST passes; removal equivalence on principal-sliced stores | **Met.** The oracle's conformance suite passes against the product engine, on SQLite, in memory and on Postgres; doctor L3 passes, including removal equivalence across principals and tenants; `smartware doctor --principals` checks it on real data. Audience-class statistics were not needed: exact per-query statistics are fast enough at the sizes measured |
| 3. Clients and P0 adapters | Studio OS agents recall through Smartware in a staging instance | **Built, not yet run against your stack.** TS and Python clients (coarse `as_of` widened to the end of the stated period), MCP memory-server compatibility, Mastra tools and processors, Hermes and OpenClaw policy notes. The staging run against Coffee and the Studio OS needs your environment |
| 4. Ranker parity and eval | Product engine reproduces Smartware Bench within tolerance, raw per-query results published | **Met** for LoCoMo and LongMemEval-S (within 0.5 points; results/PRODUCT_PARITY.md; `smartware eval --out` writes per-question results) |
| 5. Connectors and P1 adapters | Connector ACL replay passes; three outside projects publish doctor reports | Connectors (Markdown vault, Slack) and the replay kit pass; LangGraph (JS and Python), mem0 (REST and Python), Letta, AI SDK middleware built. Bundles on disk (`smartware export` / `smartware import`, `.smartware` zip or directory with a hashed manifest and Markdown view) and importers from the MCP memory server, mem0 and Letta (`smartware import-from`). **Outside doctor reports**: not possible from here |
| 6. Adaptive and hosted | L4 passes; independent audit finds nothing critical | **L4 passes. Third and fourth audits: every finding fixed (results/AUDIT3.md, results/AUDIT4.md).** Hosting: single node on SQLite, or any number of nodes on one Postgres database, each tenant served by the node holding its lease (fenced writes, forwarding, idle release); results/PERFORMANCE.md has both stores |

## How it is checked (all in CI)
TypeScript tests (core, server, clients, adapters, connectors, bundles, audit regressions), the oracle's conformance
suite on its own and against the product engine, the Python client, `smartware doctor --self` to L4, the schema pack drift
check, the release gate, persistence and differential fuzzing, and a Postgres job (store, leases, forwarding, backup,
the conformance suite and both fuzzers on Postgres).

## Action-memory review (25 November 2026)
A design review against action-grading benchmarks (DolphinBench: an agent must apply a rule stated months earlier
to a request that never mentions it) found that standing rules were stored but never surfaced. Fixed:
- **Standing context** (spec §5.12, M78-M80): `load: on_scope` now means something. Every recall returns the
  admissible `on_scope` memories that best fit the query first, within `budget.standing` (default a quarter of the
  budget), marked `standing: true`, whether or not the query mentions them. No `on_scope` memories, no change.
- **Rules that apply**: over MCP (safe toolset) a rule is active when the person confirms it in the client
  (elicitation), or, with `serve --rules user-stated`, when it cites the user's own message as `source_id`.
  Otherwise it is a draft, as before. The tools adapter has the same confirmation hook (`confirmRule`). Rules and
  preferences are written `load: on_scope`.
- **Snippets** of long units always keep the unit's segment closest to the query, so a message found by meaning is
  not cut down to its word-matching sentences.
- **Repeated short turns** ("ok", "yes") at different positions in a thread are no longer merged by content
  de-duplication.
- **MUST checklist** now includes requirements tagged in sentences phrased without MUST (M69, M72, M75, M76).
- **Edge listing** is ordered by (from, to, type) in both engines (an import no longer changes the order).

Then: model-backed extraction (below), read-only credentials (`smartware keys add --read-only`), and a
DolphinBench harness with a one-persona trial (`tools/dolphinbench/`), checked end to end against a fake model.
No paid benchmark run yet.

Still open from that review: optional `condition` text on
rules; entity matching for `#channels`, `@handles` and lowercase names; extra weighted `queries` (rejected, as the
spec allows); `observed_at` for imported history is the import time, so `known_at` is not meaningful for it; the
persistence fuzzer reports an empty claim chain that a reload drops (internal bookkeeping only, present before
this work); the product is 2.0.0 while the spec document is still titled 0.1.

## Not done, and why
- **Approximate dense index for very large tenants** (above about 50,000 units): recall is exact and linear; see
  results/PERFORMANCE.md. Split large tenants by scope meanwhile.
- **Model-backed reflect**: extraction is done (`reflect` with kinds fact/preference/procedure and `ids`, through
  `serve --extract-model openai:<model>@<url>`; any OpenAI-compatible endpoint, key from an environment variable).
  Model-backed summaries and MCP sampling as the model source are not done; summaries are still extractive.
  The Python oracle has no model and rejects extraction kinds, as the spec allows.
- **Per-tenant locking inside one process**: a node runs one verb at a time; it scales out across nodes, by tenant.
- **Host-framework interfaces** (Mastra, LangGraph, AI SDK, OpenAI Agents SDK, Claude Agent SDK, mem0, Letta) were
  written from knowledge of their 2025 releases without installing them (the package registry was blocked here);
  each adapter README says what to check.
- **Lockfile**: committed (`package-lock.json`, dev tools only: typescript, @types/node). CI and the release job use
  `npm ci`.
- **Release day**: npm trusted publishers expire if unused for 2 days, so they are added on release day, stage-only,
  following "Releasing" in docs/OPERATIONS.md. New package names are claimed first with a local staged publish.
- **npm org 2FA enforcement**: off for now, because one member does not have 2FA yet and turning enforcement on
  would remove them. Both existing packages already refuse publishes without 2FA.
- **LongMemEval v2**: not converted yet; its text-only questions can be converted to the eval format once the files
  are here.
