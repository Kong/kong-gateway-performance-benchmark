"""Unit tests for release_matrix.py (the release-matrix planner).

Run from deploy-k8s-resources/k6_tests:
    python3 -m unittest discover -s tests -v
(also collected by pytest).
"""
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
K6_TESTS = HERE.parent
sys.path.insert(0, str(K6_TESTS))

import release_matrix as rm  # noqa: E402

REAL_MATRIX = K6_TESTS / "release_matrix.yaml"

# The 22 result keys the legacy hard-coded runner produced. Guards against a
# scenario silently disappearing from the matrix (baselines depend on them).
LEGACY_KEYS = {
    "static-chat",
    "token-chat-openai", "token-chat-openai__medium",
    "direct-token-chat-openai", "direct-token-chat-openai__medium",
    "stream-openai", "stream-gemini",
    "embeddings-openai", "embeddings-openai__medium",
    "direct-embeddings-openai", "direct-embeddings-openai__medium",
    "routing-roundrobin-2", "routing-roundrobin-10", "routing-ewma", "routing-failover",
    "payload-logging",
    "token-chat-openai__mlflow50", "direct-token-chat-openai__mlflow50",
    "stream-openai__mlflow50", "direct-stream-openai__mlflow50",
    "embeddings-openai__mlflow50", "direct-embeddings-openai__mlflow50",
}


