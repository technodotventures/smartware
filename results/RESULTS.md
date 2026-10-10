# ORP-Bench results, v0.1.2

Held-out test splits only. The 15 projects are **LLM-free re-implementations of each project's recall path** (see `reports/` and `baselines/`), run with the same shared encoders as ORP, each with its best dev-tuned configuration; they are not the products themselves. Methodology, tuning budgets and limitations: `results/METHODOLOGY.md`.

**R@B**: share of gold evidence turns inside the first B tokens of the returned context (characters/4 tokens, greedy packing). **E@B**: the same, but a turn counts only if the context carries at least half of its content words. **Ans@B**: share of the gold answer's content words in the context. **SessAll@5**: LongMemEval's session metric. Verdicts use a paired bootstrap 95% CI against the best of the 15 on that metric (chosen after the fact, which favours the baselines).

**Overall: on 18 benchmark-metric comparisons against the best of the 15, ORP is better on 7, not significantly different on 6 and worse on 5.**

## LoCoMo-10 test (7 conversations, categories 1-4), n=1152

ORP against the best of the 15 on each metric: **7 better, 0 no significant difference, 0 worse**.

| Metric | ORP | Best of the 15 (tuned) | Difference [95% CI] | Verdict |
|---|---|---|---|---|
| R@500 | 74.3 | 64.0 (Supermemory Company Brain) | +10.3 [+8.0, +12.5] | ORP better |
| R@1000 | 79.7 | 74.8 (Supermemory Company Brain) | +4.9 [+3.0, +6.8] | ORP better |
| R@2000 | 85.7 | 80.9 (Supermemory Company Brain) | +4.8 [+3.1, +6.5] | ORP better |
| E@1000 | 79.7 | 74.8 (Supermemory Company Brain) | +4.9 [+3.0, +6.8] | ORP better |
| Ans@500 | 73.3 | 63.2 (Supermemory Company Brain) | +10.1 [+8.2, +11.9] | ORP better |
| Ans@1000 | 78.8 | 71.5 (Supermemory Company Brain) | +7.3 [+5.7, +9.0] | ORP better |
| Ans@2000 | 84.2 | 76.4 (Supermemory Company Brain) | +7.8 [+6.3, +9.3] | ORP better |

All systems (baselines dev-tuned):

| System | R@500 | R@1000 | R@2000 | E@1000 | Ans@500 | Ans@1000 | Ans@2000 | SessAll@5 | MRR |
|---|---|---|---|---|---|---|---|---|---|
| **ORP reference engine** | 74.3 | 79.7 | 85.7 | 79.7 | 73.3 | 78.8 | 84.2 | 80.2 | 59.3 |
| ORP, hand-tuned RRF (no learned model) | 67.9 | 76.7 | 83.5 | 76.7 | 69.5 | 77.0 | 82.8 | 75.7 | 55.1 |
| ORP, learned ranker with position/length features (not shipped) | 74.7 | 80.8 | 86.5 | 80.8 | 74.0 | 79.7 | 84.7 | 80.8 | 60.8 |
| Supermemory Company Brain | 64.0 | 74.8 | 80.9 | 74.8 | 63.2 | 71.5 | 76.4 | 79.0 | 58.9 |
| Cognee | 62.1 | 71.8 | 77.1 | 68.8 | 62.7 | 69.0 | 71.3 | 75.6 | 46.8 |
| mem0 | 62.0 | 69.0 | 74.3 | 64.8 | 60.1 | 66.6 | 72.1 | 76.6 | 45.6 |
| Supermemory | 61.8 | 68.1 | 68.1 | 60.2 | 59.3 | 64.5 | 64.5 | 72.2 | 46.3 |
| Graphiti (Zep) | 60.3 | 68.0 | 74.3 | 32.5 | 48.6 | 55.7 | 61.8 | 73.8 | 40.5 |
| Hindsight | 46.2 | 63.1 | 73.7 | 63.1 | 52.0 | 64.7 | 72.8 | 53.3 | 25.1 |
| Letta | 52.9 | 59.0 | 72.7 | 59.0 | 54.0 | 60.9 | 71.7 | 77.6 | 42.8 |
| Onyx | 17.3 | 57.4 | 75.8 | 57.4 | 30.7 | 59.6 | 72.1 | 77.6 | 75.3 |
| Basic Memory | 5.5 | 50.7 | 66.6 | 50.7 | 46.9 | 57.0 | 70.6 | 71.5 | 70.6 |
| MemOS | 38.5 | 49.8 | 61.3 | 49.8 | 47.4 | 57.6 | 67.9 | 57.9 | 33.2 |
| LangMem | 40.7 | 48.9 | 53.5 | 48.9 | 43.9 | 52.3 | 56.1 | 57.1 | 23.4 |
| MCP memory server | 39.1 | 43.4 | 47.3 | 43.4 | 47.0 | 54.4 | 61.3 | 61.3 | 32.0 |
| GBrain | 11.5 | 41.6 | 56.5 | 41.4 | 23.3 | 50.3 | 63.9 | 59.0 | 54.6 |
| MemoryOS | 32.6 | 40.7 | 47.8 | 37.5 | 45.9 | 56.8 | 62.9 | 48.2 | 28.9 |
| Sylph | 15.4 | 39.7 | 45.3 | 39.7 | 31.6 | 53.5 | 58.4 | 34.6 | 14.0 |
| _BM25 over turns (reference)_ | 60.9 | 67.8 | 73.8 | 67.8 | 60.9 | 68.0 | 72.1 | 76.6 | 46.0 |
| _Dense over turns (reference)_ | 36.3 | 45.9 | 51.0 | 45.9 | 46.2 | 56.0 | 60.4 | 52.5 | 19.6 |

Baselines with their default settings (v0.1.1 run; E@B was added later):

| System | R@500 | R@1000 | R@2000 | Ans@500 | Ans@1000 | Ans@2000 | SessAll@5 | MRR |
|---|---|---|---|---|---|---|---|---|
| Supermemory Company Brain | 62.2 | 72.5 | 79.1 | 61.9 | 69.8 | 75.6 | 77.4 | 55.8 |
| Cognee | 57.5 | 69.6 | 78.2 | 60.2 | 70.1 | 75.6 | 72.0 | 45.4 |
| Graphiti (Zep) | 60.3 | 68.0 | 74.3 | 48.6 | 55.7 | 61.8 | 73.8 | 40.5 |
| Supermemory | 31.9 | 64.5 | 66.8 | 32.6 | 62.8 | 64.7 | 13.5 | 3.8 |
| mem0 | 57.4 | 63.8 | 67.8 | 58.7 | 65.1 | 69.5 | 74.1 | 43.0 |
| Letta | 49.4 | 57.2 | 69.3 | 52.2 | 59.9 | 70.0 | 75.4 | 35.5 |
| Basic Memory | 4.6 | 50.8 | 66.8 | 33.8 | 57.1 | 70.9 | 71.5 | 71.1 |
| Hindsight | 35.3 | 50.4 | 63.1 | 44.8 | 56.8 | 66.9 | 43.5 | 19.1 |
| MemOS | 38.3 | 49.3 | 60.7 | 47.1 | 57.4 | 67.2 | 58.0 | 33.0 |
| Onyx | 5.9 | 46.7 | 71.4 | 22.0 | 52.2 | 69.7 | 76.9 | 76.0 |
| MCP memory server | 39.1 | 43.4 | 47.3 | 47.0 | 54.4 | 61.3 | 61.3 | 32.0 |
| MemoryOS | 31.0 | 38.3 | 41.1 | 44.7 | 54.4 | 56.2 | 47.8 | 27.2 |
| Sylph | 13.6 | 35.6 | 39.8 | 30.8 | 50.3 | 54.2 | 34.1 | 13.4 |
| LangMem | 27.0 | 34.9 | 43.6 | 34.5 | 42.7 | 51.4 | 57.1 | 23.4 |
| GBrain | 4.9 | 29.1 | 51.3 | 30.3 | 40.5 | 59.7 | 59.0 | 58.0 |

ORP v0.1.1 and v0.1.2 test results are identical on this benchmark.

Recall@1000 by question category:

