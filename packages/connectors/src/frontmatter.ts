// A small, safe frontmatter reader and writer. It understands only what Markdown vaults commonly use:
//   key: value
//   key: "quoted value"
//   key: [a, b, "c, d"]
//   key:            (followed by "  - item" lines: a list of strings)
// between a leading "---" line and the next "---" line. Anything else (nested maps, anchors, tags, multi-line
// strings) is ignored, never interpreted. No YAML library, no evaluation: quoted strings are read with
// JSON.parse, which only builds data.

export type FrontValue = string | number | boolean | null | string[];
export type Frontmatter = Record<string, FrontValue>;

export interface ParsedNote {
  frontmatter: Frontmatter;
  body: string;
  /** True when the file started with a frontmatter block. */
  hadFrontmatter: boolean;
}

const KEY = /^([A-Za-z_][A-Za-z0-9_.-]*)\s*:(?:\s+(.*))?$/;

function unquote(s: string): string {
  const t = s.trim();
  if (t.length >= 2 && t.startsWith('"') && t.endsWith('"')) {
    try { return String(JSON.parse(t)); } catch { return t.slice(1, -1); }
  }
  if (t.length >= 2 && t.startsWith("'") && t.endsWith("'")) return t.slice(1, -1).replace(/''/g, "'");
  return t;
}

/** Split "a, b, \"c, d\"" on commas outside quotes. */
function splitList(inner: string): string[] {
  const out: string[] = [];
  let cur = "", q: string | null = null;
  for (let i = 0; i < inner.length; i++) {
    const ch = inner[i]!;
    if (q) {
      cur += ch;
      if (ch === "\\" && q === '"' && i + 1 < inner.length) { cur += inner[++i]; continue; }
      if (ch === q) q = null;
    } else if (ch === '"' || ch === "'") { q = ch; cur += ch; }
    else if (ch === ",") { out.push(cur); cur = ""; }
    else cur += ch;
  }
  if (cur.trim() || out.length) out.push(cur);
  return out.map(unquote).filter((x) => x !== "");
}

export function parseValue(raw: string | undefined): FrontValue {
  if (raw === undefined) return null;
  let v = raw.trim();
  if (!v.startsWith('"') && !v.startsWith("'")) v = v.replace(/\s+#.*$/, ""); // trailing comment
  if (v === "" || v === "~" || v === "null") return null;
  if (v.startsWith("[") && v.endsWith("]")) return splitList(v.slice(1, -1));
  if (v === "true") return true;
  if (v === "false") return false;
  if (/^-?\d+(\.\d+)?$/.test(v) && v.length < 16) return Number(v);
  return unquote(v);
}

export function parseNote(text: string): ParsedNote {
  const src = text.replace(/^﻿/, "");
  const m = /^---[ \t]*\r?\n/.exec(src);
  if (!m) return { frontmatter: {}, body: src, hadFrontmatter: false };
  const rest = src.slice(m[0].length);
  const end = /(^|\r?\n)---[ \t]*(\r?\n|$)/.exec(rest);
  if (!end) return { frontmatter: {}, body: src, hadFrontmatter: false };
  const block = rest.slice(0, end.index);
  const body = rest.slice(end.index + end[0].length);
  const fm: Frontmatter = Object.create(null);
  const lines = block.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
    if (!line.trim() || line.trimStart().startsWith("#")) continue;
    const k = KEY.exec(line);
    if (!k) continue; // indented or unsupported syntax: ignored, never interpreted
    const key = k[1]!;
    if (key === "__proto__" || key === "constructor" || key === "prototype") continue;
    let val = parseValue(k[2]);
    // a block list ("key:" then "  - a" lines) is common enough to support as a list of strings
    if (val === null) {
      const items: string[] = [];
      while (i + 1 < lines.length && /^\s+-\s+/.test(lines[i + 1]!)) items.push(unquote(lines[++i]!.replace(/^\s+-\s+/, "")));
      if (items.length) val = items;
    }
    fm[key] = val;
  }
  return { frontmatter: { ...fm }, body, hadFrontmatter: true };
}

function quoteIfNeeded(s: string): string {
  if (s === "" || /^[\s]|[\s]$/.test(s) || /[:#,[\]{}"'\n\r]|^[-?!&*|>%@`]/.test(s) || /^(true|false|null|~|-?\d+(\.\d+)?)$/.test(s)) {
    return JSON.stringify(s);
  }
  return s;
}

export function formatValue(v: FrontValue): string {
  if (v === null) return "null";
  if (Array.isArray(v)) return "[" + v.map((x) => quoteIfNeeded(String(x))).join(", ") + "]";
  if (typeof v === "string") return quoteIfNeeded(v);
  return String(v);
}

/** Render frontmatter so that parseNote reads back the same values. */
export function formatFrontmatter(fm: Frontmatter): string {
  const keys = Object.keys(fm).filter((k) => KEY.test(`${k}: x`));
  if (!keys.length) return "";
  return "---\n" + keys.map((k) => `${k}: ${formatValue(fm[k]!)}`).join("\n") + "\n---\n";
}
