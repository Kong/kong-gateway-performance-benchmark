"""Tests for the request-size x response-mode arrangement in generate_perf_report.py (AG-2333)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
try:
    import generate_perf_report as gpr
except ImportError as exc:          # matplotlib / numpy missing: the report cannot be built here either
    gpr = None
    IMPORT_ERROR = exc


def est(scenario, fixture, p95=1.0, p99=2.0):
    return {"scenario": scenario, "fixture": fixture, "p95_delta_ms": p95, "p99_delta_ms": p99,
            "gateway_scenario_key": f"{scenario}__{fixture}"}


@unittest.skipIf(gpr is None, "generate_perf_report needs matplotlib")
class SizeMatrix(unittest.TestCase):
    def test_a_run_without_request_size_tiers_has_no_size_section(self):
        old = [est("token-chat-openai", "short"), est("stream-openai", "short"), est("stream-openai", "mlflow50")]
        self.assertIsNone(gpr.size_matrix(old))
        self.assertIsNone(gpr.size_matrix([]))

    def test_rows_follow_the_size_order_and_hold_both_modes(self):
        rows = [est("token-chat-openai", "short"), est("stream-openai", "short")]
        for size in ("req16k", "req1k", "req10k", "req4k"):       # input order does not matter
            rows += [est("token-chat-openai", size, p99=1.0), est("stream-openai", size, p99=3.0)]
        blocks = gpr.size_matrix(rows)
        self.assertEqual([b["track"] for b in blocks], ["core"])  # no mlflow50 data, so no mlflow50 block
        tiers = [r["tier"] for r in blocks[0]["rows"]]
        self.assertEqual(tiers, ["base", "req1k", "req4k", "req10k", "req16k"])
        row = blocks[0]["rows"][3]
        self.assertEqual(row["buffered"]["p99_delta_ms"], 1.0)
        self.assertEqual(row["sse"]["p99_delta_ms"], 3.0)

    def test_the_mlflow50_track_uses_its_own_fixtures(self):
        rows = [est("token-chat-openai", "mlflow50"), est("stream-openai", "mlflow50"),
                est("token-chat-openai", "mlflow50-req4k"), est("stream-openai", "req4k")]
        blocks = {b["track"]: b for b in gpr.size_matrix(rows)}
        self.assertEqual(set(blocks), {"core", "mlflow50"})
        ml = {r["tier"]: r for r in blocks["mlflow50"]["rows"]}
        self.assertIsNotNone(ml["req4k"]["buffered"])
        self.assertIsNone(ml["req4k"]["sse"])                      # SSE has no mlflow50-req4k entry here
        core = {r["tier"]: r for r in blocks["core"]["rows"]}
        self.assertIsNotNone(core["req4k"]["sse"])
        self.assertIsNone(core["req4k"]["buffered"])

    def test_a_tier_with_no_data_is_left_out(self):
        rows = [est("token-chat-openai", "short"), est("token-chat-openai", "req4k")]
        tiers = [r["tier"] for r in gpr.size_matrix(rows)[0]["rows"]]
        self.assertEqual(tiers, ["base", "req4k"])


if __name__ == "__main__":
    unittest.main()
