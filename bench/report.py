"""Render results into results/RESULTS.md (v0.1.2).

Sources (all on held-out test splits):
  results/summary.json          v0.1.1 run: the 15 baselines with default settings, ORP v0.1.1, references
  results/summary_v012.json     ORP v0.1.2 (shipped, rrf, allfeat) and references, with evidence-text recall (E@B)
  results/summary_tuned.json    the 15 baselines with dev-tuned settings, with E@B
  results/summary_hotpot*.json  HotpotQA-50 (held-out, out-of-domain; answers only)
  results/raw_v012/<bench>/<system>.test.jsonl   per-query rows used for paired bootstrap CIs
"""
from __future__ import annotations

import json
import os

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "results")
RAW = os.path.join(RES, "raw_v012")
BASE = {
    "graphiti": "Graphiti (Zep)", "hindsight": "Hindsight", "mem0": "mem0", "letta": "Letta",
    "supermemory-company-brain": "Supermemory Company Brain", "gbrain": "GBrain", "supermemory": "Supermemory",
    "onyx": "Onyx", "cognee": "Cognee", "basic-memory": "Basic Memory", "sylph": "Sylph",
    "mcp-memory": "MCP memory server", "memos": "MemOS", "langmem": "LangMem", "memoryos": "MemoryOS",
}
ORP = {"orp": "**ORP reference engine**", "orp-rrf": "ORP, hand-tuned RRF (no learned model)",
       "orp-allfeat": "ORP, learned ranker with position/length features (not shipped)",
       "bm25": "_BM25 over turns (reference)_", "dense": "_Dense over turns (reference)_"}
METRICS = [("recall@500", "R@500"), ("recall@1000", "R@1000"), ("recall@2000", "R@2000"), ("e_recall@1000", "E@1000"),
           ("ans@500", "Ans@500"), ("ans@1000", "Ans@1000"), ("ans@2000", "Ans@2000"), ("sess_all@5", "SessAll@5"),
           ("mrr", "MRR")]
HEAD = ["recall@500", "recall@1000", "recall@2000", "e_recall@1000", "ans@500", "ans@1000", "ans@2000"]


def label(n: str) -> str:
    if n in ORP:
        return ORP[n]
    if n.endswith("@tuned"):
        return f"{BASE.get(n[:-6], n[:-6])}"
    return f"{BASE.get(n, n)}"


def f(x):
    return "–" if x is None else f"{x * 100:.1f}"


def load(name):
    p = os.path.join(RES, name)
    return json.load(open(p)) if os.path.exists(p) else {}


def load_tuned():
    """Merge results/summary_tuned*.json (tuned baselines are run in batches)."""
    import glob
    out = {}
    for fn in sorted(glob.glob(os.path.join(RES, "summary_tuned*.json"))):
        for b, d in json.load(open(fn)).items():
            for split, systems in d.items():
                out.setdefault(b, {}).setdefault(split, {}).update(systems)
    return out


def rows(bench, name):
    p = os.path.join(RES, f"raw_{bench}" if bench in ("lmem", "lmes", "lmemd", "lmesd") else "raw_v012", bench, f"{name}.test.jsonl")
    if not os.path.exists(p):
        return None
    return {json.loads(l)["qid"]: json.loads(l) for l in open(p)}


def paired(bench, a, b, metric, exclude=()):
    A, B = rows(bench, a), rows(bench, b)
    if not A or not B:
        return None
    d = np.array([A[q][metric] - B[q][metric] for q in A if q in B and q not in exclude
                  and metric in A[q] and metric in B[q]])
    if not len(d):
        return None
    rng = np.random.default_rng(0)
    bs = [d[rng.integers(0, len(d), len(d))].mean() for _ in range(4000)]
    return float(d.mean()), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5)), len(d)


def mean_excl(bench, name, metric, exclude):
    R = rows(bench, name)
    if not R:
        return None
    v = [r[metric] for q, r in R.items() if q not in exclude and metric in r]
    return float(np.mean(v)) if v else None


