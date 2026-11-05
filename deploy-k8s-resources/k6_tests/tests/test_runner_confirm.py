"""Integration test for the confirmation re-run in run_release_baseline.sh.

The real runner, planner, aggregator and comparator run in a temp directory; only kubectl and
run_ai_benchmark.sh are stubs, so the numbers of every run are controlled exactly.
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

K6_TESTS = Path(__file__).resolve().parent.parent
COPY = ["run_release_baseline.sh", "release_matrix.py", "aggregate_release.py", "compare_release.py",
        "collect_env.py", "cpu_sample.py", "release_gates.yaml", "benchmark_config.yaml"]

MATRIX = """entries:
  - {scenario: token-chat-openai, fixture: short, load: 10, track: core, group: pair}
  - {scenario: direct-token-chat-openai, fixture: short, load: 10, track: core, group: pair}
  - {scenario: routing-ewma, fixture: short, load: 10, track: core}
"""

BENCH_STUB = r'''#!/bin/bash
# stands in for run_ai_benchmark.sh: writes the k6 summary the fake "pod" will return
python3 - "$1" <<'PY'
import json, os, sys
T, scenario, mode, reps = os.environ["STUB_DIR"], sys.argv[1], os.environ["STUB_MODE"], int(os.environ["REPEATS"])
path = f"{T}/calls.json"
calls = json.load(open(path)) if os.path.exists(path) else {}
calls[scenario] = calls.get(scenario, 0) + 1
json.dump(calls, open(path, "w"))
down_after = int(os.environ.get("STUB_DOWN_AFTER", "0"))
if down_after and sum(calls.values()) >= down_after:
    open(f"{T}/cluster.down", "w").write("x")       # the cluster becomes unreachable once this run is over
n = calls[scenario]
base = {"token-chat-openai": 614.5, "direct-token-chat-openai": 613.4, "routing-ewma": 614.8}[scenario]
kind, _, when = mode.partition("_")        # normal | regress_once | regress_always | breach_once | breach_always
hit = scenario == "token-chat-openai" and kind in ("regress", "breach") and (when == "always" or n <= reps)
# regress: overhead 1.1 -> 4.6 ms (+3.5, over the +3 gate, under the 5 ms budget; +0.6% total latency)
# breach: +30 ms (over the budget as well)
p95 = base + ((3.5 if kind == "regress" else 30.0) if hit else 0.0)
open(f"{T}/k6.out", "w").write(f"""AI benchmark summary: stub
http_reqs/s: 9.9
iterations total: 900
dropped_iterations total: 0
http_req_duration p95(ms): {p95}
http_req_duration p99(ms): {p95 + 1.0}
http_req_duration avg(ms): {p95}
http_req_duration med(ms): {p95}
http_req_duration p90(ms): {p95}
http_req_duration max(ms): {p95 + 2.0}
http_req_failed(%): 0.00
checks pass rate(%): 100.00
http status counts: 2xx=900 3xx=0 429=0 4xx=0 5xx=0 timeout=0 conn_err=0
""")
PY
'''

KUBECTL_STUB = r'''#!/bin/bash
[ -e "$STUB_DIR/cluster.down" ] && exit 1      # credentials expired: every call fails
case "$*" in
  "get ns k6 -o name") echo namespace/k6;;
  "get pods -n k6 --no-headers") echo "k6-ai-benchmark-1-abcde 1/1 Running 0 1m";;
  get\ pod\ -n\ k6*jsonpath*) echo -n "Succeeded";;
  logs\ -n\ k6*) cat "$STUB_DIR/k6.out";;
  *) exit 1;;
esac
'''


class RunnerConfirm(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.T = Path(self._tmp.name)
        for name in COPY:
            shutil.copy(K6_TESTS / name, self.T / name)
        (self.T / "matrix.yaml").write_text(MATRIX)
        (self.T / "run_ai_benchmark.sh").write_text(BENCH_STUB)
        (self.T / "bin").mkdir()
        (self.T / "bin" / "kubectl").write_text(KUBECTL_STUB)
        for f in (self.T / "run_ai_benchmark.sh", self.T / "bin" / "kubectl"):
            f.chmod(f.stat().st_mode | stat.S_IEXEC)

    def run_release(self, version, mode, **extra):
        (self.T / "calls.json").unlink(missing_ok=True)
        env = {**os.environ, "PATH": f"{self.T / 'bin'}:{os.environ['PATH']}", "STUB_DIR": str(self.T),
               "STUB_MODE": mode, "MATRIX_FILE": str(self.T / "matrix.yaml"), "VERSION": version,
               "REPEATS": "3", "CAPTURED_AT": "2026-01-01T00:00:00Z", "DRIVER_SETTLE_SECONDS": "0",
               "UPSTREAM_SETTLE_SECONDS": "0", "PYTHONDONTWRITEBYTECODE": "1", **extra}
        r = subprocess.run(["bash", "run_release_baseline.sh"], cwd=self.T, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-800:])
        calls = json.loads((self.T / "calls.json").read_text())
        dirs = sorted(p for p in (self.T / "results" / "releases").glob(f"{version}_*") if p.is_dir())
        main = next(p for p in dirs if not p.name.endswith("-confirm"))
        confirm = next((p for p in dirs if p.name.endswith("-confirm")), None)
        return r.stdout, calls, main, confirm

    def run_raw(self, version, mode="normal", **extra):
        (self.T / "calls.json").unlink(missing_ok=True)
        (self.T / "cluster.down").unlink(missing_ok=True)
        env = {**os.environ, "PATH": f"{self.T / 'bin'}:{os.environ['PATH']}", "STUB_DIR": str(self.T),
               "STUB_MODE": mode, "MATRIX_FILE": str(self.T / "matrix.yaml"), "VERSION": version,
               "REPEATS": "3", "CAPTURED_AT": "2026-01-01T00:00:00Z", "DRIVER_SETTLE_SECONDS": "0",
               "UPSTREAM_SETTLE_SECONDS": "0", "PYTHONDONTWRITEBYTECODE": "1", **extra}
        r = subprocess.run(["bash", "run_release_baseline.sh"], cwd=self.T, env=env, capture_output=True, text=True)
        main = next(iter(sorted((self.T / "results" / "releases").glob(f"{version}_*"))), None)
        return r, main

    def test_a_cluster_that_becomes_unreachable_stops_the_run_but_keeps_what_was_collected(self):
        # Seen on the real cluster: the SSO session expired in the middle of a long run, kubectl failed, the
        # runner crashed on an unset variable and nothing was aggregated.
        self.baseline()
        r, main = self.run_raw("cut", STUB_DOWN_AFTER="4")
        self.assertEqual(r.returncode, 3, r.stdout[-800:] + r.stderr[-800:])
        self.assertIn("kubectl cannot reach the cluster", r.stderr)
        self.assertIn("RUN INCOMPLETE", r.stderr)
        self.assertNotIn("unbound variable", r.stderr)
        logs = sorted(p.name for p in main.glob("*__run*.log") if not p.name.endswith(".driver.log"))
        self.assertEqual(len(logs), 4)                               # four runs were started before it went dark
        m = json.loads((main / "metrics.json").read_text())["scenarios"]   # ... and what was collected was aggregated
        self.assertEqual(sum(v["repeats"] for v in m.values()), 3)  # the fourth died with the cluster: empty, ignored
        self.assertIn("became unreachable while waiting", r.stderr)
        self.assertTrue((main / "verdict.json").exists())
        self.assertFalse((self.T / "results" / "releases" / f"{main.name}-confirm").exists())   # no re-run on a dead cluster

    def test_an_empty_summary_is_warned_about_and_not_counted_as_a_repeat(self):
        self.baseline()
        # the cluster answers the probe but the pod log comes back empty for one run
        stub = self.T / "run_ai_benchmark.sh"
        stub.write_text(BENCH_STUB.replace('open(f"{T}/k6.out", "w").write(f"""',
                                           'open(f"{T}/k6.out", "w").write("" if (scenario == "routing-ewma" and n == 2) else f"""'))
        r, main = self.run_raw("empty")
        self.assertEqual(r.returncode, 0, r.stdout[-600:] + r.stderr[-400:])
        self.assertIn("is empty", r.stdout)
        self.assertIn("ignoring routing-ewma__run2.log: empty summary", r.stdout + r.stderr)
        m = json.loads((main / "metrics.json").read_text())["scenarios"]
        self.assertEqual(m["routing-ewma"]["repeats"], 2)
        self.assertEqual(m["token-chat-openai"]["repeats"], 3)

    def test_an_unreachable_cluster_at_the_start_fails_fast_without_any_run(self):
        (self.T / "calls.json").unlink(missing_ok=True)
        (self.T / "cluster.down").write_text("x")
        env = {**os.environ, "PATH": f"{self.T / 'bin'}:{os.environ['PATH']}", "STUB_DIR": str(self.T), "STUB_MODE": "normal",
               "MATRIX_FILE": str(self.T / "matrix.yaml"), "VERSION": "dead", "REPEATS": "3", "DRIVER_SETTLE_SECONDS": "0",
               "PYTHONDONTWRITEBYTECODE": "1"}
        r = subprocess.run(["bash", "run_release_baseline.sh"], cwd=self.T, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)
        self.assertFalse((self.T / "calls.json").exists())           # not a single benchmark was started

    def baseline(self):
        out, calls, main, confirm = self.run_release("base", "normal")
        self.assertIsNone(confirm)
        self.assertTrue((self.T / "results" / "releases" / "baseline.json").exists())
        return main

    def verdict(self, main):
        return json.loads((main / "verdict.json").read_text())

    def test_a_regression_that_does_not_reproduce_is_noise(self):
        self.baseline()
        out, calls, main, confirm = self.run_release("cur", "regress_once")
        # first pass flagged it, so only the flagged scenario and its direct control were re-run
        self.assertEqual(calls, {"token-chat-openai": 6, "direct-token-chat-openai": 6, "routing-ewma": 3})
        self.assertIsNotNone(confirm)
        self.assertEqual(sorted(p.name.split("__run")[0] for p in confirm.glob("*__run*.log")
                                if not p.name.endswith(".driver.log")),
                         ["direct-token-chat-openai"] * 3 + ["token-chat-openai"] * 3)
        self.assertTrue((main / "report.first-pass.md").exists())
        first = json.loads((main / "verdict.first-pass.json").read_text())
        self.assertEqual(first["overall"], "REGRESSION")           # what the first pass said
        v = self.verdict(main)
        self.assertEqual(v["regressions"], [])                      # nothing reproduced
        self.assertTrue(v["unconfirmed"]["regressions"])
        self.assertNotEqual(v["overall"], "REGRESSION")
        self.assertIn("confirmation run for:", out)

    def test_a_regression_that_reproduces_is_confirmed(self):
        self.baseline()
        out, calls, main, confirm = self.run_release("cur", "regress_always")
        v = self.verdict(main)
        self.assertEqual(v["overall"], "REGRESSION")
        self.assertTrue(any(r["scenario"] == "token-chat-openai" and r["metric"] == "overhead_p95_ms"
                            for r in v["regressions"]))
        self.assertEqual(calls["routing-ewma"], 3)                 # untouched scenarios are not repeated
        self.assertEqual(calls["token-chat-openai"], 6)

    def test_an_overhead_budget_breach_that_does_not_reproduce_is_noise_too(self):
        self.baseline()
        out, calls, main, confirm = self.run_release("cur", "breach_once")
        first = json.loads((main / "verdict.first-pass.json").read_text())
        self.assertEqual(first["overall"], "ABSOLUTE_SLO_FAIL")     # +30 ms is over the 5 ms budget
        self.assertEqual(calls["token-chat-openai"], 6)             # ... and it triggered the re-run
        v = self.verdict(main)
        self.assertEqual(v["absolute_slo_violations"], [])
        self.assertTrue(v["unconfirmed"]["slo_violations"])
        self.assertNotIn(v["overall"], ("ABSOLUTE_SLO_FAIL", "REGRESSION"))

    def test_an_overhead_budget_breach_that_reproduces_stands(self):
        self.baseline()
        out, calls, main, confirm = self.run_release("cur", "breach_always")
        v = self.verdict(main)
        self.assertEqual(v["overall"], "ABSOLUTE_SLO_FAIL")
        self.assertTrue(any(x["metric"] == "overhead_p95_ms" for x in v["absolute_slo_violations"]))

    def test_confirm_rerun_false_keeps_the_first_pass_verdict(self):
        self.baseline()
        out, calls, main, confirm = self.run_release("cur", "regress_once", CONFIRM_RERUN="false")
        self.assertIsNone(confirm)
        self.assertEqual(calls["token-chat-openai"], 3)
        self.assertEqual(self.verdict(main)["overall"], "REGRESSION")
        self.assertFalse((main / "report.first-pass.md").exists())

    def test_too_many_flagged_scenarios_skips_the_rerun(self):
        self.baseline()
        out, calls, main, confirm = self.run_release("cur", "breach_always", CONFIRM_MAX_SCENARIOS="0")
        self.assertIsNone(confirm)
        self.assertIn("not re-running", out)
        self.assertEqual(self.verdict(main)["overall"], "ABSOLUTE_SLO_FAIL")

    def test_nothing_flagged_means_no_rerun(self):
        self.baseline()
        out, calls, main, confirm = self.run_release("cur", "normal")
        self.assertIsNone(confirm)
        self.assertEqual(calls, {"token-chat-openai": 3, "direct-token-chat-openai": 3, "routing-ewma": 3})
        self.assertNotIn("confirmation run", out)

    def test_the_first_release_has_no_baseline_and_no_confirmation(self):
        out, calls, main, confirm = self.run_release("first", "regress_always")
        self.assertIsNone(confirm)
        self.assertEqual(calls["token-chat-openai"], 3)

    def test_only_keys_runs_just_those_scenarios_and_their_direct_controls(self):
        out, calls, main, confirm = self.run_release("only", "normal", ONLY_KEYS="token-chat-openai")
        self.assertEqual(calls, {"token-chat-openai": 3, "direct-token-chat-openai": 3})   # routing-ewma not run
        self.assertIn("scenarios=2", out)

    def test_only_keys_with_a_singleton_runs_only_that_one(self):
        out, calls, main, confirm = self.run_release("only2", "normal", ONLY_KEYS="routing-ewma")
        self.assertEqual(calls, {"routing-ewma": 3})

    def test_a_stale_confirm_directory_from_an_earlier_attempt_is_not_mixed_in(self):
        self.baseline()
        self.run_release("cur", "regress_always")
        stale = next((self.T / "results" / "releases").glob("cur_*-confirm"))
        (stale / "routing-ewma__run1.log").write_text("iterations total: 1\nhttp_req_duration p95(ms): 1\n")
        self.run_release("cur", "regress_always")
        names = {p.name.split("__run")[0] for p in stale.glob("*__run*.log") if not p.name.endswith(".driver.log")}
        self.assertEqual(names, {"token-chat-openai", "direct-token-chat-openai"})


if __name__ == "__main__":
    unittest.main()
