// Deterministic temporal expressions (port of orp/timeparse.py).
// Query side: "last month", "in May 2023", "two weeks ago" -> a window and an ordering intent relative to as_of.
// Write side: resolve "yesterday", "last Friday" inside a memory against its own time.
import { addDays, fromParts, isValidDate, parts, replace, strftime, type Micros } from "./time.js";

const MONTH_LIST = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
  "november", "december"];
// Same key order as the Python dict (full names, then 3-letter forms except "may", then "sept"): regex alternation
// order decides matches, so it must not change.
export const MONTHS = new Map<string, number>();
MONTH_LIST.forEach((m, i) => MONTHS.set(m, i + 1));
for (const [k, v] of [...MONTHS]) if (!MONTHS.has(k.slice(0, 3))) MONTHS.set(k.slice(0, 3), v);
MONTHS.set("sept", 9);
const MONTH_ALT = [...MONTHS.keys()].join("|");
const MONTH_LONG_ALT = [...MONTHS.keys()].filter((k) => k.length > 3).join("|");

export const WEEKDAYS: Record<string, number> = { monday: 0, tuesday: 1, wednesday: 2, thursday: 3, friday: 4,
  saturday: 5, sunday: 6 };
export const NUMWORDS: Record<string, number> = { a: 1, an: 1, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6,
  seven: 7, eight: 8, nine: 9, ten: 10, couple: 2, few: 3, several: 3 };
export const UNITS: Record<string, number> = { day: 1, days: 1, week: 7, weeks: 7, month: 30, months: 30, year: 365,
  years: 365, weekend: 7, weekends: 7 };

export interface TimeIntent {
  window: [Micros, Micros] | null;
  order: "earliest" | "latest" | null;
  current: boolean;
  when: boolean;
  count: boolean;
  span: boolean;
  matched: string[];
}

export function isTemporal(t: TimeIntent): boolean {
  return Boolean(t.window || t.order || t.when || t.span);
}

function monthWindow(y: number, m: number): [Micros, Micros] {
  return [fromParts(y, m, 1), fromParts(y + (m === 12 ? 1 : 0), (m % 12) + 1, 1)];
}

const isDigits = (s: string) => /^[0-9]+$/.test(s);

const RX = {
  when: /\bwhen\b|\bwhat (date|day|time|year|month)\b|\bwhich (date|day|year|month)\b/,
  count: /\bhow (many|often|much)\b|\blist\b|\ball the\b|\btotal\b|\bin total\b/,
  span: /\bhow long\b|\bhow many (days|weeks|months|years|hours|minutes)\b|\bbetween\b|\bsince\b|\bago\b.*\?(?=\n?$)/,
  earliest: /\b(first|earliest|initially|originally|at first|start(ed)? with|began)\b/,
  latest: /\b(last|latest|most recent(ly)?|recently|newest|final|lately)\b/,
  lastX: /\blast (week|month|year|weekend|night|time|summer|winter|spring|fall|autumn|friday|saturday|sunday|monday|tuesday|wednesday|thursday)\b/,
  current: /\b(now|currently|current|still|these days|at the moment|anymore|any more|today|nowadays|present|so far)\b/,
  ago: /\b(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|couple|few|several)\s+(of\s+)?(days?|weeks?|months?|years?)\s+ago\b/,
  past: /\b(last|past|previous)\s+(week|month|year|weekend|few days|couple of days)\b/,
  near: /\b(yesterday|today|tonight|this (morning|week|month|year))\b/,
  dmy: new RegExp(String.raw`\b(\d{1,2})(st|nd|rd|th)?\s+(of\s+)?(` + MONTH_ALT + String.raw`)\.?,?\s*(\d{4})?\b`),
  mdy: new RegExp(String.raw`\b(` + MONTH_ALT + String.raw`)\.?\s+(\d{1,2})(st|nd|rd|th)?,?\s*(\d{4})?\b`),
  my: new RegExp(String.raw`\b(in|during|of|around|since|before|after|by)?\s*(` + MONTH_ALT + String.raw`)\.?,?\s+(\d{4})\b`),
  inMonth: new RegExp(String.raw`\b(in|during|of|around|last)\s+(` + MONTH_LONG_ALT + String.raw`)\b`),
  year: /\b(in|during|of|since)\s+((19|20)\d{2})\b/,
};

