"""Render results/*.json into results/RESULTS.md (v0.1.1)."""
from __future__ import annotations

import json
import os

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "results")
BASE = {
    "graphiti": "Graphiti (Zep)", "hindsight": "Hindsight", "mem0": "mem0", "letta": "Letta",
    "supermemory-company-brain": "Supermemory Company Brain", "gbrain": "GBrain", "supermemory": "Supermemory",
    "onyx": "Onyx", "cognee": "Cognee", "basic-memory": "Basic Memory", "sylph": "Sylph",
    "mcp-memory": "MCP memory server", "memos": "MemOS", "langmem": "LangMem", "memoryos": "MemoryOS",
}
ORP = {"orp": "**ORP reference engine (learned ranker)**", "orp-rrf": "ORP, hand-tuned RRF (no learned model)",
       "orp-allfeat": "ORP, learned ranker incl. position/length features (not shipped)",
       "bm25": "_BM25 over turns (reference)_", "dense": "_Dense over turns (reference)_"}


def label(n: str) -> str:
    if n in ORP:
        return ORP[n]
    if n.endswith("@tuned"):
        return f"{BASE.get(n[:-6], n[:-6])} (re-impl., dev-tuned)"
    return f"{BASE.get(n, n)} (re-impl., defaults)"


def f(x):
    return "–" if x is None else f"{x * 100:.1f}"


