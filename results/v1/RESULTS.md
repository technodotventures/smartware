# Smartware Bench results (test splits)

Evidence recall inside a fixed context budget (R@B = share of gold evidence turns contained in the first B tokens of returned context). Ans@1000 = share of the gold answer's content words present in that context. SessAll@5 = all gold sessions among the first 5 sessions surfaced (LongMemEval's retrieval metric). Every system uses the same BM25 and dense encoder and no LLM. Numbers are percentages.

## LoCoMo-10 (7 test conversations, categories 1-4), n=1152

| # | System | R@500 | R@1000 | R@2000 | R@4000 | Full@2000 | Ans@1000 | SessAll@5 | MRR | p50 ms |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **Smartware reference engine** | 72.0 | 80.2 | 86.1 | 89.5 | 79.9 | 79.5 | 80.1 | 60.2 | 20.4 |
| 2 | smartware-rrf | 66.3 | 75.0 | 81.4 | 86.2 | 74.3 | 75.7 | 77.5 | 55.2 | 7.0 |
| 3 | Supermemory Company Brain | 59.4 | 71.4 | 78.5 | 82.8 | 72.1 | 68.8 | 77.4 | 55.8 | 4.0 |
| 4 | Cognee | 54.8 | 67.5 | 76.9 | 82.7 | 69.7 | 68.9 | 72.0 | 45.4 | 2.1 |
| 5 | _BM25 turns (reference)_ | 58.6 | 67.0 | 72.7 | 74.6 | 65.6 | 66.9 | 76.6 | 46.0 | 0.3 |
| 6 | Graphiti (Zep) | 54.4 | 62.8 | 70.7 | 74.3 | 63.6 | 51.4 | 73.8 | 40.5 | 1.1 |
| 7 | mem0 | 55.6 | 62.7 | 67.6 | 68.5 | 60.3 | 63.7 | 74.1 | 43.0 | 1.1 |
| 8 | Supermemory | 22.8 | 61.2 | 66.8 | 66.8 | 61.0 | 60.3 | 13.5 | 3.8 | 6.8 |
| 9 | Letta | 41.3 | 53.9 | 61.2 | 69.8 | 55.8 | 56.0 | 75.4 | 35.5 | 1.0 |
| 10 | Onyx | 35.1 | 53.9 | 66.8 | 80.2 | 61.5 | 60.1 | 76.9 | 76.0 | 2.0 |
| 11 | Basic Memory | 33.5 | 53.4 | 63.3 | 74.1 | 58.1 | 59.3 | 71.5 | 71.1 | 1.9 |
| 12 | Hindsight | 31.9 | 47.3 | 60.4 | 64.5 | 54.2 | 53.4 | 43.5 | 19.1 | 4.3 |
| 13 | MemOS | 33.6 | 45.3 | 55.7 | 65.5 | 48.6 | 53.2 | 58.0 | 33.0 | 3.1 |
| 14 | _Dense turns (reference)_ | 33.6 | 43.4 | 50.5 | 51.3 | 44.8 | 53.1 | 52.5 | 19.6 | 0.2 |
| 15 | MCP memory server | 38.0 | 42.7 | 46.6 | 48.5 | 42.4 | 52.8 | 61.3 | 32.0 | 0.8 |
| 16 | MemoryOS | 26.3 | 35.9 | 41.1 | 41.1 | 35.9 | 53.0 | 47.8 | 27.2 | 0.5 |
| 17 | GBrain | 23.7 | 35.0 | 46.0 | 60.8 | 41.4 | 47.0 | 59.0 | 58.0 | 7.2 |
| 18 | Sylph | 9.8 | 29.8 | 39.8 | 39.8 | 34.8 | 44.5 | 34.1 | 13.4 | 3.0 |
| 19 | LangMem | 21.8 | 29.6 | 37.3 | 45.9 | 32.9 | 37.0 | 57.1 | 23.4 | 0.6 |

Recall@1000 by question category:

| System | multi-hop | open-domain | single-hop | temporal |
|---|---|---|---|---|
| **Smartware reference engine** | 52.3 | 50.6 | 91.2 | 84.3 |
| smartware-rrf | 43.4 | 41.0 | 87.9 | 78.6 |
| Supermemory Company Brain | 40.9 | 41.7 | 83.7 | 74.1 |
| Cognee | 37.6 | 32.3 | 81.2 | 67.5 |
| _BM25 turns (reference)_ | 40.8 | 35.9 | 78.2 | 69.6 |
| Graphiti (Zep) | 38.1 | 32.9 | 72.3 | 68.2 |
| mem0 | 38.2 | 35.1 | 71.7 | 68.3 |
| Supermemory | 28.7 | 33.4 | 75.4 | 59.6 |
| Letta | 23.5 | 24.9 | 65.5 | 58.4 |
| Onyx | 21.6 | 30.0 | 66.9 | 54.2 |
| Basic Memory | 17.9 | 24.6 | 67.8 | 54.4 |
| Hindsight | 23.4 | 23.1 | 55.9 | 52.6 |
| MemOS | 24.1 | 25.9 | 54.1 | 46.1 |
| _Dense turns (reference)_ | 24.2 | 23.0 | 50.1 | 48.6 |
| MCP memory server | 23.8 | 22.1 | 48.6 | 49.6 |
| MemoryOS | 19.2 | 19.0 | 43.9 | 34.3 |
| GBrain | 16.7 | 18.7 | 43.2 | 34.0 |
| Sylph | 15.9 | 21.0 | 37.5 | 24.0 |
| LangMem | 13.0 | 13.4 | 34.8 | 35.0 |
| _n_ | 208 | 73 | 641 | 230 |

## LongMemEval (reconstructed 40-session haystack), n=382

| # | System | R@500 | R@1000 | R@2000 | R@4000 | Full@2000 | Ans@1000 | SessAll@5 | MRR | p50 ms |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **Smartware reference engine** | 76.0 | 82.2 | 90.0 | 95.8 | 83.2 | 71.2 | 88.5 | 74.7 | 48.1 |
| 2 | mem0 | 63.4 | 74.7 | 82.6 | 88.2 | 74.1 | 70.0 | 83.5 | 62.0 | 2.8 |
| 3 | Graphiti (Zep) | 61.8 | 74.2 | 82.8 | 87.6 | 75.1 | 64.2 | 80.1 | 52.2 | 6.0 |
| 4 | smartware-rrf | 68.4 | 73.8 | 78.8 | 91.5 | 68.3 | 70.5 | 87.2 | 70.9 | 33.0 |
| 5 | _Dense turns (reference)_ | 56.7 | 65.2 | 75.1 | 85.2 | 63.4 | 63.9 | 83.2 | 63.6 | 0.3 |
| 6 | Supermemory Company Brain | 53.0 | 64.4 | 74.4 | 80.9 | 63.9 | 67.5 | 84.8 | 58.9 | 6.8 |
| 7 | Letta | 50.0 | 63.2 | 75.6 | 81.7 | 64.9 | 60.1 | 86.9 | 67.4 | 1.5 |
| 8 | _BM25 turns (reference)_ | 50.7 | 63.1 | 73.2 | 82.1 | 62.8 | 64.3 | 83.0 | 64.3 | 0.6 |
| 9 | LangMem | 44.8 | 56.9 | 67.4 | 79.3 | 54.7 | 49.3 | 82.2 | 65.0 | 1.0 |
| 10 | MCP memory server | 46.3 | 53.0 | 58.0 | 61.3 | 52.4 | 56.0 | 60.2 | 46.0 | 9.2 |
| 11 | Basic Memory | 37.7 | 47.4 | 68.8 | 78.5 | 55.8 | 57.4 | 82.2 | 78.9 | 5.5 |
| 12 | Hindsight | 32.2 | 46.7 | 61.1 | 79.0 | 49.5 | 52.5 | 80.1 | 46.2 | 19.9 |
| 13 | Supermemory | 10.6 | 39.8 | 41.4 | 41.8 | 26.4 | 38.8 | 10.2 | 5.1 | 89.3 |
| 14 | Onyx | 26.8 | 39.4 | 52.1 | 64.9 | 32.2 | 62.9 | 83.8 | 83.3 | 3.3 |
| 15 | Cognee | 34.3 | 38.4 | 54.5 | 71.5 | 38.5 | 54.7 | 80.9 | 64.0 | 22.6 |
| 16 | GBrain | 19.9 | 30.7 | 40.3 | 45.4 | 23.3 | 64.0 | 75.7 | 77.5 | 20.7 |
| 17 | MemoryOS | 26.6 | 30.6 | 42.5 | 61.1 | 27.7 | 49.9 | 78.5 | 53.9 | 0.8 |
| 18 | MemOS | 24.6 | 29.7 | 43.9 | 58.0 | 30.1 | 50.4 | 74.6 | 50.8 | 15.5 |
| 19 | Sylph | 10.4 | 13.6 | 23.6 | 47.2 | 15.7 | 27.3 | 41.4 | 23.0 | 11.8 |