def table(summ: dict, names: list[str], metrics=METRICS) -> str:
    out = ["| System | " + " | ".join(h for _, h in metrics) + " |", "|---|" + "---|" * len(metrics)]
    for n in names:
        s = summ.get(n)
        if s is None:
            continue
        out.append(f"| {label(n)} | " + " | ".join(f(s.get(k)) for k, _ in metrics) + " |")
    return "\n".join(out)


def verdict(bench, summ, tuned_names, metrics, exclude=(), orp="orp"):
    """ORP vs the best of the 15 (dev-tuned) on each metric, with a paired bootstrap CI."""
    out = ["| Metric | ORP | Best of the 15 (tuned) | Difference [95% CI] | Verdict |", "|---|---|---|---|---|"]
    tally = {"win": 0, "tie": 0, "loss": 0}
    for k, h in metrics:
        if summ.get("orp", {}).get(k) is None:
            continue
        cands = [(summ[n].get(k), n) for n in tuned_names if n in summ and summ[n].get(k) is not None]
        if not cands:
            continue
        bv, bn = max(cands)
        r = paired(bench, orp, bn, k, exclude)
        if r is None:
            continue
        v = "ORP better" if r[1] > 0 else "ORP worse" if r[2] < 0 else "no significant difference"
        tally["win" if r[1] > 0 else "loss" if r[2] < 0 else "tie"] += 1
        out.append(f"| {h} | {f(summ['orp'][k])} | {f(bv)} ({label(bn)}) | {r[0] * 100:+.1f} "
                   f"[{r[1] * 100:+.1f}, {r[2] * 100:+.1f}] | {v} |")
    return "\n".join(out), tally


def cat_table(summ: dict, names: list[str]) -> str:
    allc = sorted({c for n in names if n in summ for c in summ[n].get("by_category", {})})
    out = ["| System | " + " | ".join(allc) + " |", "|---|" + "---|" * len(allc)]
    for n in names:
        if n not in summ:
            continue
        bc = summ[n].get("by_category", {})
        out.append(f"| {label(n)} | " + " | ".join(f(bc.get(c, {}).get("recall@1000")) for c in allc) + " |")
    ns = {c: next((summ[n]["by_category"][c]["n"] for n in names if n in summ and c in summ[n].get("by_category", {})),
                  0) for c in allc}
    out.append("| _n_ | " + " | ".join(str(ns[c]) for c in allc) + " |")
    return "\n".join(out)


