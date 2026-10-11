// smartware eval: re-measure recall on your own data, with your own reader, on your own machine.
//
//   smartware eval --data set.swbench.jsonl [--systems smartware,bm25,dense] [--encoder ...] [--ranker auto]
//            [--reader openai:<model>@<base-url> | anthropic:<model>] [--reader-key-env SMARTWARE_EVAL_KEY]
//            [--judge same|openai:...|anthropic:...] [--qa-budget 2000] [--limit N] [--out results/]
//   smartware eval --data questions.jsonl --url <server> --token-env SMARTWARE_TOKEN   (a live deployment: questions only)
//
// Input (smartware-bench-1, one haystack per line): {"memories": [{id, content, actor, role, thread, seq, observed_at, meta}],
// "questions": [{id, question, as_of, answer, evidence: [memory ids], category}]}. tools/eval/export_bench.py writes
// the published Smartware Bench sets in this format.
//
// Every run reports BM25 over raw units and dense retrieval as baselines, and (with a reader) the gold-evidence
// ceiling, so a weak reader shows up as a low ceiling rather than a bad memory system. Every number comes with a 95%
// paired bootstrap interval. Data never leaves the machine except to the reader endpoint you configure; keys come
// only from the environment and are never logged or written.
import { createReadStream, mkdirSync, writeFileSync } from "node:fs";
import { createInterface } from "node:readline";
import { join } from "node:path";
import { BM25, chooseRanker, Engine, isoformat, loadEncoder, memoryFromJson, parseTime, recallRequest, strftime, terms,
  type Encoder } from "@technodotventures/smartware-core";

type Json = Record<string, any>;
export interface Item { ids: string[]; text: string }
export interface Question { id: string; question: string; as_of: string | null; answer: string; evidence: string[]; evidence_threads?: string[]; category?: string }
export interface Haystack { id: string; memories: Json[]; questions: Question[] }
export const BUDGETS = [500, 1000, 2000, 4000];

// ------------------------------------------------------------ metrics (bench/metrics.py, primary track)
export const chars4 = (t: string) => Math.max(1, Math.floor(([...t].length + 3) / 4));

export function pack(items: Item[], budget: number): { covered: Set<string>; used: number; kept: Item[] } {
  const covered = new Set<string>();
  const kept: Item[] = [];
  let used = 0;
  for (const it of items) {
    const c = chars4(it.text);
    if (used + c > budget) continue; // greedy fill, skipping what does not fit
    used += c;
    it.ids.forEach((x) => covered.add(x));
    kept.push(it);
  }
  if (!kept.length && items.length) { covered.add(items[0]!.ids[0]!); kept.push(items[0]!); used = budget; }
  return { covered, used, kept };
}

export function scoreQuery(items: Item[], gold: Set<string>, answer: string, unitText: Map<string, string>, unitThread: Map<string, string>,
  goldThreads?: string[]): Json {
  const out: Json = {};
  const ans = new Set(answer ? terms(answer) : []);
  const answ = new Set([...ans].filter((w) => !/^[0-9]+$/.test(w)));
  for (const b of BUDGETS) {
    const { covered, used, kept } = pack(items, b);
    if (ans.size) {
      const ctx = new Set<string>();
      for (const it of kept) for (const t of terms(it.text)) ctx.add(t);
      out[`ans@${b}`] = [...ans].filter((w) => ctx.has(w)).length / ans.size;
      if (answ.size) out[`answ@${b}`] = [...answ].filter((w) => ctx.has(w)).length / answ.size;
    }
    if (gold.size) {
      const hit = [...gold].filter((g) => covered.has(g)).length;
      out[`recall@${b}`] = hit / gold.size;
      out[`full@${b}`] = hit === gold.size ? 1 : 0;
      let eh = 0;
      for (const g of gold) {
        if (!covered.has(g)) continue;
        const need = new Set(terms(unitText.get(g) ?? ""));
        const have = new Set<string>();
        for (const it of kept) if (it.ids.includes(g)) for (const t of terms(it.text)) have.add(t);
        if (!need.size ? kept.some((it) => it.ids.includes(g)) : [...need].filter((t) => have.has(t)).length >= 0.5 * need.size) eh++;
      }
      out[`e_recall@${b}`] = eh / gold.size;
    }
    out[`tokens@${b}`] = used;
  }
  const goldSess = new Set(goldThreads ?? ([...gold].map((g) => unitThread.get(g)).filter(Boolean) as string[]));
  const ranked: string[] = [];
  for (const it of items) for (const id of it.ids) { const s = unitThread.get(id); if (s && !ranked.includes(s)) ranked.push(s); }
  for (const k of [5, 10]) {
    const top = new Set(ranked.slice(0, k));
    out[`sess_any@${k}`] = [...goldSess].some((s) => top.has(s)) ? 1 : 0;
    out[`sess_all@${k}`] = [...goldSess].every((s) => top.has(s)) ? 1 : 0;
  }
  const first = items.findIndex((it) => it.ids.some((x) => gold.has(x)));
  out.mrr = first < 0 ? 0 : 1 / (first + 1);
  return out;
}