| System | multi-hop | open-domain | single-hop | temporal |
|---|---|---|---|---|
| **ORP reference engine** | 54.6 | 44.4 | 90.3 | 83.8 |
| Supermemory Company Brain | 44.3 | 42.6 | 88.6 | 73.9 |
| Cognee | 44.7 | 41.0 | 84.4 | 71.1 |
| mem0 | 45.5 | 40.2 | 78.2 | 74.0 |
| Supermemory | 34.4 | 39.0 | 82.5 | 67.8 |
| Graphiti (Zep) | 44.9 | 40.4 | 77.1 | 72.2 |
| Hindsight | 36.8 | 32.8 | 74.6 | 64.6 |
| _BM25 over turns (reference)_ | 42.0 | 36.3 | 78.8 | 70.7 |
| _n_ | 208 | 73 | 641 | 230 |

## LME-R test (LongMemEval oracle questions, recombined 40-session haystacks), n=382

ORP against the best of the 15 on each metric: **0 better, 2 no significant difference, 5 worse**.

| Metric | ORP | Best of the 15 (tuned) | Difference [95% CI] | Verdict |
|---|---|---|---|---|
| R@500 | 73.9 | 71.7 (mem0) | +2.2 [-1.4, +5.8] | no significant difference |
| R@1000 | 81.6 | 83.2 (Hindsight) | -1.7 [-5.4, +2.0] | no significant difference |
| R@2000 | 84.9 | 88.6 (Hindsight) | -3.7 [-6.7, -0.8] | ORP worse |
| E@1000 | 72.5 | 83.2 (Hindsight) | -10.7 [-14.7, -6.6] | ORP worse |
| Ans@500 | 57.1 | 63.5 (Supermemory Company Brain) | -6.4 [-10.4, -2.7] | ORP worse |
| Ans@1000 | 63.9 | 71.4 (Supermemory Company Brain) | -7.5 [-11.6, -3.8] | ORP worse |
| Ans@2000 | 70.6 | 78.0 (mem0) | -7.4 [-10.6, -4.4] | ORP worse |

All systems (baselines dev-tuned):

| System | R@500 | R@1000 | R@2000 | E@1000 | Ans@500 | Ans@1000 | Ans@2000 | SessAll@5 | MRR |
|---|---|---|---|---|---|---|---|---|---|
| **ORP reference engine** | 73.9 | 81.6 | 84.9 | 72.5 | 57.1 | 63.9 | 70.6 | 78.5 | 71.5 |
| ORP, hand-tuned RRF (no learned model) | 73.6 | 81.6 | 86.9 | 73.3 | 55.6 | 62.6 | 71.6 | 80.4 | 68.8 |
| ORP, learned ranker with position/length features (not shipped) | 72.3 | 80.3 | 85.3 | 71.3 | 57.1 | 64.0 | 72.2 | 78.0 | 69.4 |
| Hindsight | 71.3 | 83.2 | 88.6 | 83.2 | 54.0 | 67.1 | 77.4 | 87.4 | 67.4 |
| mem0 | 71.7 | 79.8 | 86.5 | 66.0 | 60.7 | 70.0 | 78.0 | 81.9 | 58.7 |
| Graphiti (Zep) | 68.0 | 78.6 | 86.2 | 50.1 | 58.5 | 68.3 | 76.7 | 81.7 | 50.3 |
| LangMem | 68.4 | 75.2 | 81.8 | 75.2 | 57.0 | 68.1 | 76.0 | 82.2 | 64.2 |
| Letta | 62.9 | 69.7 | 76.5 | 69.7 | 57.9 | 66.4 | 76.0 | 83.0 | 64.0 |
| Supermemory Company Brain | 60.3 | 66.6 | 76.5 | 64.3 | 63.5 | 71.4 | 77.5 | 82.7 | 59.7 |
| MCP memory server | 46.8 | 54.4 | 59.9 | 54.4 | 46.4 | 55.1 | 61.5 | 60.2 | 47.2 |
| Cognee | 42.2 | 50.7 | 62.1 | 42.2 | 46.6 | 61.1 | 71.8 | 80.4 | 62.6 |
| Basic Memory | 16.1 | 46.5 | 68.7 | 46.2 | 34.8 | 57.1 | 71.6 | 81.4 | 77.4 |
| MemoryOS | 28.1 | 40.8 | 50.1 | 35.5 | 42.0 | 58.3 | 67.9 | 73.8 | 50.7 |
| Onyx | 18.9 | 37.6 | 57.7 | 37.6 | 30.8 | 48.5 | 63.6 | 86.1 | 78.4 |
| Supermemory | 33.2 | 34.3 | 34.6 | 21.8 | 30.2 | 33.3 | 33.6 | 34.0 | 46.4 |
| MemOS | 16.9 | 30.4 | 44.9 | 30.4 | 31.7 | 53.8 | 67.6 | 71.7 | 51.0 |
| GBrain | 4.0 | 27.8 | 38.5 | 27.0 | 20.5 | 51.6 | 66.0 | 74.9 | 49.9 |
| Sylph | 9.7 | 12.7 | 25.3 | 12.7 | 24.6 | 29.9 | 46.8 | 40.1 | 23.9 |
| _BM25 over turns (reference)_ | 67.7 | 75.0 | 78.0 | 75.0 | 57.3 | 68.9 | 75.0 | 83.0 | 63.4 |
| _Dense over turns (reference)_ | 68.9 | 75.1 | 80.1 | 75.1 | 55.5 | 67.9 | 75.2 | 82.2 | 63.1 |

Baselines with their default settings (v0.1.1 run; E@B was added later):

| System | R@500 | R@1000 | R@2000 | Ans@500 | Ans@1000 | Ans@2000 | SessAll@5 | MRR |
|---|---|---|---|---|---|---|---|---|
| Graphiti (Zep) | 68.0 | 78.6 | 86.2 | 58.5 | 68.3 | 76.7 | 81.7 | 50.3 |
| mem0 | 64.5 | 76.2 | 82.8 | 59.0 | 69.9 | 78.1 | 82.5 | 60.5 |
| Letta | 66.2 | 70.4 | 79.2 | 62.4 | 68.0 | 76.7 | 84.3 | 66.1 |
| Supermemory Company Brain | 59.9 | 68.0 | 75.6 | 63.8 | 71.3 | 77.5 | 83.0 | 58.1 |
| LangMem | 58.1 | 66.8 | 74.8 | 50.2 | 56.6 | 66.3 | 82.2 | 64.2 |
| Hindsight | 57.8 | 60.9 | 71.6 | 50.0 | 61.2 | 69.0 | 79.6 | 44.8 |
| MCP memory server | 46.8 | 54.4 | 59.9 | 46.4 | 55.1 | 61.5 | 60.2 | 47.2 |
| Cognee | 32.9 | 46.7 | 58.1 | 42.1 | 60.4 | 70.5 | 79.6 | 62.6 |
| Basic Memory | 8.3 | 46.6 | 68.7 | 20.1 | 57.8 | 71.9 | 81.4 | 77.5 |
| Supermemory | 13.7 | 40.9 | 42.4 | 19.6 | 39.8 | 43.7 | 9.9 | 4.8 |
| MemoryOS | 25.6 | 40.5 | 49.7 | 40.0 | 59.7 | 69.6 | 78.0 | 53.8 |
| MemOS | 17.3 | 31.0 | 44.5 | 31.8 | 54.2 | 67.0 | 72.8 | 50.9 |
| Onyx | 10.0 | 21.5 | 38.1 | 25.8 | 41.1 | 57.9 | 85.9 | 82.1 |
| Sylph | 9.9 | 12.7 | 22.4 | 24.8 | 28.1 | 41.7 | 38.2 | 23.5 |
| GBrain | 4.4 | 5.2 | 11.9 | 22.6 | 27.7 | 40.9 | 74.9 | 78.0 |

ORP v0.1.1 and v0.1.2 test results are identical on this benchmark.

Without the 45 test questions affected by LME-R construction quirks (`results/lme_quirks.json`):

