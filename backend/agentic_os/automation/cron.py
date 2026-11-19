"""Minimal 5-field cron (minute hour day-of-month month day-of-week), local time.

Supports `*`, numbers, ranges `a-b`, steps `*/n` and `a-b/n`, and lists `a,b,c`.
Day-of-week: 0 or 7 = Sunday. As in classic cron, if both day fields are
restricted, a day matches when either matches.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Set, Tuple

FIELDS: List[Tuple[str, int, int]] = [("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12),
                                      ("weekday", 0, 7)]
ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *"}


class CronError(ValueError):
    pass


def _field(text: str, lo: int, hi: int, name: str) -> Set[int]:
    out: Set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                raise CronError(f"bad step in {name}: {s!r}")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            if not (x.isdigit() and y.isdigit()):
                raise CronError(f"bad range in {name}: {part!r}")
            a, b = int(x), int(y)
        elif part.isdigit():
            a = b = int(part)
            if step > 1:
                b = hi
        else:
            raise CronError(f"bad value in {name}: {part!r}")
        if a < lo or b > hi or a > b:
            raise CronError(f"{name} must be within {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return out


class Cron:
    def __init__(self, expr: str):
        expr = ALIASES.get(expr.strip(), expr.strip())
        parts = expr.split()
        if len(parts) != 5:
            raise CronError("use five fields: minute hour day month weekday (for example '30 9 * * 1-5')")
        self.expr = expr
        sets = [_field(p, lo, hi, n) for p, (n, lo, hi) in zip(parts, FIELDS)]
        self.minutes, self.hours, self.days, self.months, wd = sets
        self.weekdays = {0 if d == 7 else d for d in wd}
        self.day_restricted = parts[2] != "*"
        self.wd_restricted = parts[4] != "*"

    def _day_ok(self, d: datetime) -> bool:
        if d.month not in self.months:
            return False
        dom = d.day in self.days
        dow = ((d.weekday() + 1) % 7) in self.weekdays      # Python Monday=0 -> cron Monday=1
        if self.day_restricted and self.wd_restricted:
            return dom or dow
        if self.day_restricted:
            return dom
        if self.wd_restricted:
            return dow
        return True

    def next_after(self, after: datetime) -> datetime:
        t = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
        day = t.replace(hour=0, minute=0)
        for _ in range(366 * 5):
            if self._day_ok(day):
                for h in sorted(self.hours):
                    for m in sorted(self.minutes):
                        cand = day.replace(hour=h, minute=m)
                        if cand >= t:
                            return cand
            day = (day + timedelta(days=1)).replace(hour=0, minute=0)
        raise CronError("this schedule never runs")


def describe(expr: str) -> str:
    """Short plain-language description for common patterns."""
    c = Cron(expr)
    p = c.expr.split()
    if p[2:] == ["*", "*", "*"] and p[0].isdigit() and p[1].isdigit():
        return f"Every day at {int(p[1]):02d}:{int(p[0]):02d}"
    if p[2:4] == ["*", "*"] and p[4] == "1-5" and p[0].isdigit() and p[1].isdigit():
        return f"Weekdays at {int(p[1]):02d}:{int(p[0]):02d}"
    if p[1:] == ["*", "*", "*", "*"] and p[0].isdigit():
        return f"Every hour at :{int(p[0]):02d}"
    if p[0].startswith("*/") and p[1:] == ["*", "*", "*", "*"]:
        return f"Every {p[0][2:]} minutes"
    return f"Cron: {c.expr}"
