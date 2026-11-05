"""Unit and golden tests for compare_release.py.

Run from deploy-k8s-resources/k6_tests:  python3 -m unittest discover -s tests -v

Releases are built from synthetic per-run logs and aggregated with the real
aggregate_release.py, so these tests exercise the whole aggregate -> compare path.
"""
import contextlib
import io
import json
import statistics
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
K6_TESTS = HERE.parent
sys.path.insert(0, str(K6_TESTS))

import aggregate_release as ag  # noqa: E402
import compare_release as cr  # noqa: E402

GATES = yaml.safe_load((K6_TESTS / "release_gates.yaml").read_text())
SLO = yaml.safe_load((K6_TESTS / "benchmark_config.yaml").read_text())

LOG = """AI benchmark summary: x
http_reqs/s: {rps}
iterations total: {it}
dropped_iterations total: {dr}
http_req_duration p95(ms): {p95}
http_req_duration p99(ms): {p99}
http_req_duration avg(ms): {p95}
http_req_duration med(ms): {p95}
http_req_duration p90(ms): {p95}
http_req_duration max(ms): {p99}
http_req_failed(%): {err}
checks pass rate(%): 100.00
{ttft}http status counts: 2xx=900 3xx=0 429=0 4xx=0 5xx=0 timeout=0 conn_err=0
"""


def pick(v, r):
    return v[r - 1] if isinstance(v, (list, tuple)) else v


def render(spec, r):
    ttft = spec.get("ttft")
    text = LOG.format(
        rps=pick(spec.get("rps", 49.9), r), it=pick(spec.get("it", 900), r), dr=pick(spec.get("dr", 0), r),
        p95=pick(spec["p95"], r),
        p99=pick(spec["p99"], r) if "p99" in spec else pick(spec["p95"], r) + 1.0,
        err=pick(spec.get("err", 0.0), r),
        ttft="" if ttft is None else f"ai_time_to_first_token_ms p95: {pick(ttft, r)}\n")
    if spec.get("cpu") is not None:  # seconds per 1k requests, as cpu_sample.py appends it
        text += f"kong cpu: seconds=10.000 iterations=9000 seconds_per_1k={pick(spec['cpu'], r)}\n"
    if spec.get("upstream"):  # (started, completed, aborted, active), as the runner appends it
        started, completed, aborted, active = spec["upstream"]
        text += f"upstream stats: started={started} completed={completed} aborted={aborted} active={active}\n"
    return text


def make_release(root, name, version, scen, repeats=6, strip_stats=False, keep_logs=True, infra=None, env=None):
    d = Path(root) / name
    d.mkdir()
    for key, spec in scen.items():
        for r in range(1, repeats + 1):
            (d / f"{key}__run{r}.log").write_text(render(spec, r))
    scenarios = ag.collect_scenarios(d)
    for key, vals in (infra or {}).items():
        scenarios[key]["infra"] = vals
    if strip_stats:
        for m in scenarios.values():
            for k in cr.EXTRA_STAT_KEYS:
                m.pop(k, None)
    (d / "metrics.json").write_text(json.dumps(
        {"version": version, "captured_at": "2026-01-01T00:00:00Z", "scenarios": scenarios}))
    if not keep_logs:
        for f in d.glob("*__run*.log"):
            f.unlink()
    if env is not None:
        (d / "env.json").write_text(json.dumps(env))
    return d


def pair(gw_key, direct_key, base=614.0, overhead=1.0, drift=None, p99_extra=1.0, ttft=None, ttft_overhead=0.0):
    """Gateway/direct specs that share the same per-run environment drift."""
    drift = drift or [0.0] * 6
    direct = {"p95": [base + x for x in drift], "p99": [base + x + p99_extra for x in drift]}
    gw = {"p95": [base + x + overhead for x in drift],
          "p99": [base + x + p99_extra + overhead for x in drift]}
    if ttft is not None:
        direct["ttft"] = [ttft + x for x in drift]
        gw["ttft"] = [ttft + x + ttft_overhead for x in drift]
    return {gw_key: gw, direct_key: direct}