// ------------------------------------------------------------ statistics
function rng(seed: number) {
  return () => { seed |= 0; seed = (seed + 0x6d2b79f5) | 0; let t = Math.imul(seed ^ (seed >>> 15), 1 | seed); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}

/** Mean and 95% bootstrap interval; with `base`, the interval of the paired difference. */
export function bootstrap(xs: number[], base?: number[], n = 2000, seed = 7): { mean: number; lo: number; hi: number } {
  const d = base ? xs.map((x, i) => x - base[i]!) : xs;
  const m = d.reduce((a, b) => a + b, 0) / Math.max(1, d.length);
  const r = rng(seed), means: number[] = [];
  for (let i = 0; i < n; i++) {
    let s = 0;
    for (let j = 0; j < d.length; j++) s += d[Math.floor(r() * d.length)]!;
    means.push(s / Math.max(1, d.length));
  }
  means.sort((a, b) => a - b);
  return { mean: m, lo: means[Math.floor(0.025 * n)] ?? m, hi: means[Math.floor(0.975 * n)] ?? m };
}

// ------------------------------------------------------------ systems
interface System { name: string; build(h: Haystack): void; retrieve(q: Question, k: number): Item[] }

function smartwareSystem(enc: Encoder, ranker: string): System {
  const { params } = chooseRanker(ranker, enc.name);
  let eng: Engine;
  return {
    name: "smartware",
    build(h) {
      eng = new Engine(enc, params);
      eng.remember(h.memories.map((d) => {
        const m = memoryFromJson({ kind: "event", ...d, meta: { ...(d.meta ?? {}), _idempotency_key: d.id } });
        return m;
      }));
    },
    retrieve(q, k) {
      const pack = eng.recall(recallRequest({ query: q.question, as_of: q.as_of ? parseTime(q.as_of) : null, budget_tokens: null, max_items: k }));
      return pack.items.map((i) => ({ ids: i.memory_ids, text: i.text }));
    },
  };
}

function fmtUnit(d: Json): string {
  const t = d.observed_at ? strftime(parseTime(d.observed_at), "%Y-%m-%d") : "";
  return `[${t}] ${d.actor ?? ""}: ${d.content}`;
}

function bm25System(): System {
  let idx: BM25, units: Json[], texts: string[], mask: Uint8Array;
  return {
    name: "bm25",
    build(h) { units = h.memories; texts = units.map(fmtUnit); idx = new BM25(); texts.forEach((t) => idx.add(t)); mask = new Uint8Array(texts.length).fill(1); },
    retrieve(q, k) {
      const s = idx.score(q.question, mask);
      return [...s.keys()].sort((a, b) => s[b]! - s[a]! || a - b).slice(0, k).map((i) => ({ ids: [units[i]!.id], text: texts[i]! }));
    },
  };
}

function denseSystem(enc: Encoder): System {
  let units: Json[], texts: string[], E: Float32Array[];
  return {
    name: "dense",
    build(h) { units = h.memories; texts = units.map(fmtUnit); E = units.map((d) => enc.embed(`${d.actor ?? ""}: ${d.content}`)); },
    retrieve(q, k) {
      const qv = enc.embed(q.question);
      const s = E.map((e) => { let x = 0; for (let j = 0; j < e.length; j++) x += e[j]! * qv[j]!; return x; });
      return [...s.keys()].sort((a, b) => s[b]! - s[a]! || a - b).slice(0, k).map((i) => ({ ids: [units[i]!.id], text: texts[i]! }));
    },
  };
}

// ------------------------------------------------------------ reader and judge (optional)
export const READER = `You answer one question from a MEMORY CONTEXT that a retrieval system returned from the user's past conversations.
Use ONLY that context: no outside knowledge. Dates: use the dates shown for when things were said; resolve "today", "yesterday", "last week" against the date of the conversation they appear in; for "how long" or "how many days between" questions compute from those dates and the question date.
If the context does not contain enough information, answer exactly: I don't know
Keep answers short: a phrase or one sentence. Reply with the answer only.`;

export const JUDGE = `Grade a candidate answer against a gold answer. CORRECT if the candidate contains the correct answer, is equivalent to it, or contains all the intermediate steps needed to reach it. Partial answers, contradictions, "I don't know", refusals and vague answers are WRONG.
temporal questions: do not penalise off-by-one errors in a number of days, weeks or months. knowledge-update questions: CORRECT if the updated answer is the one given, even if earlier information is mentioned. preference questions: the gold is a rubric; CORRECT if the candidate uses the user's information as the rubric describes.
Reply with exactly one word: CORRECT or WRONG.`;

export interface Model { name: string; complete(system: string, user: string, maxTokens: number): Promise<string> }

export function modelFrom(spec: string, keyEnv: string): Model {
  const key = process.env[keyEnv];
  if (spec.startsWith("anthropic:")) {
    const model = spec.slice(10);
    return { name: spec, async complete(system, user, maxTokens) {
      if (!key) throw new Error(`set ${keyEnv} for the reader`);
      const r = await retryFetch("https://api.anthropic.com/v1/messages", { method: "POST", headers: { "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json" },
        body: JSON.stringify({ model, max_tokens: maxTokens, temperature: 0, system, messages: [{ role: "user", content: user }] }) });
      const b = await r.json() as Json;
      return String(b.content?.[0]?.text ?? "").trim();
    } };
  }
  if (spec.startsWith("openai:")) {
    const rest = spec.slice(7);
    const at = rest.indexOf("@");
    const model = at < 0 ? rest : rest.slice(0, at);
    const base = (at < 0 ? "https://api.openai.com/v1" : rest.slice(at + 1)).replace(/\/$/, "");
    return { name: spec, async complete(system, user, maxTokens) {
      const r = await retryFetch(`${base}/chat/completions`, { method: "POST", headers: { ...(key ? { authorization: `Bearer ${key}` } : {}), "content-type": "application/json" },
        body: JSON.stringify({ model, temperature: 0, max_tokens: maxTokens, messages: [{ role: "system", content: system }, { role: "user", content: user }] }) });
      const b = await r.json() as Json;
      return String(b.choices?.[0]?.message?.content ?? "").trim();
    } };
  }
  throw new Error(`unknown reader ${spec}: use openai:<model>[@<base-url>] or anthropic:<model>`);
}

async function retryFetch(url: string, init: RequestInit): Promise<Response> {
  for (let a = 0; ; a++) {
    const r = await fetch(url, init);
    if (r.ok) return r;
    if (r.status === 401 || r.status === 403 || r.status === 402) throw new Error(`the model API refused the request (${r.status}): check the key and credit`);
    if (a >= 5) throw new Error(`model API error ${r.status}`);
    await new Promise((ok) => setTimeout(ok, 2 ** a * 500 * (0.5 + Math.random())));
  }
}

async function mapLimit<T, R>(xs: T[], n: number, fn: (x: T, i: number) => Promise<R>): Promise<R[]> {
  const out: R[] = new Array(xs.length);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(n, xs.length) }, async () => { for (;;) { const i = next++; if (i >= xs.length) return; out[i] = await fn(xs[i]!, i); } }));
  return out;
}

