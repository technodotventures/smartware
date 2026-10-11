// Time as integer microseconds since 1970-01-01, UTC, with no timezone attached (Python's naive UTC datetimes).
// Microseconds keep ordering identical to the oracle, which compares datetimes at microsecond precision.

export type Micros = number;

export const US = 1_000_000;
export const DAY = 86_400 * US;
export const MIN_TIME: Micros = -62135596800 * US; // datetime.min (0001-01-01)
const EPOCH_DATE = Date.UTC(1970, 0, 1);

export function nowMicros(): Micros {
  return Date.now() * 1000;
}

export function fromParts(y: number, mo: number, d: number, h = 0, mi = 0, s = 0, us = 0): Micros {
  const dt = new Date(0);
  dt.setUTCFullYear(y, mo - 1, d);
  dt.setUTCHours(h, mi, s, 0);
  return dt.getTime() * 1000 + us;
}

export function isValidDate(y: number, mo: number, d: number): boolean {
  if (y < 1 || y > 9999 || mo < 1 || mo > 12 || d < 1) return false;
  return d <= daysInMonth(y, mo);
}

export function daysInMonth(y: number, mo: number): number {
  return [31, (y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0)) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo - 1]!;
}

export interface Parts { y: number; mo: number; d: number; h: number; mi: number; s: number; us: number; wd: number }

export function parts(t: Micros): Parts {
  const sec = Math.floor(t / US);
  const us = t - sec * US;
  const dt = new Date(sec * 1000);
  return {
    y: dt.getUTCFullYear(), mo: dt.getUTCMonth() + 1, d: dt.getUTCDate(), h: dt.getUTCHours(),
    mi: dt.getUTCMinutes(), s: dt.getUTCSeconds(), us, wd: (dt.getUTCDay() + 6) % 7, // Monday = 0, as Python
  };
}

const ISO = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2})(?::(\d{2})(?::(\d{2})(?:[.,](\d{1,6})\d*)?)?)?)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$/;

/** Parse an ISO 8601 timestamp. Offsets are converted to UTC (never just dropped). Throws on bad input. */
export function parseTime(v: string): Micros {
  const m = ISO.exec(String(v).trim());
  if (!m) throw new RangeError(`bad timestamp ${JSON.stringify(v)}`);
  const y = +m[1]!, mo = +m[2]!, d = +m[3]!, h = +(m[4] ?? 0), mi = +(m[5] ?? 0), s = +(m[6] ?? 0);
  const us = m[7] ? +(m[7] + "000000").slice(0, 6) : 0;
  if (!isValidDate(y, mo, d) || h > 23 || mi > 59 || s > 59) throw new RangeError(`bad timestamp ${JSON.stringify(v)}`);
  let t = fromParts(y, mo, d, h, mi, s, us);
  const off = m[8];
  if (off && off !== "Z") {
    const sign = off[0] === "-" ? -1 : 1;
    const digits = off.slice(1).replace(":", "");
    const oh = +digits.slice(0, 2), om = +(digits.slice(2, 4) || 0);
    t -= sign * (oh * 3600 + om * 60) * US;
  }
  return t;
}

const pad = (n: number, w = 2) => String(n).padStart(w, "0");

/** Python datetime.isoformat() for a naive datetime: microseconds only when non-zero. */
export function isoformat(t: Micros): string {
  const p = parts(t);
  const base = `${pad(p.y, 4)}-${pad(p.mo)}-${pad(p.d)}T${pad(p.h)}:${pad(p.mi)}:${pad(p.s)}`;
  return p.us ? `${base}.${pad(p.us, 6)}` : base;
}

export function isoDate(t: Micros): string {
  const p = parts(t);
  return `${pad(p.y, 4)}-${pad(p.mo)}-${pad(p.d)}`;
}

export const MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
  "October", "November", "December"];
const DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** The strftime forms the engine uses: %-d, %d, %B, %b, %Y, %m, %a. */
export function strftime(t: Micros, fmt: string): string {
  const p = parts(t);
  return fmt.replace(/%(-?)([dBbYma%])/g, (_, dash: string, c: string) => {
    switch (c) {
      case "d": return dash ? String(p.d) : pad(p.d);
      case "B": return MONTH_NAMES[p.mo - 1]!;
      case "b": return MONTH_NAMES[p.mo - 1]!.slice(0, 3);
      case "Y": return pad(p.y, 4);
      case "m": return pad(p.mo);
      case "a": return DAY_ABBR[p.wd]!;
      default: return "%";
    }
  });
}

/** Seconds since the epoch, as Python (d - datetime(1970, 1, 1)).total_seconds(). */
export function epochSeconds(t: Micros): number {
  return t / US;
}

export function addDays(t: Micros, days: number): Micros {
  return t + days * DAY;
}

/** datetime.replace(day=...) etc. Throws RangeError when the result is not a real date, as Python does. */
export function replace(t: Micros, f: { y?: number; mo?: number; d?: number }): Micros {
  const p = parts(t);
  const y = f.y ?? p.y, mo = f.mo ?? p.mo, d = f.d ?? p.d;
  if (!isValidDate(y, mo, d)) throw new RangeError("day is out of range for month");
  return fromParts(y, mo, d, p.h, p.mi, p.s, p.us);
}

export function dateOnlyEpochDays(t: Micros): number {
  return Math.floor((t / 1000 - EPOCH_DATE) / 86_400_000);
}