def quiet_main(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cr.main(argv)
    return rc, out.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def release(self, name, scen, **kw):
        return make_release(self.tmp, name, kw.pop("version", name), scen, **kw)

    def run_cmp(self, cur, base=None, confirm=None, gates=None, slo=None):
        argv = ["--current", str(cur), "--json", str(self.tmp / "v.json"), "--out", str(self.tmp / "r.md")]
        if base:
            argv += ["--baseline", str(base)]
        if confirm:
            argv += ["--confirm", str(confirm)]
        gpath = self.tmp / "gates.yaml"
        gpath.write_text(yaml.safe_dump(gates or GATES))
        spath = self.tmp / "slo.yaml"
        spath.write_text(yaml.safe_dump(slo if slo is not None else SLO))
        argv += ["--gates", str(gpath), "--slo-config", str(spath)]
        rc, out = quiet_main(argv)
        return rc, json.loads((self.tmp / "v.json").read_text()), (self.tmp / "r.md").read_text()


class Overhead(Base):
    def test_paired_overhead_cancels_run_to_run_drift(self):
        drift = [0.0, 25.0, -20.0, 40.0, -35.0, 10.0]  # huge shared drift
        cur = self.release("cur", pair("token-chat-openai", "direct-token-chat-openai", overhead=1.0, drift=drift))
        rows, _ = cr.overhead_rows(cr.load_metrics(cur), SLO)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["basis"], "paired-median")
        self.assertEqual(r["n_pairs"], 6)
        self.assertAlmostEqual(r["p95_delta_ms"], 1.0, places=3)
        self.assertAlmostEqual(r["p99_delta_ms"], 1.0, places=3)
        self.assertEqual(r["fixture"], "short")
        lo, hi = r["p95_ci95"]
        self.assertAlmostEqual(lo, 1.0, places=2)
        self.assertAlmostEqual(hi, 1.0, places=2)

    def test_fixture_suffix_pairs_with_the_same_fixture(self):
        scen = {**pair("token-chat-openai", "direct-token-chat-openai", overhead=1.0),
                **pair("token-chat-openai__medium", "direct-token-chat-openai__medium", base=2170.0, overhead=1.3)}
        rows, _ = cr.overhead_rows(cr.load_metrics(self.release("cur", scen)), SLO)
        got = {r["gateway_scenario_key"]: r["p95_delta_ms"] for r in rows}
        self.assertAlmostEqual(got["token-chat-openai"], 1.0, places=3)
        self.assertAlmostEqual(got["token-chat-openai__medium"], 1.3, places=3)

    def test_stream_pair_reports_ttft_overhead(self):
        scen = pair("stream-openai", "direct-stream-openai", base=1326.0, overhead=0.5, ttft=123.0, ttft_overhead=8.3)
        rows, _ = cr.overhead_rows(cr.load_metrics(self.release("cur", scen)), SLO)
        self.assertAlmostEqual(rows[0]["ttft_delta_ms"], 8.3, places=3)

    def test_reference_scenarios_borrow_the_direct_control(self):
        scen = {**pair("token-chat-openai", "direct-token-chat-openai", overhead=1.0),
                "routing-ewma": {"p95": 615.5, "p99": 616.5},
                "static-chat": {"p95": 122.5}}
        paired, derived = cr.overhead_rows(cr.load_metrics(self.release("cur", scen)), SLO)
        self.assertEqual([r["gateway_scenario_key"] for r in derived], ["routing-ewma"])
        self.assertEqual(derived[0]["kind"], "reference")
        self.assertEqual(derived[0]["direct_scenario_key"], "direct-token-chat-openai")
        self.assertAlmostEqual(derived[0]["p95_delta_ms"], 1.5, places=3)
        # static-chat is mapped to null: no derived latency SLO
        self.assertNotIn("static-chat", [r["gateway_scenario_key"] for r in paired + derived])

    def test_legacy_estimate_api_keeps_its_row_layout(self):
        cur = cr.load_metrics(self.release("cur", pair("embeddings-openai", "direct-embeddings-openai", base=52.0)))
        rows = cr.estimate_gateway_overhead(cur)
        for key in ("scenario", "fixture", "gateway_scenario_key", "direct_scenario_key",
                    "p95_gateway_ms", "p95_direct_ms", "p95_delta_ms", "p99_delta_ms"):
            self.assertIn(key, rows[0])