// ------------------------------------------------------------ runner
export async function readSet(path: string, limit?: number): Promise<Haystack[]> {
  const out: Haystack[] = [];
  let nq = 0;
  const rl = createInterface({ input: createReadStream(path), crlfDelay: Infinity });
  for await (const line of rl) {
    if (!line.trim()) continue;
    const h = JSON.parse(line) as Haystack;
    if (limit !== undefined) { if (nq >= limit) break; h.questions = h.questions.slice(0, limit - nq); }
    nq += h.questions.length;
    out.push(h);
  }
  return out;
}

export interface EvalOptions { systems: string[]; encoder: string; ranker: string; k: number; reader?: Model; judge?: Model; qaBudget: number; concurrency: number }

export async function runEval(sets: Haystack[], o: EvalOptions): Promise<{ rows: Json[]; summary: Json }> {
  const enc = loadEncoder(o.encoder);
  const systems: System[] = o.systems.map((s) => s === "smartware" ? smartwareSystem(enc, o.ranker) : s === "bm25" ? bm25System() : s === "dense" ? denseSystem(enc) : (() => { throw new Error(`unknown system ${s}`); })());
  const rows: Json[] = [];
  for (const h of sets) {
    const unitText = new Map(h.memories.map((d) => [d.id, String(d.content)]));
    const unitThread = new Map(h.memories.map((d) => [d.id, String(d.thread ?? "")]));
    const unitFmt = new Map(h.memories.map((d) => [d.id, fmtUnit(d)]));
    for (const s of systems) {
      s.build(h);
      for (const q of h.questions) {
        const items = s.retrieve(q, o.k);
        const m = scoreQuery(items, new Set(q.evidence), q.answer, unitText, unitThread, q.evidence_threads);
        const ctx = pack(items, o.qaBudget).kept.map((i) => i.text).join("\n\n");
        rows.push({ system: s.name, haystack: h.id, qid: q.id, category: q.category ?? "", ...m, _q: q, _ctx: ctx });
      }
    }
    if (o.reader) for (const q of h.questions) rows.push({ system: "ceiling", haystack: h.id, qid: q.id, category: q.category ?? "", _q: q,
      _ctx: q.evidence.map((e) => unitFmt.get(e) ?? "").join("\n\n") });
  }
  if (o.reader) {
    const judge = o.judge ?? o.reader;
    await mapLimit(rows, o.concurrency, async (r) => {
      const q = r._q as Question;
      r.answer = await o.reader!.complete(READER, `Question: ${q.question}\nAsked on: ${q.as_of ?? "unknown"}\n\nMEMORY CONTEXT:\n${r._ctx || "(empty)"}`, 200);
      const v = await judge.complete(JUDGE, `Question: ${q.question}\nQuestion type: ${q.category ?? ""}\nGold answer: ${q.answer}\nCandidate answer: ${r.answer}`, 5);
      r.qa = v.toUpperCase().startsWith("CORRECT") ? 1 : 0;
    });
  }
  // summary: per system, every metric with a 95% interval; differences paired against the first system
  const bySys = new Map<string, Json[]>();
  for (const r of rows) { let a = bySys.get(r.system); if (!a) bySys.set(r.system, (a = [])); a.push(r); }
  const metrics = ["recall@500", "recall@1000", "recall@2000", "e_recall@1000", "ans@500", "ans@1000", "ans@2000", "sess_all@5", "mrr", "qa"];
  const summary: Json = { questions: (bySys.get(o.systems[0]!) ?? []).length, systems: {}, reader: o.reader?.name ?? null, judge: (o.judge ?? o.reader)?.name ?? null,
    encoder: enc.name, ranker: chooseRanker(o.ranker, enc.name).name, budgets: BUDGETS, qa_budget: o.qaBudget };
  const ref = bySys.get(o.systems[0]!) ?? [];
  const key = (r: Json) => `${r.haystack}\0${r.qid}`;
  for (const [name, rs] of bySys) {
    const s: Json = {};
    const refBy = new Map(ref.map((r) => [key(r), r]));
    for (const m of metrics) {
      const xs = rs.filter((r) => typeof r[m] === "number");
      if (!xs.length) continue;
      s[m] = bootstrap(xs.map((r) => r[m]));
      if (name !== o.systems[0]) {
        const pairs = xs.filter((r) => refBy.has(key(r)) && typeof refBy.get(key(r))![m] === "number");
        if (pairs.length) s[`${m}_vs_${o.systems[0]}`] = bootstrap(pairs.map((r) => r[m]), pairs.map((r) => refBy.get(key(r))![m]));
      }
    }
    summary.systems[name] = s;
  }
  return { rows, summary };
}

