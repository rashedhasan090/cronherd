"""Minimal, dependency-free 5-field cron parser and wall-clock simulator."""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
DAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


class CronError(ValueError):
    pass


def _parse_field(text: str, lo: int, hi: int, names: dict | None = None) -> set[int]:
    out: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"empty list item in {text!r}")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) == 0:
                raise CronError(f"bad step {step_s!r}")
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _atom(a, names), _atom(b, names)
        else:
            start = _atom(part, names)
            end = hi if step != 1 else start
        if start < lo or end > hi or start > end:
            raise CronError(f"value out of range in {text!r} (allowed {lo}-{hi})")
        out.update(range(start, end + 1, step))
    return out


def _atom(tok: str, names: dict | None) -> int:
    t = tok.lower()
    if names and t in names:
        return names[t]
    if not t.isdigit():
        raise CronError(f"bad token {tok!r}")
    return int(t)


@dataclass
class Cron:
    expr: str
    minutes: set[int]
    hours: set[int]
    doms: set[int]
    months: set[int]
    dows: set[int]
    dom_star: bool
    dow_star: bool

    @classmethod
    def parse(cls, expr: str) -> "Cron":
        raw = expr.strip()
        norm = MACROS.get(raw.lower(), raw)
        if norm.startswith("@"):
            raise CronError(f"unsupported macro {raw!r}")
        fields = norm.split()
        if len(fields) != 5:
            raise CronError(f"expected 5 fields, got {len(fields)} in {raw!r}")
        m, h, dom, mon, dow = fields
        dows = _parse_field(dow, 0, 7, DAYS)
        if 7 in dows:
            dows.discard(7)
            dows.add(0)
        return cls(
            expr=raw,
            minutes=_parse_field(m, 0, 59),
            hours=_parse_field(h, 0, 23),
            doms=_parse_field(dom, 1, 31),
            months=_parse_field(mon, 1, 12, MONTHS),
            dows=dows,
            dom_star=dom.startswith("*"),
            dow_star=dow.startswith("*"),
        )

    @property
    def dom_dow_or(self) -> bool:
        """True when both day fields are restricted, so cron ORs them (classic gotcha)."""
        return not self.dom_star and not self.dow_star

    def day_matches(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        cron_dow = (d.weekday() + 1) % 7  # Python Mon=0 -> cron Mon=1, Sun=0
        dom_ok = d.day in self.doms
        dow_ok = cron_dow in self.dows
        if self.dom_star and self.dow_star:
            return True
        if self.dom_star:
            return dow_ok
        if self.dow_star:
            return dom_ok
        return dom_ok or dow_ok

    def iter_wall(self, start: datetime, end: datetime):
        """Yield naive wall-clock datetimes in [start, end) that match."""
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)
        d = start.date()
        while d <= end.date():
            if self.day_matches(d):
                for h in hours:
                    for mi in minutes:
                        t = datetime(d.year, d.month, d.day, h, mi)
                        if start <= t < end:
                            yield t
            d += timedelta(days=1)

    def min_gap_minutes(self) -> int:
        """Smallest gap between consecutive firings within a single matching day."""
        times = sorted(h * 60 + m for h in self.hours for m in self.minutes)
        if len(times) < 2:
            return 24 * 60
        gaps = [b - a for a, b in zip(times, times[1:])]
        gaps.append(times[0] + 1440 - times[-1])
        return min(gaps)