def write_matrix(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(textwrap.dedent(text))
    f.close()
    return f.name


class RealMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.all = rm.load_matrix(REAL_MATRIX)
        cls.tier2 = rm.select(cls.all, "tier2")
        cls.tier1 = rm.select(cls.all, "tier1")

    def test_keys_unique(self):
        keys = [e["key"] for e in self.all]
        self.assertEqual(len(keys), len(set(keys)))

    def test_legacy_scenarios_all_present_with_same_load(self):
        got = {e["key"] for e in self.tier2}
        self.assertTrue(LEGACY_KEYS <= got, f"missing: {sorted(LEGACY_KEYS - got)}")
        loads = {e["key"]: e["load"] for e in self.tier2}
        # streaming scenarios are VUs, the rest RPS
        for k in ("stream-openai", "stream-gemini", "stream-openai__mlflow50"):
            self.assertEqual(loads[k], 30)
        for k in ("token-chat-openai", "embeddings-openai", "static-chat"):
            self.assertEqual(loads[k], 50)
        for k in ("routing-ewma", "payload-logging"):
            self.assertEqual(loads[k], 25)

    def test_result_key_derivation_matches_legacy_naming(self):
        self.assertEqual(rm.result_key("token-chat-openai", "short"), "token-chat-openai")
        self.assertEqual(rm.result_key("token-chat-openai", "medium"), "token-chat-openai__medium")
        self.assertEqual(rm.result_key("token-chat-openai", "mlflow50"), "token-chat-openai__mlflow50")

    def test_tier1_is_subset_of_tier2(self):
        self.assertTrue({e["key"] for e in self.tier1} <= {e["key"] for e in self.tier2})

    def test_disabled_entries_are_never_planned(self):
        # none are disabled at the moment; the rule holds for any that will be
        disabled = {e["key"] for e in self.all if not e["enabled"]}
        planned = {r["key"] for r in rm.plan(self.tier2, 2)}
        self.assertFalse(disabled & planned)
        for e in self.all:
            if not e["enabled"]:
                self.assertTrue(e["pending"], f"{e['key']} is disabled without a 'pending' note")

    def test_the_scenarios_added_with_the_http_churn_and_disconnect_work_are_enabled(self):
        # they ran cleanly on the cluster (2026-11-05) and are part of the default plan
        keys = {e["key"] for e in self.tier2}
        for k in ("token-chat-openai-http", "token-chat-openai-http__mlflow50", "embeddings-openai-http",
                  "embeddings-openai-http__mlflow50", "direct-stream-gemini", "token-chat-openai-churn",
                  "token-chat-openai-http-churn", "stream-openai-disconnect"):
            self.assertIn(k, keys)
        self.assertEqual([e["key"] for e in self.all if not e["enabled"]], [])

    def test_mlflow50_toggle(self):
        no_ml = rm.select(self.all, "tier2", include_mlflow50=False)
        self.assertTrue(all(e["track"] != "mlflow50" for e in no_ml))
        self.assertEqual(len(self.tier2) - len(no_ml), 8)    # 6 original + the two HTTP-entry variants

    def test_every_gateway_has_its_direct_pair_in_the_same_group(self):
        groups = rm.build_groups(self.tier2)
        for members in groups:
            names = {m["scenario"] for m in members}
            if len(members) > 1 and not all("churn" in n for n in names):    # HTTPS-vs-HTTP churn pair has no direct twin
                self.assertTrue(any(n.startswith("direct-") for n in names),
                                f"group of >1 without a direct control: {names}")

    def test_expected_counts(self):
        # 22 legacy + direct-stream-openai + 8 added with the HTTP-entry / churn / disconnect work
        self.assertEqual(len(self.tier2), 31)
        self.assertEqual(len(self.tier1), 10)


PENDING_MATRIX = """
entries:
  - {scenario: token-chat-openai, fixture: short, load: 50, track: core, group: pair, tier1: true}
  - {scenario: token-chat-openai-http, fixture: short, load: 50, track: core, group: pair, tier1: true, enabled: false, pending: "needs a cluster smoke run"}
  - {scenario: direct-token-chat-openai, fixture: short, load: 50, track: core, group: pair, tier1: true}
  - {scenario: routing-ewma, fixture: short, load: 25, track: core}
  - {scenario: stream-openai-disconnect, fixture: short, load: 30, track: core, enabled: false, pending: "needs a cluster smoke run"}
  - {scenario: retired, fixture: short, load: 5, track: core, enabled: false}
"""


class PendingEntries(unittest.TestCase):
    """The INCLUDE_PENDING mechanism, on a synthetic matrix: the real one has no pending entry any more."""

    @classmethod
    def setUpClass(cls):
        cls.path = write_matrix(PENDING_MATRIX)
        cls.all = rm.load_matrix(cls.path)

    def test_default_plan_excludes_pending_and_retired_entries(self):
        keys = {e["key"] for e in rm.select(self.all, "tier2")}
        self.assertEqual(keys, {"token-chat-openai", "direct-token-chat-openai", "routing-ewma"})

    def test_include_pending_adds_only_entries_that_carry_a_note(self):
        keys = {e["key"] for e in rm.select(self.all, "tier2", include_pending=True)}
        self.assertEqual(keys, {"token-chat-openai", "token-chat-openai-http", "direct-token-chat-openai",
                                "routing-ewma", "stream-openai-disconnect"})
        self.assertNotIn("retired", keys)                       # disabled without a note: never planned
        self.assertEqual({e["key"] for e in rm.select(self.all, "tier1", include_pending=True)},
                         {"token-chat-openai", "token-chat-openai-http", "direct-token-chat-openai"})

    def test_pending_runs_are_flagged_and_enabled_ones_are_not(self):
        runs = rm.plan(rm.select(self.all, "tier2", include_pending=True), 1)
        self.assertEqual({r["key"] for r in runs if r["pending"]}, {"token-chat-openai-http", "stream-openai-disconnect"})
        self.assertTrue(all(not r["pending"] for r in rm.plan(rm.select(self.all, "tier2"), 2)))

    def test_a_pending_variant_joins_its_group_and_runs_adjacent(self):
        seq = [r["key"] for r in rm.plan(rm.select(self.all, "tier2", include_pending=True), 1)]
        i = seq.index("token-chat-openai")
        self.assertEqual(set(seq[i:i + 3]), {"token-chat-openai", "token-chat-openai-http", "direct-token-chat-openai"})

    def test_cli_summary_reports_the_pending_count_only_when_included(self):
        def summary(*extra):
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rm.main(["summary", "--matrix", self.path, "--repeats", "3", *extra])
            return buf.getvalue()
        self.assertNotIn("pending=", summary())
        out = summary("--include-pending", "true")
        self.assertIn("scenarios=5", out)
        self.assertIn("pending=2", out)

    def test_only_keys_and_expand_groups(self):
        entries = rm.select(self.all, "tier2", include_pending=True)
        self.assertEqual({e["key"] for e in rm.restrict(entries, ["token-chat-openai-http"])},
                         {"token-chat-openai", "token-chat-openai-http", "direct-token-chat-openai"})
        self.assertEqual({e["key"] for e in rm.restrict(entries, ["token-chat-openai-http"], expand_groups=False)},
                         {"token-chat-openai-http"})
        self.assertEqual({e["key"] for e in rm.restrict(entries, ["routing-ewma"])}, {"routing-ewma"})
        self.assertEqual(rm.restrict(entries, ["nope"]), [])


class Ordering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries = rm.select(rm.load_matrix(REAL_MATRIX), "tier2")

    def test_interleaved_is_round_major_and_complete(self):
        repeats = 4
        runs = rm.plan(self.entries, repeats)
        self.assertEqual(len(runs), len(self.entries) * repeats)
        self.assertEqual([r["round"] for r in runs], sorted(r["round"] for r in runs))
        for rnd in range(1, repeats + 1):
            keys = [r["key"] for r in runs if r["round"] == rnd]
            self.assertEqual(sorted(keys), sorted(e["key"] for e in self.entries))

    def test_pair_members_are_adjacent_and_rotate_each_round(self):
        runs = rm.plan(self.entries, 4)
        for members in rm.build_groups(self.entries):
            if len(members) < 2:
                continue
            gkeys = {m["key"] for m in members}
            first_in_round = []
            for rnd in range(1, 5):
                seq = [r["key"] for r in runs if r["round"] == rnd]
                idx = [i for i, k in enumerate(seq) if k in gkeys]
                self.assertEqual(idx, list(range(idx[0], idx[0] + len(idx))),
                                 f"group {gkeys} not adjacent in round {rnd}")
                first_in_round.append(seq[idx[0]])
            # the member that leads rotates each round: A B | B A | A B ... for a pair, A B C | B C A | ... for three
            expected = [members[(r - 1) % len(members)]["key"] for r in range(1, 5)]
            self.assertEqual(first_in_round, expected)
            self.assertEqual(len(set(first_in_round)), min(len(members), 4))

    def test_group_order_is_identical_across_rounds_by_default(self):
        runs = rm.plan(self.entries, 3)
        def group_seq(rnd):
            seq = [r["key"] for r in runs if r["round"] == rnd]
            lookup = {m["key"]: i for i, g in enumerate(rm.build_groups(self.entries)) for m in g}
            out = []
            for k in seq:
                if not out or out[-1] != lookup[k]:
                    out.append(lookup[k])
            return out
        self.assertEqual(group_seq(1), group_seq(2))
        self.assertEqual(group_seq(2), group_seq(3))

    def test_sequential_is_scenario_major(self):
        runs = rm.plan(self.entries, 3, order="sequential")
        self.assertEqual([r["key"] for r in runs[:3]], [self.entries[0]["key"]] * 3)
        self.assertEqual([r["round"] for r in runs[:3]], [1, 2, 3])
        self.assertEqual({r["key"] for r in runs}, {e["key"] for e in self.entries})

    def test_shuffle_is_seeded_complete_and_keeps_pairs_adjacent(self):
        a = rm.plan(self.entries, 3, order="shuffle", seed=7)
        b = rm.plan(self.entries, 3, order="shuffle", seed=7)
        c = rm.plan(self.entries, 3, order="shuffle", seed=8)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len(a), len(self.entries) * 3)
        for rnd in (1, 2, 3):
            seq = [r["key"] for r in a if r["round"] == rnd]
            self.assertEqual(sorted(seq), sorted(e["key"] for e in self.entries))
            for members in rm.build_groups(self.entries):
                gkeys = {m["key"] for m in members}
                idx = [i for i, k in enumerate(seq) if k in gkeys]
                self.assertEqual(idx, list(range(idx[0], idx[0] + len(idx))))

    def test_interrupted_run_leaves_equal_repeat_counts(self):
        runs = rm.plan(self.entries, 6)
        cut = runs[: len(self.entries) * 2 + 5]  # killed 5 runs into round 3
        counts = {}
        for r in cut:
            counts[r["key"]] = counts.get(r["key"], 0) + 1
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)
        self.assertEqual(set(counts), {e["key"] for e in self.entries})