| System | R@500 | R@1000 | R@2000 | E@1000 | Ans@500 | Ans@1000 | Ans@2000 |
|---|---|---|---|---|---|---|---|
| **ORP reference engine** | 77.7 | 86.1 | 89.7 | 75.8 | 59.4 | 66.4 | 72.7 |
| Hindsight | 70.2 | 82.5 | 87.9 | 82.5 | 53.2 | 67.3 | 77.5 |
| mem0 | 71.1 | 79.0 | 86.0 | 65.6 | 60.7 | 70.0 | 78.1 |
| Graphiti (Zep) | 67.8 | 78.0 | 85.6 | 49.4 | 58.5 | 68.4 | 76.7 |
| LangMem | 67.8 | 74.6 | 81.4 | 74.6 | 57.5 | 68.7 | 76.3 |
| Letta | 61.9 | 68.9 | 75.7 | 68.9 | 57.9 | 67.3 | 76.1 |
| _BM25 over turns (reference)_ | 67.1 | 74.4 | 77.2 | 74.4 | 58.2 | 69.3 | 75.3 |

Verdicts on this subset: 2 better, 1 no significant difference, 4 worse.

| Metric | ORP | Best of the 15 (tuned) | Difference [95% CI] | Verdict |
|---|---|---|---|---|
| R@500 | 77.7 | 71.1 (mem0) | +6.7 [+3.2, +10.2] | ORP better |
| R@1000 | 86.1 | 82.5 (Hindsight) | +3.6 [+0.3, +6.8] | ORP better |
| R@2000 | 89.7 | 87.9 (Hindsight) | +1.8 [-0.7, +4.2] | no significant difference |
| E@1000 | 75.8 | 82.5 (Hindsight) | -6.7 [-10.9, -2.8] | ORP worse |
| Ans@500 | 59.4 | 64.0 (Supermemory Company Brain) | -4.6 [-8.7, -0.6] | ORP worse |
| Ans@1000 | 66.4 | 71.6 (Supermemory Company Brain) | -5.2 [-9.3, -1.3] | ORP worse |
| Ans@2000 | 72.7 | 78.1 (mem0) | -5.4 [-9.0, -2.1] | ORP worse |

Recall@1000 by question category:

| System | knowledge-update | multi-session | single-session-assistant | single-session-preference | single-session-user | temporal-reasoning |
|---|---|---|---|---|---|---|
| **ORP reference engine** | 94.1 | 81.0 | 95.7 | 45.5 | 100.0 | 66.2 |
| Hindsight | 87.4 | 80.3 | 73.9 | 67.9 | 95.4 | 85.5 |
| mem0 | 93.0 | 62.2 | 93.5 | 55.8 | 96.3 | 80.5 |
| Graphiti (Zep) | 90.1 | 62.2 | 93.5 | 56.4 | 98.1 | 76.4 |
| LangMem | 91.7 | 60.9 | 76.1 | 62.2 | 95.4 | 71.2 |
| Letta | 81.7 | 56.4 | 82.6 | 42.9 | 91.7 | 64.5 |
| Supermemory Company Brain | 76.9 | 47.9 | 91.3 | 39.7 | 90.7 | 61.5 |
| _BM25 over turns (reference)_ | 87.4 | 62.8 | 84.8 | 59.0 | 88.0 | 71.9 |
| _n_ | 62 | 101 | 46 | 26 | 54 | 93 |

## HotpotQA-50 (held-out, out-of-domain, answers only), n=50

Cognee's evaluation set: 500 Wikipedia paragraphs, 50 multi-hop questions. Added after the v0.1.1 freeze; nothing was tuned on it. Only answer recall can be scored.

ORP against the best of the 15: 0 better, 4 no significant difference, 0 worse.

| Metric | ORP | Best of the 15 (tuned) | Difference [95% CI] | Verdict |
|---|---|---|---|---|
| Ans@500 | 79.5 | 84.3 (Cognee) | -4.8 [-13.0, +2.7] | no significant difference |
| Ans@1000 | 88.2 | 90.0 (Onyx) | -1.8 [-5.8, +1.3] | no significant difference |
| Ans@2000 | 93.2 | 94.0 (Letta) | -0.8 [-5.8, +4.7] | no significant difference |
| Ans@4000 | 93.2 | 98.0 (Letta) | -4.8 [-10.5, +0.0] | no significant difference |

| System | Ans@500 | Ans@1000 | Ans@2000 | Ans@4000 |
|---|---|---|---|---|
| **ORP reference engine** | 79.5 | 88.2 | 93.2 | 93.2 |
| ORP, hand-tuned RRF (no learned model) | 74.2 | 88.2 | 93.2 | 93.2 |
| Graphiti (Zep) | 71.0 | 81.2 | 87.2 | 88.2 |
| Hindsight | 64.8 | 75.8 | 86.7 | 89.2 |
| mem0 | 74.2 | 87.5 | 91.0 | 92.0 |
| Letta | 76.0 | 88.3 | 94.0 | 98.0 |
| Supermemory Company Brain | 78.7 | 83.7 | 90.3 | 96.0 |
| GBrain | 62.4 | 74.2 | 84.3 | 87.0 |
| Supermemory | 77.5 | 80.7 | 80.7 | 80.7 |
| Onyx | 77.1 | 90.0 | 90.7 | 96.0 |
| Cognee | 84.3 | 87.0 | 92.3 | 92.3 |
| Basic Memory | 78.2 | 81.5 | 87.5 | 87.5 |
| Sylph | 64.2 | 75.7 | 85.0 | 87.7 |
| MCP memory server | 77.5 | 84.2 | 89.2 | 93.6 |
| MemOS | 58.9 | 73.0 | 81.5 | 85.7 |
| LangMem | 60.5 | 78.7 | 84.0 | 90.3 |
| MemoryOS | 0.0 | 0.0 | 0.0 | 0.0 |
| Graphiti (Zep) | 71.0 | 81.2 | 87.2 | 88.2 |
| Hindsight | 36.2 | 40.1 | 67.7 | 89.2 |
| mem0 | 81.2 | 88.2 | 91.7 | 94.0 |
| Letta | 65.2 | 83.7 | 90.3 | 98.0 |
| Supermemory Company Brain | 71.0 | 80.0 | 89.7 | 94.7 |
| GBrain | 62.4 | 74.2 | 84.3 | 87.0 |
| Supermemory | 5.0 | 45.5 | 81.3 | 81.3 |
| Onyx | 72.8 | 84.7 | 92.7 | 96.0 |
| Cognee | 80.9 | 86.3 | 95.0 | 95.0 |
| Basic Memory | 80.2 | 84.2 | 87.5 | 90.2 |
| Sylph | 57.2 | 69.3 | 87.7 | 87.7 |
| MCP memory server | 77.5 | 84.2 | 89.2 | 93.6 |
| MemOS | 58.9 | 73.0 | 81.5 | 85.7 |
| LangMem | 54.4 | 66.2 | 80.2 | 85.7 |
| MemoryOS | 0.0 | 0.0 | 0.0 | 0.0 |
| _BM25 over turns (reference)_ | 82.7 | 91.0 | 96.0 | 96.0 |
| _Dense over turns (reference)_ | 64.4 | 81.7 | 83.7 | 92.0 |

## Official LongMemEval-S (frozen configurations, scored once), n=382

The official `longmemeval_s_cleaned.json` haystacks (38-62 sessions, about 490 turns and 115k tokens per question; LME-R used 40 rebuilt sessions), the same 382 test questions. Nothing was tuned on it: ORP's shipped ranker and every baseline's raw-turn dev-tuned configuration. Raw turns only.

ORP against the best of the 15: **0 better, 3 no significant difference, 6 worse**.