class OverheadGateSensitivity(Base):
    """Golden perturbation test: the gate must stay quiet for noise and fire for real regressions."""

    def verdict_for(self, delta_ms):
        base = self.release("base", pair("token-chat-openai", "direct-token-chat-openai", overhead=1.0))
        cur = self.release("cur", pair("token-chat-openai", "direct-token-chat-openai", overhead=1.0 + delta_ms))
        rc, v, _ = self.run_cmp(cur, base)
        return rc, v

    def test_half_a_millisecond_is_ok(self):
        rc, v = self.verdict_for(0.5)
        self.assertEqual(v["overall"], "PASS")
        self.assertEqual(rc, 0)

    def test_two_milliseconds_warns(self):
        rc, v = self.verdict_for(2.0)
        self.assertEqual(v["overall"], "WARN")
        self.assertTrue(any(w["metric"] == "overhead_p95_ms" for w in v["warnings"]))
        self.assertEqual(rc, 0)

    def test_four_milliseconds_is_a_regression(self):
        rc, v = self.verdict_for(4.0)
        self.assertEqual(v["overall"], "REGRESSION")
        self.assertTrue(any(r["metric"] == "overhead_p95_ms" for r in v["regressions"]))
        self.assertEqual(rc, 1)

    def test_total_latency_gate_alone_would_have_missed_it(self):
        # overhead 1.0 -> 4.9 ms is +0.6% on a 614 ms path: invisible to the percentage gate,
        # still inside the 5 ms budget, but a +3.9 ms jump the overhead gate must catch.
        base = self.release("base", pair("token-chat-openai", "direct-token-chat-openai", overhead=1.0))
        cur = self.release("cur", pair("token-chat-openai", "direct-token-chat-openai", overhead=4.9))
        no_overhead_gates = {k: v for k, v in GATES.items() if k != "overhead"}
        _, without, _ = self.run_cmp(cur, base, gates=no_overhead_gates)
        self.assertEqual((without["overall"], without["regressions"]), ("PASS", []))
        rc, with_gate, _ = self.run_cmp(cur, base)
        self.assertEqual(with_gate["overall"], "REGRESSION")
        self.assertEqual(rc, 1)

    def test_overhead_over_budget_is_an_slo_violation(self):
        cur = self.release("cur", pair("token-chat-openai", "direct-token-chat-openai", overhead=6.0))
        rc, v, _ = self.run_cmp(cur)
        self.assertEqual(v["verdict"], "ABSOLUTE_SLO_FAIL")
        self.assertEqual([x["metric"] for x in v["absolute_slo_violations"]], ["overhead_p95_ms"])
        self.assertEqual(rc, 1)

    def test_per_scenario_budget_override(self):
        slo = dict(SLO)
        slo["overhead_slo"] = {**SLO["overhead_slo"], "scenarios": {"token-chat-openai": {"budget_p95_ms": 8.0}}}
        cur = self.release("cur", pair("token-chat-openai", "direct-token-chat-openai", overhead=6.0))
        _, v, _ = self.run_cmp(cur, slo=slo)
        self.assertEqual(v["absolute_slo_violations"], [])

    def test_p99_and_ttft_overhead_gates(self):
        base = self.release("base", pair("stream-openai", "direct-stream-openai", base=1326.0, overhead=0.5,
                                         ttft=123.0, ttft_overhead=8.0))
        cur = self.release("cur", pair("stream-openai", "direct-stream-openai", base=1326.0, overhead=0.5,
                                       ttft=123.0, ttft_overhead=12.0))
        _, v, _ = self.run_cmp(cur, base)
        self.assertTrue(any(r["metric"] == "overhead_ttft_ms" for r in v["regressions"]))  # +4 ms >= 3 ms
        over = self.release("over", pair("stream-openai", "direct-stream-openai", base=1326.0, overhead=0.5,
                                         ttft=123.0, ttft_overhead=25.0))
        _, v, _ = self.run_cmp(over)
        self.assertIn("overhead_ttft_ms", [x["metric"] for x in v["absolute_slo_violations"]])  # budget 20 ms


