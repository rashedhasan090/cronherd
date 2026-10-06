import json
import os
import subprocess
import sys
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cronherd.analyze import analyze
from cronherd.cron import Cron, CronError
from cronherd.sources import Job, load_path, parse_crontab, parse_github, parse_k8s

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START = datetime(2026, 10, 5)


def rules(findings):
    return sorted(f.rule for f in findings)


def job(expr, name="j", tz=None, kind="crontab", dur=None):
    return Job(name, expr, "t", 1, kind, tz, dur)


class CronParse(unittest.TestCase):
    def test_fields(self):
        c = Cron.parse("*/15 9-17 * jan-mar mon-fri")
        self.assertEqual(c.minutes, {0, 15, 30, 45})
        self.assertEqual(c.hours, set(range(9, 18)))
        self.assertEqual(c.months, {1, 2, 3})
        self.assertEqual(c.dows, {1, 2, 3, 4, 5})

    def test_sunday_seven(self):
        self.assertEqual(Cron.parse("0 0 * * 7").dows, {0})

    def test_macro(self):
        self.assertEqual(Cron.parse("@hourly").minutes, {0})

    def test_step_from_value(self):
        self.assertEqual(Cron.parse("5/20 * * * *").minutes, {5, 25, 45})

    def test_errors(self):
        for bad in ["60 * * * *", "* * *", "*/0 * * * *", "1-x * * * *", "@reboot"]:
            with self.assertRaises(CronError):
                Cron.parse(bad)

    def test_dom_dow_or_semantics(self):
        c = Cron.parse("0 0 1 * 1")
        self.assertTrue(c.dom_dow_or)
        self.assertTrue(c.day_matches(date(2026, 10, 1)))   # 1st, a Thursday
        self.assertTrue(c.day_matches(date(2026, 10, 5)))   # Monday
        self.assertFalse(c.day_matches(date(2026, 10, 6)))

    def test_min_gap(self):
        self.assertEqual(Cron.parse("*/15 * * * *").min_gap_minutes(), 15)
        self.assertEqual(Cron.parse("0 3 * * *").min_gap_minutes(), 1440)


class Rules(unittest.TestCase):
    def test_never_fires(self):
        f, _ = analyze([job("0 0 30 2 *")], START)
        self.assertIn("never-fires", rules(f))

    def test_leap_day_is_not_never(self):
        f, _ = analyze([job("0 0 29 2 *")], START)
        self.assertNotIn("never-fires", rules(f))

    def test_dst_skip_and_repeat_eu(self):
        # EU springs forward 02:00->03:00 and falls back 03:00->02:00, so 02:30 is both
        f, _ = analyze([job("30 2 * * *", tz="Europe/Berlin")], START)
        self.assertEqual(rules(f), ["dst-repeat", "dst-skip"])

    def test_dst_precise_us(self):
        f, _ = analyze([job("30 1 * * *", tz="America/New_York")], START)
        self.assertEqual(rules(f), ["dst-repeat"])
        f, _ = analyze([job("30 2 * * *", tz="America/New_York")], START)
        self.assertIn("dst-skip", rules(f))

    def test_utc_has_no_dst(self):
        f, _ = analyze([job("30 2 * * *", tz="UTC")], START)
        self.assertEqual(rules(f), [])

    def test_hourly_not_dst_flagged(self):
        f, _ = analyze([job("30 * * * *", tz="Europe/Berlin")], START)
        self.assertNotIn("dst-skip", rules(f))

    def test_unknown_tz(self):
        f, _ = analyze([job("0 3 * * *", tz="Mars/Olympus")], START)
        self.assertIn("unknown-timezone", rules(f))

    def test_herd_across_timezones(self):
        jobs = [job("0 7 * * *", "a", "UTC"), job("0 2 * * *", "b", "America/Chicago"),
                job("0 9 * * *", "c", "Europe/Berlin")]
        f, stats = analyze(jobs, START, herd=3)
        herds = [x for x in f if x.rule == "herd"]
        self.assertEqual(len(herds), 1)
        self.assertIn("7x", herds[0].message)
        self.assertEqual(stats["peak_slot_size"], 3)

    def test_herd_threshold(self):
        jobs = [job("0 7 * * *", "a"), job("0 7 * * *", "b")]
        f, _ = analyze(jobs, START, herd=3)
        self.assertNotIn("herd", rules(f))

    def test_self_overlap(self):
        f, _ = analyze([job("*/10 * * * *", dur=25)], START)
        self.assertIn("self-overlap", rules(f))

    def test_github_rules(self):
        f, _ = analyze([job("*/1 * * * *", kind="github", tz="UTC"), job("0 4 * * *", kind="github", tz="UTC")], START)
        r = rules(f)
        self.assertIn("gha-too-frequent", r)
        self.assertIn("gha-top-of-hour", r)

    def test_parse_error(self):
        f, _ = analyze([job("61 * * * *")], START)
        self.assertEqual(rules(f), ["parse-error"])


class Sources(unittest.TestCase):
    def test_crontab(self):
        text = "CRON_TZ=Asia/Dhaka\nFOO=bar\n# c\n@reboot x\n15 3 * * * /bin/x --y  # duration=2h\n@daily /bin/z\n"
        js = parse_crontab(text, "c")
        self.assertEqual([j.expr for j in js], ["15 3 * * *", "@daily"])
        self.assertEqual(js[0].tz, "Asia/Dhaka")
        self.assertEqual(js[0].duration_min, 120)

    def test_github(self):
        text = "name: ci\non:\n  schedule:\n    - cron: '17 3 * * 1'\n    - cron: \"0 0 * * *\"  # duration=10m\n"
        js = parse_github(text, ".github/workflows/ci.yml")
        self.assertEqual([j.expr for j in js], ["17 3 * * 1", "0 0 * * *"])
        self.assertEqual(js[1].duration_min, 10)
        self.assertEqual(js[0].name, "ci")

    def test_k8s(self):
        text = open(os.path.join(ROOT, "examples/k8s/cronjobs.yaml")).read()
        js = parse_k8s(text, "k.yaml")
        self.assertEqual([(j.name, j.tz) for j in js], [("db-vacuum", None), ("report-mailer", "Europe/Berlin")])
        self.assertEqual(js[1].line, 15)

    def test_load_examples_dir(self):
        js = load_path(os.path.join(ROOT, "examples"))
        self.assertEqual(len(js), 11)


class CLI(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "cronherd", *args], cwd=ROOT,
                              capture_output=True, text=True)

    def test_json_and_exit(self):
        r = self.run_cli("examples", "--start", "2026-10-05T00:00:00Z", "--format", "json")
        self.assertEqual(r.returncode, 1)
        data = json.loads(r.stdout)
        self.assertIn("never-fires", {f["rule"] for f in data["findings"]})

    def test_fail_on_none(self):
        r = self.run_cli("examples", "--start", "2026-10-05T00:00:00Z", "--fail-on", "none")
        self.assertEqual(r.returncode, 0)
        self.assertIn("busiest minute-of-hour", r.stdout)

    def test_expr_clean(self):
        r = self.run_cli("-e", "17 3 * * *", "--start", "2026-10-05T00:00:00Z", "--fail-on", "low")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("no findings", r.stdout)


if __name__ == "__main__":
    unittest.main()