| Metric | ORP | Best of the 15 (tuned) | Difference [95% CI] | Verdict |
|---|---|---|---|---|
| R@500 | 75.7 | 74.0 (mem0) | +1.7 [-1.8, +5.2] | no significant difference |
| R@1000 | 82.8 | 82.0 (mem0) | +0.9 [-2.3, +3.8] | no significant difference |
| R@2000 | 86.2 | 87.1 (mem0) | -0.9 [-4.1, +2.1] | no significant difference |
| E@1000 | 73.8 | 79.9 (Hindsight) | -6.2 [-10.0, -2.3] | ORP worse |
| Ans@500 | 58.4 | 62.4 (Supermemory Company Brain) | -4.0 [-7.8, -0.1] | ORP worse |
| Ans@1000 | 65.8 | 70.0 (mem0) | -4.1 [-7.5, -1.0] | ORP worse |
| Ans@2000 | 73.8 | 78.7 (Graphiti (Zep)) | -4.9 [-8.2, -1.8] | ORP worse |
| Ans@4000 | 78.1 | 82.3 (Supermemory Company Brain) | -4.1 [-7.2, -1.1] | ORP worse |
| SessAll@5 | 80.6 | 88.2 (Onyx) | -7.6 [-11.0, -4.5] | ORP worse |

| System | R@500 | R@1000 | R@2000 | E@1000 | Ans@500 | Ans@1000 | Ans@2000 | Ans@4000 | SessAll@5 |
|---|---|---|---|---|---|---|---|---|---|
| **ORP reference engine** | 75.7 | 82.8 | 86.2 | 73.8 | 58.4 | 65.8 | 73.8 | 78.1 | 80.6 |
| ORP, hand-tuned RRF (no learned model) | 75.3 | 82.6 | 87.3 | 74.0 | 57.0 | 63.7 | 70.9 | 78.3 | 82.2 |
| mem0 | 74.0 | 82.0 | 87.1 | 67.5 | 60.0 | 70.0 | 77.1 | 79.2 | 85.3 |
| Graphiti (Zep) | 70.4 | 80.1 | 86.8 | 51.6 | 57.6 | 68.9 | 78.7 | 80.3 | 86.1 |
| Hindsight | 70.8 | 79.9 | 86.3 | 79.9 | 52.4 | 64.0 | 76.5 | 79.1 | 86.6 |
| LangMem | 69.5 | 77.5 | 83.1 | 77.5 | 56.6 | 69.2 | 77.0 | 81.8 | 84.8 |
| Letta | 63.8 | 72.6 | 78.7 | 72.6 | 58.8 | 67.7 | 75.1 | 81.0 | 84.6 |
| Supermemory Company Brain | 61.5 | 69.4 | 78.3 | 67.3 | 62.4 | 69.8 | 76.8 | 82.3 | 86.1 |
| Cognee | 41.8 | 52.8 | 65.3 | 44.1 | 44.0 | 61.1 | 71.9 | 79.1 | 83.8 |
| MCP memory server | 48.0 | 52.6 | 57.2 | 52.6 | 46.7 | 54.9 | 64.3 | 70.6 | 65.4 |
| Basic Memory | 18.7 | 49.2 | 71.2 | 48.8 | 34.2 | 57.4 | 72.1 | 80.2 | 83.2 |
| MemoryOS | 27.9 | 44.3 | 52.7 | 39.0 | 39.1 | 57.0 | 67.0 | 75.2 | 74.3 |
| Onyx | 17.9 | 36.6 | 57.6 | 36.6 | 31.0 | 48.3 | 64.3 | 78.1 | 88.2 |
| Supermemory | 34.0 | 35.2 | 35.5 | 21.9 | 30.0 | 33.6 | 33.9 | 33.9 | 35.6 |
| MemOS | 15.3 | 31.7 | 44.7 | 31.7 | 29.6 | 53.1 | 64.4 | 75.6 | 73.6 |
| GBrain | 3.1 | 28.1 | 38.8 | 27.3 | 17.0 | 52.0 | 65.3 | 73.7 | 76.2 |
| Sylph | 9.0 | 10.5 | 23.0 | 10.5 | 19.3 | 24.3 | 43.5 | 67.8 | 35.3 |
| _BM25 over turns (reference)_ | 69.2 | 75.8 | 80.2 | 75.8 | 56.7 | 69.4 | 75.7 | 81.0 | 85.3 |
| _Dense over turns (reference)_ | 69.7 | 75.3 | 83.0 | 75.3 | 55.5 | 68.2 | 75.2 | 81.9 | 83.5 |

Without the 29 test questions with gold evidence dated after the question (inadmissible to an engine that honours `as_of`) in the official data (`results/lmes_quirks.json`): **3 better, 5 no significant difference, 1 worse**.

| Metric | ORP | Best of the 15 (tuned) | Difference [95% CI] | Verdict |
|---|---|---|---|---|
| R@500 | 80.2 | 73.6 (mem0) | +6.6 [+3.4, +9.8] | ORP better |
| R@1000 | 87.6 | 81.3 (mem0) | +6.3 [+3.9, +8.7] | ORP better |
| R@2000 | 91.1 | 86.6 (mem0) | +4.6 [+2.1, +7.0] | ORP better |
| E@1000 | 77.8 | 78.7 (Hindsight) | -0.9 [-4.4, +2.6] | no significant difference |
| Ans@500 | 59.3 | 61.2 (Supermemory Company Brain) | -1.9 [-5.8, +2.1] | no significant difference |
| Ans@1000 | 66.8 | 69.3 (mem0) | -2.5 [-5.7, +0.6] | no significant difference |
| Ans@2000 | 75.0 | 78.2 (Graphiti (Zep)) | -3.1 [-6.3, -0.2] | ORP worse |
| Ans@4000 | 79.6 | 82.1 (Supermemory Company Brain) | -2.4 [-5.5, +0.6] | no significant difference |
| SessAll@5 | 87.3 | 87.8 (Onyx) | -0.6 [-2.8, +1.4] | no significant difference |

Without the 54 test questions with any haystack session dated after the question in the official data (`results/lmes_quirks.json`): **3 better, 6 no significant difference, 0 worse**.

| Metric | ORP | Best of the 15 (tuned) | Difference [95% CI] | Verdict |
|---|---|---|---|---|
| R@500 | 79.1 | 72.5 (mem0) | +6.6 [+3.2, +9.9] | ORP better |
| R@1000 | 86.8 | 80.3 (mem0) | +6.5 [+3.9, +8.9] | ORP better |
| R@2000 | 90.5 | 86.0 (mem0) | +4.5 [+1.7, +7.1] | ORP better |
| E@1000 | 76.3 | 78.2 (Hindsight) | -1.9 [-5.6, +1.8] | no significant difference |
| Ans@500 | 58.0 | 60.7 (Supermemory Company Brain) | -2.7 [-6.9, +1.5] | no significant difference |
| Ans@1000 | 65.8 | 68.9 (Supermemory Company Brain) | -3.1 [-7.0, +0.9] | no significant difference |
| Ans@2000 | 74.5 | 77.4 (Graphiti (Zep)) | -2.9 [-6.1, +0.2] | no significant difference |
| Ans@4000 | 79.4 | 81.3 (Supermemory Company Brain) | -1.9 [-5.1, +1.2] | no significant difference |
| SessAll@5 | 86.3 | 86.9 (Onyx) | -0.6 [-2.7, +1.5] | no significant difference |

Recall@1000 by question category:

| System | knowledge-update | multi-session | single-session-assistant | single-session-preference | single-session-user | temporal-reasoning |
|---|---|---|---|---|---|---|
| **ORP reference engine** | 97.0 | 83.3 | 95.7 | 49.4 | 100.0 | 65.8 |
| mem0 | 93.0 | 67.3 | 93.5 | 60.9 | 94.4 | 83.5 |
| Graphiti (Zep) | 90.1 | 63.5 | 95.7 | 60.3 | 98.1 | 78.7 |
| Hindsight | 85.5 | 77.9 | 54.3 | 66.0 | 91.7 | 88.2 |
| LangMem | 89.0 | 64.0 | 78.3 | 74.4 | 97.2 | 73.5 |
| Letta | 82.8 | 61.2 | 82.6 | 44.9 | 96.3 | 67.2 |
| Supermemory Company Brain | 75.5 | 51.2 | 93.5 | 44.9 | 94.4 | 65.6 |
| _BM25 over turns (reference)_ | 87.6 | 65.3 | 87.0 | 43.6 | 90.7 | 74.3 |
| _n_ | 62 | 101 | 46 | 26 | 54 | 93 |

## Official LongMemEval-M (frozen configurations, scored once), n=382