class AbsoluteSlo(Base):
    def test_healthy_gateway_over_a_slow_upstream_passes(self):
        # 614 ms simulated upstream would have failed the old 300 ms absolute SLO.
        scen = {**pair("token-chat-openai", "direct-token-chat-openai", base=614.0, overhead=1.0),
                "routing-ewma": {"p95": 615.0}}
        _, v, _ = self.run_cmp(self.release("cur", scen))
        self.assertEqual(v["absolute_slo_violations"], [])
        self.assertEqual(v["verdict"], "BASELINE_PASS")

    def test_config_without_overhead_slo_keeps_the_legacy_behaviour(self):
        legacy = {k: v for k, v in SLO.items() if k not in ("overhead_slo", "legacy_absolute_latency_slo")}
        _, v, _ = self.run_cmp(self.release("cur", pair("token-chat-openai", "direct-token-chat-openai")), slo=legacy)
        self.assertIn("p95_ms", [x["metric"] for x in v["absolute_slo_violations"]])

    def test_legacy_flag_can_be_switched_on_explicitly(self):
        slo = {**SLO, "legacy_absolute_latency_slo": True}
        _, v, _ = self.run_cmp(self.release("cur", pair("token-chat-openai", "direct-token-chat-openai")), slo=slo)
        self.assertIn("p95_ms", [x["metric"] for x in v["absolute_slo_violations"]])

    def test_dropped_iterations_limit_uses_the_worst_repeat(self):
        scen = {"token-chat-openai": {"p95": 100.0, "it": [900, 900, 900, 900, 900, 880], "dr": [0, 0, 0, 0, 0, 20]}}
        _, v, _ = self.run_cmp(self.release("cur", scen))  # worst repeat: 20/900 = 2.2% > 1%
        d = [x for x in v["absolute_slo_violations"] if x["metric"] == "dropped_pct"]
        self.assertEqual(len(d), 1)
        self.assertAlmostEqual(d[0]["actual"], 20 / 900 * 100, places=2)
        ok = {"token-chat-openai": {"p95": 100.0, "it": 900, "dr": 5}}  # 0.55%
        _, v, _ = self.run_cmp(self.release("cur2", ok))
        self.assertEqual([x for x in v["absolute_slo_violations"] if x["metric"] == "dropped_pct"], [])

    def test_logs_without_dropped_iterations_are_not_penalised(self):
        cur = self.release("cur", {"token-chat-openai": {"p95": 100.0}})
        data = json.loads((cur / "metrics.json").read_text())
        data["scenarios"]["token-chat-openai"]["dropped_pct"] = None
        (cur / "metrics.json").write_text(json.dumps(data))
        _, v, _ = self.run_cmp(cur)
        self.assertEqual([x for x in v["absolute_slo_violations"] if x["metric"] == "dropped_pct"], [])

    def test_error_rate_slo_still_applies(self):
        _, v, _ = self.run_cmp(self.release("cur", {"token-chat-openai": {"p95": 100.0, "err": 2.0}}))
        self.assertIn("error_rate_pct", [x["metric"] for x in v["absolute_slo_violations"]])


class Statistic(Base):
    def test_one_cold_run_does_not_trip_the_relative_gate(self):
        base = self.release("base", {"token-chat-openai": {"p95": [100.0] * 6}})
        cur = self.release("cur", {"token-chat-openai": {"p95": [500.0, 100, 101, 99, 100, 102]}})  # run 1 cold
        rc, v, md = self.run_cmp(cur, base, slo={**SLO, "legacy_absolute_latency_slo": False})
        self.assertEqual(v["regressions"], [])
        self.assertIn("median", v["gate_statistic"])
        # the mean (200 ms) would have been a huge regression
        self.assertGreater(ag.aggregate_scenario(
            [ag.parse_log(self._write(i, p)) for i, p in enumerate([500.0, 100, 101, 99, 100, 102])])["p95_ms"], 150)

    def _write(self, i, p95):
        f = self.tmp / f"tmp{i}__run1.log"
        f.write_text(render({"p95": p95}, 1))
        return f

    def test_error_rate_uses_the_mean_so_one_bad_repeat_cannot_hide(self):
        base = self.release("base", {"token-chat-openai": {"p95": 100.0, "err": 0.0}})
        cur = self.release("cur", {"token-chat-openai": {"p95": 100.0, "err": [3.0, 0, 0, 0, 0, 0]}})
        _, v, _ = self.run_cmp(cur, base)
        self.assertTrue(any(w["metric"] == "error_rate_pct" for w in v["warnings"] + v["regressions"]))

    def test_baseline_without_per_run_stats_and_logs_falls_back_to_the_mean(self):
        base = self.release("base", {"token-chat-openai": {"p95": 100.0}}, strip_stats=True, keep_logs=False)
        cur = self.release("cur", {"token-chat-openai": {"p95": 100.0}})
        _, v, _ = self.run_cmp(cur, base)
        self.assertEqual(v["gate_statistic"], ["mean"])
        self.assertEqual(v["regressions"], [])

    def test_old_baseline_is_enriched_from_its_logs(self):
        base = self.release("base", pair("token-chat-openai", "direct-token-chat-openai", overhead=1.0),
                            strip_stats=True)
        loaded = cr.load_metrics(base)
        self.assertIn("per_run", loaded["scenarios"]["token-chat-openai"])
        cur = self.release("cur", pair("token-chat-openai", "direct-token-chat-openai", overhead=1.0))
        _, v, _ = self.run_cmp(cur, base)
        self.assertTrue(v["overhead_checks"], "baseline overhead should be computable from its logs")
        self.assertIn("median", v["gate_statistic"])

    def test_noise_inside_the_old_percentage_tolerance_is_ok(self):
        base = self.release("base", {"token-chat-openai": {"p95": 600.0}})
        cur = self.release("cur", {"token-chat-openai": {"p95": 640.0}})  # +6.7%: warn < x < fail(20)
        _, v, _ = self.run_cmp(cur, base)
        self.assertEqual(v["regressions"], [])

    def test_a_real_total_latency_regression_is_still_caught(self):
        base = self.release("base", {"token-chat-openai": {"p95": 600.0}})
        cur = self.release("cur", {"token-chat-openai": {"p95": 900.0}})  # +50%
        rc, v, _ = self.run_cmp(cur, base)
        self.assertTrue(any(r["metric"] == "p95_ms" for r in v["regressions"]))
        self.assertEqual(rc, 1)


