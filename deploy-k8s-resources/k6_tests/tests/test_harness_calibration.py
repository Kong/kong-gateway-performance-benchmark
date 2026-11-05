"""Tests for harness_calibration.py: the decision logic, and the whole sweep against a simulated harness
(stub kubectl / run_ai_benchmark.sh whose upstream saturates at a known rate)."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
K6_TESTS = HERE.parent
sys.path.insert(0, str(K6_TESTS))

import cpu_sample as cs  # noqa: E402
import harness_calibration as hc  # noqa: E402

LOG = """AI benchmark summary: direct calibration (zero delay)
http_reqs/s: {rps}
iterations total: {iterations}
dropped_iterations total: {dropped}
http_req_duration p95(ms): {p95}
http_req_duration p99(ms): {p99}
http_req_failed(%): {err}
checks pass rate(%): 100.00
http status counts: 2xx=1000 3xx=0 429=0 4xx=0 5xx=0 timeout=0 conn_err=0
"""


def step(target, achieved=None, dropped=0.0, err=0.0, up=None, lg=None):
    return {"target_rps": target, "achieved_rps": target if achieved is None else achieved, "p95_ms": 2.0, "p99_ms": 3.0,
            "error_pct": err, "dropped_pct": dropped, "status": None, "upstream_cpu_cores": up, "loadgen_cpu_cores": lg}


class Parsing(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(hc.parse_duration_seconds("30s"), 30)
        self.assertEqual(hc.parse_duration_seconds("2m"), 120)
        for bad in ("0s", "90", "1h", "s", ""):
            with self.assertRaises(ValueError):
                hc.parse_duration_seconds(bad)

    def test_top_output_takes_the_largest_cpu(self):
        self.assertEqual(hc.parse_top_cores("k6-a 800m 100Mi\nk6-b 2 3Gi\n"), 2.0)
        self.assertAlmostEqual(hc.parse_top_cores("k6-a 1523m 100Mi"), 1.523)
        self.assertIsNone(hc.parse_top_cores(""))
        self.assertIsNone(hc.parse_top_cores(None))
        self.assertIsNone(hc.parse_top_cores("error: Metrics API not available"))

    def test_step_from_log_uses_the_shared_parser_and_divides_cpu_by_the_step_duration(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "step.log"
            p.write_text(LOG.format(rps=1990.5, dropped=2, p95=2.5, p99=4.0, err=0.0, iterations=59715))
            s = hc.step_from_log(2000, p, 30, upstream_cpu_seconds=27.0, loadgen_cores=3.5)
        self.assertEqual((s["target_rps"], s["achieved_rps"], s["p95_ms"]), (2000, 1990.5, 2.5))   # 59715 / 30 s
        self.assertAlmostEqual(s["dropped_pct"], 2 / (59715 + 2) * 100, places=4)
        self.assertEqual(s["upstream_cpu_cores"], 0.9)
        self.assertEqual(s["loadgen_cpu_cores"], 3.5)
        self.assertEqual(s["status"]["2xx"], 1000)


class Evaluate(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(hc.evaluate_step(step(1000, achieved=990)), (True, []))

    def test_each_criterion_fails_on_its_own(self):
        ok, why = hc.evaluate_step(step(1000, achieved=960))
        self.assertFalse(ok); self.assertIn("96%", why[0])
        ok, why = hc.evaluate_step(step(1000, dropped=1.5)); self.assertFalse(ok); self.assertIn("dropped", why[0])
        ok, why = hc.evaluate_step(step(1000, err=2.0)); self.assertFalse(ok); self.assertIn("failed", why[0])

    def test_queueing_fails_a_step_that_still_meets_the_rate(self):
        # 16000 rps on the local machine: 99% achieved, 0.85% dropped, but p95 went from 3.5 ms to 103 ms
        s = step(16000, achieved=15863.6, dropped=0.85); s["p95_ms"] = 103.4
        ok, why = hc.evaluate_step(s)
        self.assertFalse(ok)
        self.assertIn("harness is queueing", why[0])
        self.assertEqual(len(why), 1)

    def test_the_p95_limit_is_a_parameter_and_its_boundary_passes(self):
        s = step(1000); s["p95_ms"] = 10.0
        self.assertTrue(hc.evaluate_step(s)[0])
        s["p95_ms"] = 10.1
        self.assertFalse(hc.evaluate_step(s)[0])
        self.assertTrue(hc.evaluate_step(s, max_p95=20)[0])

    def test_boundaries(self):
        self.assertTrue(hc.evaluate_step(step(1000, achieved=970))[0])                 # exactly 97%
        self.assertTrue(hc.evaluate_step(step(1000, dropped=1.0, err=1.0))[0])
        self.assertFalse(hc.evaluate_step(step(1000, achieved=969.9))[0])

    def test_unreadable_summary_fails(self):
        s = step(1000); s["achieved_rps"] = None
        self.assertEqual(hc.evaluate_step(s), (False, ["no k6 summary could be read"]))

    def test_the_reason_includes_what_k6_said(self):
        s = step(8000); s["achieved_rps"] = None
        s["k6_error"] = "preAllocatedVUs (2050) must be <= maxVUs (2000)"
        ok, why = hc.evaluate_step(s)
        self.assertFalse(ok)
        self.assertIn("k6 said: preAllocatedVUs (2050)", why[0])

    def test_finding_the_error_line_in_a_k6_log(self):
        log = "          /\\      |‾‾| /‾‾/\n ERRO[0000] invalid configuration: preAllocatedVUs must be <= maxVUs   \n"
        self.assertIn("preAllocatedVUs must be <= maxVUs", hc.k6_error_line(log))
        self.assertIsNone(hc.k6_error_line("all good\nrunning\n"))
        self.assertIsNone(hc.k6_error_line(None))
        self.assertLessEqual(len(hc.k6_error_line("ERRO " + "x" * 500)), 200)


class Attribution(unittest.TestCase):
    def test_upstream_saturated(self):
        who, text = hc.attribute_bottleneck(step(6000, up=0.97, lg=5.0))
        self.assertEqual(who, "upstream"); self.assertIn("scale the fake provider out", text)
        self.assertIn("97%", text)

    def test_load_generator_saturated(self):
        who, text = hc.attribute_bottleneck(step(6000, up=0.3, lg=55.0))
        self.assertEqual(who, "load generator"); self.assertIn("92%", text)

    def test_the_busier_component_is_blamed_when_both_are_high(self):
        self.assertEqual(hc.attribute_bottleneck(step(6000, up=1.0, lg=59.0))[0], "upstream")     # 100% vs 98%
        self.assertEqual(hc.attribute_bottleneck(step(6000, up=0.8, lg=59.0))[0], "load generator")  # 80% vs 98%

    def test_the_measured_local_case_is_the_upstream(self):
        # real sweep: at 12000 rps p95 jumped to 26 ms with the fake provider at 0.74 core and k6 at 1 of 60.
        # An absolute 0.75-core threshold said "neither"; relative utilisation says what it is.
        who, _ = hc.attribute_bottleneck(step(12000, up=0.74, lg=1.0))
        self.assertEqual(who, "upstream")

    def test_neither_busy(self):
        who, text = hc.attribute_bottleneck(step(6000, up=0.2, lg=4.0))
        self.assertEqual(who, "undetermined"); self.assertIn("kube-proxy", text)

    def test_just_below_half_is_not_enough_and_the_limit_is_a_parameter(self):
        self.assertEqual(hc.attribute_bottleneck(step(1, up=0.49, lg=1.0))[0], "undetermined")
        self.assertEqual(hc.attribute_bottleneck(step(1, up=0.5, lg=1.0))[0], "upstream")
        self.assertEqual(hc.attribute_bottleneck(step(1, up=0.3, lg=1.0), min_utilization=0.25)[0], "upstream")

    def test_no_cpu_data_is_said_so(self):
        who, text = hc.attribute_bottleneck(step(6000))
        self.assertEqual(who, "undetermined"); self.assertIn("no CPU data", text)

    def test_only_one_component_measured(self):
        self.assertEqual(hc.attribute_bottleneck(step(6000, up=0.9))[0], "upstream")
        self.assertEqual(hc.attribute_bottleneck(step(6000, lg=50.0))[0], "load generator")

    def test_capacities_are_parameters(self):
        self.assertEqual(hc.attribute_bottleneck(step(1, up=1.5), upstream_capacity=2.0)[0], "upstream")   # 75% of 2 cores
        self.assertEqual(hc.attribute_bottleneck(step(1, up=0.9, lg=9.5), loadgen_limit=10.0)[0], "load generator")

    def test_a_tie_blames_the_upstream(self):
        # the single Node process is the cheaper thing to fix, so it is named first when both are equally busy
        self.assertEqual(hc.attribute_bottleneck(step(1, up=0.9, lg=9.0), loadgen_limit=10.0)[0], "upstream")


class SweepAndSummary(unittest.TestCase):
    def model(self, ceiling):
        return lambda rps: step(rps, achieved=min(rps, ceiling), up=min(rps / ceiling, 1.0) * 0.98, lg=5.0)

    def test_stops_at_the_first_failure(self):
        res = hc.sweep([200, 1000, 3000, 6000, 8000], self.model(3000))
        self.assertEqual([s["target_rps"] for s in res], [200, 1000, 3000, 6000])
        self.assertEqual([s["passed"] for s in res], [True, True, True, False])

    def test_no_stop_runs_everything(self):
        res = hc.sweep([200, 3000, 6000, 8000], self.model(3000), stop_on_fail=False)
        self.assertEqual(len(res), 4)

    def test_summary_ceiling_and_cause(self):
        s = hc.summarize(hc.sweep([200, 1000, 3000, 6000], self.model(3000)))
        self.assertEqual((s["harness_ceiling_rps"], s["first_failing_rps"], s["limited_by"]), (3000, 6000, "upstream"))
        self.assertTrue(s["found_a_limit"])

    def test_a_later_pass_after_a_failure_does_not_raise_the_ceiling(self):
        calls = {200: True, 1000: False, 2000: True}
        res = hc.sweep([200, 1000, 2000],
                       lambda r: step(r, achieved=r if calls[r] else r * 0.5, up=0.1, lg=1.0), stop_on_fail=False)
        s = hc.summarize(res)
        self.assertEqual(s["harness_ceiling_rps"], 200)

    def test_all_steps_pass_means_no_limit_found(self):
        s = hc.summarize(hc.sweep([200, 500], self.model(10 ** 9)))
        self.assertFalse(s["found_a_limit"])
        self.assertIsNone(s["limited_by"])
        self.assertIn("at least", s["explanation"])

    def test_first_step_failing(self):
        s = hc.summarize(hc.sweep([200], self.model(100)))
        self.assertIsNone(s["harness_ceiling_rps"])
        self.assertIn("even the first step", s["explanation"])

    def test_markdown(self):
        res = hc.sweep([200, 3000, 6000], self.model(3000))
        md = hc.render_markdown(res, hc.summarize(res), {"started_at": "now", "step_duration": "30s"})
        for part in ("| 6000 |", "FAIL: achieved 3000 of 6000", "Harness ceiling: 3000 rps", "Limited by: upstream",
                     "~2400 rps", "p95 <= 10 ms"):
            self.assertIn(part, md)


class CpuSampleTarget(unittest.TestCase):
    def test_defaults_still_ask_for_the_kong_proxy(self):
        self.assertEqual(cs.kubectl_args(), cs.KUBECTL_ARGS)

    def test_other_targets_and_a_single_container_pod(self):
        a = cs.kubectl_args("upstream", "deploy/fake-provider", "")
        self.assertEqual(a[:4], ["exec", "-n", "upstream", "deploy/fake-provider"])
        self.assertNotIn("-c", a[:a.index("--")])          # no container flag before the command (sh -c is after)
        withc = cs.kubectl_args("k6", "pod/x", "runner")
        self.assertEqual(withc[withc.index("-c") + 1], "runner")
        self.assertLess(withc.index("-c"), withc.index("--"))


BENCH_STUB = r'''#!/bin/bash
# stands in for run_ai_benchmark.sh direct-calibration zero RATE DURATION on a harness whose upstream saturates
python3 - "$3" "$4" <<'PY'
import json, os, sys
T = os.environ["STUB_DIR"]; rps = int(sys.argv[1]); dur = int(sys.argv[2].rstrip("s"))
model = json.load(open(f"{T}/model.json"))
ceiling = model["ceiling"]
achieved = min(rps, ceiling)
dropped = max(0.0, (rps - ceiling) / rps * 100)
cores = min(rps / ceiling, 1.0) * model["upstream_peak_cores"]
cpu_ns = int(open(f"{T}/cpu.counter").read()) + int(cores * dur * 1e9)
open(f"{T}/cpu.counter", "w").write(str(cpu_ns))
open(f"{T}/loadgen.milli", "w").write(str(model["loadgen_millicores"]))
open(f"{T}/k6.out", "w").write(f"""AI benchmark summary: direct calibration (zero delay)
http_reqs/s: {achieved}
iterations total: {int(achieved * dur)}
dropped_iterations total: {int(dropped / 100 * rps * dur)}
http_req_duration p95(ms): 2.0
http_req_duration p99(ms): 3.0
http_req_failed(%): 0.00
checks pass rate(%): 100.00
http status counts: 2xx=100 3xx=0 429=0 4xx=0 5xx=0 timeout=0 conn_err=0
""")
PY
'''

KUBECTL_STUB = r'''#!/bin/bash
case "$*" in
  "exec -n upstream deploy/fake-provider -- sh -c "*) echo "v1 $(cat "$STUB_DIR/cpu.counter")";;
  "top pod -n k6 --no-headers") echo "k6-ai-benchmark-1-abcde $(cat "$STUB_DIR/loadgen.milli" 2>/dev/null || echo 100)m 100Mi";;
  "get pods -n k6 --no-headers") echo "k6-ai-benchmark-1-abcde 1/1 Running 0 1m";;
  get\ pod\ -n\ k6*jsonpath*) echo -n "Succeeded";;
  logs\ -n\ k6*) cat "$STUB_DIR/k6.out";;
  delete\ testrun*) ;;
  *) exit 1;;
esac
'''


class Orchestration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.T = Path(self._tmp.name)
        (self.T / "run_ai_benchmark.sh").write_text(BENCH_STUB)
        (self.T / "kubectl").write_text(KUBECTL_STUB)
        for f in ("run_ai_benchmark.sh", "kubectl"):
            (self.T / f).chmod((self.T / f).stat().st_mode | stat.S_IEXEC)
        (self.T / "cpu.counter").write_text("0")

    def run_cli(self, ceiling=3000, upstream_cores=0.98, loadgen_milli=5000, steps="200,1000,3000,6000,8000", *extra):
        (self.T / "model.json").write_text(json.dumps({"ceiling": ceiling, "upstream_peak_cores": upstream_cores,
                                                       "loadgen_millicores": loadgen_milli}))
        (self.T / "cpu.counter").write_text("0")
        out = self.T / "out"
        env = {**os.environ, "STUB_DIR": str(self.T), "PYTHONDONTWRITEBYTECODE": "1"}
        r = subprocess.run([sys.executable, "-B", str(K6_TESTS / "harness_calibration.py"), "--steps", steps,
                            "--step-duration", "5s", "--out-dir", str(out), "--kubectl", str(self.T / "kubectl"),
                            "--bench-dir", str(self.T), "--settle-seconds", "0", "--poll-seconds", "0.01",
                            "--top-interval", "0.02", *extra], env=env, capture_output=True, text=True)
        return r, out

    def test_finds_the_ceiling_and_blames_the_upstream(self):
        r, out = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = json.loads((out / "calibration.json").read_text())
        self.assertEqual([s["target_rps"] for s in data["steps"]], [200, 1000, 3000, 6000])   # stopped at the first failure
        self.assertEqual(data["summary"]["harness_ceiling_rps"], 3000)
        self.assertEqual(data["summary"]["limited_by"], "upstream")
        last = data["steps"][-1]
        self.assertAlmostEqual(last["upstream_cpu_cores"], 0.98, places=2)       # CPU seconds / step duration
        self.assertEqual(last["loadgen_cpu_cores"], 5.0)
        self.assertTrue((out / "calibration.md").exists())
        self.assertTrue((out / "step_6000.log").exists())
        self.assertIn("Harness ceiling: 3000 rps", r.stdout)

    def test_blames_the_load_generator_when_the_upstream_is_idle(self):
        r, out = self.run_cli(upstream_cores=0.2, loadgen_milli=55000)
        data = json.loads((out / "calibration.json").read_text())
        self.assertEqual(data["summary"]["limited_by"], "load generator")

    def test_no_stop_runs_every_step(self):
        r, out = self.run_cli(3000, 0.98, 5000, "200,1000,3000,6000,8000", "--no-stop")
        data = json.loads((out / "calibration.json").read_text())
        self.assertEqual(len(data["steps"]), 5)
        self.assertEqual(data["summary"]["harness_ceiling_rps"], 3000)

    def test_nothing_fails_when_the_ceiling_is_high(self):
        r, out = self.run_cli(ceiling=10 ** 6, steps="200,500")
        data = json.loads((out / "calibration.json").read_text())
        self.assertFalse(data["summary"]["found_a_limit"])
        self.assertIn("at least", r.stdout)

    def test_bad_arguments(self):
        for steps in ("", "500,200", "0,100", "100,100", "a,b"):
            r, _ = self.run_cli(steps=steps)
            self.assertEqual(r.returncode, 2, steps)

    def test_an_unreachable_cluster_does_not_crash_the_sweep(self):
        (self.T / "model.json").write_text("{}")
        out = self.T / "out2"
        r = subprocess.run([sys.executable, "-B", str(K6_TESTS / "harness_calibration.py"), "--steps", "200",
                            "--step-duration", "5s", "--out-dir", str(out), "--kubectl", "/nonexistent/kubectl",
                            "--bench-dir", str(self.T), "--bench-script", "does-not-exist.sh",
                            "--settle-seconds", "0", "--poll-seconds", "0.001", "--max-polls", "2", "--top-interval", "0.01"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads((out / "calibration.json").read_text())
        self.assertFalse(data["steps"][0]["passed"])
        self.assertIn("no k6 summary", data["steps"][0]["reasons"][0])


if __name__ == "__main__":
    unittest.main()
