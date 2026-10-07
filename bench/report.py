"""Render results/summary.json (+ governance.json) into results/RESULTS.md."""
from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES = {
    "orp": "**ORP reference engine**", "graphiti": "Graphiti (Zep)", "hindsight": "Hindsight", "mem0": "mem0",
    "letta": "Letta", "supermemory-company-brain": "Supermemory Company Brain", "gbrain": "GBrain",
    "supermemory": "Supermemory", "onyx": "Onyx", "cognee": "Cognee", "basic-memory": "Basic Memory",
    "sylph": "Sylph", "mcp-memory": "MCP memory server", "memos": "MemOS", "langmem": "LangMem",
    "memoryos": "MemoryOS", "bm25": "_BM25 turns (reference)_", "dense": "_Dense turns (reference)_",
}


def fmt(x):
    return "–" if x is None else f"{x * 100:.1f}"


def table(summ: dict, bench: str) -> str:
    rows = sorted(summ.items(), key=lambda kv: -(kv[1].get("recall@1000") or 0))
    out = ["| # | System | R@500 | R@1000 | R@2000 | R@4000 | Full@2000 | Ans@1000 | SessAll@5 | MRR | p50 ms |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, (n, s) in enumerate(rows, 1):
        out.append(f"| {i} | {NAMES.get(n, n)} | {fmt(s.get('recall@500'))} | {fmt(s.get('recall@1000'))} | "
                   f"{fmt(s.get('recall@2000'))} | {fmt(s.get('recall@4000'))} | {fmt(s.get('full@2000'))} | "
                   f"{fmt(s.get('ans@1000'))} | {fmt(s.get('sess_all@5'))} | {fmt(s.get('mrr'))} | "
                   f"{s.get('latency_p50_ms', 0):.1f} |")
    return "\n".join(out)


def cats(summ: dict) -> str:
    allc = sorted({c for s in summ.values() for c in s.get("by_category", {})})
    rows = sorted(summ.items(), key=lambda kv: -(kv[1].get("recall@1000") or 0))
    out = ["| System | " + " | ".join(allc) + " |", "|---|" + "---|" * len(allc)]
    for n, s in rows:
        bc = s.get("by_category", {})
        out.append(f"| {NAMES.get(n, n)} | " + " | ".join(fmt(bc.get(c, {}).get("recall@1000")) for c in allc) + " |")
    ns = {c: next((s["by_category"][c]["n"] for s in summ.values() if c in s.get("by_category", {})), 0) for c in allc}
    out.append("| _n_ | " + " | ".join(str(ns[c]) for c in allc) + " |")
    return "\n".join(out)


def main():
    S = json.load(open(os.path.join(ROOT, "results", "summary.json")))
    md = ["# ORP-Bench results (test splits)", "",
          "Evidence recall inside a fixed context budget (R@B = share of gold evidence turns contained in the first B "
          "tokens of returned context). Ans@1000 = share of the gold answer's content words present in that context. "
          "SessAll@5 = all gold sessions among the first 5 sessions surfaced (LongMemEval's retrieval metric). "
          "Every system uses the same BM25 and dense encoder and no LLM. Numbers are percentages.", ""]
    for b, title in (("locomo", "LoCoMo-10 (7 test conversations, categories 1-4)"),
                     ("lme", "LongMemEval (reconstructed 40-session haystack)")):
        if b not in S or "test" not in S[b]:
            continue
        summ = S[b]["test"]
        n = next(iter(summ.values()))["n"]
        md += [f"## {title}, n={n}", "", table(summ, b), "", "Recall@1000 by question category:", "", cats(summ), ""]
    gp = os.path.join(ROOT, "results", "governance.json")
    if os.path.exists(gp):
        G = json.load(open(gp))
        md += ["## ORP-Gov: permission-aware recall (same ranker, different access-control strategy)", "",
               "| Strategy | Recall@1000 (permitted askers) | Queries with any unreadable item in context | "
               "Forbidden askers shown gold evidence |", "|---|---|---|---|"]
        lab = {"pre_filter": "ORP: admissibility before ranking + provenance closure",
               "post_filter@10": "Filter after top-10", "post_filter@50": "Filter after top-50",
               "scope_only": "Workspace scope only (no per-item ACL)",
               "derived_bank": "Per-item ACL on raw units, derived summaries scoped to workspace"}
        for k, v in G.items():
            md.append(f"| {lab.get(k, k)} | {fmt(v['recall@1000_permitted'])} | {fmt(v['queries_with_any_leak'])} | "
                      f"{fmt(v['forbidden_answer_evidence_leaked'])} |")
        md.append("")
    open(os.path.join(ROOT, "results", "RESULTS.md"), "w").write("\n".join(md))
    print("\n".join(md))


if __name__ == "__main__":
    main()