class CpuGate(Base):
    def test_cpu_per_1k_requests_gate_fires_only_with_data(self):
        spec = {"token-chat-openai": {"p95": 100.0}}
        base = self.release("base", spec, infra={"token-chat-openai": {"cpu_seconds_per_1k_req": 0.50}})
        cur = self.release("cur", spec, infra={"token-chat-openai": {"cpu_seconds_per_1k_req": 0.70}})  # +40%
        _, v, _ = self.run_cmp(cur, base)
        self.assertTrue(any(r["metric"] == "cpu_seconds_per_1k_req" for r in v["regressions"]))
        cur_small = self.release("cur2", spec, infra={"token-chat-openai": {"cpu_seconds_per_1k_req": 0.52}})
        _, v, _ = self.run_cmp(cur_small, base)
        self.assertEqual([r for r in v["regressions"] + v["warnings"] if r["metric"] == "cpu_seconds_per_1k_req"], [])

    def test_cpu_gate_works_through_aggregate_and_compare_from_per_run_logs(self):
        base = self.release("base", {"token-chat-openai": {"p95": 100.0, "cpu": 0.50}})
        slow = self.release("slow", {"token-chat-openai": {"p95": 100.0, "cpu": 0.70}})     # +40%
        same = self.release("same", {"token-chat-openai": {"p95": 100.0, "cpu": 0.51}})     # +2%
        _, v, _ = self.run_cmp(slow, base)
        self.assertTrue(any(r["metric"] == "cpu_seconds_per_1k_req" for r in v["regressions"]))
        _, v, _ = self.run_cmp(same, base)
        self.assertEqual([r for r in v["regressions"] + v["warnings"] if r["metric"] == "cpu_seconds_per_1k_req"], [])

    def test_an_older_baseline_without_cpu_lines_is_not_gated_on_cpu(self):
        base = self.release("base", {"token-chat-openai": {"p95": 100.0}})
        cur = self.release("cur", {"token-chat-openai": {"p95": 100.0, "cpu": 9.9}})
        _, v, _ = self.run_cmp(cur, base)
        self.assertEqual([r for r in v["regressions"] + v["warnings"] if r["metric"] == "cpu_seconds_per_1k_req"], [])

    def test_missing_cpu_data_is_skipped_silently(self):
        spec = {"token-chat-openai": {"p95": 100.0}}
        _, v, _ = self.run_cmp(self.release("cur", spec), self.release("base", spec))
        self.assertEqual(v["regressions"], [])
        self.assertEqual(v["overall"], "PASS")


