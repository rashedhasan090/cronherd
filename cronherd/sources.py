"""Extract cron schedules from crontabs, GitHub Actions workflows, and Kubernetes CronJobs."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

DUR_RE = re.compile(r"duration\s*=\s*(\d+)\s*([smhSMH]?)")
GHA_CRON_RE = re.compile(r"^\s*-?\s*cron\s*:\s*['\"]?([^'\"#]+?)['\"]?\s*(#.*)?$")
K8S_SCHED_RE = re.compile(r"^\s*schedule\s*:\s*['\"]?([^'\"#]+?)['\"]?\s*(#.*)?$")
K8S_TZ_RE = re.compile(r"^\s*timeZone\s*:\s*['\"]?([^'\"#\s]+)")
K8S_NAME_RE = re.compile(r"^\s{2}name\s*:\s*['\"]?([^'\"#\s]+)")
CRON_TZ_RE = re.compile(r"^\s*(?:CRON_TZ|TZ)\s*=\s*(\S+)")


@dataclass
class Job:
    name: str
    expr: str
    source: str
    line: int
    kind: str  # crontab | github | k8s | text
    tz: str | None = None
    duration_min: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def where(self) -> str:
        return f"{self.source}:{self.line}"


def _duration(comment: str | None) -> int | None:
    if not comment:
        return None
    m = DUR_RE.search(comment)
    if not m:
        return None
    n, unit = int(m.group(1)), (m.group(2) or "m").lower()
    return max(1, {"s": n // 60, "m": n, "h": n * 60}[unit])


def detect_kind(path: str, text: str) -> str:
    p = path.replace("\\", "/")
    if "/.github/workflows/" in p or p.startswith(".github/workflows/"):
        return "github"
    if re.search(r"^\s*kind\s*:\s*CronJob", text, re.M):
        return "k8s"
    if p.endswith((".yml", ".yaml")) and re.search(r"^\s*-?\s*cron\s*:", text, re.M):
        return "github"
    return "crontab"


def parse_crontab(text: str, source: str) -> list[Job]:
    jobs: list[Job] = []
    tz = None
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = CRON_TZ_RE.match(s)
        if m:
            tz = m.group(1)
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=", s):
            continue  # other env assignments
        comment = None
        if " #" in s:
            s, comment = s.split(" #", 1)
        parts = s.split()
        if parts[0].startswith("@"):
            if parts[0].lower() == "@reboot":
                continue
            expr, cmd = parts[0], " ".join(parts[1:])
        else:
            if len(parts) < 6:
                expr, cmd = " ".join(parts[:5]), ""
            else:
                expr, cmd = " ".join(parts[:5]), " ".join(parts[5:])
        name = (cmd.split()[0] if cmd else expr)[:60]
        jobs.append(Job(name, expr, source, i, "crontab", tz, _duration(comment)))
    return jobs


def parse_github(text: str, source: str) -> list[Job]:
    jobs: list[Job] = []
    wf = os.path.splitext(os.path.basename(source))[0]
    m = re.search(r"^name\s*:\s*['\"]?(.+?)['\"]?\s*$", text, re.M)
    if m:
        wf = m.group(1)
    for i, line in enumerate(text.splitlines(), 1):
        mm = GHA_CRON_RE.match(line)
        if mm:
            jobs.append(Job(wf, mm.group(1).strip(), source, i, "github", "UTC", _duration(mm.group(2))))
    return jobs


def parse_k8s(text: str, source: str) -> list[Job]:
    jobs: list[Job] = []
    for doc in re.split(r"^---\s*$", text, flags=re.M):
        if not re.search(r"^\s*kind\s*:\s*CronJob", doc, re.M):
            continue
        offset = text.find(doc)
        base = text[:offset].count("\n") if offset >= 0 else 0
        name, tz = "cronjob", None
        for line in doc.splitlines():
            n = K8S_NAME_RE.match(line)
            if n and name == "cronjob":
                name = n.group(1)
            t = K8S_TZ_RE.match(line)
            if t:
                tz = t.group(1)
        for j, line in enumerate(doc.splitlines(), 1):
            mm = K8S_SCHED_RE.match(line)
            if mm:
                jobs.append(Job(name, mm.group(1).strip(), source, base + j, "k8s", tz, _duration(mm.group(2))))
    return jobs


def load_path(path: str) -> list[Job]:
    files: list[str] = []
    if os.path.isdir(path):
        for root, dirs, names in os.walk(path):
            dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".venv", "venv", "__pycache__"}]
            for n in names:
                if n.endswith((".yml", ".yaml", ".cron", ".crontab")) or n in {"crontab", "Crontab"}:
                    files.append(os.path.join(root, n))
    else:
        files.append(path)
    jobs: list[Job] = []
    for f in sorted(files):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        kind = detect_kind(f, text)
        if kind == "github":
            jobs += parse_github(text, f)
        elif kind == "k8s":
            jobs += parse_k8s(text, f)
        elif not f.endswith((".yml", ".yaml")):
            jobs += parse_crontab(text, f)
    return jobs
