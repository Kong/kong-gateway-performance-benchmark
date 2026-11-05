"""Runs tests/k6_shared_check.js with k6 (skipped when k6 is not installed)."""
import shutil
import subprocess
import unittest
from pathlib import Path

K6_TESTS = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("k6"), "k6 is not installed")
class K6SharedLibrary(unittest.TestCase):
    def test_status_taxonomy_and_summary_lines(self):
        r = subprocess.run(["k6", "run", "--quiet", "tests/k6_shared_check.js"], cwd=K6_TESTS,
                           capture_output=True, text=True)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("K6_SHARED_CHECK_OK", out)


if __name__ == "__main__":
    unittest.main()