class EnvironmentDrift(unittest.TestCase):
    def env(self, **over):
        e = {"k6": {"version": "0.56.0"}, "config_hash": "abc", "fake_provider": {"hash": "f1"},
             "kong": {"version": "3.9", "image_digest": "sha256:aaa"},
             "nodes": [{"role": "kong", "instance_type": "c5.4xlarge", "zone": "us-west-2b", "kernel": "5.10", "os": "al2"},
                       {"role": "loadgen", "instance_type": "c5.metal", "zone": "us-west-2b", "kernel": "5.10", "os": "al2"}]}
        e.update(over)
        return e

    def test_missing_records(self):
        d = cr.env_drift(None, self.env())
        self.assertEqual((d["status"], d["missing"]), ("missing", ["current"]))
        self.assertEqual(cr.env_drift(None, None)["missing"], ["current", "baseline"])

    def test_identical_environments(self):
        self.assertEqual(cr.env_drift(self.env(), self.env())["status"], "ok")

    def test_kong_version_and_digest_differences_are_expected_and_ignored(self):
        cur = self.env(kong={"version": "3.10", "image_digest": "sha256:bbb"})
        self.assertEqual(cr.env_drift(cur, self.env())["status"], "ok")

    def test_absolute_zone_name_is_not_drift_but_the_layout_is(self):
        moved = self.env()
        for n in moved["nodes"]:
            n["zone"] = "us-west-2c"          # whole cluster in another AZ, same layout
        self.assertEqual(cr.env_drift(moved, self.env())["status"], "ok")
        split = self.env()
        split["nodes"][1]["zone"] = "us-west-2c"  # loadgen no longer shares Kong's AZ
        d = cr.env_drift(split, self.env())
        self.assertEqual(d["status"], "drift")
        self.assertTrue(any("zone_layout" in x["field"] for x in d["drift"]))

    def test_instance_type_k6_and_config_changes_are_drift(self):
        cur = self.env(k6={"version": "0.57.0"}, config_hash="zzz")
        cur["nodes"][0] = {**cur["nodes"][0], "instance_type": "c5.2xlarge"}
        fields = {x["field"] for x in cr.env_drift(cur, self.env())["drift"]}
        self.assertTrue({"k6.version", "config_hash", "nodes.kong.instance_type"} <= fields)


class EnvironmentInReport(Base):
    def test_drift_is_reported_but_does_not_change_the_verdict(self):
        spec = pair("token-chat-openai", "direct-token-chat-openai")
        e1, e2 = EnvironmentDrift().env(), EnvironmentDrift().env(k6={"version": "9.9.9"})
        base = self.release("base", spec, env=e1)
        cur = self.release("cur", spec, env=e2)
        rc, v, md = self.run_cmp(cur, base)
        self.assertEqual(v["environment"]["status"], "drift")
        self.assertEqual(v["overall"], "PASS")
        self.assertEqual(rc, 0)
        self.assertIn("Environment differs", md)

    def test_missing_env_json_is_noted(self):
        spec = pair("token-chat-openai", "direct-token-chat-openai")
        _, v, md = self.run_cmp(self.release("cur", spec), self.release("base", spec))
        self.assertEqual(v["environment"]["status"], "missing")
        self.assertIn("No env.json", md)