function table(summary: Json): string {
  const ms = ["recall@500", "recall@1000", "recall@2000", "e_recall@1000", "ans@500", "ans@1000", "ans@2000", "sess_all@5", "mrr", "qa"];
  const pct = (x: Json | undefined) => (x ? `${(100 * x.mean).toFixed(1)} [${(100 * x.lo).toFixed(1)}, ${(100 * x.hi).toFixed(1)}]` : "");
  const head = ["system", ...ms.filter((m) => Object.values(summary.systems).some((s: any) => s[m]))];
  const lines = [head.join(" | "), head.map(() => "---").join(" | ")];
  for (const [name, s] of Object.entries(summary.systems) as Array<[string, Json]>) lines.push([name, ...head.slice(1).map((m) => pct(s[m]))].join(" | "));
  return lines.join("\n");
}

export async function evalCmd(_pos: string[], f: Record<string, any>): Promise<number> {
  const data = typeof f.data === "string" ? f.data : null;
  if (!data) { process.stderr.write("smartware eval --data <set.swbench.jsonl> (see tools/eval/export_bench.py)\n"); return 2; }
  const sets = await readSet(data, f.limit ? Number(f.limit) : undefined);
  const keyEnv = typeof f["reader-key-env"] === "string" ? f["reader-key-env"] : "SMARTWARE_EVAL_KEY";
  const reader = typeof f.reader === "string" ? modelFrom(f.reader, keyEnv) : undefined;
  const judge = typeof f.judge === "string" && f.judge !== "same" ? modelFrom(f.judge, typeof f["judge-key-env"] === "string" ? f["judge-key-env"] : keyEnv) : undefined;
  const t0 = Date.now();
  const { rows, summary } = await runEval(sets, {
    systems: (typeof f.systems === "string" ? f.systems : "smartware,bm25,dense").split(","), encoder: typeof f.encoder === "string" ? f.encoder : "hashing",
    ranker: typeof f.ranker === "string" ? f.ranker : "auto", k: Number(f.k ?? 50), reader, judge, qaBudget: Number(f["qa-budget"] ?? 2000),
    concurrency: Number(f.concurrency ?? 8),
  });
  summary.seconds = (Date.now() - t0) / 1000;
  summary.data = data;
  process.stdout.write(table(summary) + "\n");
  if (typeof f.out === "string") {
    mkdirSync(f.out, { recursive: true });
    writeFileSync(join(f.out, "summary.json"), JSON.stringify(summary, null, 1));
    writeFileSync(join(f.out, "per_question.jsonl"), rows.map((r) => JSON.stringify(Object.fromEntries(Object.entries(r).filter(([k]) => !k.startsWith("_"))))).join("\n") + "\n");
    process.stdout.write(`wrote ${join(f.out, "summary.json")} and per_question.jsonl\n`);
  }
  void isoformat;
  return 0;
}
