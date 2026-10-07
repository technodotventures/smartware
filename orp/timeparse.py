"""Deterministic temporal expression handling.

Two jobs:
1. Query side: turn "last month", "in May 2023", "two weeks ago", "first", "currently"
   into a time window and an ordering intent, relative to the query's as-of time.
2. Write side: resolve relative expressions inside a memory ("yesterday",
   "last Friday") against the memory's observed time, so recall can attach an
   absolute date and the temporal index can place the event correctly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9
WEEKDAYS = {d: i for i, d in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}
NUMWORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "couple": 2, "few": 3, "several": 3}
UNITS = {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30, "year": 365, "years": 365,
         "weekend": 7, "weekends": 7}


@dataclass
class TimeIntent:
    window: tuple[datetime, datetime] | None = None
    order: str | None = None  # "earliest" | "latest"
    current: bool = False  # asks about present state ("now", "currently", "still")
    when: bool = False  # asks for a date/time
    count: bool = False  # "how many", "how often", "list all"
    span: bool = False  # "how long", "how many days between"
    matched: list[str] = field(default_factory=list)

    @property
    def temporal(self) -> bool:
        return bool(self.window or self.order or self.when or self.span)


def _month_window(y: int, m: int) -> tuple[datetime, datetime]:
    s = datetime(y, m, 1)
    e = datetime(y + (m == 12), m % 12 + 1, 1)
    return s, e


def parse_query(q: str, now: datetime) -> TimeIntent:
    t = TimeIntent()
    ql = q.lower()
    if re.search(r"\bwhen\b|\bwhat (date|day|time|year|month)\b|\bwhich (date|day|year|month)\b", ql):
        t.when = True
    if re.search(r"\bhow (many|often|much)\b|\blist\b|\ball the\b|\btotal\b|\bin total\b", ql):
        t.count = True
    if re.search(r"\bhow long\b|\bhow many (days|weeks|months|years|hours|minutes)\b|\bbetween\b|\bsince\b|\bago\b.*\?$",
                 ql):
        t.span = True
    if re.search(r"\b(first|earliest|initially|originally|at first|start(ed)? with|began)\b", ql):
        t.order = "earliest"
    if re.search(r"\b(last|latest|most recent(ly)?|recently|newest|final|lately)\b", ql) and not re.search(
            r"\blast (week|month|year|weekend|night|time|summer|winter|spring|fall|autumn|friday|saturday|sunday|"
            r"monday|tuesday|wednesday|thursday)\b", ql):
        t.order = "latest"
    if re.search(r"\b(now|currently|current|still|these days|at the moment|anymore|any more|today|nowadays|"
                 r"present|so far)\b", ql):
        t.current = True
        t.order = t.order or "latest"
    # explicit windows
    m = re.search(r"\b(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|couple|few|several)\s+(of\s+)?"
                  r"(days?|weeks?|months?|years?)\s+ago\b", ql)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else NUMWORDS.get(m.group(1), 1)
        d = n * UNITS[m.group(3)]
        c = now - timedelta(days=d)
        pad = max(3, d // 3)
        t.window = (c - timedelta(days=pad), c + timedelta(days=pad))
        t.matched.append(m.group(0))
    m = re.search(r"\b(last|past|previous)\s+(week|month|year|weekend|few days|couple of days)\b", ql)
    if m and not t.window:
        span = {"week": 7, "weekend": 7, "month": 31, "year": 366, "few days": 5, "couple of days": 3}[m.group(2)]
        t.window = (now - timedelta(days=span * 2 if m.group(2) != "year" else span), now)
        t.matched.append(m.group(0))
    m = re.search(r"\b(yesterday|today|tonight|this (morning|week|month|year))\b", ql)
    if m and not t.window:
        span = {"yesterday": 2, "today": 1, "tonight": 1}.get(m.group(1), 31)
        t.window = (now - timedelta(days=span), now + timedelta(days=1))
    # absolute: "8 May 2023", "May 8, 2023", "May 2023", "in May", "2023"
    m = re.search(r"\b(\d{1,2})(st|nd|rd|th)?\s+(of\s+)?(" + "|".join(MONTHS) + r")\.?,?\s*(\d{4})?\b", ql) or \
        re.search(r"\b(" + "|".join(MONTHS) + r")\.?\s+(\d{1,2})(st|nd|rd|th)?,?\s*(\d{4})?\b", ql)
    if m and not t.window:
        g = m.groups()
        if g[0].isdigit():
            d, mon, y = int(g[0]), MONTHS[g[3]], int(g[4]) if g[4] else now.year
        else:
            mon, d, y = MONTHS[g[0]], int(g[1]), int(g[3]) if g[3] else now.year
        try:
            c = datetime(y, mon, d)
            t.window = (c - timedelta(days=3), c + timedelta(days=4))
            t.matched.append(m.group(0))
        except ValueError:
            pass
    if not t.window:
        m = re.search(r"\b(in|during|of|around|since|before|after|by)?\s*(" + "|".join(MONTHS) + r")\.?,?\s+(\d{4})\b",
                      ql)
        if m:
            s, e = _month_window(int(m.group(3)), MONTHS[m.group(2)])
            t.window = (s - timedelta(days=7), e + timedelta(days=7))
            t.matched.append(m.group(0))
    if not t.window:
        m = re.search(r"\b(in|during|of|around|last)\s+(" + "|".join(k for k in MONTHS if len(k) > 3) + r")\b", ql)
        if m:
            mon = MONTHS[m.group(2)]
            y = now.year if mon <= now.month else now.year - 1
            s, e = _month_window(y, mon)
            t.window = (s - timedelta(days=7), e + timedelta(days=7))
            t.matched.append(m.group(0))
    if not t.window:
        m = re.search(r"\b(in|during|of|since)\s+((19|20)\d{2})\b", ql)
        if m:
            y = int(m.group(2))
            t.window = (datetime(y, 1, 1) - timedelta(days=15), datetime(y + 1, 1, 1) + timedelta(days=15))
            t.matched.append(m.group(0))
    return t


# ---------------------------------------------------------------- write side
_REL = [
    (re.compile(r"\byesterday\b", re.I), lambda ts, m: ts - timedelta(days=1), "day"),
    (re.compile(r"\b(today|tonight|this morning|this afternoon|this evening)\b", re.I), lambda ts, m: ts, "day"),
    (re.compile(r"\btomorrow\b", re.I), lambda ts, m: ts + timedelta(days=1), "day"),
    (re.compile(r"\blast (week|weekend)\b", re.I), lambda ts, m: ts - timedelta(days=7), "week"),
    (re.compile(r"\blast month\b", re.I), lambda ts, m: (ts.replace(day=1) - timedelta(days=1)), "month"),
    (re.compile(r"\blast year\b", re.I), lambda ts, m: ts.replace(year=ts.year - 1), "year"),
    (re.compile(r"\bnext (week|weekend)\b", re.I), lambda ts, m: ts + timedelta(days=7), "week"),
    (re.compile(r"\bnext month\b", re.I), lambda ts, m: (ts.replace(day=28) + timedelta(days=5)), "month"),
    (re.compile(r"\b(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|couple|few)\s+(of\s+)?"
                r"(days?|weeks?|months?|years?)\s+ago\b", re.I),
     lambda ts, m: ts - timedelta(days=(int(m.group(1)) if m.group(1).isdigit() else NUMWORDS.get(m.group(1).lower(), 1))
                                   * UNITS[m.group(3).lower()]), "auto"),
    (re.compile(r"\blast (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I),
     lambda ts, m: ts - timedelta(days=((ts.weekday() - WEEKDAYS[m.group(1).lower()]) % 7) or 7), "day"),
]


def resolve_relative(text: str, ts: datetime) -> list[tuple[str, str]]:
    """Return [(expression, absolute rendering)] for relative time expressions in text."""
    out = []
    for rx, fn, gran in _REL:
        for m in rx.finditer(text):
            try:
                d = fn(ts, m)
            except Exception:
                continue
            g = gran
            if g == "auto":
                g = "year" if "year" in m.group(0).lower() else "month" if "month" in m.group(0).lower() else "week" \
                    if "week" in m.group(0).lower() else "day"
            r = {"day": d.strftime("%-d %B %Y"), "week": "week of " + d.strftime("%-d %B %Y"),
                 "month": d.strftime("%B %Y"), "year": d.strftime("%Y")}[g]
            out.append((m.group(0), r))
    return out
