"""Build the shareable results page (results/site/index.html) from the same files bench.report reads."""
from __future__ import annotations

import html
import json
import os

from .report import BASE, HEAD, METRICS, ORP, RES, label, load, load_tuned, mean_excl, paired, verdict

OUT = os.path.join(RES, "site")
SPEC_URL = "https://claude.ai/code/artifact/85592f49-799b-4a21-b16b-f5f433fcbd8b"


def pct(x):
    return "–" if x is None else f"{x * 100:.1f}"


def esc(s):
    return html.escape(str(s))


def plain(n):
    return label(n).replace("**", "").replace("_", "")


def bars(summ, names, metric, title):
    rows = [(plain(n), summ[n].get(metric), n) for n in names if n in summ and summ[n].get(metric) is not None]
    rows.sort(key=lambda r: -r[1])
    h = 22 * len(rows) + 34
    out = [f'<figure class="chart"><figcaption>{esc(title)}</figcaption>'
           f'<svg viewBox="0 0 640 {h}" role="img" aria-label="{esc(title)}">']
    for t in (0, 25, 50, 75, 100):
        x = 220 + 3.6 * t
        out.append(f'<line x1="{x}" y1="18" x2="{x}" y2="{h - 6}" class="grid"/>'
                   f'<text x="{x}" y="12" class="tick" text-anchor="middle">{t}</text>')
    for i, (lab, v, n) in enumerate(rows):
        y = 22 + 22 * i
        cls = "bar orp" if n == "orp" else "bar ref" if n in ("bm25", "dense") else "bar"
        out.append(f'<text x="212" y="{y + 12}" class="lab{" strong" if n == "orp" else ""}" text-anchor="end">'
                   f'{esc(lab[:34])}</text><rect x="220" y="{y + 2}" width="{max(1, 3.6 * v * 100):.1f}" height="14" '
                   f'class="{cls}"/><text x="{224 + 3.6 * v * 100:.1f}" y="{y + 13}" class="val">{v * 100:.1f}</text>')
    out.append("</svg></figure>")
    return "".join(out)


