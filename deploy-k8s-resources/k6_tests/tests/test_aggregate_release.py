"""Unit tests for aggregate_release.py.

Run from deploy-k8s-resources/k6_tests:  python3 -m unittest discover -s tests -v
"""
import csv
import json
import math
import statistics
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
K6_TESTS = HERE.parent
sys.path.insert(0, str(K6_TESTS))

import aggregate_release as ag  # noqa: E402

NEW_LOG = """AI benchmark summary: token chat openai (short)
http_reqs/s: {rps}
iterations total: {it}
dropped_iterations total: {dr}
http_req_duration p95(ms): {p95}
http_req_duration p99(ms): {p99}
http_req_duration avg(ms): {avg}
http_req_duration med(ms): {med}
http_req_duration p90(ms): {p90}
http_req_duration max(ms): {mx}
http_req_failed(%): {err}
checks pass rate(%): 100.00
http status counts: 2xx={ok} 3xx=0 429=0 4xx=0 5xx={e5} timeout=0 conn_err=0
"""

OLD_LOG = """AI benchmark summary: token chat openai (short)
http_reqs/s: 49.84
http_req_duration p95(ms): 614.58
http_req_duration p99(ms): 615.14
http_req_failed(%): 0.00
checks pass rate(%): 100.00
"""


def new_log(p95=100.0, p99=110.0, rps=49.9, it=900, dr=0, avg=95.0, med=94.0, p90=99.0, mx=130.0,
            err=0.0, ok=900, e5=0):
    return NEW_LOG.format(p95=p95, p99=p99, rps=rps, it=it, dr=dr, avg=avg, med=med, p90=p90,
                          mx=mx, err=err, ok=ok, e5=e5)


