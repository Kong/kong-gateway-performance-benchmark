"""Unit tests for cpu_sample.py (Kong CPU seconds per 1k requests).

Run from deploy-k8s-resources/k6_tests:  python3 -m unittest discover -s tests -v
"""
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


def run_cli(*args):
    return subprocess.run([sys.executable, "-B", str(K6_TESTS / "cpu_sample.py"), *args],
                          capture_output=True, text=True)


class Parsing(unittest.TestCase):
    def test_cgroup_v2_is_microseconds(self):
        self.assertAlmostEqual(cs.parse_cpu_sample("v2 2500000\n"), 2.5)

    def test_cgroup_v1_is_nanoseconds(self):
        self.assertAlmostEqual(cs.parse_cpu_sample("v1 2500000000"), 2.5)

    def test_unreadable_values_are_none(self):
        for bad in ("", None, "v2", "v2 abc", "v3 100", "100", "v2 1 2", "v1 -5"):
            self.assertIsNone(cs.parse_cpu_sample(bad), repr(bad))

    def test_the_shell_snippet_really_reads_this_machines_cgroup(self):
        if not (os.path.exists("/sys/fs/cgroup/cpu.stat") or os.path.exists("/sys/fs/cgroup/cpuacct/cpuacct.usage")
                or os.path.exists("/sys/fs/cgroup/cpu,cpuacct/cpuacct.usage")):
            self.skipTest("no readable cgroup cpu accounting file here")
        out = subprocess.run(["sh", "-c", cs.SNIPPET], capture_output=True, text=True).stdout
        seconds = cs.parse_cpu_sample(out)
        self.assertIsNotNone(seconds, f"snippet printed {out!r}")
        self.assertGreaterEqual(seconds, 0)


class Formatting(unittest.TestCase):
    def test_line(self):
        line, why = cs.format_line(10.0, 22.5, 9000)
        self.assertIsNone(why)
        self.assertEqual(line, "kong cpu: seconds=12.500 iterations=9000 seconds_per_1k=1.3889")

    def test_untrustworthy_samples_are_rejected_with_a_reason(self):
        self.assertIn("backwards", cs.format_line(30.0, 5.0, 100)[1])
        self.assertIn("no iterations", cs.format_line(1.0, 2.0, 0)[1])
        self.assertIn("iterations total", cs.format_line(1.0, 2.0, None)[1])

    def test_iterations_are_read_from_the_log_not_from_dropped_iterations(self):
        text = "dropped_iterations total: 40\niterations total: 900\n"
        self.assertEqual(cs.iterations_in(text), 900)
        self.assertIsNone(cs.iterations_in("dropped_iterations total: 40\n"))


class Cli(unittest.TestCase):
    def test_record_appends_the_line(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "s__run1.log"
            log.write_text("iterations total: 4500\nhttp_req_duration p95(ms): 1\n")
            r = run_cli("record", "--before", "100", "--after", "154", "--log", str(log))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("seconds=54.000 iterations=4500 seconds_per_1k=12.0000", log.read_text())

    def test_record_refuses_without_iterations_and_leaves_the_log_alone(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "s__run1.log"
            log.write_text("http_req_duration p95(ms): 1\n")
            r = run_cli("record", "--before", "1", "--after", "2", "--log", str(log))
            self.assertEqual(r.returncode, 1)
            self.assertNotIn("kong cpu", log.read_text())
            self.assertIn("not recording", r.stderr)

    def stub(self, d, body):
        p = Path(d) / "kubectl"
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
        return str(p)

    def test_read_prints_seconds(self):
        with tempfile.TemporaryDirectory() as d:
            r = run_cli("read", "--kubectl", self.stub(d, "echo 'v1 5000000000'"))
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "5.000000"))

    def test_read_fails_cleanly_when_kubectl_fails_or_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run_cli("read", "--kubectl", self.stub(d, "exit 1")).returncode, 1)
            self.assertEqual(run_cli("read", "--kubectl", self.stub(d, "echo garbage")).returncode, 1)
        self.assertEqual(run_cli("read", "--kubectl", "/nonexistent/kubectl").returncode, 1)

    def test_read_asks_for_the_kong_proxy_container(self):
        with tempfile.TemporaryDirectory() as d:
            argv = Path(d) / "argv"
            stub = self.stub(d, f'echo "$@" > {argv}; echo "v2 1000000"')
            run_cli("read", "--kubectl", stub)
            asked = argv.read_text()
            for part in ("exec", "-n kong", "deploy/kong-kong", "-c proxy"):
                self.assertIn(part, asked)


if __name__ == "__main__":
    unittest.main()