class Validation(unittest.TestCase):
    def test_duplicate_result_key(self):
        p = write_matrix("""
            entries:
              - {scenario: a, fixture: short, load: 5, track: core}
              - {scenario: a, fixture: short, load: 6, track: core}
        """)
        with self.assertRaisesRegex(rm.MatrixError, "duplicate"):
            rm.load_matrix(p)

    def test_bad_track(self):
        p = write_matrix("entries:\n  - {scenario: a, fixture: short, load: 5, track: nope}\n")
        with self.assertRaisesRegex(rm.MatrixError, "track"):
            rm.load_matrix(p)

    def test_bad_load(self):
        for bad in ("0", "-3", "abc", "true", "1.5"):
            p = write_matrix(f"entries:\n  - {{scenario: a, fixture: short, load: {bad}, track: core}}\n")
            with self.assertRaisesRegex(rm.MatrixError, "load"):
                rm.load_matrix(p)

    def test_missing_field(self):
        p = write_matrix("entries:\n  - {scenario: a, fixture: short, track: core}\n")
        with self.assertRaisesRegex(rm.MatrixError, "missing"):
            rm.load_matrix(p)

    def test_unsafe_names_rejected(self):
        p = write_matrix("entries:\n  - {scenario: 'a;rm -rf', fixture: short, load: 5, track: core}\n")
        with self.assertRaisesRegex(rm.MatrixError, "characters"):
            rm.load_matrix(p)

    def test_empty_selection_is_an_error_from_the_cli(self):
        p = write_matrix("entries:\n  - {scenario: a, fixture: short, load: 5, track: core, enabled: false}\n")
        self.assertEqual(rm.main(["plan", "--matrix", p]), 2)

    def test_bad_duration(self):
        with self.assertRaises(rm.MatrixError):
            rm.parse_duration_minutes("90s")


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = rm.main(list(args))
        return rc, buf.getvalue()

    def test_summary_estimate(self):
        rc, out = self.run_cli("summary", "--repeats", "6", "--duration", "3m")
        self.assertEqual(rc, 0)
        self.assertIn("scenarios=31", out)
        self.assertIn("runs=186", out)
        self.assertIn("est_minutes=744", out)  # 186 * (3 + 1)

    def test_plan_tsv_shape(self):
        rc, out = self.run_cli("plan", "--tier", "tier1", "--repeats", "1")
        self.assertEqual(rc, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 10)
        for line in lines:
            cols = line.split("\t")
            self.assertEqual(len(cols), 6)          # round, scenario, fixture, load, key, status
            self.assertEqual(cols[5], "enabled")


if __name__ == "__main__":
    unittest.main()