class ParseLog(unittest.TestCase):
    def parse(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x__run1.log"
            p.write_text(text)
            return ag.parse_log(p)

    def test_new_format_all_fields(self):
        r = self.parse(new_log(p95=101.5, med=94.2, p90=99.1, mx=131.0, it=950, dr=50, ok=940, e5=10))
        self.assertEqual(r["p95_ms"], 101.5)
        self.assertEqual(r["p50_ms"], 94.2)
        self.assertEqual(r["p90_ms"], 99.1)
        self.assertEqual(r["max_ms"], 131.0)
        self.assertAlmostEqual(r["dropped_pct"], 5.0)
        self.assertEqual(r["status"]["2xx"], 940)
        self.assertEqual(r["status"]["5xx"], 10)

    def test_old_seven_line_format_still_parses(self):
        r = self.parse(OLD_LOG)
        self.assertEqual(r["p95_ms"], 614.58)
        self.assertEqual(r["throughput_rps"], 49.84)
        for k in ("avg_ms", "p50_ms", "p90_ms", "max_ms", "dropped_pct", "status"):
            self.assertIsNone(r[k], k)

    def test_dropped_line_is_not_mistaken_for_iterations(self):
        # only the dropped line present -> cannot compute a percentage
        r = self.parse("dropped_iterations total: 40\nhttp_req_duration p95(ms): 1\n")
        self.assertIsNone(r["dropped_pct"])
        # only iterations present -> likewise
        r = self.parse("iterations total: 40\nhttp_req_duration p95(ms): 1\n")
        self.assertIsNone(r["dropped_pct"])

    def test_zero_total_iterations(self):
        self.assertEqual(self.parse(new_log(it=0, dr=0))["dropped_pct"], 0.0)

    def test_p90_does_not_leak_into_p95_or_p99(self):
        r = self.parse(new_log(p95=200.0, p99=300.0, p90=150.0))
        self.assertEqual((r["p90_ms"], r["p95_ms"], r["p99_ms"]), (150.0, 200.0, 300.0))


class AggregateScenario(unittest.TestCase):
    def reps(self, p95s, **kw):
        out = []
        for v in p95s:
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "s__run1.log"
                p.write_text(new_log(p95=v, **kw))
                out.append(ag.parse_log(p))
        return out

    def test_flat_keys_are_the_mean_and_layout_is_unchanged(self):
        vals = [100.0, 102.0, 98.0, 101.0, 99.0, 103.0]
        a = ag.aggregate_scenario(self.reps(vals))
        self.assertEqual(a["p95_ms"], round(statistics.mean(vals), 3))
        self.assertEqual(a["repeats"], 6)
        self.assertEqual(set(a["cv_pct"]), set(ag.PATTERNS))

    def test_cv_uses_sample_standard_deviation(self):
        vals = [100.0, 102.0, 98.0, 101.0, 99.0, 103.0]
        a = ag.aggregate_scenario(self.reps(vals))
        expected = statistics.stdev(vals) / statistics.mean(vals) * 100
        self.assertAlmostEqual(a["cv_pct"]["p95_ms"], round(expected, 2), places=2)
        population = statistics.pstdev(vals) / statistics.mean(vals) * 100
        self.assertGreater(a["cv_pct"]["p95_ms"], round(population, 2))

    def test_extra_stats_are_not_in_cv_pct(self):
        # compare_release.py takes max(cv_pct.values()) as the stability score;
        # a noisy-by-nature metric must never end up there.
        a = ag.aggregate_scenario(self.reps([100, 101, 99], mx=500.0))
        for extra in ("max_ms", "avg_ms", "p50_ms", "p90_ms"):
            self.assertNotIn(extra, a["cv_pct"])

    def test_median_iqr_stdev_min_max(self):
        vals = [100.0, 102.0, 98.0, 101.0, 99.0, 103.0]
        a = ag.aggregate_scenario(self.reps(vals))
        q = statistics.quantiles(vals, n=4, method="inclusive")
        self.assertEqual(a["median"]["p95_ms"], round(statistics.median(vals), 3))
        self.assertEqual(a["iqr"]["p95_ms"], round(q[2] - q[0], 3))
        self.assertEqual(a["stdev"]["p95_ms"], round(statistics.stdev(vals), 3))
        self.assertEqual((a["min"]["p95_ms"], a["max"]["p95_ms"]), (98.0, 103.0))

    def test_median_is_robust_to_one_outlier_run(self):
        vals = [500.0, 100.0, 101.0, 99.0, 100.0, 102.0]  # run 1 hit by a cold start
        a = ag.aggregate_scenario(self.reps(vals))
        self.assertGreater(a["p95_ms"], 150)                 # mean is dragged up
        self.assertLess(a["median"]["p95_ms"], 102)          # median is not

    def test_single_repeat_has_zero_spread(self):
        a = ag.aggregate_scenario(self.reps([100.0]))
        self.assertEqual((a["stdev"]["p95_ms"], a["iqr"]["p95_ms"], a["cv_pct"]["p95_ms"]), (0.0, 0.0, 0.0))
        self.assertEqual(a["median"]["p95_ms"], 100.0)

    def test_dropped_pct_and_status_totals(self):
        reps = self.reps([100, 100, 100], it=950, dr=50, ok=940, e5=10)
        reps[2]["dropped_pct"] = 0.0
        a = ag.aggregate_scenario(reps)
        self.assertAlmostEqual(a["dropped_pct"]["max"], 5.0)
        self.assertAlmostEqual(a["dropped_pct"]["mean"], round(10 / 3, 3), places=3)
        self.assertEqual(a["status_totals"]["2xx"], 940 * 3)
        self.assertEqual(a["status_totals"]["5xx"], 30)

    def test_old_logs_leave_new_fields_null(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s__run1.log"
            p.write_text(OLD_LOG)
            a = ag.aggregate_scenario([ag.parse_log(p)] * 3)
        self.assertIsNone(a["dropped_pct"])
        self.assertIsNone(a["status_totals"])
        self.assertIsNone(a["median"]["p50_ms"])
        self.assertEqual(a["median"]["p95_ms"], 614.58)

    def test_mixed_old_and_new_logs(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s__run1.log"
            p.write_text(OLD_LOG)
            old = ag.parse_log(p)
        new = self.reps([100.0], it=950, dr=50)[0]
        a = ag.aggregate_scenario([old, new])
        self.assertAlmostEqual(a["dropped_pct"]["max"], 5.0)   # only the log that has it
        self.assertEqual(a["repeats"], 2)


class UpstreamStats(unittest.TestCase):
    def reps(self, stats_lines):
        out = []
        for line in stats_lines:
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "s-disconnect__run1.log"
                p.write_text(new_log() + (line + "\n" if line else ""))
                out.append(ag.parse_log(p))
        return out

    def test_parse(self):
        r = self.reps(["upstream stats: started=56 completed=0 aborted=56 active=0"])[0]
        self.assertEqual(r["upstream"], {"started": 56, "completed": 0, "aborted": 56, "active": 0})
        self.assertIsNone(self.reps([""])[0]["upstream"])

    def test_totals_and_aborted_pct_over_repeats(self):
        a = ag.aggregate_scenario(self.reps([
            "upstream stats: started=100 completed=0 aborted=100 active=0",
            "upstream stats: started=100 completed=50 aborted=50 active=3"]))
        t = a["upstream_totals"]
        self.assertEqual((t["started"], t["completed"], t["aborted"], t["active_max"]), (200, 50, 150, 3))
        self.assertAlmostEqual(t["aborted_pct"], 75.0)

    def test_null_for_scenarios_without_stats_and_for_zero_started(self):
        self.assertIsNone(ag.aggregate_scenario(self.reps([""]))["upstream_totals"])
        zero = ag.aggregate_scenario(self.reps(["upstream stats: started=0 completed=0 aborted=0 active=0"]))
        self.assertIsNone(zero["upstream_totals"]["aborted_pct"])

    def test_a_repeat_without_stats_does_not_break_the_others(self):
        a = ag.aggregate_scenario(self.reps(["upstream stats: started=10 completed=0 aborted=10 active=0", ""]))
        self.assertEqual(a["upstream_totals"]["started"], 10)


class CpuPerRequest(unittest.TestCase):
    def rep(self, per_1k):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s__run1.log"
            line = "" if per_1k is None else f"kong cpu: seconds=12.5 iterations=9000 seconds_per_1k={per_1k}\n"
            p.write_text(new_log() + line)
            return ag.parse_log(p)

    def test_parse(self):
        self.assertEqual(self.rep(1.3889)["cpu_seconds_per_1k_req"], 1.3889)
        self.assertIsNone(self.rep(None)["cpu_seconds_per_1k_req"])

    def test_infra_is_the_median_across_repeats(self):
        a = ag.aggregate_scenario([self.rep(v) for v in (1.0, 1.1, 5.0)])   # one disturbed repeat
        self.assertEqual(a["infra"], {"cpu_seconds_per_1k_req": 1.1})

    def test_infra_is_null_without_cpu_lines_and_ignores_repeats_that_lack_one(self):
        self.assertIsNone(ag.aggregate_scenario([self.rep(None)])["infra"])
        self.assertEqual(ag.aggregate_scenario([self.rep(None), self.rep(2.0)])["infra"]["cpu_seconds_per_1k_req"], 2.0)

    def test_per_run_keeps_the_raw_value(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "s__run1.log").write_text(new_log() + "kong cpu: seconds=1 iterations=100 seconds_per_1k=10.0\n")
            (d / "s__run2.log").write_text(new_log())
            subprocess.run([sys.executable, "-B", str(K6_TESTS / "aggregate_release.py"), "--dir", str(d),
                            "--version", "v", "--captured-at", "x"], check=True, capture_output=True)
            runs = json.loads((d / "metrics.json").read_text())["scenarios"]["s"]["per_run"]
        self.assertEqual([r["cpu_seconds_per_1k_req"] for r in runs], [10.0, None])

    def test_cpu_stays_out_of_cv_pct(self):
        a = ag.aggregate_scenario([self.rep(v) for v in (1.0, 9.0)])
        self.assertNotIn("cpu_seconds_per_1k_req", a["cv_pct"])


class PerRun(unittest.TestCase):
    def test_per_run_is_ordered_numerically_and_keeps_raw_values(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            # run10 sorts before run2 lexicographically; per_run must order by number
            for n, v in ((10, 110.0), (2, 102.0), (1, 101.0)):
                (d / f"s__run{n}.log").write_text(new_log(p95=v))
            subprocess.run([sys.executable, "-B", str(K6_TESTS / "aggregate_release.py"), "--dir", str(d),
                            "--version", "v", "--captured-at", "x"], check=True, capture_output=True)
            m = json.loads((d / "metrics.json").read_text())["scenarios"]["s"]
        self.assertEqual([r["run"] for r in m["per_run"]], [1, 2, 10])
        self.assertEqual([r["p95_ms"] for r in m["per_run"]], [101.0, 102.0, 110.0])
        self.assertIn("p50_ms", m["per_run"][0])
        self.assertEqual(m["per_run"][0]["dropped_pct"], 0.0)

    def test_per_run_values_can_be_null_for_old_logs(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "s__run1.log").write_text(OLD_LOG)
            subprocess.run([sys.executable, "-B", str(K6_TESTS / "aggregate_release.py"), "--dir", str(d),
                            "--version", "v", "--captured-at", "x"], check=True, capture_output=True)
            m = json.loads((d / "metrics.json").read_text())["scenarios"]["s"]
        self.assertEqual(m["per_run"][0]["p95_ms"], 614.58)
        self.assertIsNone(m["per_run"][0]["p50_ms"])
        self.assertIsNone(m["per_run"][0]["dropped_pct"])


class EndToEnd(unittest.TestCase):
    def test_main_writes_schema_v2_skips_driver_logs_and_keeps_csv_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for i, v in enumerate([100.0, 101.0, 99.0], 1):
                (d / f"token-chat-openai__run{i}.log").write_text(new_log(p95=v))
                (d / f"token-chat-openai__run{i}.driver.log").write_text("[driver] noise p95 999\n")
            (d / "old-scn__medium__run1.log").write_text(OLD_LOG)
            r = subprocess.run([sys.executable, "-B", str(K6_TESTS / "aggregate_release.py"), "--dir", str(d),
                                "--version", "v", "--captured-at", "2026-01-01T00:00:00Z"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            m = json.loads((d / "metrics.json").read_text())
            self.assertEqual(m["schema_version"], 2)
            self.assertEqual(set(m["scenarios"]), {"token-chat-openai", "old-scn__medium"})
            self.assertEqual(m["scenarios"]["token-chat-openai"]["repeats"], 3)   # driver logs not counted
            self.assertTrue(math.isclose(m["scenarios"]["token-chat-openai"]["p95_ms"], 100.0, abs_tol=1e-9))
            with (d / "summary.csv").open() as fh:
                rows = list(csv.reader(fh))
            self.assertEqual(rows[0][:9], ["scenario", "p95_ms", "p99_ms", "ttft_p95_ms", "throughput_rps",
                                           "error_rate_pct", "checks_pass_pct", "repeats", "cv_p95_pct"])
            self.assertEqual(len(rows[0]), 13)
            self.assertTrue(all(len(r) == 13 for r in rows))


if __name__ == "__main__":
    unittest.main()
