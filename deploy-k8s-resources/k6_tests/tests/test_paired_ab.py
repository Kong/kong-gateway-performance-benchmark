"""Tests for paired_ab.py: interleaved A/B analysis, on synthetic rounds with a known answer."""
import io
import math
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
K6_TESTS = HERE.parent
sys.path.insert(0, str(K6_TESTS))

import paired_ab as pb  # noqa: E402

LOG = ("AI benchmark summary: x\niterations total: 4080\nhttp_req_duration p95(ms): {p95}\n"
       "http_reqs/s: 22.6\n{cpu}")


def write_round(root, prefix, config, rnd, scenario, cpu=None, p95=100.0, content=None):
    d = Path(root) / f"{prefix}-{config}-r{rnd}_2026-11-05"
    d.mkdir(exist_ok=True)
    text = content if content is not None else LOG.format(
        p95=p95, cpu="" if cpu is None else f"kong cpu: seconds=15.0 iterations=4080 seconds_per_1k={cpu}\n")
    (d / f"{scenario}__run1.log").write_text(text)


class Statistics(unittest.TestCase):
    def test_sign_test_exact_p_values(self):
        self.assertAlmostEqual(pb.sign_test_p(6, 6), 2 / 64)          # smallest possible with 6 rounds
        self.assertAlmostEqual(pb.sign_test_p(0, 6), 2 / 64)
        self.assertAlmostEqual(pb.sign_test_p(3, 6), 1.0)
        self.assertAlmostEqual(pb.sign_test_p(5, 6), 2 * 7 / 64)
        self.assertIsNone(pb.sign_test_p(0, 0))
        self.assertLessEqual(pb.sign_test_p(1, 2), 1.0)

    def test_bootstrap_ci_is_deterministic_and_brackets_the_mean(self):
        vals = [0.3, 0.5, 0.4, 0.6, 0.35, 0.45]
        ci = pb.bootstrap_ci_of_mean(vals)
        self.assertEqual(ci, pb.bootstrap_ci_of_mean(vals))
        self.assertLess(ci[0], sum(vals) / 6)
        self.assertGreater(ci[1], sum(vals) / 6)
        self.assertIsNone(pb.bootstrap_ci_of_mean([1.0, 2.0]))         # too few to bootstrap

    def test_a_consistent_effect_is_visible(self):
        a = {r: 3.30 + 0.05 * (r % 3) for r in range(1, 7)}
        b = {r: v + 0.40 for r, v in a.items()}                        # B is always 0.40 higher
        s = pb.analyze(a, b)
        self.assertAlmostEqual(s["mean_diff"], 0.40)
        self.assertAlmostEqual(s["sd_diff"], 0.0, places=9)
        self.assertEqual((s["b_greater"], s["nonzero"]), (6, 6))
        self.assertAlmostEqual(s["sign_p"], 2 / 64)
        lo, hi = s["ci95"]
        self.assertAlmostEqual(lo, 0.40)
        self.assertAlmostEqual(hi, 0.40)
        self.assertAlmostEqual(s["pct_diff"], 0.40 / s["mean_a"] * 100)

    def test_no_effect_with_noise_includes_zero(self):
        a = {1: 3.3, 2: 3.1, 3: 3.4, 4: 3.2, 5: 3.35, 6: 3.25}
        b = {1: 3.2, 2: 3.3, 3: 3.3, 4: 3.25, 5: 3.4, 6: 3.2}          # mixed signs
        s = pb.analyze(a, b)
        lo, hi = s["ci95"]
        self.assertLess(lo, 0)
        self.assertGreater(hi, 0)
        self.assertGreater(s["sign_p"], 0.3)

    def test_pairing_removes_a_shared_drift(self):
        drift = {1: 0.0, 2: 1.0, 3: 2.0, 4: 3.0, 5: 4.0, 6: 5.0}        # large slow drift, same for A and B
        a = {r: 3.0 + d for r, d in drift.items()}
        b = {r: 3.1 + d for r, d in drift.items()}                      # real effect: +0.1
        paired = pb.analyze(a, b)
        self.assertAlmostEqual(paired["mean_diff"], 0.1)
        self.assertAlmostEqual(paired["sd_diff"], 0.0, places=9)
        self.assertGreater(paired["sd_a"], 1.0)                         # unpaired spread is ~10x the effect

    def test_only_rounds_present_for_both_configs_count(self):
        s = pb.analyze({1: 1.0, 2: 2.0, 3: 3.0}, {2: 2.5, 3: 3.5, 4: 9.0})
        self.assertEqual(s["rounds"], [2, 3])
        self.assertAlmostEqual(s["mean_diff"], 0.5)
        self.assertIsNone(pb.analyze({1: 1.0}, {2: 1.0}))

    def test_one_round_has_no_spread(self):
        s = pb.analyze({1: 1.0}, {1: 1.5})
        self.assertEqual(s["n"], 1)
        self.assertIsNone(s["sd_diff"])
        self.assertIsNone(s["ci95"])

    def test_order_effect_is_separated(self):
        # B is +0.0 when A ran first (odd rounds) but +0.6 when B ran first: the second run is different
        a = {r: 3.0 for r in range(1, 7)}
        b = {r: 3.0 if r % 2 == 1 else 3.6 for r in range(1, 7)}
        s = pb.analyze(a, b, first_odd="a")
        self.assertAlmostEqual(s["order"]["a_first"][0], 0.0)
        self.assertAlmostEqual(s["order"]["b_first"][0], 0.6)
        self.assertEqual((s["order"]["a_first"][1], s["order"]["b_first"][1]), (3, 3))
        flipped = pb.analyze(a, b, first_odd="b")
        self.assertAlmostEqual(flipped["order"]["a_first"][0], 0.6)