Recall@1000 by question category:

| System | knowledge-update | multi-session | single-session-assistant | single-session-preference | single-session-user | temporal-reasoning |
|---|---|---|---|---|---|---|
| **Smartware reference engine** | 86.8 | 71.7 | 89.1 | 80.1 | 94.4 | 80.6 |
| mem0 | 89.2 | 55.6 | 89.1 | 57.7 | 90.7 | 74.1 |
| Graphiti (Zep) | 85.5 | 53.7 | 93.5 | 56.4 | 96.3 | 71.7 |
| smartware-rrf | 79.3 | 61.2 | 91.3 | 66.0 | 88.9 | 68.6 |
| _Dense turns (reference)_ | 74.7 | 45.8 | 80.4 | 60.3 | 83.3 | 63.1 |
| Supermemory Company Brain | 70.2 | 44.4 | 91.3 | 43.6 | 90.7 | 59.4 |
| Letta | 72.0 | 45.0 | 78.3 | 54.5 | 87.0 | 58.1 |
| _BM25 turns (reference)_ | 77.2 | 46.0 | 87.0 | 36.5 | 84.3 | 55.7 |
| LangMem | 61.6 | 39.0 | 71.7 | 37.8 | 83.3 | 56.1 |
| MCP memory server | 76.1 | 39.3 | 37.0 | 26.9 | 88.9 | 47.0 |
| Basic Memory | 44.1 | 29.0 | 82.6 | 37.8 | 75.9 | 38.4 |
| Hindsight | 56.7 | 35.9 | 10.9 | 54.5 | 66.7 | 55.8 |
| Supermemory | 43.8 | 25.2 | 71.7 | 13.5 | 53.7 | 36.6 |
| Onyx | 29.0 | 27.4 | 71.7 | 34.0 | 56.5 | 35.0 |
| Cognee | 32.3 | 20.2 | 91.3 | 33.3 | 61.1 | 24.4 |
| GBrain | 23.1 | 19.0 | 69.6 | 22.4 | 34.3 | 29.4 |
| MemoryOS | 24.5 | 12.5 | 76.1 | 23.1 | 53.7 | 20.6 |
| MemOS | 22.0 | 13.0 | 82.6 | 21.8 | 46.3 | 19.2 |
| Sylph | 10.5 | 10.2 | 30.4 | 14.7 | 13.0 | 10.9 |
| _n_ | 62 | 101 | 46 | 26 | 54 | 93 |

## Smartware Gov: permission-aware recall (same ranker, different access-control strategy)

| Strategy | Recall@1000 (permitted askers) | Queries with any unreadable item in context | Forbidden askers shown gold evidence |
|---|---|---|---|
| Smartware: admissibility before ranking + provenance closure | 82.1 | 0.0 | 0.0 |
| Filter after top-10 | 77.2 | 0.0 | 0.0 |
| Filter after top-50 | 82.2 | 0.0 | 0.0 |
| Workspace scope only (no per-item ACL) | 79.4 | 99.7 | 83.3 |
| Per-item ACL on raw units, derived summaries scoped to workspace | 81.6 | 40.9 | 0.2 |