def facts_track() -> list[str]:
    """LLM-extraction track: every system may store the shared Haiku-extracted facts; each system's store (raw turns
    or facts; ORP also evidence plus facts) is chosen on that benchmark's dev split (results/facts_dev*.json)."""
    md = []
    for bench, sfile, dfile, title in (("locomo", "summary_facts.json", "facts_dev.json", "LoCoMo-10 test, n=1152"),
                                       ("lme", "summary_facts_lme.json", "facts_dev_lme.json", "LME-R test, n=382")):
        F, D = load(sfile), load(dfile)
        if not F or not D:
            continue
        S = {**load_tuned().get(bench, {}).get("test", {}), **F[bench]["test"]}
        ch = D["choice"]
        pool = {ch[f"{b}@tuned"]: S[ch[f"{b}@tuned"]] for b in BASE if ch.get(f"{b}@tuned") in S}
        orp = ch["orp"]
        if not md:
            md += ["## LLM-extraction track", "",
                   "Facts extracted once by Claude Haiku sub-agents with a mem0-style brief "
                   "(`bench/extraction/EXTRACT.md`), frozen in `results/extraction/` and shared by every system, like "
                   "the encoders. Each system's store (raw turns or extracted facts; for ORP also raw turns as evidence "
                   "plus facts as derived memories) was chosen on that benchmark's dev split only.", ""]
        out = ["| Metric | ORP | Best of the 15 | Difference [95% CI] | Verdict |", "|---|---|---|---|---|"]
        tally = {"win": 0, "tie": 0, "loss": 0}
        for k, h in [m for m in METRICS if m[0] in HEAD and m[0] != "e_recall@1000"]:
            bv, bn = max((v.get(k) or 0, n) for n, v in pool.items())
            r = paired(bench, orp, bn, k)
            st = "win" if r[1] > 0 else "loss" if r[2] < 0 else "tie"
            tally[st] += 1
            out.append(f"| {h} | {f(S[orp][k])} | {f(bv)} ({label(bn.replace('+facts', ''))}"
                       f"{', facts' if bn.endswith('+facts') else ''}) | {r[0] * 100:+.1f} [{r[1] * 100:+.1f}, "
                       f"{r[2] * 100:+.1f}] | "
                       f"{dict(win='ORP better', tie='no significant difference', loss='ORP worse')[st]} |")
        md += [f"### {title}", "", f"ORP's dev choice: `{orp}`. Against the best of the 15: **{tally['win']} better, "
               f"{tally['tie']} no significant difference, {tally['loss']} worse**.", ""] + out + [""]
        names = ["orp+evidence", "orp+facts"] + sorted(pool, key=lambda n: -(S[n].get("recall@1000") or 0)) + \
            ["bm25+facts", "dense+facts"]
        rows = ["| System (store) | " + " | ".join(h for k, h in METRICS if k != "e_recall@1000") + " |",
                "|---|" + "---|" * (len(METRICS) - 1)]
        for n in names:
            if n in S:
                lab = {"orp+evidence": "ORP (turns as evidence + facts)", "orp+facts": "ORP (facts only)",
                       "bm25+facts": "_BM25 (facts)_", "dense+facts": "_Dense (facts)_"}.get(
                    n, label(n.replace("+facts", "")) + (" (facts)" if n.endswith("+facts") else " (turns)"))
                if n == orp:
                    lab = f"**{lab}**"
                rows.append(f"| {lab} | " + " | ".join(f(S[n].get(k)) for k, _ in METRICS if k != "e_recall@1000")
                            + " |")
        md += rows + [""]
        for k in tally:
            TRACK[k] = TRACK.get(k, 0) + tally[k]
    if md:
        md += ["Extraction lifts almost every system, and plain BM25 over the facts is competitive with the best "
               "baselines, so much of the gain comes from the facts themselves. On LME-R the strongest fact stores "
               "(Hindsight's and mem0's recall paths) beat ORP.", ""]
    return md


def retuned_track() -> list[str]:
    """Re-tuned fact track (pre-registered in METHODOLOGY): knobs and ORP's ranker refitted on dev fact stores,
    configuration chosen on dev (results/facts_dev2.json), scored once per benchmark."""
    D = load("facts_dev2.json")
    if not D:
        return []
    md = ["## Re-tuned LLM-extraction track", "",
          "Same shared facts; every baseline's knobs re-tuned on dev fact stores and ORP's ranker refitted on them; "
          "each system's configuration (raw turns, facts, facts re-tuned; ORP also evidence plus facts) chosen on "
          "dev. HotpotQA-50 has no dev split, so it uses each system's best mean dev configuration; it is the only "
          "benchmark whose test results had not been seen.", ""]
    for bench, title in (("locomo", "LoCoMo-10 test, n=1152"), ("lme", "LME-R test, n=382"),
                         ("hotpot", "HotpotQA-50 (held out), n=50")):
        F = load(f"summary_ft_{bench}.json").get(bench, {}).get("test", {})
        if not F:
            continue
        ch = D["choice"][bench]
        orp = ch["orp"]
        pool = {ch[b]: F[ch[b]] for b in BASE if ch.get(b) in F}
        mets = [m for m in METRICS if m[0] in HEAD and m[0] != "e_recall@1000"] if bench != "hotpot" else \
            [("ans@500", "Ans@500"), ("ans@1000", "Ans@1000"), ("ans@2000", "Ans@2000"), ("ans@4000", "Ans@4000")]
        summ = {**pool, "orp": F[orp]}
        vt, t = verdict(bench, summ, list(pool), mets, orp=orp)
        for k in t:
            RETUNED[k] = RETUNED.get(k, 0) + t[k]
        md += [f"### {title}", "", f"ORP configuration chosen on dev: `{orp}`. Against the best of the 15: "
               f"**{t['win']} better, {t['tie']} no significant difference, {t['loss']} worse**.", "", vt, ""]
        rows = ["| System (configuration) | " + " | ".join(h for _, h in mets) + " |", "|---|" + "---|" * len(mets)]
        for n in [orp] + sorted(pool, key=lambda n: -(F[n].get(mets[1][0]) or 0)) + ["bm25+facts"]:
            if n in F:
                rows.append(f"| {'**ORP**' if n == orp else label(n.split('@')[0]) if '@' in n else '_BM25 (facts)_'}"
                            f" `{n}` | " + " | ".join(f(F[n].get(k)) for k, _ in mets) + " |")
        md += rows + [""]
    return md