The official `longmemeval_m_cleaned.json` haystacks (about 475 sessions, 4,900 turns and 1.2M tokens per question; LME-R used 40 rebuilt sessions), the same 382 test questions. Nothing was tuned on it: ORP's shipped ranker and every baseline's raw-turn dev-tuned configuration. Raw turns only.

ORP against the best of the 15: **2 better, 5 no significant difference, 2 worse**.

| Metric | ORP | Best of the 15 (tuned) | Difference [95% CI] | Verdict |
|---|---|---|---|---|
| R@500 | 67.3 | 60.4 (mem0) | +6.9 [+3.2, +10.7] | ORP better |
| R@1000 | 73.6 | 68.8 (mem0) | +4.8 [+1.3, +8.3] | ORP better |
| R@2000 | 79.6 | 76.7 (mem0) | +2.9 [-0.6, +6.2] | no significant difference |
| E@1000 | 65.0 | 65.4 (Hindsight) | -0.4 [-3.9, +3.1] | no significant difference |
| Ans@500 | 57.0 | 60.9 (Supermemory Company Brain) | -3.9 [-7.7, -0.1] | ORP worse |
| Ans@1000 | 64.4 | 67.7 (Supermemory Company Brain) | -3.3 [-7.1, +0.5] | no significant difference |
| Ans@2000 | 73.6 | 75.7 (Graphiti (Zep)) | -2.1 [-5.6, +1.4] | no significant difference |
| Ans@4000 | 79.0 | 80.4 (Supermemory Company Brain) | -1.4 [-4.4, +1.4] | no significant difference |
| SessAll@5 | 68.6 | 72.0 (Onyx) | -3.4 [-6.8, -0.3] | ORP worse |

| System | R@500 | R@1000 | R@2000 | E@1000 | Ans@500 | Ans@1000 | Ans@2000 | Ans@4000 | SessAll@5 |
|---|---|---|---|---|---|---|---|---|---|
| **ORP reference engine** | 67.3 | 73.6 | 79.6 | 65.0 | 57.0 | 64.4 | 73.6 | 79.0 | 68.6 |
| ORP, hand-tuned RRF (no learned model) | 64.0 | 72.2 | 77.1 | 64.2 | 53.8 | 64.1 | 72.0 | 79.8 | 68.6 |
| mem0 | 60.4 | 68.8 | 76.7 | 55.4 | 58.2 | 66.3 | 74.5 | 78.5 | 67.5 |
| Graphiti (Zep) | 56.8 | 67.3 | 73.9 | 41.5 | 55.8 | 65.3 | 75.7 | 76.8 | 66.2 |
| Hindsight | 56.0 | 65.4 | 74.3 | 65.4 | 47.2 | 57.1 | 70.4 | 73.0 | 65.7 |
| Letta | 55.6 | 62.6 | 69.7 | 62.6 | 55.7 | 65.4 | 73.7 | 79.6 | 70.7 |
| Supermemory Company Brain | 55.1 | 62.0 | 68.8 | 59.0 | 60.9 | 67.7 | 74.6 | 80.4 | 70.9 |
| LangMem | 55.0 | 61.6 | 67.9 | 61.6 | 53.9 | 65.4 | 71.6 | 78.7 | 62.6 |
| MCP memory server | 42.9 | 47.1 | 50.1 | 47.1 | 43.5 | 50.4 | 58.2 | 65.5 | 44.0 |
| Basic Memory | 12.7 | 41.5 | 56.9 | 41.3 | 33.6 | 53.8 | 66.9 | 76.6 | 64.1 |
| Cognee | 28.7 | 38.9 | 51.1 | 33.3 | 41.3 | 57.6 | 69.2 | 76.9 | 65.4 |
| Onyx | 22.8 | 37.4 | 58.0 | 37.4 | 37.2 | 52.3 | 66.2 | 76.0 | 72.0 |
| MemoryOS | 27.1 | 35.1 | 41.1 | 30.1 | 40.7 | 51.8 | 62.5 | 72.5 | 49.0 |
| Supermemory | 29.8 | 31.0 | 31.3 | 19.8 | 28.7 | 32.0 | 32.3 | 32.3 | 30.4 |
| MemOS | 11.7 | 23.0 | 31.2 | 23.0 | 29.8 | 47.7 | 59.4 | 70.5 | 47.9 |
| GBrain | 2.3 | 22.7 | 29.7 | 21.9 | 20.2 | 47.8 | 60.1 | 69.6 | 50.0 |
| Sylph | 3.2 | 6.2 | 17.1 | 6.2 | 15.2 | 22.2 | 41.1 | 60.6 | 7.9 |
| _BM25 over turns (reference)_ | 59.1 | 66.5 | 72.7 | 66.5 | 55.3 | 66.9 | 74.7 | 80.2 | 72.0 |
| _Dense over turns (reference)_ | 52.8 | 60.1 | 66.2 | 60.1 | 53.1 | 64.0 | 72.2 | 79.0 | 62.8 |

Without the 29 test questions with gold evidence dated after the question (inadmissible to an engine that honours `as_of`) in the official data (`results/lmem_quirks.json`): **4 better, 5 no significant difference, 0 worse**.

| Metric | ORP | Best of the 15 (tuned) | Difference [95% CI] | Verdict |
|---|---|---|---|---|
| R@500 | 71.5 | 59.9 (mem0) | +11.5 [+8.1, +15.0] | ORP better |
| R@1000 | 77.8 | 68.2 (mem0) | +9.6 [+6.5, +12.7] | ORP better |
| R@2000 | 84.1 | 76.3 (mem0) | +7.8 [+4.7, +10.8] | ORP better |
| E@1000 | 68.5 | 64.6 (Hindsight) | +4.0 [+0.6, +7.2] | ORP better |
| Ans@500 | 57.7 | 60.7 (Supermemory Company Brain) | -3.0 [-7.1, +1.0] | no significant difference |
| Ans@1000 | 64.8 | 67.6 (Supermemory Company Brain) | -2.8 [-6.8, +1.2] | no significant difference |
| Ans@2000 | 74.0 | 75.0 (Graphiti (Zep)) | -1.0 [-4.5, +2.7] | no significant difference |
| Ans@4000 | 79.0 | 80.1 (Supermemory Company Brain) | -1.1 [-4.2, +1.8] | no significant difference |
| SessAll@5 | 74.2 | 73.1 (Onyx) | +1.1 [-1.7, +4.0] | no significant difference |

Without the 85 test questions with any haystack session dated after the question in the official data (`results/lmem_quirks.json`): **3 better, 4 no significant difference, 2 worse**.

| Metric | ORP | Best of the 15 (tuned) | Difference [95% CI] | Verdict |
|---|---|---|---|---|
| R@500 | 68.0 | 57.3 (mem0) | +10.7 [+7.0, +14.5] | ORP better |
| R@1000 | 74.5 | 65.9 (mem0) | +8.6 [+5.2, +12.0] | ORP better |
| R@2000 | 81.5 | 73.3 (mem0) | +8.2 [+4.8, +11.8] | ORP better |
| E@1000 | 63.4 | 61.9 (Hindsight) | +1.6 [-2.2, +5.2] | no significant difference |
| Ans@500 | 53.5 | 58.8 (Supermemory Company Brain) | -5.3 [-9.6, -1.0] | ORP worse |
| Ans@1000 | 61.0 | 65.6 (Supermemory Company Brain) | -4.5 [-8.9, -0.5] | ORP worse |
| Ans@2000 | 71.5 | 72.8 (Graphiti (Zep)) | -1.3 [-5.6, +2.7] | no significant difference |
| Ans@4000 | 77.0 | 79.1 (Supermemory Company Brain) | -2.1 [-5.8, +1.3] | no significant difference |
| SessAll@5 | 70.7 | 69.0 (Onyx) | +1.7 [-1.3, +4.7] | no significant difference |

Recall@1000 by question category:

| System | knowledge-update | multi-session | single-session-assistant | single-session-preference | single-session-user | temporal-reasoning |
|---|---|---|---|---|---|---|
| **ORP reference engine** | 94.9 | 67.6 | 93.5 | 32.1 | 93.5 | 56.2 |
| mem0 | 81.2 | 47.8 | 91.3 | 35.9 | 90.7 | 68.7 |
| Graphiti (Zep) | 86.8 | 45.9 | 91.3 | 25.6 | 92.6 | 62.8 |
| Hindsight | 75.5 | 67.8 | 28.3 | 30.1 | 85.2 | 72.8 |
| Letta | 78.0 | 48.9 | 82.6 | 17.3 | 86.1 | 56.3 |
| Supermemory Company Brain | 72.8 | 44.6 | 95.7 | 16.0 | 88.9 | 54.2 |
| LangMem | 79.0 | 45.8 | 76.1 | 26.3 | 81.5 | 58.2 |
| _BM25 over turns (reference)_ | 79.6 | 52.5 | 84.8 | 26.3 | 82.4 | 66.0 |
| _n_ | 62 | 101 | 46 | 26 | 54 | 93 |

## LLM-extraction track

Facts extracted once by Claude Haiku sub-agents with a mem0-style brief (`bench/extraction/EXTRACT.md`), frozen in `results/extraction/` and shared by every system, like the encoders. Each system's store (raw turns or extracted facts; for ORP also raw turns as evidence plus facts as derived memories) was chosen on that benchmark's dev split only.

### LoCoMo-10 test, n=1152

ORP's dev choice: `orp+evidence`. Against the best of the 15: **5 better, 1 no significant difference, 0 worse**.

| Metric | ORP | Best of the 15 | Difference [95% CI] | Verdict |
|---|---|---|---|---|
| R@500 | 76.2 | 74.5 (mem0, facts) | +1.6 [-0.2, +3.5] | no significant difference |
| R@1000 | 82.4 | 79.8 (mem0, facts) | +2.6 [+1.0, +4.3] | ORP better |
| R@2000 | 87.3 | 85.5 (mem0, facts) | +1.8 [+0.3, +3.4] | ORP better |
| Ans@500 | 75.2 | 70.6 (mem0, facts) | +4.6 [+2.9, +6.3] | ORP better |
| Ans@1000 | 80.6 | 76.4 (mem0, facts) | +4.2 [+2.8, +5.6] | ORP better |
| Ans@2000 | 84.6 | 81.3 (mem0, facts) | +3.3 [+2.0, +4.5] | ORP better |

| System (store) | R@500 | R@1000 | R@2000 | Ans@500 | Ans@1000 | Ans@2000 | SessAll@5 | MRR |
|---|---|---|---|---|---|---|---|---|
| **ORP (turns as evidence + facts)** | 76.2 | 82.4 | 87.3 | 75.2 | 80.6 | 84.6 | 80.6 | 61.7 |
| ORP (facts only) | 78.5 | 84.8 | 89.5 | 72.7 | 78.9 | 83.1 | 79.1 | 63.9 |
| mem0 (facts) | 74.5 | 79.8 | 85.5 | 70.6 | 76.4 | 81.3 | 77.9 | 59.3 |
| Supermemory Company Brain (facts) | 73.1 | 79.6 | 84.1 | 69.3 | 75.2 | 79.3 | 78.2 | 69.0 |
| Graphiti (Zep) (facts) | 69.8 | 76.7 | 83.4 | 66.9 | 73.4 | 79.3 | 77.0 | 54.6 |
| Cognee (facts) | 68.7 | 75.9 | 77.9 | 66.9 | 73.2 | 75.9 | 76.4 | 55.6 |
| Letta (facts) | 68.4 | 73.7 | 80.2 | 64.9 | 71.0 | 76.6 | 77.9 | 59.3 |
| Supermemory (facts) | 64.7 | 72.6 | 72.6 | 63.4 | 70.1 | 70.1 | 71.4 | 57.5 |
| Onyx (facts) | 38.6 | 66.0 | 78.7 | 46.2 | 67.9 | 77.3 | 77.4 | 75.4 |
| Hindsight (facts) | 44.6 | 62.9 | 77.6 | 51.4 | 65.4 | 75.9 | 43.9 | 27.8 |
| LangMem (facts) | 50.3 | 61.3 | 73.8 | 56.3 | 66.7 | 76.7 | 54.9 | 39.3 |
| Basic Memory (facts) | 34.2 | 60.5 | 72.1 | 42.4 | 63.5 | 73.6 | 72.0 | 69.2 |
| MemOS (facts) | 49.2 | 59.3 | 71.1 | 55.3 | 65.6 | 75.4 | 58.9 | 41.8 |
| MCP memory server (facts) | 52.3 | 56.5 | 60.5 | 56.1 | 63.0 | 70.1 | 64.1 | 44.3 |
| GBrain (facts) | 27.4 | 48.1 | 61.7 | 38.0 | 57.2 | 69.3 | 60.0 | 56.1 |
| Sylph (facts) | 25.5 | 40.8 | 49.0 | 38.8 | 54.6 | 61.8 | 52.1 | 34.6 |
| MemoryOS (turns) | 32.6 | 40.7 | 47.8 | 45.9 | 56.8 | 62.9 | 48.2 | 28.9 |
| _BM25 (facts)_ | 74.3 | 79.7 | 85.2 | 69.9 | 75.6 | 81.0 | 77.6 | 59.7 |
| _Dense (facts)_ | 60.8 | 69.1 | 78.1 | 61.9 | 70.0 | 77.2 | 66.4 | 43.2 |

### LME-R test, n=382

ORP's dev choice: `orp+facts`. Against the best of the 15: **0 better, 0 no significant difference, 6 worse**.

| Metric | ORP | Best of the 15 | Difference [95% CI] | Verdict |
|---|---|---|---|---|
| R@500 | 79.7 | 83.5 (Hindsight, facts) | -3.8 [-7.2, -0.5] | ORP worse |
| R@1000 | 84.1 | 90.7 (Hindsight, facts) | -6.6 [-9.5, -4.0] | ORP worse |
| R@2000 | 87.5 | 93.8 (Hindsight, facts) | -6.3 [-8.9, -3.9] | ORP worse |
| Ans@500 | 61.0 | 65.3 (Letta, facts) | -4.3 [-8.1, -0.6] | ORP worse |
| Ans@1000 | 67.5 | 74.1 (Letta, facts) | -6.6 [-9.9, -3.4] | ORP worse |
| Ans@2000 | 74.6 | 78.9 (Supermemory Company Brain, facts) | -4.3 [-7.4, -1.4] | ORP worse |

| System (store) | R@500 | R@1000 | R@2000 | Ans@500 | Ans@1000 | Ans@2000 | SessAll@5 | MRR |
|---|---|---|---|---|---|---|---|---|
| ORP (turns as evidence + facts) | 71.1 | 81.3 | 85.2 | 56.5 | 63.9 | 72.4 | 78.8 | 69.7 |
| **ORP (facts only)** | 79.7 | 84.1 | 87.5 | 61.0 | 67.5 | 74.6 | 76.4 | 72.8 |
| Hindsight (facts) | 83.5 | 90.7 | 93.8 | 63.5 | 69.8 | 78.2 | 88.0 | 70.6 |
| mem0 (facts) | 82.0 | 87.7 | 92.0 | 63.2 | 69.8 | 78.1 | 82.7 | 69.6 |
| Graphiti (Zep) (facts) | 80.8 | 87.5 | 93.0 | 63.2 | 70.1 | 76.5 | 84.0 | 72.7 |
| Cognee (facts) | 68.1 | 81.6 | 86.1 | 61.9 | 73.6 | 76.9 | 80.9 | 65.6 |
| LangMem (facts) | 71.2 | 81.2 | 90.9 | 59.2 | 69.0 | 76.5 | 80.6 | 65.3 |
| Letta (facts) | 74.6 | 80.5 | 82.4 | 65.3 | 74.1 | 78.2 | 79.6 | 69.4 |
| Supermemory Company Brain (facts) | 71.4 | 80.3 | 86.3 | 62.9 | 73.6 | 78.9 | 84.3 | 74.1 |
| MCP memory server (facts) | 61.2 | 69.2 | 74.0 | 50.6 | 59.1 | 68.1 | 61.8 | 56.4 |
| Supermemory (facts) | 61.0 | 68.9 | 68.9 | 52.8 | 61.9 | 61.9 | 66.5 | 70.2 |
| MemOS (facts) | 52.6 | 68.5 | 80.9 | 53.1 | 63.1 | 73.4 | 77.2 | 58.4 |
| MemoryOS (facts) | 45.5 | 55.6 | 66.5 | 45.8 | 56.1 | 66.5 | 74.6 | 51.2 |
| Basic Memory (facts) | 22.2 | 51.9 | 75.4 | 44.9 | 54.2 | 70.7 | 80.4 | 86.6 |
| Sylph (facts) | 22.5 | 51.4 | 73.9 | 30.3 | 55.7 | 67.3 | 76.2 | 67.7 |
| Onyx (facts) | 8.4 | 44.7 | 72.0 | 22.9 | 52.0 | 70.7 | 81.9 | 85.3 |
| GBrain (facts) | 9.3 | 43.0 | 61.6 | 23.6 | 54.4 | 68.0 | 75.4 | 69.3 |
| _BM25 (facts)_ | 78.5 | 85.5 | 90.1 | 61.4 | 69.3 | 77.3 | 79.8 | 69.5 |
| _Dense (facts)_ | 74.8 | 84.3 | 92.4 | 61.4 | 70.9 | 77.5 | 80.6 | 65.5 |