class Reading(unittest.TestCase):
    def test_reads_cpu_and_other_metrics_from_per_round_directories(self):
        with tempfile.TemporaryDirectory() as root:
            for r in range(1, 4):
                write_round(root, "x", "buffered", r, "stream-openai", cpu=3.3, p95=1326.0)
                write_round(root, "x", "unbuffered", r, "stream-openai", cpu=3.7, p95=1325.0)
            self.assertEqual(pb.collect(root, "x", "buffered", "stream-openai", "cpu"), {1: 3.3, 2: 3.3, 3: 3.3})
            self.assertEqual(pb.collect(root, "x", "unbuffered", "stream-openai", "p95_ms")[2], 1325.0)

    def test_rounds_without_a_readable_value_are_absent_not_zero(self):
        with tempfile.TemporaryDirectory() as root:
            write_round(root, "x", "a", 1, "s", cpu=3.0)
            write_round(root, "x", "a", 2, "s", content="")                      # kubectl lost its credentials
            write_round(root, "x", "a", 3, "s", cpu=None)                        # no cpu line
            self.assertEqual(pb.collect(root, "x", "a", "s", "cpu"), {1: 3.0})

    def test_directory_matching_does_not_confuse_prefixes_or_configs(self):
        with tempfile.TemporaryDirectory() as root:
            write_round(root, "x", "buffered", 1, "s", cpu=1.0)
            write_round(root, "x", "unbuffered", 1, "s", cpu=2.0)                # 'buffered' is a suffix of 'unbuffered'
            write_round(root, "other", "buffered", 1, "s", cpu=9.0)
            self.assertEqual(sorted(pb.round_dirs(root, "x", "buffered")), [1])
            self.assertEqual(pb.collect(root, "x", "buffered", "s", "cpu"), {1: 1.0})
            self.assertEqual(pb.collect(root, "x", "unbuffered", "s", "cpu"), {1: 2.0})

    def test_round_numbers_are_numeric_not_lexicographic(self):
        with tempfile.TemporaryDirectory() as root:
            for r in (1, 2, 10):
                write_round(root, "x", "a", r, "s", cpu=float(r))
            self.assertEqual(pb.collect(root, "x", "a", "s", "cpu"), {1: 1.0, 2: 2.0, 10: 10.0})


class Cli(unittest.TestCase):
    def run_cli(self, root, *extra):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = pb.main(["--root", str(root), "--prefix", "x", "--a", "buffered", "--b", "unbuffered",
                          "--scenarios", "stream-openai,stream-gemini", "--first-odd", "a", *extra])
        return rc, out.getvalue()

    def test_report(self):
        with tempfile.TemporaryDirectory() as root:
            for r in range(1, 7):
                for sc, base in (("stream-openai", 3.3), ("stream-gemini", 3.9)):
                    write_round(root, "x", "buffered", r, sc, cpu=base)
                    write_round(root, "x", "unbuffered", r, sc, cpu=base + 0.4)
            rc, out = self.run_cli(root)
        self.assertEqual(rc, 0)
        for part in ("stream-openai", "stream-gemini", "mean paired diff: +0.400", "excludes 0",
                     "unbuffered > buffered in 6 of 6 rounds", "sign test p = 0.031", "order effect"):
            self.assertIn(part, out)

    def test_missing_data_is_reported_and_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as root:
            rc, out = self.run_cli(root)
        self.assertEqual(rc, 2)
        self.assertIn("no round has a value for both configurations", out)

    def test_a_scenario_with_data_is_reported_even_if_another_has_none(self):
        with tempfile.TemporaryDirectory() as root:
            for r in range(1, 4):
                write_round(root, "x", "buffered", r, "stream-openai", cpu=3.0)
                write_round(root, "x", "unbuffered", r, "stream-openai", cpu=3.1)
            rc, out = self.run_cli(root)
        self.assertEqual(rc, 0)
        self.assertIn("stream-gemini: no round has a value", out)


if __name__ == "__main__":
    unittest.main()