class Confirmation(Base):
    def specs(self, a, b):
        return {"token-chat-openai": {"p95": a}, "stream-openai": {"p95": b}}

    def test_only_reproduced_regressions_count(self):
        base = self.release("base", self.specs(100.0, 100.0))
        cur = self.release("cur", self.specs(200.0, 200.0))          # both scenarios regress
        confirm = self.release("conf", self.specs(200.0, 100.0))     # only token-chat reproduces
        rc, v, md = self.run_cmp(cur, base, confirm=confirm)
        self.assertEqual({r["scenario"] for r in v["regressions"]}, {"token-chat-openai"})
        self.assertEqual({r["scenario"] for r in v["unconfirmed"]["regressions"]}, {"stream-openai"})
        self.assertEqual(v["overall"], "REGRESSION")
        self.assertEqual(rc, 1)
        self.assertIn("Not reproduced", md)

    def test_nothing_reproduced_means_noise(self):
        base = self.release("base", self.specs(100.0, 100.0))
        cur = self.release("cur", self.specs(200.0, 200.0))
        confirm = self.release("conf", self.specs(100.0, 100.0))
        rc, v, _ = self.run_cmp(cur, base, confirm=confirm)
        self.assertEqual(v["regressions"], [])
        # p95 and p99 both regressed in each of the two scenarios
        self.assertEqual({r["scenario"] for r in v["unconfirmed"]["regressions"]},
                         {"token-chat-openai", "stream-openai"})
        self.assertEqual(v["overall"], "PASS")
        self.assertEqual(rc, 0)

    def test_overhead_budget_violations_are_confirmable_but_error_rate_is_not(self):
        base = self.release("base", pair("token-chat-openai", "direct-token-chat-openai", overhead=1.0))
        breach = {**pair("token-chat-openai", "direct-token-chat-openai", overhead=30.0)}
        breach["token-chat-openai"]["err"] = 5.0                      # a real error problem as well
        cur = self.release("cur", breach)
        calm = self.release("conf", pair("token-chat-openai", "direct-token-chat-openai", overhead=1.0))
        rc, v, _ = self.run_cmp(cur, base, confirm=calm)
        metrics = {x["metric"] for x in v["absolute_slo_violations"]}
        self.assertIn("error_rate_pct", metrics)                      # never washed away
        self.assertNotIn("overhead_p95_ms", metrics)                  # did not reproduce
        self.assertIn("overhead_p95_ms", {x["metric"] for x in v["unconfirmed"]["slo_violations"]})
        self.assertEqual(v["overall"], "ABSOLUTE_SLO_FAIL")           # because of the errors
        self.assertEqual(rc, 1)

    def test_a_reproduced_overhead_breach_stands(self):
        base = self.release("base", pair("token-chat-openai", "direct-token-chat-openai", overhead=1.0))
        cur = self.release("cur", pair("token-chat-openai", "direct-token-chat-openai", overhead=30.0))
        again = self.release("conf", pair("token-chat-openai", "direct-token-chat-openai", overhead=28.0))
        _, v, _ = self.run_cmp(cur, base, confirm=again)
        self.assertIn("overhead_p95_ms", {x["metric"] for x in v["absolute_slo_violations"]})
        self.assertEqual(v["unconfirmed"]["slo_violations"], [])

    def test_without_confirm_the_field_is_null(self):
        base = self.release("base", self.specs(100.0, 100.0))
        _, v, _ = self.run_cmp(self.release("cur", self.specs(100.0, 100.0)), base)
        self.assertIsNone(v["unconfirmed"])

    def test_absolute_slo_failures_are_not_subject_to_confirmation(self):
        base = self.release("base", {"token-chat-openai": {"p95": 100.0}})
        cur = self.release("cur", {"token-chat-openai": {"p95": 100.0, "err": 5.0}})
        confirm = self.release("conf", {"token-chat-openai": {"p95": 100.0}})
        rc, v, _ = self.run_cmp(cur, base, confirm=confirm)
        self.assertEqual(v["overall"], "ABSOLUTE_SLO_FAIL")
        self.assertEqual(rc, 1)


class VerdictJson(Base):
    def test_overhead_budgets_are_exported_for_reports(self):
        spec = pair("token-chat-openai", "direct-token-chat-openai")
        _, v, _ = self.run_cmp(self.release("cur", spec), self.release("base", spec))
        self.assertEqual(v["overhead_budgets"], {"p95_ms": 5.0, "p99_ms": 10.0, "ttft_p95_ms": 20.0})
        _, first, _ = self.run_cmp(self.release("cur2", spec))
        self.assertEqual(first["overhead_budgets"], v["overhead_budgets"])

    def test_budgets_are_null_for_a_config_without_overhead_slo(self):
        legacy = {k: val for k, val in SLO.items() if k not in ("overhead_slo", "legacy_absolute_latency_slo")}
        spec = pair("token-chat-openai", "direct-token-chat-openai")
        _, v, _ = self.run_cmp(self.release("cur", spec), self.release("base", spec), slo=legacy)
        self.assertIsNone(v["overhead_budgets"])

    def test_budget_overrides_in_the_config_are_reflected(self):
        slo = {**SLO, "overhead_slo": {**SLO["overhead_slo"], "budget_p95_ms": 7.5}}
        _, v, _ = self.run_cmp(self.release("cur", pair("token-chat-openai", "direct-token-chat-openai")), slo=slo)
        self.assertEqual(v["overhead_budgets"]["p95_ms"], 7.5)