Extraction lifts almost every system, and plain BM25 over the facts is competitive with the best baselines, so much of the gain comes from the facts themselves. On LME-R the strongest fact stores (Hindsight's and mem0's recall paths) beat ORP.

## Re-tuned LLM-extraction track

Same shared facts; every baseline's knobs re-tuned on dev fact stores and ORP's ranker refitted on them; each system's configuration (raw turns, facts, facts re-tuned; ORP also evidence plus facts) chosen on dev. HotpotQA-50 has no dev split, so it uses each system's best mean dev configuration; it is the only benchmark whose test results had not been seen.

### LoCoMo-10 test, n=1152

ORP configuration chosen on dev: `orp-ft+facts`. Against the best of the 15: **6 better, 0 no significant difference, 0 worse**.

| Metric | ORP | Best of the 15 (tuned) | Difference [95% CI] | Verdict |
|---|---|---|---|---|
| R@500 | 80.2 | 74.4 (mem0@ftuned+facts) | +5.8 [+4.4, +7.3] | ORP better |
| R@1000 | 85.6 | 79.7 (mem0@ftuned+facts) | +5.8 [+4.5, +7.3] | ORP better |
| R@2000 | 90.4 | 85.3 (mem0@ftuned+facts) | +5.1 [+3.8, +6.5] | ORP better |
| Ans@500 | 73.9 | 70.7 (mem0@ftuned+facts) | +3.2 [+2.0, +4.5] | ORP better |
| Ans@1000 | 79.2 | 76.3 (mem0@ftuned+facts) | +2.9 [+1.7, +4.0] | ORP better |
| Ans@2000 | 83.6 | 81.2 (mem0@ftuned+facts) | +2.4 [+1.5, +3.3] | ORP better |

| System (configuration) | R@500 | R@1000 | R@2000 | Ans@500 | Ans@1000 | Ans@2000 |
|---|---|---|---|---|---|---|
| **ORP** `orp-ft+facts` | 80.2 | 85.6 | 90.4 | 73.9 | 79.2 | 83.6 |
| mem0 `mem0@ftuned+facts` | 74.4 | 79.7 | 85.3 | 70.7 | 76.3 | 81.2 |
| Supermemory Company Brain `supermemory-company-brain@tuned+facts` | 73.1 | 79.6 | 84.1 | 69.3 | 75.2 | 79.3 |
| Graphiti (Zep) `graphiti@tuned+facts` | 69.8 | 76.7 | 83.4 | 66.9 | 73.4 | 79.3 |
| Cognee `cognee@ftuned+facts` | 66.0 | 74.3 | 83.3 | 65.3 | 73.2 | 80.5 |
| Letta `letta@ftuned+facts` | 67.9 | 74.1 | 79.9 | 64.3 | 71.1 | 76.6 |
| Supermemory `supermemory@tuned+facts` | 64.7 | 72.6 | 72.6 | 63.4 | 70.1 | 70.1 |
| Onyx `onyx@ftuned+facts` | 38.6 | 66.3 | 78.8 | 46.0 | 68.3 | 77.6 |
| LangMem `langmem@ftuned+facts` | 52.6 | 65.2 | 76.3 | 57.8 | 68.7 | 77.7 |
| Hindsight `hindsight@ftuned+facts` | 44.6 | 62.9 | 77.5 | 51.4 | 65.5 | 75.8 |
| Basic Memory `basic-memory@ftuned+facts` | 34.1 | 60.5 | 72.2 | 42.4 | 63.5 | 73.9 |
| MemOS `memos@tuned+facts` | 49.2 | 59.3 | 71.1 | 55.3 | 65.6 | 75.4 |
| MCP memory server `mcp-memory@tuned+facts` | 52.3 | 56.5 | 60.5 | 56.1 | 63.0 | 70.1 |
| GBrain `gbrain@tuned+facts` | 27.4 | 48.1 | 61.7 | 38.0 | 57.2 | 69.3 |
| Sylph `sylph@tuned+facts` | 25.5 | 40.8 | 49.0 | 38.8 | 54.6 | 61.8 |
| MemoryOS `memoryos@tuned` | 32.6 | 40.7 | 47.8 | 45.9 | 56.8 | 62.9 |
| _BM25 (facts)_ `bm25+facts` | 74.3 | 79.7 | 85.2 | 69.9 | 75.6 | 81.0 |

### LME-R test, n=382

ORP configuration chosen on dev: `orp-ft+facts`. Against the best of the 15: **0 better, 1 no significant difference, 5 worse**.

| Metric | ORP | Best of the 15 (tuned) | Difference [95% CI] | Verdict |
|---|---|---|---|---|
| R@500 | 80.4 | 83.5 (hindsight@tuned+facts) | -3.1 [-6.4, +0.1] | no significant difference |
| R@1000 | 85.1 | 90.7 (hindsight@tuned+facts) | -5.7 [-8.3, -3.2] | ORP worse |
| R@2000 | 88.0 | 93.8 (hindsight@tuned+facts) | -5.7 [-8.3, -3.3] | ORP worse |
| Ans@500 | 60.6 | 65.6 (letta@ftuned+facts) | -5.0 [-8.8, -1.4] | ORP worse |
| Ans@1000 | 68.3 | 74.4 (letta@ftuned+facts) | -6.1 [-9.4, -2.7] | ORP worse |
| Ans@2000 | 75.2 | 79.4 (letta@ftuned+facts) | -4.2 [-7.1, -1.3] | ORP worse |

| System (configuration) | R@500 | R@1000 | R@2000 | Ans@500 | Ans@1000 | Ans@2000 |
|---|---|---|---|---|---|---|
| **ORP** `orp-ft+facts` | 80.4 | 85.1 | 88.0 | 60.6 | 68.3 | 75.2 |
| Hindsight `hindsight@tuned+facts` | 83.5 | 90.7 | 93.8 | 63.5 | 69.8 | 78.2 |
| mem0 `mem0@ftuned+facts` | 82.4 | 87.9 | 92.5 | 63.3 | 69.7 | 77.6 |
| Graphiti (Zep) `graphiti@tuned+facts` | 80.8 | 87.5 | 93.0 | 63.2 | 70.1 | 76.5 |
| LangMem `langmem@ftuned+facts` | 72.6 | 82.1 | 91.9 | 59.8 | 68.5 | 76.5 |
| Cognee `cognee@tuned+facts` | 68.1 | 81.6 | 86.1 | 61.9 | 73.6 | 76.9 |
| Letta `letta@ftuned+facts` | 74.6 | 81.2 | 86.3 | 65.6 | 74.4 | 79.4 |
| Supermemory Company Brain `supermemory-company-brain@ftuned+facts` | 72.1 | 81.0 | 87.8 | 63.6 | 74.0 | 79.3 |
| MCP memory server `mcp-memory@ftuned+facts` | 61.5 | 69.3 | 74.0 | 50.4 | 58.8 | 68.1 |
| Supermemory `supermemory@ftuned+facts` | 60.9 | 68.8 | 68.8 | 52.9 | 62.0 | 62.0 |
| MemOS `memos@tuned+facts` | 52.6 | 68.5 | 80.9 | 53.1 | 63.1 | 73.4 |
| MemoryOS `memoryos@ftuned+facts` | 44.8 | 55.7 | 65.8 | 45.6 | 55.7 | 66.5 |
| Basic Memory `basic-memory@ftuned+facts` | 22.2 | 51.9 | 75.4 | 44.9 | 54.2 | 70.7 |
| Sylph `sylph@ftuned+facts` | 23.3 | 51.5 | 74.0 | 29.6 | 55.3 | 67.5 |
| Onyx `onyx@ftuned+facts` | 8.9 | 44.9 | 71.5 | 23.2 | 52.8 | 70.3 |
| GBrain `gbrain@ftuned+facts` | 14.4 | 40.1 | 63.2 | 30.3 | 46.1 | 65.5 |
| _BM25 (facts)_ `bm25+facts` | 78.5 | 85.5 | 90.1 | 61.4 | 69.3 | 77.3 |