def lmem_track(bench: str = "lmem") -> list[str]:
    """Official LongMemEval-S / -M, frozen raw-turn configurations, scored once (pre-registered in METHODOLOGY)."""
    S = load(f"summary_{bench}.json").get(bench, {}).get("test", {})
    if "orp" not in S:
        return []
    tn = [f"{n}@tuned" for n in BASE if f"{n}@tuned" in S]
    mets = [(k, h) for k, h in METRICS if k in HEAD] + [("ans@4000", "Ans@4000"), ("sess_all@5", "SessAll@5")]
    if all("answ@1000" in v for v in S.values()):
        mets += [("answ@500", "AnsW@500"), ("answ@1000", "AnsW@1000"), ("answ@2000", "AnsW@2000")]
    q = load(f"{bench}_quirks.json")
    v = bench[3]
    day = bench.endswith("d")
    size = ("about 475 sessions, 4,900 turns and 1.2M tokens" if v == "m" else
            "38-62 sessions, about 490 turns and 115k tokens")
    md = [f"## Official LongMemEval-{v.upper()}{', day-precision question times' if day else ''} "
          f"(frozen configurations{', post hoc' if day else ', scored once'}), n={S['orp']['n']}", "",
          f"The official `longmemeval_{v}_cleaned.json` haystacks ({size} per question; LME-R used 40 rebuilt "
          "sessions), the same 382 test questions. Nothing was tuned on it: ORP's shipped ranker and every baseline's "
          "raw-turn dev-tuned configuration. Raw turns only.", ""]
    if day:
        md += ["Every system is given the end of the question's day as the question time: in the official data every "
               "gold session dated after its question falls on that same day. Found by a dev-only diagnosis after "
               "the strict-time results above had been seen (`results/dev_diag/PLAN.md`). **AnsW@B** (post hoc) "
               "counts answer words that are not bare numbers, which numbered lists, scores and ids otherwise match "
               "by chance.", ""]
    vt, t = verdict(bench, S, tn, mets)
    for k in t:
        LMEM[k] = LMEM.get(k, 0) + t[k]
    md += [f"ORP against the best of the 15: **{t['win']} better, {t['tie']} no significant difference, "
           f"{t['loss']} worse**.", "", vt, ""]
    order = ["orp", "orp-rrf"] + sorted(tn, key=lambda x: -(S[x].get("recall@1000") or 0)) + ["bm25", "dense"]
    md += [table(S, order, mets), ""]
    for key, what in (("gold_session_after_question", "gold evidence dated after the question (inadmissible to an "
                       "engine that honours `as_of`)"),
                      ("excluded", "any haystack session dated after the question")):
        ex = set(q.get(key, []))
        if not ex:
            continue
        sub = {k: {m: mean_excl(bench, k, m, ex) for m, _ in mets} for k in ["orp"] + tn}
        vq, tq = verdict(bench, sub, tn, mets, ex)
        md += [f"Without the {len(ex)} test questions with {what} in the official data (`results/{bench}_quirks.json`): "
               f"**{tq['win']} better, {tq['tie']} no significant difference, {tq['loss']} worse**.", "", vq, ""]
    md += ["Recall@1000 by question category:", "",
           cat_table(S, ["orp"] + sorted(tn, key=lambda x: -(S[x].get("recall@1000") or 0))[:6] + ["bm25"]), ""]
    return md


LMEM: dict = {}


RETUNED: dict = {}


TRACK: dict = {}


