"""Manifest generation of run_ai_benchmark.sh for the scenarios added with the calibration work.

Needs yq (mikefarah). yq installed as a snap cannot read /tmp, so the working directory lives under $HOME.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

K6_TESTS = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("yq"), "yq is not installed")
class CalibrationManifest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="aigwtest.", dir=str(Path.home()))
        self.addCleanup(self._tmp.cleanup)
        self.T = Path(self._tmp.name)
        for name in ("run_ai_benchmark.sh", "k6-ai-benchmark-test.yaml"):
            shutil.copy(K6_TESTS / name, self.T / name)
        (self.T / "bin").mkdir()
        stub = self.T / "bin" / "kubectl"
        stub.write_text("#!/bin/bash\nexit 0\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    def env_values(self, *args):
        (self.T / "k6-ai-benchmark-test-temp.yaml").unlink(missing_ok=True)
        env = {**os.environ, "SKIP_EKS_ISOLATION_CHECK": "true", "PATH": f"{self.T / 'bin'}:{os.environ['PATH']}"}
        r = subprocess.run(["bash", "run_ai_benchmark.sh", *args], cwd=self.T, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout[-800:] + r.stderr[-400:])
        out = subprocess.run(["yq", "-r", '.spec.runner.env[] | .name + "=" + .value',
                              str(self.T / "k6-ai-benchmark-test-temp.yaml")], capture_output=True, text=True)
        return dict(line.split("=", 1) for line in out.stdout.splitlines()), subprocess.run(
            ["yq", "-r", ".spec.script.configMap.file", str(self.T / "k6-ai-benchmark-test-temp.yaml")],
            capture_output=True, text=True).stdout.strip()

    def test_k6_never_gets_more_preallocated_vus_than_the_maximum(self):
        # k6 refuses to start when preAllocatedVUs > maxVUs; at 8000 rps the pre-allocation is 2050
        for rps in (200, 2000, 7800, 8000, 20000, 60000):
            env, script = self.env_values("direct-calibration", "zero", str(rps), "30s")
            pre, mx = int(env["K6_AI_PRE_ALLOCATED_VUS"]), int(env["K6_AI_MAX_VUS"])
            self.assertEqual(script, "k6_ai_calibration.js")
            self.assertLessEqual(pre, mx, f"{rps} rps: pre-allocated {pre} > max {mx}")
            self.assertGreaterEqual(mx, 2000)

    def test_it_targets_the_fake_provider_directly(self):
        env, _ = self.env_values("direct-calibration", "zero", "500", "30s")
        self.assertEqual(env["K6_AI_CHAT_URL"], "http://fake-provider.upstream.svc.cluster.local:8080/v1/chat/completions")
        self.assertEqual(env["K6_AI_RATE"], "500")
        self.assertEqual(env["K6_AI_DURATION"], "30s")
        self.assertEqual(env["K6_AI_TIMEOUT"], "10s")


if __name__ == "__main__":
    unittest.main()