def bench_table(summ: dict) -> str:
    rows = sorted(summ.items(), key=lambda kv: -(kv[1].get("recall@1000") or 0))
    out = ["| # | System | R@500 | R@1000 | R@2000 | R@4000 | Ans@1000 | SessAll@5 | MRR | R@1000 (secondary) |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for i, (n, s) in enumerate(rows, 1):
        out.append(f"| {i} | {label(n)} | {f(s.get('recall@500'))} | {f(s.get('recall@1000'))} | "
                   f"{f(s.get('recall@2000'))} | {f(s.get('recall@4000'))} | {f(s.get('ans@1000'))} | "
                   f"{f(s.get('sess_all@5'))} | {f(s.get('mrr'))} | {f(s.get('s_recall@1000'))} |")
    return "\n".join(out)


def cat_table(summ: dict) -> str:
    allc = sorted({c for s in summ.values() for c in s.get("by_category", {})})
    rows = sorted(summ.items(), key=lambda kv: -(kv[1].get("recall@1000") or 0))
    out = ["| System | " + " | ".join(allc) + " |", "|---|" + "---|" * len(allc)]
    for n, s in rows:
        bc = s.get("by_category", {})
        out.append(f"| {label(n)} | " + " | ".join(f(bc.get(c, {}).get("recall@1000")) for c in allc) + " |")
    ns = {c: next((s["by_category"][c]["n"] for s in summ.values() if c in s.get("by_category", {})), 0) for c in allc}
    out.append("| _n_ | " + " | ".join(str(ns[c]) for c in allc) + " |")
    return "\n".join(out)


def paired(bench: str, a: str, b: str, metric: str):
    pa, pb = os.path.join(RES, "raw", bench, f"{a}.test.jsonl"), os.path.join(RES, "raw", bench, f"{b}.test.jsonl")
    if not (os.path.exists(pa) and os.path.exists(pb)):
        return None
    A = {json.loads(l)["qid"]: json.loads(l) for l in open(pa)}
    B = {json.loads(l)["qid"]: json.loads(l) for l in open(pb)}
    d = np.array([A[q][metric] - B[q][metric] for q in A if q in B and metric in A[q] and metric in B[q]])
    rng = np.random.default_rng(0)
    bs = [d[rng.integers(0, len(d), len(d))].mean() for _ in range(5000)]
    return d.mean(), np.percentile(bs, 2.5), np.percentile(bs, 97.5), len(d)


def main():
    S = json.load(open(os.path.join(RES, "summary.json")))
    md = ["# ORP-Bench results, v0.1.1 (test splits)", "",
          "All numbers are percentages on held-out test splits. Baselines are LLM-free re-implementations of each "
          "project's recall path (see `reports/` and `baselines/`), run with the same shared encoders as ORP; they are "
          "not the products themselves. **R@B**: share of gold evidence turns inside the first B tokens of the returned "
          "context (primary packer: characters/4 tokens, greedy fill skipping items that do not fit). **Ans@1000**: "
          "share of the gold answer's content words in that context. **SessAll@5**: LongMemEval's session retrieval "
          "metric. **Secondary**: word/punctuation token estimate with stop-at-first-overflow packing.", ""]
    for b, title in (("locomo", "LoCoMo-10 test (7 conversations, categories 1-4)"),
                     ("lme", "LME-R test (LongMemEval oracle questions, recombined 40-session haystacks)")):
        if b not in S or "test" not in S[b]:
            continue
        summ = S[b]["test"]
        n = next(iter(summ.values()))["n"]
        md += [f"## {title}, n={n}", "", bench_table(summ), ""]
        best = max((k for k in summ if k not in ORP), key=lambda k: summ[k].get("recall@1000") or 0)
        lines = []
        for metric in ("recall@500", "recall@1000", "recall@2000", "ans@1000"):
            r = paired(b, "orp", best, metric)
            if r:
                lines.append(f"{metric}: {r[0] * 100:+.1f} [{r[1] * 100:+.1f}, {r[2] * 100:+.1f}]")
        md += [f"ORP minus the strongest baseline ({label(best)}), paired bootstrap 95% CI: " + "; ".join(lines), "",
               "Recall@1000 by question category:", "", cat_table(summ), ""]
    for vis in ("50", "15"):
        gp = os.path.join(RES, f"governance_v{vis}.json")
        if not os.path.exists(gp):
            continue
        G = json.load(open(gp))
        lab = {"orp": "ORP: admissible-set ranking + provenance closure",
               "global_stats": "Pre-filter, but statistics and neighbour features over all memories",
               "post_filter@10": "Filter after top-10", "post_filter@50": "Filter after top-50",
               "scope_only": "Workspace scope only (no per-item ACL)",
               "derived_bank": "Per-item ACL on raw units, summaries workspace-scoped"}
        md += [f"## ORP-Gov v2, {vis}% channel visibility (same ranker; access control differs)", "",
               "| Strategy | Recall@1000 (permitted) | Questions with unreadable text in context | Forbidden askers shown "
               "gold evidence | Rankings changed by unreadable memories |", "|---|---|---|---|---|"]
        for k in ("orp", "global_stats", "post_filter@10", "post_filter@50", "scope_only", "derived_bank"):
            if k in G:
                v = G[k]
                md.append(f"| {lab[k]} | {f(v['recall@1000'])} | {f(v['text_leak'])} | {f(v['answer_leak'])} | "
                          f"{f(v['rank_side_channel'])} |")
        md.append("")
    qp = os.path.join(RES, "qa_blind.json")
    if os.path.exists(qp):
        Q = json.load(open(qp))
        md += ["## Blind QA (Claude Sonnet readers and judges, 1,000-token contexts)", "",
               "| System | LoCoMo acc. | LME-R acc. |", "|---|---|---|"]
        for s in Q["systems"]:
            md.append(f"| {label(s)} | {f(Q['accuracy']['locomo'][s]['acc'])} | {f(Q['accuracy']['lme'][s]['acc'])} |")
        md.append("")
        for b in ("locomo", "lme"):
            md.append(f"{b}: ORP minus " + "; ".join(
                f"{label(s)} {v['diff'] * 100:+.1f} [{v['ci95'][0] * 100:+.1f}, {v['ci95'][1] * 100:+.1f}]"
                for s, v in Q["orp_minus"][b].items()))
        md.append("")
    lp = os.path.join(RES, "latency.json")
    if os.path.exists(lp):
        L = json.load(open(lp))
        md += ["## Latency (single process, one core, LoCoMo test)", "", "| System | p50 ms | p95 ms | build ms |",
               "|---|---|---|---|"]
        for n, v in sorted(L.items(), key=lambda kv: kv[1]["p50"]):
            md.append(f"| {label(n)} | {v['p50']:.1f} | {v['p95']:.1f} | {v['build']:.0f} |")
        md.append("")
    open(os.path.join(RES, "RESULTS.md"), "w").write("\n".join(md))
    print("\n".join(md))


if __name__ == "__main__":
    main()
