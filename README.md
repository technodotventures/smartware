# ORP: Open Recall Protocol (working name)

Memory and context for AI agents and SaaS platforms, shipped as a kit: a protocol, a production engine, one server
binary, typed clients, adapters for the memory APIs projects already call, connectors that carry source permissions
into memory, and the tools to prove all of it on your own data. "ORP" and every package name are placeholders: the
rename is one find-and-replace.

What makes it different, in one line each:
- **Admissibility before ranking.** Recall over a store returns exactly what it would over the same store with every
  record you cannot read removed: scores, order and text (M67). Hidden records cannot leak through statistics.
- **Time is first class.** Every record has when it was learned, when it happened and when it was valid; `as_of` and
  `known_at` answer "what was true then" and "what did we know then". Newer facts supersede older ones explicitly.
- **Provenance closes access.** A summary of records you cannot read is a record you cannot read.
- **Budgeted, cited context packs**, ranked by a declared, versioned ranker, with `why` for every item.
- **Proof travels with it.** `orp doctor` checks any endpoint against the spec by MUST id; `orp eval` re-measures
  recall on your data with your reader.

## Start here (the adoption ladder)

| Rung | Time | What you do | Level |
|---|---|---|---|
| 1. Plug in | 5 minutes | Add `orp serve` to your MCP client; paste the [memory policy](policy/memory-policy.md) | L1 |
| 2. Wire in | 1 hour | Use [@orp/client](packages/client) or [orp_client](clients/python), or an [adapter](packages/adapters); map your auth to principals | L2 |
| 3. Govern | 1 day | Turn on [connectors](packages/connectors) so source permissions flow into readers; multi-tenant; audit; `orp doctor --principals` | L3 |
| 4. Adapt | 1 week | Reflect jobs, feedback, profile budgets, scheduled bundle exports | L4 |

**MCP (Claude, Cursor, Hermes, OpenClaw, any MCP client), local:**

```json
{ "mcpServers": { "memory": { "command": "orp", "args": ["serve", "--store", "/Users/you/.orp/orp.db"],
  "env": { "ORP_PRINCIPAL": "you", "ORP_TENANT": "personal" } } } }
```

**A server for an app or a team (HTTP binding and MCP Streamable HTTP on one port):**

```bash
orp keys add --keys keys.json --id alice --tenant acme --groups sales     # prints a token once; only its hash is kept
orp serve --http 8787 --keys keys.json --store /var/lib/orp/orp.db        # or --oidc-issuer https://idp... for OAuth
```

**From code:**

```ts
import { OrpClient } from "@orp/client";
const memory = new OrpClient({ url: "http://127.0.0.1:8787", getToken: () => process.env.ORP_TOKEN! });
await memory.remember("Alice prefers oat-milk flat whites", { kind: "preference" });   // private to the writer by default
const pack = await memory.recall("What coffee does Alice like?", { budgetTokens: 1500 });
```

Already on another memory API? Keep your code: the [MCP memory server tools](policy/claude.md) (`--tools mcp-memory`),
a [mem0-compatible REST API](docs/OPERATIONS.md#mem0-compatible-rest) at `/mem0`, the mem0 Python class, a LangGraph
store (JS and Python), Vercel AI SDK middleware, Mastra tools and processors, and a Letta `.af` importer.

## The kit

| Part | Where | What it is |
|---|---|---|
| Spec | [spec/ORP-0.1.md](spec/ORP-0.1.md), [spec/MUSTS.md](spec/MUSTS.md) | The protocol; every MUST has a stable id |
| Schema pack | [schema/](schema) | JSON Schemas, OpenAPI 3.1 and MCP tool schemas, generated from one source and drift-tested |
| Engine | [packages/core](packages/core) | TypeScript product engine on Node's built-in SQLite. Zero runtime dependencies |
| Server | [packages/server](packages/server) | `orp`: serve, doctor, eval, keys, backup |
| Clients | [packages/client](packages/client), [clients/python](clients/python) | Typed clients; identity is never an argument |
| Adapters | [packages/adapters](packages/adapters) | tools, mastra, langgraph, ai-sdk; Python: LangGraph, mem0, Letta |
| Connectors | [packages/connectors](packages/connectors) | SDK, Markdown vault, Slack, ACL replay test kit |
| Policy pack | [policy/](policy) | Memory instructions for agents, also served as the MCP prompt `memory_policy` |
| Oracle | [orp/](orp) | The Python reference engine: the specification the product engine is tested against |
| Benchmark | [bench/](bench), [baselines/](baselines), [results/](results) | ORP-Bench and LLM-free re-implementations of 15 memory systems |

## How it is verified

- **Parity with the oracle**: 100% identical packs on LoCoMo test (1,152 questions, three ranker configurations) and on
  LongMemEval-S; the oracle's own conformance suite passes against the product engine; one store file is shared by
  both engines with byte-identical exports. [results/PRODUCT_PARITY.md](results/PRODUCT_PARITY.md)
- **Benchmarks**: the product engine reproduces the published results within 0.5 points. On LoCoMo test it is ahead of
  the best of the 15 systems on all 7 headline metrics; on LongMemEval-S (day-precision times) 5 better, 7 level,
  0 worse; in a pre-registered blind QA it was level with or ahead of all 16 systems with two different readers.
  Baselines are LLM-free re-implementations of each project's recall path, not the products themselves; read
  [results/METHODOLOGY.md](results/METHODOLOGY.md) before quoting numbers. [results/RESULTS.md](results/RESULTS.md)
- **Conformance**: `orp doctor --self` reaches L4 (29 wire-level checks). It is engine-agnostic (the Python oracle also
  reaches L4) and catches deliberate violations (global statistics, ignored parameters).
- **Performance**: [results/PERFORMANCE.md](results/PERFORMANCE.md).
- **Security and supply chain**: [SECURITY.md](SECURITY.md). No lifecycle scripts, no third-party runtime dependencies,
  release gate in CI and as a pre-push hook, tag-only trusted publishing with provenance.

## Develop

```bash
npm run build                 # TypeScript (needs typescript and @types/node: npm install --ignore-scripts)
npm test                      # 70+ TypeScript tests
npm run test:py               # oracle suite, oracle suite against the TS engine, Python client
node tools/release-check.mjs  # the release gate
git config core.hooksPath tools/hooks
```

Licence: Apache-2.0 (placeholder pending the owner's decision). Status against the implementation design doc:
[docs/STATUS.md](docs/STATUS.md).
