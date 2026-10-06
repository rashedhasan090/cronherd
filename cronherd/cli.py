"""Command line entry point."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import __version__
from .analyze import SEVERITY, analyze
from .sources import load_path, parse_crontab


def _bar(n: int, top: int, width: int = 30) -> str:
    return "#" * max(1, round(width * n / top)) if top else ""


def render_text(findings, stats) -> str:
    out = [f"cronherd: {stats['parsed']}/{stats['jobs']} schedules parsed, "
           f"{stats['window_days']}d window from {stats['window_start_utc']} UTC"]
    if stats["busiest_minutes"]:
        out.append("busiest minute-of-hour (UTC firings in window):")
        top = stats["busiest_minutes"][0][1]
        for minute, n in stats["busiest_minutes"]:
            out.append(f"  :{minute:02d} {n:5d} {_bar(n, top)}")
    if not findings:
        out.append("no findings")
    for f in findings:
        loc = f" [{f.where}]" if f.where else ""
        out.append(f"{f.severity.upper():6s} {f.rule}{loc}: {f.message}")
        if f.rule == "herd":
            for j in f.jobs:
                out.append(f"         - {j}")
    return "\n".join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cronherd",
                                description="Find thundering herds, DST traps, and dead schedules across "
                                            "crontabs, GitHub Actions workflows, and Kubernetes CronJobs.")
    p.add_argument("paths", nargs="*", help="files or directories (default: .)")
    p.add_argument("-e", "--expr", action="append", default=[], help="ad-hoc cron expression (repeatable)")
    p.add_argument("--tz", default="UTC", help="zone for crontabs without CRON_TZ (default UTC)")
    p.add_argument("--start", help="window start, ISO UTC (default: now, truncated to the minute)")
    p.add_argument("--days", type=int, default=7, help="herd window length in days (default 7)")
    p.add_argument("--herd", type=int, default=3, help="jobs per minute that count as a herd (default 3)")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument("--fail-on", choices=["low", "medium", "high", "none"], default="high",
                   help="exit 1 if any finding is at or above this severity (default high)")
    p.add_argument("--version", action="version", version=f"cronherd {__version__}")
    a = p.parse_args(argv)

    if a.start:
        start = datetime.fromisoformat(a.start.replace("Z", "+00:00"))
        if start.tzinfo:
            start = start.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        start = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)

    jobs = []
    for i, e in enumerate(a.expr, 1):
        jobs += [j for j in parse_crontab(e + " expr" + str(i), "<expr>")]
    for path in a.paths or ([] if a.expr else ["."]):
        jobs += load_path(path)

    findings, stats = analyze(jobs, start, days=a.days, herd=a.herd, default_tz=a.tz)
    if a.format == "json":
        print(json.dumps({"stats": stats, "findings": [f.to_dict() for f in findings]}, indent=2, default=str))
    else:
        print(render_text(findings, stats))

    if a.fail_on != "none":
        bar = SEVERITY[a.fail_on]
        if any(SEVERITY[f.severity] >= bar for f in findings):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