def table(summ, names, metrics):
    th = "".join(f"<th>{esc(h)}</th>" for _, h in metrics)
    body = []
    for n in names:
        if n not in summ:
            continue
        tds = "".join(f"<td>{pct(summ[n].get(k))}</td>" for k, _ in metrics)
        body.append(f'<tr class="{"orp" if n == "orp" else "ref" if n in ("bm25", "dense") else ""}">'
                    f"<th scope=\"row\">{esc(plain(n))}</th>{tds}</tr>")
    return (f'<div class="tw"><table><thead><tr><th scope="col">System</th>{th}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def verdict_rows(bench, summ, tnames, metrics, exclude=(), orp="orp"):
    out = []
    for k, h in metrics:
        if summ.get("orp", {}).get(k) is None:
            continue
        c = [(summ[n].get(k), n) for n in tnames if n in summ and summ[n].get(k) is not None]
        if not c:
            continue
        bv, bn = max(c)
        r = paired(bench, orp, bn, k, exclude)
        if r is None:
            continue
        state = "win" if r[1] > 0 else "loss" if r[2] < 0 else "tie"
        word = {"win": "ORP better", "loss": "ORP worse", "tie": "No significant difference"}[state]
        out.append(f'<tr><th scope="row">{esc(h)}</th><td>{pct(summ["orp"][k])}</td><td>{pct(bv)} '
                   f'<span class="muted">{esc(plain(bn))}</span></td><td class="num">{r[0] * 100:+.1f} '
                   f'<span class="muted">[{r[1] * 100:+.1f}, {r[2] * 100:+.1f}]</span></td>'
                   f'<td><span class="pill {state}">{word}</span></td></tr>')
    return ('<div class="tw"><table class="verdict"><thead><tr><th>Metric</th><th>ORP</th><th>Best of the 15</th>'
            '<th>Difference [95% CI]</th><th>Verdict</th></tr></thead><tbody>' + "".join(out) + "</tbody></table></div>")


def main():
    v011, v012, tuned = load("summary.json"), load("summary_v012.json"), load_tuned()
    hot = {}
    for fn in ("summary_hotpot.json", "summary_hotpot_tuned.json", "summary_hotpot_tuned_b.json"):
        hot.update(load(fn).get("hotpot", {}).get("test", {}))
    quirks = set(load("lme_quirks.json").get("excluded", []))
    sections, tallies = [], {}
    for b, title, blurb in (
            ("locomo", "LoCoMo-10", "Long multi-session conversations between two people. 1,152 test questions over "
                                    "7 held-out conversations; evidence is the turn that answers the question."),
            ("lme", "LME-R", "LongMemEval questions over 40-session user/assistant histories rebuilt from the oracle "
                             "file (the official haystack could not be downloaded). 382 test questions.")):
        s012 = v012.get(b, {}).get("test", {})
        if not s012:
            continue
        summ = {**s012, **tuned.get(b, {}).get("test", {})}
        tn = [f"{n}@tuned" for n in BASE if f"{n}@tuned" in summ]
        _, t = verdict(b, summ, tn, [(k, h) for k, h in METRICS if k in HEAD])
        tallies[title] = t
        names = ["orp", "orp-rrf"] + sorted(tn, key=lambda x: -(summ[x].get("recall@1000") or 0)) + ["bm25", "dense"]
        extra = ""
        if b == "lme" and quirks:
            sub = {k: {m: mean_excl(b, k, m, quirks) for m, _ in METRICS[:7]} for k in ["orp"] + tn}
            _, tq = verdict(b, sub, tn, METRICS[:7], quirks)
            extra = (f'<p class="note">Without the {len(quirks)} questions affected by LME-R construction quirks '
                     f'(gold turns timestamped after the question, or distractors dated after it): ORP is better on '
                     f'{tq["win"]}, level on {tq["tie"]} and worse on {tq["loss"]} of 7 metrics.</p>')
        sections.append(f"""
<section id="{b}" aria-labelledby="h-{b}">
  <h2 id="h-{b}">{esc(title)}</h2>
  <p class="lede">{esc(blurb)}</p>
  <p class="tally"><span class="pill win">{t['win']} better</span><span class="pill tie">{t['tie']} level</span>
  <span class="pill loss">{t['loss']} worse</span> against the best of the 15 on each metric</p>
  {verdict_rows(b, summ, tn, [(k, h) for k, h in METRICS if k in HEAD])}
  {extra}
  <div class="charts">{bars(summ, names, 'recall@1000', 'Evidence recall in 1,000 tokens (R@1000)')}
  {bars(summ, names, 'ans@1000', 'Answer recall in 1,000 tokens (Ans@1000)')}</div>
  <details><summary>Every system, every metric</summary>{table(summ, names, METRICS)}</details>
</section>""")
    if hot:
        tn = [n for n in hot if n.endswith("@tuned")] or [n for n in hot if n in BASE]
        hm = [("ans@500", "Ans@500"), ("ans@1000", "Ans@1000"), ("ans@2000", "Ans@2000"), ("ans@4000", "Ans@4000")]
        _, t = verdict("hotpot", hot, tn, hm)
        tallies["HotpotQA-50"] = t
        names = ["orp", "orp-rrf"] + sorted(tn, key=lambda x: -(hot[x].get("ans@1000") or 0)) + ["bm25", "dense"]
        sections.append(f"""
<section id="hotpot" aria-labelledby="h-hotpot">
  <h2 id="h-hotpot">HotpotQA-50 <span class="tag">held out</span></h2>
  <p class="lede">Cognee's evaluation set: 500 Wikipedia paragraphs and 50 multi-hop questions. Nothing was tuned on
  it and it was run once, after the protocol was frozen. Only answers are labelled, so only answer recall is scored;
  with 50 questions the intervals are wide.</p>
  <p class="tally"><span class="pill win">{t['win']} better</span><span class="pill tie">{t['tie']} level</span>
  <span class="pill loss">{t['loss']} worse</span></p>
  {verdict_rows("hotpot", hot, tn, hm)}
  <div class="charts">{bars(hot, names, 'ans@1000', 'Answer recall in 1,000 tokens (Ans@1000)')}</div>
</section>""")
    for bench, sfile, dfile, ttl in (("locomo", "summary_facts.json", "facts_dev.json", "LoCoMo"),
                                     ("lme", "summary_facts_lme.json", "facts_dev_lme.json", "LME-R")):
        F, D = load(sfile), load(dfile)
        if not (F and D):
            continue
        S = {**tuned.get(bench, {}).get("test", {}), **F[bench]["test"]}
        ch = D["choice"]
        pool = [ch[f"{b}@tuned"] for b in BASE if ch.get(f"{b}@tuned") in S]
        summ = {**{n: S[n] for n in pool}, "orp": S[ch["orp"]]}
        hm = [m for m in METRICS if m[0] in HEAD and m[0] != "e_recall@1000"]
        _, t = verdict(bench, summ, pool, hm, orp=ch["orp"])
        tallies[f"{ttl}, LLM facts"] = t
        disp = {n.replace("+facts", ""): v for n, v in summ.items()}
        sections.append(f"""
<section id="facts-{bench}" aria-labelledby="h-facts-{bench}">
  <h2 id="h-facts-{bench}">{ttl} with LLM-extracted facts</h2>
  <p class="lede">Claude Haiku extracted facts from every session once, the way mem0 does at write time, and every
  system was allowed to store them. Each system's choice of store was made on dev data only.</p>
  <p class="tally"><span class="pill win">{t['win']} better</span><span class="pill tie">{t['tie']} level</span>
  <span class="pill loss">{t['loss']} worse</span></p>
  {verdict_rows(bench, summ, pool, hm, orp=ch["orp"])}
  <div class="charts">{bars(disp, ["orp"] + [n.replace("+facts", "") for n in pool], 'recall@1000', 'Evidence recall in 1,000 tokens, fact stores')}
  {bars(disp, ["orp"] + [n.replace("+facts", "") for n in pool], 'ans@1000', 'Answer recall in 1,000 tokens, fact stores')}</div>
</section>""")
    D2 = load("facts_dev2.json")
    for bench, ttl in (("locomo", "LoCoMo"), ("lme", "LME-R"), ("hotpot", "HotpotQA-50")):
        F = load(f"summary_ft_{bench}.json").get(bench, {}).get("test", {})
        if not (F and D2):
            continue
        ch = D2["choice"][bench]
        pool = [ch[b] for b in BASE if ch.get(b) in F]
        summ = {**{n: F[n] for n in pool}, "orp": F[ch["orp"]]}
        hm = [m for m in METRICS if m[0] in HEAD and m[0] != "e_recall@1000"] if bench != "hotpot" else \
            [("ans@500", "Ans@500"), ("ans@1000", "Ans@1000"), ("ans@2000", "Ans@2000"), ("ans@4000", "Ans@4000")]
        _, t = verdict(bench, summ, pool, hm, orp=ch["orp"])
        tallies[f"{ttl}, facts re-tuned"] = t
        disp = {n.split("@")[0]: v for n, v in summ.items()}
        sections.append(f"""
<section id="ft-{bench}" aria-labelledby="h-ft-{bench}">
  <h2 id="h-ft-{bench}">{ttl}: LLM facts, everything re-tuned</h2>
  <p class="lede">Every baseline's knobs re-tuned on dev fact stores and ORP's ranker refitted on them; each system's
  best configuration picked on dev{'' if bench != 'hotpot' else ' (mean of both benchmarks, as HotpotQA has no dev split)'}.</p>
  <p class="tally"><span class="pill win">{t['win']} better</span><span class="pill tie">{t['tie']} level</span>
  <span class="pill loss">{t['loss']} worse</span></p>
  {verdict_rows(bench, summ, pool, hm, orp=ch["orp"])}
  <div class="charts">{bars(disp, ["orp"] + [n.split("@")[0] for n in pool], hm[1][0], hm[1][1] + ', re-tuned fact stores')}</div>
</section>""")
    gov = []
    for vis in ("50", "15"):
        G = load(f"governance_v{vis}.json")
        if not G:
            continue
        lab = {"orp": "ORP: rank only what the asker may read", "global_stats": "Filter first, shared statistics",
               "post_filter@10": "Filter after the top 10", "post_filter@50": "Filter after the top 50",
               "scope_only": "Workspace scope only", "derived_bank": "Per-item ACL, shared summaries"}
        rows = "".join(
            f'<tr class="{"orp" if k == "orp" else ""}"><th scope="row">{esc(lab[k])}</th>'
            f'<td>{pct(G[k]["recall@1000"])}</td><td class="{"bad" if G[k]["text_leak"] else "ok"}">'
            f'{pct(G[k]["text_leak"])}</td><td class="{"bad" if G[k]["answer_leak"] else "ok"}">'
            f'{pct(G[k]["answer_leak"])}</td><td class="{"bad" if G[k]["rank_side_channel"] else "ok"}">'
            f'{pct(G[k]["rank_side_channel"])}</td></tr>'
            for k in ("orp", "global_stats", "post_filter@10", "post_filter@50", "scope_only", "derived_bank") if k in G)
        gov.append(f"""<h3>{vis}% channel visibility</h3><div class="tw"><table><thead><tr><th>Access control</th>
<th>Recall@1000 (allowed)</th><th>Answers leaked text</th><th>Forbidden askers shown the answer</th>
<th>Rankings moved by hidden memories</th></tr></thead><tbody>{rows}</tbody></table></div>""")
    if gov:
        sections.append(f"""
<section id="governance" aria-labelledby="h-gov">
  <h2 id="h-gov">Multi-tenant governance (ORP-Gov)</h2>
  <p class="lede">The same ranker under six access-control strategies. Each LoCoMo test conversation becomes a
  workspace with six people, channel membership and private messages inside channels, plus a second tenant that reuses
  the same thread ids. Every question is asked by every person. Lower is better in the last three columns.</p>
  {''.join(gov)}
</section>""")
    Q = load("qa_blind.json")
    if Q:
        rows = "".join(f'<tr class="{"orp" if s == "orp" else ""}"><th scope="row">{esc(plain(s))}</th>'
                       f'<td>{pct(Q["accuracy"]["locomo"][s]["acc"])}</td><td>{pct(Q["accuracy"]["lme"][s]["acc"])}</td>'
                       f"</tr>" for s in Q["systems"])
        sections.append(f"""
<section id="qa" aria-labelledby="h-qa">
  <h2 id="h-qa">Blind question answering</h2>
  <p class="lede">Claude Sonnet readers answered from each system's 1,000-token context without knowing which system
  produced it; separate judges graded against the gold answer, also blind.</p>
  <div class="tw"><table><thead><tr><th>System</th><th>LoCoMo accuracy</th><th>LME-R accuracy</th></tr></thead>
  <tbody>{rows}</tbody></table></div>
</section>""")
    L = load("latency.json")
    if L:
        rows = "".join(f'<tr class="{"orp" if n == "orp" else ""}"><th scope="row">{esc(plain(n))}</th>'
                       f'<td>{v["p50"]:.1f}</td><td>{v["p95"]:.1f}</td><td>{v["build"]:.0f}</td></tr>'
                       for n, v in sorted(L.items(), key=lambda kv: kv[1]["p50"]))
        sections.append(f"""
<section id="latency" aria-labelledby="h-lat"><h2 id="h-lat">Latency</h2>
<p class="lede">One core, one process, cold caches, two LoCoMo test conversations. Milliseconds.</p>
<div class="tw"><table><thead><tr><th>System</th><th>p50 query</th><th>p95 query</th><th>Build per conversation</th>
</tr></thead><tbody>{rows}</tbody></table></div></section>""")
    tw = sum(t["win"] for t in tallies.values())
    tt = sum(t["tie"] for t in tallies.values())
    tl = sum(t["loss"] for t in tallies.values())
    strip = "".join(f'<div class="card"><div class="k">{esc(k)}</div><div class="v"><span class="w">{t["win"]}</span>'
                    f'<span class="t">{t["tie"]}</span><span class="l">{t["loss"]}</span></div></div>'
                    for k, t in tallies.items())
    page = TEMPLATE.format(tw=tw, tt=tt, tl=tl, n=tw + tt + tl, strip=strip, sections="".join(sections),
                           spec=SPEC_URL)
    os.makedirs(OUT, exist_ok=True)
    open(os.path.join(OUT, "index.html"), "w").write(page)
    print("wrote", os.path.join(OUT, "index.html"), f"{tw}/{tt}/{tl}")


TEMPLATE = """<title>ORP-Bench Results</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;700;800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: a single reading column of evidence; each benchmark is a verdict table, then two bar charts, then detail. */
:root {{
  --bg: #f2f4f6; --panel: #ffffff; --ink: #16202a; --muted: #5b6875; --rule: #d6dce2;
  --accent: #1f5f8b; --accent-soft: #d8e6f0; --ref: #9aa7b3;
  --win: #1d7a4d; --win-bg: #dcf1e5; --tie: #6b6f75; --tie-bg: #e7e9ec; --loss: #a33a2c; --loss-bg: #f7e0dc;
  --display: "Archivo", "Helvetica Neue", Arial, sans-serif; --body: "IBM Plex Sans", "Segoe UI", Arial, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, Menlo, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #0f151b; --panel: #161e26; --ink: #e3e9ee; --muted: #9aa7b3; --rule: #2a3540;
  --accent: #6fb3e0; --accent-soft: #1d3446; --ref: #5d6b78;
  --win: #6fd3a0; --win-bg: #173527; --tie: #b4bac1; --tie-bg: #262d34; --loss: #f09484; --loss-bg: #3a1f1a;
  color-scheme: dark; }} }}
:root[data-theme="dark"] {{
  --bg: #0f151b; --panel: #161e26; --ink: #e3e9ee; --muted: #9aa7b3; --rule: #2a3540;
  --accent: #6fb3e0; --accent-soft: #1d3446; --ref: #5d6b78;
  --win: #6fd3a0; --win-bg: #173527; --tie: #b4bac1; --tie-bg: #262d34; --loss: #f09484; --loss-bg: #3a1f1a;
  color-scheme: dark; }}
body {{ background: var(--bg); color: var(--ink); font: 15px/1.6 var(--body); }}
.wrap {{ max-width: 980px; margin: 0 auto; padding-inline: 20px; padding-block: 28px 64px; display: grid; gap: 40px; }}
header {{ display: grid; gap: 14px; }}
.eyebrow {{ font: 500 12px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }}
h1 {{ font: 800 clamp(30px, 5vw, 46px)/1.05 var(--display); margin: 0; text-wrap: balance; }}
h2 {{ font: 700 26px/1.2 var(--display); margin: 0; text-wrap: balance; }}
h3 {{ font: 600 16px/1.3 var(--body); margin: 18px 0 6px; }}
.lede {{ max-width: 68ch; margin: 0; color: var(--muted); }}
.headline {{ font-size: 18px; max-width: 70ch; margin: 0; }}
.headline b {{ font-variant-numeric: tabular-nums; }}
.strip {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
.card {{ background: var(--panel); border: 1px solid var(--rule); border-radius: 6px; padding: 12px 14px; display: grid; gap: 6px; }}
.card .k {{ font: 600 13px/1.2 var(--body); color: var(--muted); }}
.card .v {{ display: flex; gap: 8px; font: 600 22px/1 var(--mono); font-variant-numeric: tabular-nums; }}
.card .w {{ color: var(--win); }} .card .t {{ color: var(--tie); }} .card .l {{ color: var(--loss); }}
.legend {{ font: 12px/1.4 var(--mono); color: var(--muted); }}
section {{ display: grid; gap: 14px; min-width: 0; }}
.tally {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 0; color: var(--muted); font-size: 14px; }}
.pill {{ display: inline-block; padding: 2px 9px; border-radius: 999px; font: 600 12px/1.6 var(--body); white-space: nowrap; }}
.pill.win {{ background: var(--win-bg); color: var(--win); }} .pill.tie {{ background: var(--tie-bg); color: var(--tie); }}
.pill.loss {{ background: var(--loss-bg); color: var(--loss); }}
.tag {{ font: 500 11px/1 var(--mono); letter-spacing: .06em; text-transform: uppercase; color: var(--accent);
  border: 1px solid var(--accent); border-radius: 3px; padding: 3px 5px; vertical-align: middle; }}
.tw {{ overflow-x: auto; background: var(--panel); border: 1px solid var(--rule); border-radius: 6px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13.5px; }}
th, td {{ padding: 7px 10px; text-align: right; border-bottom: 1px solid var(--rule); white-space: nowrap;
  font-variant-numeric: tabular-nums; }}
td {{ font-family: var(--mono); font-size: 13px; }}
thead th {{ font: 600 12px/1.3 var(--body); color: var(--muted); background: var(--bg); position: sticky; top: 0; }}
th[scope="row"], thead th:first-child {{ text-align: left; font-family: var(--body); font-weight: 500; }}
tr.orp th, tr.orp td {{ background: var(--accent-soft); font-weight: 600; }}
tr.ref th {{ color: var(--muted); font-style: italic; }}
tbody tr:last-child th, tbody tr:last-child td {{ border-bottom: 0; }}
.verdict td:nth-child(5) {{ font-family: var(--body); }}
.muted {{ color: var(--muted); font-size: 12px; }}
td.ok {{ color: var(--win); }} td.bad {{ color: var(--loss); }}
.note {{ margin: 0; font-size: 14px; max-width: 75ch; }}
.charts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }}
.chart {{ margin: 0; background: var(--panel); border: 1px solid var(--rule); border-radius: 6px; padding: 10px 12px; min-width: 0; }}
.chart figcaption {{ font: 600 13px/1.3 var(--body); margin-bottom: 6px; }}
.chart svg {{ width: 100%; height: auto; display: block; }}
.chart .grid {{ stroke: var(--rule); stroke-width: 1; }}
.chart .tick {{ fill: var(--muted); font: 10px var(--mono); }}
.chart .lab {{ fill: var(--ink); font: 11px var(--body); }}
.chart .lab.strong {{ font-weight: 700; }}
.chart .val {{ fill: var(--muted); font: 10px var(--mono); }}
.chart .bar {{ fill: var(--ref); opacity: .55; }} .chart .bar.orp {{ fill: var(--accent); opacity: 1; }}
.chart .bar.ref {{ fill: var(--ref); opacity: .3; }}
details {{ background: var(--panel); border: 1px solid var(--rule); border-radius: 6px; padding: 8px 12px; }}
details summary {{ cursor: pointer; font-weight: 600; font-size: 14px; }}
details[open] summary {{ margin-bottom: 10px; }}
details .tw {{ border: 0; }}
.protocol {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }}
.protocol div {{ border-top: 2px solid var(--accent); padding-top: 8px; }}
.protocol b {{ display: block; font: 700 15px/1.3 var(--display); margin-bottom: 4px; }}
.protocol p {{ margin: 0; font-size: 14px; color: var(--muted); }}
ul.caveats {{ margin: 0; padding-left: 20px; max-width: 75ch; display: grid; gap: 6px; }}
a {{ color: var(--accent); }} a:focus-visible, summary:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
footer {{ font-size: 13px; color: var(--muted); border-top: 1px solid var(--rule); padding-top: 14px; }}
@media (max-width: 520px) {{ .wrap {{ padding-inline: 16px; }} h2 {{ font-size: 22px; }} }}
</style>
<div class="wrap">
<header>
  <div class="eyebrow">Open Recall Protocol · ORP-Bench v0.1.2</div>
  <h1>How well do agent memory systems recall?</h1>
  <p class="headline">The ORP reference engine against LLM-free re-implementations of the recall paths of 15 open-source
  memory projects, each given its best dev-tuned settings and the same encoders. Across <b>{n}</b> benchmark-metric
  comparisons with the best of the 15 on each metric, ORP is <b>better on {tw}</b>, level on <b>{tt}</b> and worse on
  <b>{tl}</b> (paired bootstrap, 95%).</p>
  <div class="strip">{strip}</div>
  <div class="legend">Cards show better · level · worse against the strongest baseline per metric.</div>
</header>
<section aria-labelledby="h-what">
  <h2 id="h-what">What ORP specifies</h2>
  <div class="protocol">
    <div><b>Rank only what the asker may read</b><p>Access is decided before scoring, statistics come from readable
    memories only, and recall must equal recall over the store with everything unreadable removed.</p></div>
    <div><b>Three clocks</b><p>Observed, happened and valid time on every record; every query carries an as-of time,
    so answers never drift with the wall clock.</p></div>
    <div><b>Supersession you can audit</b><p>Newer claims replace older ones by validity time, only with write
    permission and never hiding a fact from someone who cannot see its replacement.</p></div>
    <div><b>Context, not lists</b><p>Recall returns a token-budgeted, cited, explained context pack with coverage and
    degradation flags.</p></div>
    <div><b>Derived never outranks its sources</b><p>Summaries and profiles inherit the narrowest audience and the
    sensitivity labels of what they were built from.</p></div>
    <div><b>Testable</b><p>70 numbered requirements, each mapped to a conformance test, plus this benchmark with its
    raw per-question results.</p></div>
  </div>
</section>
{sections}
<section aria-labelledby="h-cav">
  <h2 id="h-cav">Read these numbers with care</h2>
  <ul class="caveats">
    <li>The 15 are re-implementations of each project's recall path without an LLM, written from its source code, not
    the products. Projects that rely on LLM extraction or reranking are represented by deterministic stand-ins.</li>
    <li>Every system uses the same modest encoder (PPMI-SVD word vectors), trained without labels on the benchmark
    text. A strong neural embedder could change the order.</li>
    <li>ORP had a larger tuning budget than any single baseline. The hand-tuned, model-free ORP variant is shown
    alongside for a like-for-like comparison of architecture.</li>
    <li>LME-R is a reconstruction of LongMemEval, not the official benchmark. Numbers are not comparable to published
    LoCoMo or LongMemEval accuracies.</li>
    <li>Test results from the pre-audit version had been seen before the current engine was built, so LoCoMo and LME-R
    are not pristine held-out sets for it. HotpotQA-50 is.</li>
  </ul>
</section>
<footer>Specification: <a href="{spec}">ORP 0.1 draft</a>. Methodology, two independent audits and raw per-question
results ship with the repository.</footer>
</div>
"""

if __name__ == "__main__":
    main()