### HotpotQA-50 (held out), n=50

ORP configuration chosen on dev: `orp-ft+facts`. Against the best of the 15: **0 better, 4 no significant difference, 0 worse**.

| Metric | ORP | Best of the 15 (tuned) | Difference [95% CI] | Verdict |
|---|---|---|---|---|
| Ans@500 | 81.7 | 83.5 (cognee@tuned+facts) | -1.9 [-9.3, +4.8] | no significant difference |
| Ans@1000 | 93.3 | 92.0 (graphiti@tuned+facts) | +1.3 [-2.0, +6.0] | no significant difference |
| Ans@2000 | 94.0 | 94.0 (letta@ftuned+facts) | +0.0 [+0.0, +0.0] | no significant difference |
| Ans@4000 | 94.0 | 95.3 (onyx@ftuned+facts) | -1.3 [-6.0, +1.3] | no significant difference |

| System (configuration) | Ans@500 | Ans@1000 | Ans@2000 | Ans@4000 |
|---|---|---|---|---|
| **ORP** `orp-ft+facts` | 81.7 | 93.3 | 94.0 | 94.0 |
| Graphiti (Zep) `graphiti@tuned+facts` | 81.5 | 92.0 | 94.0 | 94.0 |
| Hindsight `hindsight@ftuned+facts` | 73.9 | 88.2 | 92.2 | 92.2 |
| MCP memory server `mcp-memory@ftuned+facts` | 78.8 | 87.5 | 89.5 | 89.9 |
| mem0 `mem0@ftuned+facts` | 76.8 | 87.0 | 89.0 | 89.0 |
| Supermemory Company Brain `supermemory-company-brain@ftuned+facts` | 75.0 | 85.7 | 91.3 | 94.0 |
| Cognee `cognee@tuned+facts` | 83.5 | 85.5 | 87.5 | 89.5 |
| Letta `letta@ftuned+facts` | 75.5 | 82.8 | 94.0 | 94.0 |
| Onyx `onyx@ftuned+facts` | 68.9 | 82.8 | 90.4 | 95.3 |
| Supermemory `supermemory@tuned+facts` | 76.1 | 82.3 | 82.3 | 82.3 |
| Basic Memory `basic-memory@ftuned+facts` | 65.8 | 80.2 | 86.2 | 86.2 |
| LangMem `langmem@ftuned+facts` | 63.2 | 77.5 | 82.5 | 91.3 |
| Sylph `sylph@ftuned+facts` | 58.8 | 75.2 | 75.2 | 75.2 |
| MemOS `memos@tuned+facts` | 57.2 | 72.2 | 82.5 | 88.3 |
| GBrain `gbrain@ftuned+facts` | 62.7 | 71.5 | 81.5 | 89.0 |
| MemoryOS `memoryos@tuned` | 0.0 | 0.0 | 0.0 | 0.0 |
| _BM25 (facts)_ `bm25+facts` | 84.0 | 94.0 | 94.0 | 94.0 |

## ORP-Gov v2, 50% channel visibility (same ranker; access control differs)

| Strategy | Recall@1000 (permitted) | Questions with unreadable text in context | Forbidden askers shown gold evidence | Rankings changed by unreadable memories |
|---|---|---|---|---|
| ORP: admissible-set ranking + provenance closure | 89.7 | 0.0 | 0.0 | 0.0 |
| Pre-filter, but statistics and neighbour features over all memories | 89.7 | 0.0 | 0.0 | 100.0 |
| Filter after top-10 | 80.2 | 0.0 | 0.0 | – |
| Filter after top-50 | 89.7 | 0.0 | 0.0 | – |
| Workspace scope only (no per-item ACL) | 84.2 | 100.0 | 87.1 | – |
| Per-item ACL on raw units, summaries workspace-scoped | 89.1 | 30.9 | 18.9 | – |

## ORP-Gov v2, 15% channel visibility (same ranker; access control differs)

| Strategy | Recall@1000 (permitted) | Questions with unreadable text in context | Forbidden askers shown gold evidence | Rankings changed by unreadable memories |
|---|---|---|---|---|
| ORP: admissible-set ranking + provenance closure | 97.7 | 0.0 | 0.0 | 0.0 |
| Pre-filter, but statistics and neighbour features over all memories | 97.3 | 0.0 | 0.0 | 100.0 |
| Filter after top-10 | 81.0 | 0.0 | 0.0 | – |
| Filter after top-50 | 89.9 | 0.0 | 0.0 | – |
| Workspace scope only (no per-item ACL) | 84.4 | 100.0 | 87.3 | – |
| Per-item ACL on raw units, summaries workspace-scoped | 96.4 | 79.7 | 28.1 | – |

## Blind QA (Claude Sonnet readers and judges, 1,000-token contexts)

| System | LoCoMo acc. | LME-R acc. |
|---|---|---|
| _BM25 over turns (reference)_ | 64.7 | 70.6 |
| Hindsight | 64.0 | 75.6 |
| mem0 | 60.7 | 71.4 |
| **ORP reference engine** | 67.3 | 75.6 |
| Supermemory Company Brain | 62.7 | 64.7 |

locomo: ORP minus _BM25 over turns (reference)_ +2.7 [-3.3, +9.3]; Hindsight +3.3 [-2.0, +8.7]; mem0 +6.7 [+1.3, +12.7]; Supermemory Company Brain +4.7 [-1.3, +10.7]
lme: ORP minus _BM25 over turns (reference)_ +5.0 [-3.4, +14.3]; Hindsight +0.0 [-8.4, +9.2]; mem0 +4.2 [-3.4, +12.6]; Supermemory Company Brain +10.9 [+3.4, +19.3]

## Latency (single process, one core, LoCoMo test, cold caches)

| System | p50 ms | p95 ms | build ms |
|---|---|---|---|
| _Dense over turns (reference)_ | 0.2 | 0.3 | 49 |
| LangMem | 0.2 | 0.4 | 79 |
| _BM25 over turns (reference)_ | 0.4 | 0.7 | 21 |
| MemoryOS | 0.6 | 1.0 | 133 |
| MCP memory server | 0.9 | 1.3 | 25 |
| GBrain | 1.1 | 1.5 | 91 |
| Letta | 1.2 | 1.9 | 63 |
| Onyx | 1.6 | 2.6 | 96 |
| mem0 | 1.8 | 2.2 | 181 |
| Cognee | 2.2 | 3.5 | 135 |
| Basic Memory | 2.6 | 6.7 | 74 |
| MemOS | 2.9 | 4.4 | 56 |
| Sylph | 4.8 | 6.9 | 143 |
| Graphiti (Zep) | 5.0 | 6.5 | 186 |
| Hindsight | 5.2 | 7.7 | 238 |
| Supermemory | 6.4 | 8.8 | 289 |
| Supermemory Company Brain | 6.8 | 9.1 | 123 |
| ORP, hand-tuned RRF (no learned model) | 12.8 | 18.1 | 252 |
| **ORP reference engine** | 18.1 | 28.1 | 218 |
