"""Findings: herds, DST traps, dead schedules, cron gotchas."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .cron import Cron, CronError
from .sources import Job

SEVERITY = {"low": 1, "medium": 2, "high": 3}
UTC = timezone.utc


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    where: str
    jobs: list

    def to_dict(self):
        return asdict(self)


def _zone(name: str | None):
    if not name or name.upper() in {"UTC", "ETC/UTC", "GMT"}:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _nonexistent(wall: datetime, z) -> bool:
    aware = wall.replace(tzinfo=z)
    back = aware.astimezone(UTC).astimezone(z).replace(tzinfo=None)
    return back != wall


def _ambiguous(wall: datetime, z) -> bool:
    return wall.replace(tzinfo=z, fold=0).utcoffset() != wall.replace(tzinfo=z, fold=1).utcoffset()


def _to_utc(wall: datetime, z) -> datetime:
    return wall.replace(tzinfo=z).astimezone(UTC).replace(tzinfo=None)


def analyze(jobs: list[Job], start: datetime, days: int = 7, herd: int = 3,
            default_tz: str = "UTC") -> tuple[list[Finding], dict]:
    findings: list[Finding] = []
    parsed: list[tuple[Job, Cron, object]] = []

    for j in jobs:
        tzname = j.tz or (default_tz if j.kind in {"crontab", "text"} else None)
        try:
            c = Cron.parse(j.expr)
        except CronError as e:
            findings.append(Finding("parse-error", "high", f"cannot parse {j.expr!r}: {e}", j.where, [j.name]))
            continue
        z = _zone(tzname) if tzname else UTC
        if z is None:
            findings.append(Finding("unknown-timezone", "high", f"timezone {tzname!r} is not a known IANA zone", j.where, [j.name]))
            z = UTC
        parsed.append((j, c, z))

        # never fires: scan 4 years of days so Feb 29 counts
        d0 = start.date()
        if not any(c.day_matches(d0 + timedelta(days=k)) for k in range(366 * 4 + 1)):
            findings.append(Finding("never-fires", "high",
                                    f"{j.expr!r} matches no calendar day in 4 years; it will never run", j.where, [j.name]))
            continue

        if c.dom_dow_or:
            findings.append(Finding("dom-dow-or", "medium",
                                    f"{j.expr!r} restricts both day-of-month and day-of-week; cron ORs them, "
                                    f"so it runs on EITHER condition, not only when both hold", j.where, [j.name]))

        gap = c.min_gap_minutes()
        if j.duration_min and j.duration_min > gap:
            findings.append(Finding("self-overlap", "medium",
                                    f"declared duration {j.duration_min}m exceeds the {gap}m gap between runs; "
                                    f"runs will stack unless the job is locked", j.where, [j.name]))

        if j.kind == "github":
            if gap < 5:
                findings.append(Finding("gha-too-frequent", "low",
                                        f"{j.expr!r} fires every {gap}m but GitHub Actions schedules run at most every 5 minutes",
                                        j.where, [j.name]))
            if 0 in c.minutes:
                findings.append(Finding("gha-top-of-hour", "low",
                                        "fires at minute :00, when GitHub documents the heaviest scheduled-run load "
                                        "and delays or drops; pick an odd minute", j.where, [j.name]))
        if j.kind == "k8s" and not j.tz:
            findings.append(Finding("k8s-implicit-timezone", "low",
                                    "CronJob has no spec.timeZone, so it follows kube-controller-manager's local zone",
                                    j.where, [j.name]))

        # DST traps: only meaningful for schedules sparser than hourly
        if z is not UTC and gap > 60:
            skipped, doubled = [], []
            for wall in c.iter_wall(start, start + timedelta(days=366)):
                if _nonexistent(wall, z):
                    skipped.append(wall)
                elif _ambiguous(wall, z):
                    doubled.append(wall)
            if skipped:
                findings.append(Finding("dst-skip", "high",
                                        f"in {z} the wall time {skipped[0]:%Y-%m-%d %H:%M} does not exist (spring forward); "
                                        f"{len(skipped)} run(s) in the next year will be skipped or shifted depending on the daemon",
                                        j.where, [j.name]))
            if doubled:
                findings.append(Finding("dst-repeat", "medium",
                                        f"in {z} the wall time {doubled[0]:%Y-%m-%d %H:%M} happens twice (fall back); "
                                        f"{len(doubled)} run(s) in the next year may execute twice",
                                        j.where, [j.name]))

    # Herd detection on the shared UTC timeline
    slots: dict[datetime, list[Job]] = defaultdict(list)
    minute_load: Counter = Counter()
    end = start + timedelta(days=days)
    for j, c, z in parsed:
        if c.min_gap_minutes() < 5:
            continue  # near-continuous jobs collide with everything; not a herd signal
        for wall in c.iter_wall(start - timedelta(days=1), end + timedelta(days=1)):
            if z is not UTC and _nonexistent(wall, z):
                continue
            u = _to_utc(wall, z) if z is not UTC else wall
            if start <= u < end:
                slots[u].append(j)
                minute_load[u.minute] += 1

    groups: dict[tuple, list[datetime]] = defaultdict(list)
    for t, js in slots.items():
        if len(js) >= herd:
            key = tuple(sorted({f"{x.name} ({x.where})" for x in js}))
            groups[key].append(t)
    for key, times in sorted(groups.items(), key=lambda kv: (-len(kv[0]), min(kv[1]))):
        times.sort()
        findings.append(Finding("herd", "medium",
                                f"{len(key)} jobs fire in the same UTC minute {len(times)}x in {days}d, "
                                f"first at {times[0]:%Y-%m-%d %H:%M} UTC; stagger their minutes", "", list(key)))

    stats = {
        "jobs": len(jobs),
        "parsed": len(parsed),
        "window_start_utc": start.isoformat(),
        "window_days": days,
        "busiest_minutes": minute_load.most_common(5),
        "peak_slot_size": max((len(v) for v in slots.values()), default=0),
    }
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order[f.severity], f.rule, f.where))
    return findings, stats