class DisconnectPropagation(Base):
    def verdict(self, spec, name="cur"):
        _, v, md = self.run_cmp(self.release(name, {"stream-openai-disconnect": spec}))
        return v, md

    def test_cancel_reaching_the_upstream_passes(self):
        v, _ = self.verdict({"p95": 300.0, "upstream": (100, 0, 100, 0)})
        self.assertEqual([x for x in v["absolute_slo_violations"] if x["metric"] == "disconnect_propagation_pct"], [])

    def test_gateway_that_keeps_streaming_for_a_gone_client_violates_the_slo(self):
        v, _ = self.verdict({"p95": 300.0, "upstream": (100, 80, 20, 0)})
        hit = [x for x in v["absolute_slo_violations"] if x["metric"] == "disconnect_propagation_pct"]
        self.assertEqual(len(hit), 1)
        self.assertAlmostEqual(hit[0]["actual"], 20.0, places=1)
        self.assertEqual(hit[0]["threshold"], 95.0)
        self.assertIn("completed=480", hit[0]["detail"])  # 6 repeats x 80
        self.assertEqual(v["verdict"], "ABSOLUTE_SLO_FAIL")

    def test_just_above_the_floor_passes_and_just_below_fails(self):
        ok, _ = self.verdict({"p95": 300.0, "upstream": (1000, 40, 960, 0)}, "ok")      # 96%
        bad, _ = self.verdict({"p95": 300.0, "upstream": (1000, 60, 940, 0)}, "bad")    # 94%
        self.assertEqual([x for x in ok["absolute_slo_violations"] if x["metric"] == "disconnect_propagation_pct"], [])
        self.assertEqual(len([x for x in bad["absolute_slo_violations"] if x["metric"] == "disconnect_propagation_pct"]), 1)

    def test_missing_stats_are_reported_as_not_checked_instead_of_passing_silently(self):
        _, v, md = self.run_cmp(self.release("cur", {"stream-openai-disconnect": {"p95": 300.0}}),
                                self.release("base", {"stream-openai-disconnect": {"p95": 300.0}}))
        self.assertEqual(len(v["notes"]), 1)
        self.assertIn("stream-openai-disconnect", v["notes"][0])
        self.assertIn("NOT checked", v["notes"][0])
        self.assertIn("Not checked", md)

    def test_other_scenarios_are_not_subject_to_the_propagation_floor(self):
        _, v, _ = self.run_cmp(self.release("cur", {"token-chat-openai": {"p95": 100.0, "upstream": (100, 100, 0, 0)}}))
        self.assertEqual([x for x in v["absolute_slo_violations"] if x["metric"] == "disconnect_propagation_pct"], [])

    def test_no_floor_in_the_config_means_no_check(self):
        slo = {**SLO, "slo": {k: val for k, val in SLO["slo"].items() if k != "min_disconnect_propagation_percent"}}
        _, v, _ = self.run_cmp(self.release("cur", {"stream-openai-disconnect": {"p95": 300.0, "upstream": (100, 100, 0, 0)}}),
                               slo=slo)
        self.assertEqual([x for x in v["absolute_slo_violations"] if x["metric"] == "disconnect_propagation_pct"], [])

    def test_intended_hang_ups_do_not_count_as_errors(self):
        # the k6 script registers status 0 as expected, so error_rate stays 0 for a healthy run
        v, _ = self.verdict({"p95": 300.0, "err": 0.0, "upstream": (100, 0, 100, 0)})
        self.assertEqual([x for x in v["absolute_slo_violations"] if x["metric"] == "error_rate_pct"], [])


class FirstRelease(Base):
    def test_no_baseline_records_and_keeps_both_verdict_keys(self):
        cur = self.release("cur", pair("token-chat-openai", "direct-token-chat-openai", overhead=1.0))
        rc, v, md = self.run_cmp(cur)
        self.assertEqual((rc, v["verdict"], v["overall"]), (0, "BASELINE_PASS", "BASELINE_PASS"))
        self.assertIn("Gateway Overhead", md)
        self.assertIn("TLS", md)
        self.assertEqual(len(v["gateway_overhead_estimates"]), 1)


class RealData(unittest.TestCase):
    """Golden check against stored results; skipped when they are not on disk."""
    CUR = K6_TESTS / "results/releases/ai-2.3.0-rc.2_2026-11-04"
    BASE = K6_TESTS / "results/releases/ai-2.0.2-rc.3_2026-08-20"

    @unittest.skipUnless(CUR.exists() and BASE.exists(), "stored release results not available")
    def test_2_3_0_rc_2_vs_2_0_2_rc_3(self):
        gates = yaml.safe_load((K6_TESTS / "release_gates.non_dedicated.yaml").read_text()) \
            if (K6_TESTS / "release_gates.non_dedicated.yaml").exists() else GATES
        cur, base = cr.load_metrics(self.CUR), cr.load_metrics(self.BASE)
        res = cr.evaluate(cur, base, gates, SLO)
        self.assertEqual(res["absolute_slo_violations"], [], "the 17 simulated-upstream violations must be gone")
        self.assertEqual(res["regressions"], [])
        token = next(r for r in res["paired_overhead"]
                     if r["gateway_scenario_key"] == "token-chat-openai")
        self.assertTrue(0.9 <= token["p95_delta_ms"] <= 1.3, token)
        self.assertEqual(token["basis"], "paired-median")


if __name__ == "__main__":
    unittest.main()