def main():
    v011, v012, tuned = load("summary.json"), load("summary_v012.json"), load_tuned()
    hot = {}
    for fn in ("summary_hotpot.json", "summary_hotpot_tuned.json", "summary_hotpot_tuned_b.json"):
        hot.update(load(fn).get("hotpot", {}).get("test", {}))
    quirks = load("lme_quirks.json").get("excluded", [])
    md = ["# ORP-Bench results, v0.1.2", "",
          "Held-out test splits only. The 15 projects are **LLM-free re-implementations of each project's recall "
          "path** (see `reports/` and `baselines/`), run with the same shared encoders as ORP, each with its best "
          "dev-tuned configuration; they are not the products themselves. Methodology, tuning budgets and limitations: "
          "`results/METHODOLOGY.md`.", "",
          "**R@B**: share of gold evidence turns inside the first B tokens of the returned context (characters/4 "
          "tokens, greedy packing). **E@B**: the same, but a turn counts only if the context carries at least half of "
          "its content words. **Ans@B**: share of the gold answer's content words in the context. **SessAll@5**: "
          "LongMemEval's session metric. Verdicts use a paired bootstrap 95% CI against the best of the 15 on that "
          "metric (chosen after the fact, which favours the baselines).", ""]
    tally_all = {"win": 0, "tie": 0, "loss": 0}
    for b, title in (("locomo", "LoCoMo-10 test (7 conversations, categories 1-4)"),
                     ("lme", "LME-R test (LongMemEval oracle questions, recombined 40-session haystacks)")):
        s012 = v012.get(b, {}).get("test", {})
        st = {k: v for k, v in tuned.get(b, {}).get("test", {}).items()}
        if not s012:
            continue
        summ = {**s012, **st}
        tnames = [f"{n}@tuned" for n in BASE if f"{n}@tuned" in summ]
        n = s012["orp"]["n"]
        md += [f"## {title}, n={n}", ""]
        vt, tally = verdict(b, summ, tnames, [(k, h) for k, h in METRICS if k in HEAD])
        for k in tally:
            tally_all[k] += tally[k]
        md += [f"ORP against the best of the 15 on each metric: **{tally['win']} better, {tally['tie']} no significant "
               f"difference, {tally['loss']} worse**.", "", vt, ""]
        order = ["orp", "orp-rrf", "orp-allfeat"] + sorted(tnames, key=lambda x: -(summ[x].get("recall@1000") or 0)) \
            + ["bm25", "dense"]
        md += ["All systems (baselines dev-tuned):", "", table(summ, order), ""]
        dflt = v011.get(b, {}).get("test", {})
        if dflt:
            dn = sorted([x for x in BASE if x in dflt], key=lambda x: -(dflt[x].get("recall@1000") or 0))
            md += ["Baselines with their default settings (v0.1.1 run; E@B was added later):", "",
                   table(dflt, dn, [m for m in METRICS if m[0] != "e_recall@1000"]), ""]
            if "orp" in dflt and "orp" in s012:
                same = all(abs((dflt["orp"].get(k) or 0) - (s012["orp"].get(k) or 0)) < 1e-9
                           for k in ("recall@500", "recall@1000", "recall@2000", "ans@1000", "mrr"))
                md += [f"ORP v0.1.1 and v0.1.2 test results are {'identical' if same else 'different'} on this "
                       "benchmark" + ("." if same else f" (v0.1.1 R@1000 {f(dflt['orp']['recall@1000'])}).") + "", ""]
        if b == "lme" and quirks:
            md += [f"Without the {len(quirks)} test questions affected by LME-R construction quirks "
                   "(`results/lme_quirks.json`):", "",
                   "| System | " + " | ".join(h for _, h in METRICS[:7]) + " |", "|---|" + "---|" * 7]
            for nme in ["orp"] + sorted(tnames, key=lambda x: -(summ[x].get("recall@1000") or 0))[:5] + ["bm25"]:
                md.append(f"| {label(nme)} | " + " | ".join(f(mean_excl(b, nme, k, set(quirks)))
                                                          for k, _ in METRICS[:7]) + " |")
            vq, tq = verdict(b, {k: {m: mean_excl(b, k, m, set(quirks)) for m, _ in METRICS[:7]}
                                 for k in ["orp"] + tnames}, tnames, METRICS[:7], set(quirks))
            md += ["", f"Verdicts on this subset: {tq['win']} better, {tq['tie']} no significant difference, "
                   f"{tq['loss']} worse.", "", vq, ""]
        md += ["Recall@1000 by question category:", "", cat_table(summ, ["orp"] + tnames[:0] + sorted(
            tnames, key=lambda x: -(summ[x].get("recall@1000") or 0))[:6] + ["bm25"]), ""]
    if hot:
        names = [n for n in ["orp", "orp-rrf"] + [f"{x}@tuned" for x in BASE] + list(BASE) + ["bm25", "dense"]
                 if n in hot]
        md += ["## HotpotQA-50 (held-out, out-of-domain, answers only), n=50", "",
               "Cognee's evaluation set: 500 Wikipedia paragraphs, 50 multi-hop questions. Added after the v0.1.1 "
               "freeze; nothing was tuned on it. Only answer recall can be scored.", ""]
        hm = [("ans@500", "Ans@500"), ("ans@1000", "Ans@1000"), ("ans@2000", "Ans@2000"), ("ans@4000", "Ans@4000")]
        tn = [n for n in hot if n.endswith("@tuned")] or [n for n in hot if n in BASE]
        vh, th = verdict("hotpot", hot, tn, hm)
        md += [f"ORP against the best of the 15: {th['win']} better, {th['tie']} no significant difference, "
               f"{th['loss']} worse.", "", vh, "", table(hot, names, hm), ""]
        for k in th:
            tally_all[k] += th[k]
    md.insert(6, f"**Overall: on {sum(tally_all.values())} benchmark-metric comparisons against the best of the 15, "
                 f"ORP is better on {tally_all['win']}, not significantly different on {tally_all['tie']} and worse on "
                 f"{tally_all['loss']}.**\n")
    md += lmem_track("lmes")
    md += lmem_track("lmem")
    md += lmem_track("lmesd")
    md += lmem_track("lmemd")
    md += facts_track()
    md += retuned_track()
    for vis in ("50", "15"):
        G = load(f"governance_v{vis}.json")
        if not G:
            continue
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
    C5 = {src: load(f"qa5test/score_{src}_orp-clean.json") for src in ("claude", "external")}
    if all(C5.values()):
        acc_c, acc_x = C5["claude"]["accuracy"], C5["external"]["accuracy"]
        md += ["## Final blind QA: ORP v0.1.3 on held-out LongMemEval-S questions (pre-registered)", "",
               f"{C5['claude']['n_questions']} official test questions never used in an earlier QA test; every one of "
               "the 15 baselines, BM25, the gold-evidence oracle, ORP v0.1.2 rendering and ORP v0.1.3 (`orp-clean`: "
               "every line carries its own date, chosen on dev by both readers, `results/qa4dev/`). 1,000-token "
               "contexts, day-precision times. Two independent reader-and-judge pairs: Claude Sonnet and DeepSeek V3. "
               "Protocol: `results/qa5test/PREREGISTRATION.md`.", "",
               "| System | Claude: correct | v0.1.3 minus system [95% CI], Holm p | DeepSeek: correct | "
               "v0.1.3 minus system [95% CI], Holm p |", "|---|---|---|---|---|"]
        for sname in sorted(acc_c, key=lambda x: -(acc_c[x] + acc_x[x])):
            cells = []
            for src, a in (("claude", acc_c), ("external", acc_x)):
                c = C5[src]["ref_minus"].get(sname)
                cells += [f(a[sname]), (f"{c['diff'] * 100:+.1f} [{c['ci95'][0] * 100:+.1f}, {c['ci95'][1] * 100:+.1f}], "
                                        f"{c['p_holm']:.3f}") if c else "–"]
            lab = {"oracle": "Oracle (gold evidence only)", "orp-clean": "**ORP v0.1.3**",
                   "orp": "ORP v0.1.2 rendering"}.get(sname, label(sname.split("@")[0]))
            md.append(f"| {lab} | " + " | ".join(cells) + " |")
        md.append("")
    Q3 = load("qa3_blind.json")
    if Q3:
        acc, cmp_ = Q3["accuracy"], Q3["orp_minus"]
        cats = list(Q3["by_category"])
        md += ["## Blind QA v3 on official LongMemEval-S (pre-registered; all 15 baselines)", "",
               f"{Q3['n_questions']} stratified test questions, 1,000-token contexts, day-precision question times for "
               "every system. Claude Sonnet readers answered from each context without knowing the system; separate "
               "Sonnet judges graded with LongMemEval's official per-type rules, also blind. A second judge pass on "
               f"{Q3['judge_agreement']['n']} items agreed on {Q3['judge_agreement']['agreement'] * 100:.1f}% "
               f"(Cohen's kappa {Q3['judge_agreement']['kappa']:.3f}). Protocol: `results/qa3/PREREGISTRATION.md`.", "",
               "| System | Accuracy | ORP minus system [95% CI] | Holm p | " + " | ".join(cats) + " |",
               "|---|---|---|---|" + "---|" * len(cats)]
        for sname in sorted(acc, key=lambda x: -acc[x]):
            c = cmp_.get(sname)
            lab = "Oracle (gold evidence only)" if sname == "oracle" else label(sname.split("@")[0])
            md.append(f"| {lab} | {f(acc[sname])} | " + (f"{c['diff'] * 100:+.1f} [{c['ci95'][0] * 100:+.1f}, "
                      f"{c['ci95'][1] * 100:+.1f}] | {c['p_holm']:.3f}" if c else "– | –") + " | " +
                      " | ".join(f(Q3["by_category"][ct].get(sname)) for ct in cats) + " |")
        md.append("| _questions_ | | | | " + " | ".join(str(Q3["n_by_category"][ct]) for ct in cats) + " |")
        md.append("")
    QX = load("qa3_blind_external.json")
    if QX and Q3:
        accx, cmpx = QX["accuracy"], QX["orp_minus"]
        md += [f"### The same blind items, read and judged by a non-Claude model ({QX['run']['model']}, "
               f"{QX['run']['provider']} API)", "",
               "Pre-registered as an addendum before any output existed; run on the user's machine (this workspace "
               "cannot reach other model APIs). Same contexts, briefs and statistics. **It does not confirm ORP's lead.** "
               f"The gold-evidence ceiling falls from {f(acc['oracle'])}% to {f(accx['oracle'])}%: this reader often "
               "answers \"I don't know\" when an answer needs combining several facts (multi-session, preference and "
               "temporal questions), and it did so most often on ORP's contexts.", "",
               "| System | Accuracy (non-Claude) | ORP minus system [95% CI] | Holm p | Accuracy (Claude) |",
               "|---|---|---|---|---|"]
        for sname in sorted(accx, key=lambda x: -accx[x]):
            c = cmpx.get(sname)
            lab = "Oracle (gold evidence only)" if sname == "oracle" else label(sname.split("@")[0])
            md.append(f"| {lab} | {f(accx[sname])} | " + (f"{c['diff'] * 100:+.1f} [{c['ci95'][0] * 100:+.1f}, "
                      f"{c['ci95'][1] * 100:+.1f}] | {c['p_holm']:.3f}" if c else "– | –") + f" | {f(acc[sname])} |")
        md.append("")
    Q = load("qa_blind.json")
    if Q:
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
    L = load("latency.json")
    if L:
        md += ["## Latency (single process, one core, LoCoMo test, cold caches)", "",
               "| System | p50 ms | p95 ms | build ms |", "|---|---|---|---|"]
        for n, v in sorted(L.items(), key=lambda kv: kv[1]["p50"]):
            md.append(f"| {label(n)} | {v['p50']:.1f} | {v['p95']:.1f} | {v['build']:.0f} |")
        md.append("")
    open(os.path.join(RES, "RESULTS.md"), "w").write("\n".join(md))
    print("\n".join(md))


if __name__ == "__main__":
    main()
