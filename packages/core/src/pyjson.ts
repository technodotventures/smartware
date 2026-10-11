// json.dumps exactly as Python writes it (separators ", " and ": ", ensure_ascii, Python float repr).
// Used wherever bytes must match the oracle: de-duplication keys in the shared store and bundle hashes.

export interface DumpOptions {
  sortKeys?: boolean;
  /** Object keys whose numeric values are Python floats (rendered "1.0", not "1"). */
  floatKeys?: ReadonlySet<string>;
}

function pyFloat(x: number): string {
  if (Number.isNaN(x)) return "NaN";
  if (!Number.isFinite(x)) return x > 0 ? "Infinity" : "-Infinity";
  if (x === 0) return Object.is(x, -0) ? "-0.0" : "0.0";
  const [mant, expS] = x.toExponential().split("e") as [string, string];
  const exp = parseInt(expS, 10);
  const neg = mant.startsWith("-");
  const digits = mant.replace("-", "").replace(".", "");
  if (exp >= -5 && exp < 16) {
    // Python repr uses fixed notation for 1e-4 <= |x| < 1e16
    if (exp < -4) return sci();
    let s: string;
    if (exp >= 0) {
      s = digits.length > exp + 1 ? digits.slice(0, exp + 1) + "." + digits.slice(exp + 1)
        : digits + "0".repeat(exp + 1 - digits.length) + ".0";
    } else {
      s = "0." + "0".repeat(-exp - 1) + digits;
    }
    return (neg ? "-" : "") + s;
  }
  return sci();

  function sci(): string {
    const m = digits.length > 1 ? digits[0] + "." + digits.slice(1) : digits;
    const e = Math.abs(exp) < 10 ? "0" + Math.abs(exp) : String(Math.abs(exp));
    return (neg ? "-" : "") + m + "e" + (exp < 0 ? "-" : "+") + e;
  }
}

function pyStr(s: string): string {
  let out = '"';
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    const ch = s[i]!;
    if (ch === '"') out += '\\"';
    else if (ch === "\\") out += "\\\\";
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (ch === "\b") out += "\\b";
    else if (ch === "\f") out += "\\f";
    else if (c < 0x20 || c > 0x7e) out += "\\u" + c.toString(16).padStart(4, "0"); // UTF-16 units, as Python
    else out += ch;
  }
  return out + '"';
}

function pyKeyCmp(a: string, b: string): number {
  // Python sorts str keys by code point
  const A = [...a], B = [...b];
  for (let i = 0; i < Math.min(A.length, B.length); i++) {
    const x = A[i]!.codePointAt(0)!, y = B[i]!.codePointAt(0)!;
    if (x !== y) return x - y;
  }
  return A.length - B.length;
}

export function pyDumps(v: unknown, opt: DumpOptions = {}, asFloat = false): string {
  if (v === null || v === undefined) return "null";
  if (v === true) return "true";
  if (v === false) return "false";
  if (typeof v === "number") return asFloat || !Number.isInteger(v) ? pyFloat(v) : String(v);
  if (typeof v === "bigint") return v.toString();
  if (typeof v === "string") return pyStr(v);
  if (Array.isArray(v)) return "[" + v.map((x) => pyDumps(x, opt)).join(", ") + "]";
  if (v instanceof Set) return pyDumps([...v], opt);
  if (typeof v === "object") {
    let keys = Object.keys(v as object).filter((k) => (v as any)[k] !== undefined);
    if (opt.sortKeys) keys = keys.sort(pyKeyCmp);
    return "{" + keys.map((k) => pyStr(k) + ": " + pyDumps((v as any)[k], opt, opt.floatKeys?.has(k) ?? false))
      .join(", ") + "}";
  }
  return pyStr(String(v));
}
