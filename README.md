# cronherd

Find thundering herds, daylight-saving traps, and dead schedules across **crontabs, GitHub Actions workflows, and Kubernetes CronJobs** in one pass. Pure Python standard library, offline, no dependencies.

## Why it's different

Most cron tools explain or validate *one* expression. The bugs that actually bite in production live *between* schedules and *across* timezones:

- **Cross-system herds.** A `0 2 * * *` crontab in `America/Chicago`, a GitHub Actions `0 7 * * *` (always UTC), and a Kubernetes CronJob at `0 9 * * *` in `Europe/Berlin` all hit the same database at the same UTC minute. cronherd puts every schedule from every source on a single UTC timeline and reports which jobs collide, how often, and the busiest minutes of the hour.
- **DST traps, checked against real tz data.** Using `zoneinfo`, it walks the next year of wall-clock firings and flags times that don't exist on spring-forward night (run skipped or shifted, depending on the daemon) and times that happen twice on fall-back night (run may execute twice). Hourly-or-faster jobs are ignored since they're expected to cross the change.
- **Schedules that never run.** `0 0 30 2 *` parses fine everywhere and fires never. cronherd simulates four years of calendar days, so `Feb 29` jobs are correctly considered alive.
- **The day-of-month / day-of-week OR gotcha.** `0 0 1 * 1` runs on the 1st *or* any Monday, not "the 1st if it's a Monday".
- **Self-overlap.** Annotate a job with `# duration=40m` and cronherd tells you when runs will stack.
- **Platform specifics.** GitHub Actions schedules at `:00` (the hour GitHub documents as heaviest and most delayed), intervals under GitHub's 5-minute minimum, and Kubernetes CronJobs with no `spec.timeZone`.

## Install

```bash
pip install git+https://github.com/rashedhasan090/cronherd
# or, from a clone, no install needed:
python -m cronherd --help
```

Requires Python 3.9+ (for `zoneinfo`). On systems without OS tz data, `pip install tzdata`.

## Usage

```bash
cronherd .                                   # scan a repo: crontabs, .github/workflows, k8s yaml
cronherd /etc/crontab --tz America/Chicago   # zone for crontab lines without CRON_TZ=
cronherd -e "30 2 * * *" --tz Europe/Berlin  # ad-hoc expression
cronherd . --herd 4 --days 14                # bigger herd threshold, longer window
cronherd . --format json --fail-on medium    # CI gate
```

| Option | Meaning |
| --- | --- |
| `--tz` | Zone for crontab entries with no `CRON_TZ=` / `TZ=` line (default UTC). GitHub Actions is always UTC; k8s uses `spec.timeZone`. |
| `--start` | Window start, ISO UTC (default now). Pin it for reproducible CI output. |
| `--days` | Herd window length (default 7). |
| `--herd` | Jobs in one UTC minute that count as a herd (default 3). Jobs firing more often than every 5 minutes are left out of herd math. |
| `--format` | `text` or `json`. |
| `--fail-on` | Exit 1 at or above `low`, `medium`, `high` (default), or `none`. |

### Rules

| Rule | Severity |
| --- | --- |
| `parse-error`, `unknown-timezone`, `never-fires`, `dst-skip` | high |
| `dst-repeat`, `dom-dow-or`, `self-overlap`, `herd` | medium |
| `gha-top-of-hour`, `gha-too-frequent`, `k8s-implicit-timezone` | low |

## Demo

The `examples/` folder has a Chicago crontab, two GitHub workflows, and two Kubernetes CronJobs.

```console
$ python -m cronherd examples --start 2026-10-05T00:00:00Z
cronherd: 11/11 schedules parsed, 7d window from 2026-10-05T00:00:00 UTC
busiest minute-of-hour (UTC firings in window):
  :00   365 ##############################
  :30   182 ###############
  :15   168 ##############
  :45   168 ##############
HIGH   dst-skip [examples/crontab:4]: in America/Chicago the wall time 2027-03-14 02:30 does not exist (spring forward); 1 run(s) in the next year will be skipped or shifted depending on the daemon
HIGH   dst-skip [examples/k8s/cronjobs.yaml:15]: in Europe/Berlin the wall time 2027-03-28 02:30 does not exist (spring forward); 1 run(s) in the next year will be skipped or shifted depending on the daemon
HIGH   never-fires [examples/crontab:9]: '0 0 30 2 *' matches no calendar day in 4 years; it will never run
MEDIUM dom-dow-or [examples/crontab:8]: '0 0 1 * 1' restricts both day-of-month and day-of-week; cron ORs them, so it runs on EITHER condition, not only when both hold
MEDIUM dst-repeat [examples/crontab:6]: in America/Chicago the wall time 2026-11-01 01:00 happens twice (fall back); 1 run(s) in the next year may execute twice
MEDIUM dst-repeat [examples/k8s/cronjobs.yaml:15]: in Europe/Berlin the wall time 2026-10-25 02:30 happens twice (fall back); 1 run(s) in the next year may execute twice
MEDIUM herd: 5 jobs fire in the same UTC minute 7x in 7d, first at 2026-10-05 07:00 UTC; stagger their minutes
         - /usr/local/bin/healthcheck (examples/crontab:7)
         - /usr/local/bin/rotate-logs (examples/crontab:5)
         - db-vacuum (examples/k8s/cronjobs.yaml:6)
         - nightly (examples/.github/workflows/nightly.yml:4)
         - stale (examples/.github/workflows/stale.yml:4)
MEDIUM herd: 3 jobs fire in the same UTC minute 1x in 7d, first at 2026-10-05 05:00 UTC; stagger their minutes
         - /usr/local/bin/healthcheck (examples/crontab:7)
         - /usr/local/bin/monthly-report (examples/crontab:8)
         - /usr/local/bin/rotate-logs (examples/crontab:5)
MEDIUM herd: 3 jobs fire in the same UTC minute 7x in 7d, first at 2026-10-05 06:00 UTC; stagger their minutes
         - /usr/local/bin/healthcheck (examples/crontab:7)
         - /usr/local/bin/reindex (examples/crontab:6)
         - /usr/local/bin/rotate-logs (examples/crontab:5)
MEDIUM self-overlap [examples/crontab:7]: declared duration 20m exceeds the 15m gap between runs; runs will stack unless the job is locked
LOW    gha-too-frequent [examples/.github/workflows/nightly.yml:5]: '*/2 * * * *' fires every 2m but GitHub Actions schedules run at most every 5 minutes
LOW    gha-top-of-hour [examples/.github/workflows/nightly.yml:4]: fires at minute :00, when GitHub documents the heaviest scheduled-run load and delays or drops; pick an odd minute
LOW    gha-top-of-hour [examples/.github/workflows/nightly.yml:5]: fires at minute :00, when GitHub documents the heaviest scheduled-run load and delays or drops; pick an odd minute
LOW    gha-top-of-hour [examples/.github/workflows/stale.yml:4]: fires at minute :00, when GitHub documents the heaviest scheduled-run load and delays or drops; pick an odd minute
LOW    k8s-implicit-timezone [examples/k8s/cronjobs.yaml:6]: CronJob has no spec.timeZone, so it follows kube-controller-manager's local zone
```

Five jobs from three different systems (two crontab entries, a k8s CronJob, and two GitHub workflows) land on 07:00 UTC every day. None of them look related in their own files.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Limits

The YAML readers are line-based (no PyYAML), which covers the common `- cron:` and `schedule:` layouts but not anchors or flow-style maps. DST behavior differs by daemon (Vixie cron, cronie, systemd timers, Kubernetes), so cronherd reports the hazard rather than guessing what your daemon does.

## License

MIT