export function parseQuery(q: string, now: Micros): TimeIntent {
  const t: TimeIntent = { window: null, order: null, current: false, when: false, count: false, span: false,
    matched: [] };
  const ql = q.toLowerCase();
  if (RX.when.test(ql)) t.when = true;
  if (RX.count.test(ql)) t.count = true;
  if (RX.span.test(ql)) t.span = true;
  if (RX.earliest.test(ql)) t.order = "earliest";
  if (RX.latest.test(ql) && !RX.lastX.test(ql)) t.order = "latest";
  if (RX.current.test(ql)) {
    t.current = true;
    t.order = t.order ?? "latest";
  }
  let m = RX.ago.exec(ql);
  if (m) {
    const n = isDigits(m[1]!) ? parseInt(m[1]!, 10) : (NUMWORDS[m[1]!] ?? 1);
    const d = n * UNITS[m[3]!]!;
    const c = addDays(now, -d);
    const pad = Math.max(3, Math.floor(d / 3));
    t.window = [addDays(c, -pad), addDays(c, pad)];
    t.matched.push(m[0]);
  }
  m = RX.past.exec(ql);
  if (m && !t.window) {
    const span = ({ week: 7, weekend: 7, month: 31, year: 366, "few days": 5, "couple of days": 3 } as Record<string, number>)[m[2]!]!;
    t.window = [addDays(now, -(m[2] !== "year" ? span * 2 : span)), now];
    t.matched.push(m[0]);
  }
  m = RX.near.exec(ql);
  if (m && !t.window) {
    const span = ({ yesterday: 2, today: 1, tonight: 1 } as Record<string, number>)[m[1]!] ?? 31;
    t.window = [addDays(now, -span), addDays(now, 1)];
  }
  m = RX.dmy.exec(ql) ?? RX.mdy.exec(ql);
  if (m && !t.window) {
    const g = m.slice(1);
    const nowY = parts(now).y;
    let d: number, mon: number, y: number;
    if (isDigits(g[0]!)) {
      d = parseInt(g[0]!, 10); mon = MONTHS.get(g[3]!)!; y = g[4] ? parseInt(g[4], 10) : nowY;
    } else {
      mon = MONTHS.get(g[0]!)!; d = parseInt(g[1]!, 10); y = g[3] ? parseInt(g[3], 10) : nowY;
    }
    if (isValidDate(y, mon, d)) {
      const c = fromParts(y, mon, d);
      t.window = [addDays(c, -3), addDays(c, 4)];
      t.matched.push(m[0]);
    }
  }
  if (!t.window) {
    m = RX.my.exec(ql);
    if (m) {
      const [s, e] = monthWindow(parseInt(m[3]!, 10), MONTHS.get(m[2]!)!);
      t.window = [addDays(s, -7), addDays(e, 7)];
      t.matched.push(m[0]);
    }
  }
  if (!t.window) {
    m = RX.inMonth.exec(ql);
    if (m) {
      const mon = MONTHS.get(m[2]!)!;
      const p = parts(now);
      const y = mon <= p.mo ? p.y : p.y - 1;
      const [s, e] = monthWindow(y, mon);
      t.window = [addDays(s, -7), addDays(e, 7)];
      t.matched.push(m[0]);
    }
  }
  if (!t.window) {
    m = RX.year.exec(ql);
    if (m) {
      const y = parseInt(m[2]!, 10);
      t.window = [addDays(fromParts(y, 1, 1), -15), addDays(fromParts(y + 1, 1, 1), 15)];
      t.matched.push(m[0]);
    }
  }
  return t;
}

// ---------------------------------------------------------------- write side
type Gran = "day" | "week" | "month" | "year" | "auto";
const REL: Array<[RegExp, (ts: Micros, m: RegExpExecArray) => Micros, Gran]> = [
  [/\byesterday\b/gi, (ts) => addDays(ts, -1), "day"],
  [/\b(today|tonight|this morning|this afternoon|this evening)\b/gi, (ts) => ts, "day"],
  [/\btomorrow\b/gi, (ts) => addDays(ts, 1), "day"],
  [/\blast (week|weekend)\b/gi, (ts) => addDays(ts, -7), "week"],
  [/\blast month\b/gi, (ts) => addDays(replace(ts, { d: 1 }), -1), "month"],
  [/\blast year\b/gi, (ts) => replace(ts, { y: parts(ts).y - 1 }), "year"],
  [/\bnext (week|weekend)\b/gi, (ts) => addDays(ts, 7), "week"],
  [/\bnext month\b/gi, (ts) => addDays(replace(ts, { d: 28 }), 5), "month"],
  [/\b(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|couple|few)\s+(of\s+)?(days?|weeks?|months?|years?)\s+ago\b/gi,
    (ts, m) => addDays(ts, -((isDigits(m[1]!) ? parseInt(m[1]!, 10) : (NUMWORDS[m[1]!.toLowerCase()] ?? 1)) *
      UNITS[m[3]!.toLowerCase()]!)), "auto"],
  [/\blast (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b/gi,
    (ts, m) => addDays(ts, -((((parts(ts).wd - WEEKDAYS[m[1]!.toLowerCase()]!) % 7) + 7) % 7 || 7)), "day"],
];

/** [(expression, absolute rendering)] for the relative time expressions in a text. */
export function resolveRelative(text: string, ts: Micros): Array<[string, string]> {
  const out: Array<[string, string]> = [];
  for (const [rx, fn, gran] of REL) {
    rx.lastIndex = 0;
    for (const m of text.matchAll(rx)) {
      let d: Micros;
      try {
        d = fn(ts, m as RegExpExecArray);
      } catch {
        continue;
      }
      let g = gran;
      if (g === "auto") {
        const s = m[0].toLowerCase();
        g = s.includes("year") ? "year" : s.includes("month") ? "month" : s.includes("week") ? "week" : "day";
      }
      const r = g === "day" ? strftime(d, "%-d %B %Y") : g === "week" ? "week of " + strftime(d, "%-d %B %Y")
        : g === "month" ? strftime(d, "%B %Y") : strftime(d, "%Y");
      out.push([m[0], r]);
    }
  }
  return out;
}
