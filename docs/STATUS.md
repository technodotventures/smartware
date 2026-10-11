# Status against the implementation layer design (11 October 2026)

## Decisions taken (each is the design doc's own recommendation, and easy to reverse)
1. **Stack**: TypeScript product engine on Node, SQLite from the runtime (`node:sqlite`) or Postgres. Python stays as
   the oracle and the benchmark harness.
2. **Engine gaps first**: done (tenant partitions, incremental index, SQLite and Postgres stores, Streamable HTTP and
   OAuth).
3. **Name**: still the placeholder "orp"; every package name is a placeholder for one find-and-replace.
4. **Licence**: Apache-2.0 placeholder for everything in this repository; hosted multi-tenant operations would stay
   closed. Copyright holder in NOTICE is a placeholder.
5. **Moving off the live Smartware**: not started (after the name is fixed, as the doc says). The importers below
   are the pattern a Smartware importer would follow.

## Build order
| Phase | Exit gate | Status |
|---|---|---|
| 0. Preconditions | Benchmarking complete | Done (v0.1.3 results). Name and licence: placeholders, see above |
| 1. Foundation | L1 conformance; fast-mode packs match the oracle id for id on LoCoMo test | **Met.** 100% identical packs (fast, rrf and the shipped linear ranker); `orp doctor` L1 passes |
| 2. Temporal and governed | Every L1 to L3 MUST passes; removal equivalence on principal-sliced stores | **Met.** The oracle's conformance suite passes against the product engine, on SQLite, in memory and on Postgres; doctor L3 passes, including removal equivalence across principals and tenants; `orp doctor --principals` checks it on real data. Audience-class statistics were not needed: exact per-query statistics are fast enough at the sizes measured |
| 3. Clients and P0 adapters | Studio OS agents recall through ORP in a staging instance | **Built, not yet run against your stack.** TS and Python clients (coarse `as_of` widened to the end of the stated period), MCP memory-server compatibility, Mastra tools and processors, Hermes and OpenClaw policy notes. The staging run against Coffee and the Studio OS needs your environment |
| 4. Ranker parity and eval | Product engine reproduces ORP-Bench within tolerance, raw per-query results published | **Met** for LoCoMo and LongMemEval-S (within 0.5 points; results/PRODUCT_PARITY.md; `orp eval --out` writes per-question results) |
| 5. Connectors and P1 adapters | Connector ACL replay passes; three outside projects publish doctor reports | Connectors (Markdown vault, Slack) and the replay kit pass; LangGraph (JS and Python), mem0 (REST and Python), Letta, AI SDK middleware built. Bundles on disk (`orp export` / `orp import`, `.orp` zip or directory with a hashed manifest and Markdown view) and importers from the MCP memory server, mem0 and Letta (`orp import-from`). **Outside doctor reports**: not possible from here |
| 6. Adaptive and hosted | L4 passes; independent audit finds nothing critical | **L4 passes. Third and fourth audits: every finding fixed (results/AUDIT3.md, results/AUDIT4.md).** Hosting: single node on SQLite, or any number of nodes on one Postgres database, each tenant served by the node holding its lease (fenced writes, forwarding, idle release); results/PERFORMANCE.md has both stores |

## How it is checked (all in CI)
TypeScript tests (core, server, clients, adapters, connectors, bundles, audit regressions), the oracle's conformance
suite on its own and against the product engine, the Python client, `orp doctor --self` to L4, the schema pack drift
check, the release gate, persistence and differential fuzzing, and a Postgres job (store, leases, forwarding, backup,
the conformance suite and both fuzzers on Postgres).

## Not done, and why
- **Approximate dense index for very large tenants** (above about 50,000 units): recall is exact and linear; see
  results/PERFORMANCE.md. Split large tenants by scope meanwhile.
- **Model-backed reflect and extraction** (the Model port, via MCP sampling or an injected completion function):
  reflect is still deterministic and extractive.
- **Per-tenant locking inside one process**: a node runs one verb at a time; it scales out across nodes, by tenant.
- **Host-framework interfaces** (Mastra, LangGraph, AI SDK, OpenAI Agents SDK, Claude Agent SDK, mem0, Letta) were
  written from knowledge of their 2025 releases without installing them (the package registry was blocked here);
  each adapter README says what to check.
- **Lockfile**: `package-lock.json` could not be generated here (registry blocked). Run `npm install --ignore-scripts`
  once on a clean machine and commit it; CI then uses `npm ci`.
- **Registry settings only you can make**: trusted publishing for each package on npmjs.com, the `release`
  environment with a required reviewer, secret scanning and push protection on the repository.
- **LongMemEval v2**: not converted yet; its text-only questions can be converted to the eval format once the files
  are here.
