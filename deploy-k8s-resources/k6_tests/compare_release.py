#!/usr/bin/env python3
"""
compare_release.py — Release regression gate for Kong AI Gateway benchmarks.

Compares a release's aggregated metrics against a baseline release, classifies
each scenario/metric as OK / WARN / REGRESSION per the gates file, writes a
human-readable evidence report, and exits non-zero if any REGRESSION, UNSTABLE
scenario or absolute SLO violation is found (so it can gate CI / release
sign-off).

What is checked
---------------
1. Relative gate (vs baseline), per scenario: p95, p99, TTFT p95, throughput,
   error rate (and CPU seconds per 1k requests when the run carries infra data).
   The statistic is the MEDIAN across repeats when both sides have per-run stats
   (robust to one cold or noisy run), else the mean. Error rate stays on the mean
   so one bad repeat cannot hide.
2. Gateway overhead (gateway - direct upstream), paired by run number: gateway
   and direct runs of the same round run back-to-back, so their difference is
   not polluted by slow environment drift. Checked against an absolute budget
   (benchmark_config.yaml: overhead_slo) and, with a baseline, against the
   baseline's overhead (gates file: overhead).
3. Absolute SLOs: error rate, checks pass rate, dropped iterations, and the
   overhead budgets. The old absolute latency thresholds measured the simulated
   upstream delay, not the gateway; they only apply when
   `legacy_absolute_latency_slo: true` (or when the config has no
   `overhead_slo` section at all, so old config files behave as before).
4. Stability: worst cv_pct of a scenario vs the gates file.
5. Environment drift (informational): env.json of both runs, when present.
6. Optional confirmation run (--confirm DIR): a REGRESSION / UNSTABLE only counts
   when the same scenario+metric also flags in the confirmation run.

Inputs are metrics.json files produced by aggregate_release.py (schema 1 or 2).
A baseline without per-run statistics is re-aggregated from its per-run logs
when those sit next to metrics.json.

Usage:
  python3 compare_release.py --current <dir|metrics.json> --baseline <dir|metrics.json> \\
      [--gates release_gates.yaml] [--slo-config benchmark_config.yaml] \\
      [--confirm <dir|metrics.json>] [--out report.md] [--json verdict.json]
  # First release (no baseline): omit --baseline; it records and passes.

env.json (written by the environment collector, optional) is compared on:
  k6.version, config_hash, fake_provider.hash, nodes[] (role, instance_type,
  zone, kernel, os) and whether each node shares the Kong node's zone.
"""
import argparse
import json
import random
import statistics
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aggregate_release as ag  # noqa: E402

# HIGHER-is-worse metrics; throughput is the exception (lower is worse).
LATENCY_METRICS = ["p95_ms", "p99_ms", "ttft_p95_ms"]
CPU_METRIC = "cpu_seconds_per_1k_req"
RISE_METRICS = LATENCY_METRICS + [CPU_METRIC]
# Gate on the median across repeats; error rate stays on the mean.
MEDIAN_METRICS = {"p95_ms", "p99_ms", "ttft_p95_ms", "throughput_rps"}
GATE_METRICS = ["p95_ms", "p99_ms", "ttft_p95_ms", "throughput_rps", "error_rate_pct", CPU_METRIC]
EXTRA_STAT_KEYS = ("median", "iqr", "stdev", "min", "max", "per_run", "dropped_pct", "status_totals",
                   "upstream_totals")

# Used only when the SLO config has an overhead_slo section without the value.
DEFAULT_OVERHEAD_BUDGET_MS = {"p95_ms": 5.0, "p99_ms": 10.0, "ttft_p95_ms": 20.0}
OVERHEAD_METRIC_NAME = {"p95_ms": "overhead_p95_ms", "p99_ms": "overhead_p99_ms",
                        "ttft_p95_ms": "overhead_ttft_ms"}
OVERHEAD_ROW_KEY = {"p95_ms": "p95_delta_ms", "p99_ms": "p99_delta_ms", "ttft_p95_ms": "ttft_delta_ms"}
BUDGET_KEY = {"p95_ms": "budget_p95_ms", "p99_ms": "budget_p99_ms", "ttft_p95_ms": "ttft_budget_p95_ms"}


# --------------------------------------------------------------------------- names
def base_scenario_name(scenario):
    return str(scenario).split("__", 1)[0]


def scenario_fixture_suffix(scenario):
    parts = str(scenario).split("__", 1)
    return f"__{parts[1]}" if len(parts) == 2 else ""


# --------------------------------------------------------------------------- config
def load_slo_config(path_str):
    p = Path(path_str)
    if not p.exists():
        return {}
    with open(p) as f:
        return yaml.safe_load(f) or {}


def use_legacy_latency_slo(slo_cfg):
    """Absolute latency thresholds apply when asked for, or when the config
    predates the overhead_slo section (so old config files keep their behaviour)."""
    return bool(slo_cfg.get("legacy_absolute_latency_slo", "overhead_slo" not in slo_cfg))


def scenario_slo_thresholds(slo_cfg, scenario):
    scenario = base_scenario_name(scenario)
    base = slo_cfg.get("slo", {})
    overrides = slo_cfg.get("scenarios", {}).get(scenario, {})

    thresholds = {
        "max_error_rate_percent": base.get("max_error_rate_percent", 1.0),
        "min_checks_pass_rate_percent": base.get("min_checks_pass_rate_percent", 99.0),
        "max_p95_latency_ms": base.get("max_p95_latency_ms", 500),
        "max_p99_latency_ms": base.get("max_p99_latency_ms", 1000),
    }
    if "max_dropped_iterations_rate_percent" in base:
        thresholds["max_dropped_iterations_rate_percent"] = base["max_dropped_iterations_rate_percent"]
    if "min_disconnect_propagation_percent" in base:
        thresholds["min_disconnect_propagation_percent"] = base["min_disconnect_propagation_percent"]

    if "max_p95_latency_ms" in overrides:
        thresholds["max_p95_latency_ms"] = overrides["max_p95_latency_ms"]
    if "max_p99_latency_ms" in overrides:
        thresholds["max_p99_latency_ms"] = overrides["max_p99_latency_ms"]
    if "max_ttft_p95_ms" in overrides:
        thresholds["max_ttft_p95_ms"] = overrides["max_ttft_p95_ms"]

    return thresholds


def gate_for(gates, scenario, metric):
    scenario = base_scenario_name(scenario)
    g = dict(gates.get("defaults", {}).get(metric, {}))
    g.update(gates.get("scenarios", {}).get(scenario, {}).get(metric, {}))
    return g


# --------------------------------------------------------------------------- loading
def load_metrics(path_str):
    """Load metrics.json. A scenario without per-run statistics (schema 1) is
    enriched from the per-run logs next to it, when they exist."""
    p = Path(path_str)
    d = p if p.is_dir() else p.parent
    mp = p / "metrics.json" if p.is_dir() else p
    with open(mp) as f:
        data = json.load(f)
    data["_dir"] = str(d)
    missing = [s for s, m in data.get("scenarios", {}).items() if "per_run" not in m]
    if missing and any(d.glob("*__run*.log")):
        fresh = ag.collect_scenarios(d)
        for s in missing:
            if s in fresh:
                for k in EXTRA_STAT_KEYS:
                    data["scenarios"][s][k] = fresh[s][k]
    return data


def load_env(metrics):
    p = Path(metrics.get("_dir", ".")) / "env.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- statistics
def stat_value(scn, metric):
    """(value, basis): the median across repeats when available, else the mean."""
    if metric in MEDIAN_METRICS:
        v = (scn.get("median") or {}).get(metric)
        if v is not None:
            return v, "median"
    return scn.get(metric), "mean"


def metric_pair(cm, bm, metric):
    """(current, baseline, basis) for the relative gate. The median is used only
    when both sides have it, so we never compare a median to a mean."""
    if metric == CPU_METRIC:
        return (cm.get("infra") or {}).get(metric), (bm.get("infra") or {}).get(metric), "mean"
    if metric in MEDIAN_METRICS:
        c = (cm.get("median") or {}).get(metric)
        b = (bm.get("median") or {}).get(metric)
        if c is not None and b is not None:
            return c, b, "median"
    return cm.get(metric), bm.get(metric), "mean"


def bootstrap_median_ci(values, n_boot=2000, seed=12345, alpha=0.05):
    """Percentile bootstrap CI of the median; deterministic. None for < 3 values."""
    if len(values) < 3:
        return None
    rng = random.Random(seed)
    meds = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(n_boot))
    lo = meds[int(n_boot * alpha / 2)]
    hi = meds[int(n_boot * (1 - alpha / 2)) - 1]
    return [round(lo, 3), round(hi, 3)]


def overhead_of(gw, ref, metric):
    """Gateway minus reference for one latency metric.

    Preferred: paired differences of the same run number (gateway and reference
    run back-to-back in each round), summarised by their median with a bootstrap
    CI. Fallback: difference of the per-scenario statistics.
    """
    gw_runs = {r["run"]: r.get(metric) for r in gw.get("per_run") or [] if r.get("run") is not None}
    ref_runs = {r["run"]: r.get(metric) for r in ref.get("per_run") or [] if r.get("run") is not None}
    diffs = [gw_runs[k] - ref_runs[k] for k in sorted(set(gw_runs) & set(ref_runs))
             if gw_runs[k] is not None and ref_runs[k] is not None]
    if diffs:
        return {"value": round(statistics.median(diffs), 3), "basis": "paired-median",
                "n": len(diffs), "ci95": bootstrap_median_ci(diffs)}
    g, gb = stat_value(gw, metric)
    r, rb = stat_value(ref, metric)
    if g is not None and r is not None and gb != rb:
        g, r, gb = gw.get(metric), ref.get(metric), "mean"
    if g is None or r is None:
        return None
    return {"value": round(g - r, 3), "basis": f"{gb}-difference", "n": None, "ci95": None}


# --------------------------------------------------------------------------- overhead
def find_pairs(scenarios):
    """(gateway_key, direct_key) for every gateway scenario that has a
    direct-<scenario> control with the same fixture."""
    out = []
    for key in scenarios:
        base = base_scenario_name(key)
        if base.startswith("direct-"):
            continue
        direct = f"direct-{base}{scenario_fixture_suffix(key)}"
        if direct in scenarios:
            out.append((key, direct))
    return out


def reference_pairs(scenarios, slo_cfg, already):
    """Scenarios without their own direct control that borrow one, per
    overhead_slo.reference (base scenario name -> reference base name, or null
    for 'different upstream, no latency SLO')."""
    mapping = (slo_cfg.get("overhead_slo") or {}).get("reference") or {}
    paired = {g for g, _ in already}
    out = []
    for key in scenarios:
        if key in paired:
            continue
        ref_base = mapping.get(base_scenario_name(key))
        if not ref_base:
            continue
        ref_key = f"{ref_base}{scenario_fixture_suffix(key)}"
        if ref_key in scenarios:
            out.append((key, ref_key))
    return out


def overhead_row(scenarios, gw_key, ref_key, kind):
    gw, ref = scenarios[gw_key], scenarios[ref_key]
    p95 = overhead_of(gw, ref, "p95_ms")
    if p95 is None:
        return None
    p99 = overhead_of(gw, ref, "p99_ms")
    ttft = overhead_of(gw, ref, "ttft_p95_ms")
    gv, _ = stat_value(gw, "p95_ms")
    rv, _ = stat_value(ref, "p95_ms")
    suffix = scenario_fixture_suffix(gw_key)
    return {
        "scenario": base_scenario_name(gw_key),
        "fixture": suffix[2:] if suffix else "short",
        "kind": kind,
        "gateway_scenario_key": gw_key,
        "direct_scenario_key": ref_key,
        "p95_gateway_ms": gv,
        "p95_direct_ms": rv,
        "p95_delta_ms": p95["value"],
        "p99_delta_ms": None if p99 is None else p99["value"],
        "ttft_delta_ms": None if ttft is None else ttft["value"],
        "basis": p95["basis"],
        "n_pairs": p95["n"],
        "p95_ci95": p95["ci95"],
    }


def overhead_rows(metrics, slo_cfg):
    """(paired rows, derived rows). Paired rows keep the layout the PDF report
    expects; derived rows are scenarios that borrow a reference."""
    sc = metrics.get("scenarios", {})
    pairs = find_pairs(sc)
    paired = [r for r in (overhead_row(sc, g, d, "paired") for g, d in pairs) if r]
    derived = [r for r in (overhead_row(sc, g, d, "reference")
                           for g, d in reference_pairs(sc, slo_cfg, pairs)) if r]
    return paired, derived


def estimate_gateway_overhead(cur_metrics, slo_cfg=None):
    """Kept for callers of the old API: the paired gateway-vs-direct rows."""
    return overhead_rows(cur_metrics, slo_cfg or {})[0]


def overhead_budget(slo_cfg, row, metric):
    cfg = slo_cfg.get("overhead_slo") or {}
    per = cfg.get("scenarios") or {}
    override = per.get(row["gateway_scenario_key"], per.get(row["scenario"], {})) or {}
    key = BUDGET_KEY[metric]
    if key in override:
        return override[key]
    return cfg.get(key, DEFAULT_OVERHEAD_BUDGET_MS[metric])


def effective_overhead_budgets(slo_cfg):
    """The default overhead budgets in force (ms), or None when the config has no
    overhead_slo section. Written to verdict.json so reports can colour the numbers."""
    cfg = slo_cfg.get("overhead_slo")
    if cfg is None:
        return None
    return {m: cfg.get(BUDGET_KEY[m], DEFAULT_OVERHEAD_BUDGET_MS[m]) for m in BUDGET_KEY}


def evaluate_overhead_slo(rows, slo_cfg):
    if "overhead_slo" not in slo_cfg:
        return []
    violations = []
    for row in rows:
        for metric in ("p95_ms", "p99_ms", "ttft_p95_ms"):
            actual = row.get(OVERHEAD_ROW_KEY[metric])
            if actual is None:
                continue
            budget = overhead_budget(slo_cfg, row, metric)
            if budget is not None and actual > budget:
                name = OVERHEAD_METRIC_NAME[metric]
                violations.append({
                    "scenario": row["gateway_scenario_key"],
                    "metric": name,
                    "actual": actual,
                    "threshold": budget,
                    "detail": f"{name} {actual:.3f}ms exceeds the {budget:.3f}ms budget "
                              f"(vs {row['direct_scenario_key']})",
                })
    return violations


def evaluate_overhead_regression(cur_rows, base_rows, gates):
    """Overhead now vs overhead in the baseline, in absolute milliseconds."""
    cfg = gates.get("overhead") or {}
    if not cfg:
        return []
    base_by_key = {r["gateway_scenario_key"]: r for r in base_rows}
    checks = []
    for row in cur_rows:
        base = base_by_key.get(row["gateway_scenario_key"])
        if not base:
            continue
        for metric in ("p95_ms", "p99_ms", "ttft_p95_ms"):
            g = cfg.get(metric)
            cur_v, base_v = row.get(OVERHEAD_ROW_KEY[metric]), base.get(OVERHEAD_ROW_KEY[metric])
            if not g or cur_v is None or base_v is None:
                continue
            delta = cur_v - base_v
            warn, fail = g.get("warn_abs_ms", 1.0), g.get("fail_abs_ms", 3.0)
            verdict = "REGRESSION" if delta >= fail else ("WARN" if delta >= warn else "OK")
            name = OVERHEAD_METRIC_NAME[metric]
            checks.append({
                "scenario": row["gateway_scenario_key"], "metric": name,
                "baseline": round(base_v, 3), "current": round(cur_v, 3), "delta": round(delta, 3),
                "verdict": verdict,
                "delta_str": f"{base_v:.2f} → {cur_v:.2f} ms ({delta:+.2f} ms)",
                "detail": f"{name} up {delta:+.2f} ms (warn {warn}, limit {fail})",
            })
    return checks


# --------------------------------------------------------------------------- absolute SLO
def evaluate_absolute_slo(cur_metrics, slo_cfg, overhead_rows_all=None):
    violations = []
    legacy_latency = use_legacy_latency_slo(slo_cfg)
    for scenario, metric_set in cur_metrics.get("scenarios", {}).items():
        th = scenario_slo_thresholds(slo_cfg, scenario)

        error_rate = metric_set.get("error_rate_pct")
        if error_rate is not None and error_rate > th["max_error_rate_percent"]:
            violations.append({
                "scenario": scenario,
                "metric": "error_rate_pct",
                "actual": error_rate,
                "threshold": th["max_error_rate_percent"],
                "detail": f"error rate {error_rate:.2f}% exceeds {th['max_error_rate_percent']:.2f}%",
            })

        checks_pass = metric_set.get("checks_pass_pct")
        if checks_pass is not None and checks_pass < th["min_checks_pass_rate_percent"]:
            violations.append({
                "scenario": scenario,
                "metric": "checks_pass_pct",
                "actual": checks_pass,
                "threshold": th["min_checks_pass_rate_percent"],
                "detail": f"checks pass {checks_pass:.2f}% below {th['min_checks_pass_rate_percent']:.2f}%",
            })

        dropped = (metric_set.get("dropped_pct") or {}).get("max")
        limit = th.get("max_dropped_iterations_rate_percent")
        if dropped is not None and limit is not None and dropped > limit:
            violations.append({
                "scenario": scenario,
                "metric": "dropped_pct",
                "actual": dropped,
                "threshold": limit,
                "detail": f"dropped iterations {dropped:.2f}% (worst repeat) exceeds {limit:.2f}%",
            })

        # Client-disconnect scenarios: after the client hangs up mid-stream the gateway should close the
        # upstream connection (aborted ~ started). completed ~ started means it kept the upstream busy.
        floor = th.get("min_disconnect_propagation_percent")
        up = metric_set.get("upstream_totals")
        if floor is not None and base_scenario_name(scenario).endswith("-disconnect") and up:
            pct = up.get("aborted_pct")
            if pct is not None and pct < floor:
                violations.append({
                    "scenario": scenario,
                    "metric": "disconnect_propagation_pct",
                    "actual": pct,
                    "threshold": floor,
                    "detail": f"only {pct:.1f}% of client disconnects reached the upstream as an abort "
                              f"(started={up['started']}, completed={up['completed']}, aborted={up['aborted']}); "
                              f"the gateway keeps streaming for clients that are gone (limit {floor:.0f}%)",
                })

        if legacy_latency:
            p95 = metric_set.get("p95_ms")
            if p95 is not None and p95 > th["max_p95_latency_ms"]:
                violations.append({
                    "scenario": scenario,
                    "metric": "p95_ms",
                    "actual": p95,
                    "threshold": th["max_p95_latency_ms"],
                    "detail": f"p95 {p95:.2f}ms exceeds {th['max_p95_latency_ms']:.2f}ms",
                })

            p99 = metric_set.get("p99_ms")
            if p99 is not None and p99 > th["max_p99_latency_ms"]:
                violations.append({
                    "scenario": scenario,
                    "metric": "p99_ms",
                    "actual": p99,
                    "threshold": th["max_p99_latency_ms"],
                    "detail": f"p99 {p99:.2f}ms exceeds {th['max_p99_latency_ms']:.2f}ms",
                })

            ttft = metric_set.get("ttft_p95_ms")
            if ttft is not None and "max_ttft_p95_ms" in th and ttft > th["max_ttft_p95_ms"]:
                violations.append({
                    "scenario": scenario,
                    "metric": "ttft_p95_ms",
                    "actual": ttft,
                    "threshold": th["max_ttft_p95_ms"],
                    "detail": f"ttft p95 {ttft:.2f}ms exceeds {th['max_ttft_p95_ms']:.2f}ms",
                })

    violations += evaluate_overhead_slo(overhead_rows_all or [], slo_cfg)
    return violations


# --------------------------------------------------------------------------- relative gate
def classify(metric, cur, base, gate, noise_floor):
    """Return (verdict, delta_str, detail). verdict in OK/WARN/REGRESSION/SKIP."""
    if cur is None or base is None:
        return "SKIP", "n/a", "metric not present"

    # error_rate uses absolute-point thresholds
    if metric == "error_rate_pct":
        delta = cur - base
        warn, fail = gate.get("warn_abs", 0.5), gate.get("fail_abs", 1.0)
        ds = f"{base:.2f}% → {cur:.2f}% ({delta:+.2f}pt)"
        if delta >= fail:
            return "REGRESSION", ds, f"error rate up {delta:+.2f}pt (limit {fail})"
        if delta >= warn:
            return "WARN", ds, f"error rate up {delta:+.2f}pt"
        return "OK", ds, ""

    # throughput: lower is worse
    if metric == "throughput_rps":
        if base == 0:
            return "SKIP", "n/a", "baseline zero"
        drop_pct = (base - cur) / base * 100.0
        warn, fail = gate.get("warn_pct", 5), gate.get("fail_pct", 10)
        ds = f"{base:.1f} → {cur:.1f} rps ({-drop_pct:+.1f}%)"
        if drop_pct >= fail:
            return "REGRESSION", ds, f"throughput down {drop_pct:.1f}% (limit {fail}%)"
        if drop_pct >= warn:
            return "WARN", ds, f"throughput down {drop_pct:.1f}%"
        return "OK", ds, ""

    # higher-is-worse, relative: latency (with an absolute-noise floor) and CPU per 1k requests
    if metric in RISE_METRICS:
        is_latency = metric in LATENCY_METRICS
        if is_latency:
            floor = noise_floor.get("latency_ms", 0)
            if max(cur, base) < floor:
                return "SKIP", f"{base:.0f}→{cur:.0f}ms", f"below {floor}ms noise floor"
        if base == 0:
            return "SKIP", "n/a", "baseline zero"
        rise_pct = (cur - base) / base * 100.0
        warn, fail = gate.get("warn_pct", 5), gate.get("fail_pct", 15)
        ds = f"{base:.0f} → {cur:.0f} ms ({rise_pct:+.1f}%)" if is_latency else \
             f"{base:.3f} → {cur:.3f} ({rise_pct:+.1f}%)"
        if rise_pct >= fail:
            return "REGRESSION", ds, f"{metric} up {rise_pct:.1f}% (limit {fail}%)"
        if rise_pct >= warn:
            return "WARN", ds, f"{metric} up {rise_pct:.1f}%"
        return "OK", ds, ""

    return "SKIP", "n/a", "unknown metric"


def evaluate(cur, base, gates, slo_cfg):
    """All checks for one run against an optional baseline. Pure function: no I/O."""
    noise_floor = gates.get("noise_floor", {})
    max_cv = gates.get("stability", {}).get("max_coefficient_of_variation_percent", 15)

    paired, derived = overhead_rows(cur, slo_cfg)
    res = {
        "rows": [], "regressions": [], "warnings": [], "unstable": [],
        "paired_overhead": paired, "derived_overhead": derived,
        "overhead_checks": [], "bases": set(),
    }

    if base is not None:
        for s, cm in cur["scenarios"].items():
            bm = base["scenarios"].get(s)
            if not bm:
                res["rows"].append((s, "—", "NEW", "no baseline entry"))
                continue
            cv = (cm.get("cv_pct") or {})
            worst_cv = max([v for v in cv.values() if v is not None] or [0])
            scen_verdict = "OK"
            detail_bits = []
            for metric in GATE_METRICS:
                gate = gate_for(gates, s, metric)
                if metric == CPU_METRIC and not gate:
                    continue  # only gated when the gates file asks for it
                cv_, bv_, basis = metric_pair(cm, bm, metric)
                v, ds, detail = classify(metric, cv_, bv_, gate, noise_floor)
                if v != "SKIP":
                    res["bases"].add(basis)
                if v == "REGRESSION":
                    res["regressions"].append((s, metric, ds, detail))
                    scen_verdict = "REGRESSION"
                    detail_bits.append(f"{metric} {ds} ❌")
                elif v == "WARN":
                    res["warnings"].append((s, metric, ds, detail))
                    if scen_verdict != "REGRESSION":
                        scen_verdict = "WARN"
                    detail_bits.append(f"{metric} {ds} ⚠️")
            if worst_cv > max_cv:
                res["unstable"].append((s, worst_cv))
                if scen_verdict == "OK":
                    scen_verdict = "UNSTABLE"
            icon = {"OK": "✅", "WARN": "⚠️", "REGRESSION": "❌", "UNSTABLE": "🌀"}[scen_verdict]
            res["rows"].append((s, f"{icon} {scen_verdict}", "; ".join(detail_bits) or "within tolerance",
                                f"CV {worst_cv:.1f}%"))

        base_paired, base_derived = overhead_rows(base, slo_cfg)
        res["overhead_checks"] = evaluate_overhead_regression(paired + derived, base_paired + base_derived, gates)
        for c in res["overhead_checks"]:
            if c["verdict"] == "REGRESSION":
                res["regressions"].append((c["scenario"], c["metric"], c["delta_str"], c["detail"]))
            elif c["verdict"] == "WARN":
                res["warnings"].append((c["scenario"], c["metric"], c["delta_str"], c["detail"]))

    res["absolute_slo_violations"] = evaluate_absolute_slo(cur, slo_cfg, paired + derived)
    res["notes"] = [
        f"`{s}` ran but no upstream stats were recorded, so disconnect propagation was NOT checked "
        f"(the fake provider /__stats could not be read)"
        for s, m in cur["scenarios"].items()
        if base_scenario_name(s).endswith("-disconnect") and not m.get("upstream_totals")
    ]
    return res


def confirm_findings(res, confirm_res):
    """Split regressions / unstable scenarios into confirmed and unconfirmed by a
    second run. Only findings that also flag in the confirmation run are kept."""
    reg_keys = {(s, m) for s, m, _, _ in confirm_res["regressions"]}
    unst_keys = {s for s, _ in confirm_res["unstable"]}
    confirmed_regs = [r for r in res["regressions"] if (r[0], r[1]) in reg_keys]
    unconfirmed_regs = [r for r in res["regressions"] if (r[0], r[1]) not in reg_keys]
    confirmed_unst = [u for u in res["unstable"] if u[0] in unst_keys]
    unconfirmed_unst = [u for u in res["unstable"] if u[0] not in unst_keys]
    return confirmed_regs, unconfirmed_regs, confirmed_unst, unconfirmed_unst


# --------------------------------------------------------------------------- environment
def _zone_layout(env):
    nodes = env.get("nodes") or []
    kong_zone = next((n.get("zone") for n in nodes if n.get("role") == "kong"), None)
    return {n.get("role"): (n.get("zone") == kong_zone) if kong_zone else None for n in nodes}


def env_drift(cur_env, base_env):
    """Differences between two env.json records that can move results. The Kong
    image and version are expected to differ between releases and are ignored."""
    if cur_env is None or base_env is None:
        missing = [n for n, e in (("current", cur_env), ("baseline", base_env)) if e is None]
        return {"status": "missing", "missing": missing, "drift": []}

    def get(env, path):
        node = env
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        return node

    drift = []
    for field in ("k6.version", "config_hash", "fake_provider.hash"):
        c, b = get(cur_env, field), get(base_env, field)
        if c != b:
            drift.append({"field": field, "current": c, "baseline": b})

    cur_nodes = {n.get("role"): n for n in cur_env.get("nodes") or []}
    base_nodes = {n.get("role"): n for n in base_env.get("nodes") or []}
    for role in sorted(set(cur_nodes) | set(base_nodes), key=str):
        for key in ("instance_type", "zone", "kernel", "os"):
            c = (cur_nodes.get(role) or {}).get(key)
            b = (base_nodes.get(role) or {}).get(key)
            if key == "zone":
                continue  # the zone itself is not comparable across clusters; the layout below is
            if c != b:
                drift.append({"field": f"nodes.{role}.{key}", "current": c, "baseline": b})
    cl, bl = _zone_layout(cur_env), _zone_layout(base_env)
    if cl != bl:
        drift.append({"field": "zone_layout (shares Kong node's zone)", "current": cl, "baseline": bl})
    return {"status": "drift" if drift else "ok", "missing": [], "drift": drift}


# --------------------------------------------------------------------------- report
def append_not_measured(lines):
    lines += [
        "",
        "## Not Measured",
        "",
        "- Real provider latency variance (mock upstream returns synthetic deterministic responses).",
        "- Public internet path distance and external provider network conditions.",
        "- End-to-end model generation behavior beyond configured mock timing.",
        "- Full auth/RBAC business-flow impact unless explicitly covered by policy scenarios.",
    ]


def _fmt(v, nd=3):
    return "-" if v is None else f"{v:.{nd}f}"


def append_overhead_section(lines, res, slo_cfg):
    paired, derived, checks = res["paired_overhead"], res["derived_overhead"], res["overhead_checks"]
    if not paired and not derived:
        return
    by_key = {c["scenario"]: [] for c in checks}
    for c in checks:
        by_key[c["scenario"]].append(c)

    def vs_baseline(key):
        cs = by_key.get(key)
        if not cs:
            return "-"
        icon = {"OK": "✅", "WARN": "⚠️", "REGRESSION": "❌"}
        order = ["OK", "WARN", "REGRESSION"]
        worst = max(cs, key=lambda c: (order.index(c["verdict"]), abs(c["delta"])))
        label = {"overhead_p95_ms": "p95", "overhead_p99_ms": "p99", "overhead_ttft_ms": "TTFT"}[worst["metric"]]
        return f"{icon[worst['verdict']]} {label} {worst['delta']:+.2f} ms"

    def table(rows, title):
        lines.extend(["", title, ""])
        lines.append("| Scenario | Fixture | p95 gateway (ms) | p95 direct (ms) | p95 delta (ms) | p95 CI95 | "
                     "p99 delta (ms) | TTFT delta (ms) | Basis | vs baseline |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for r in rows:
            ci = "-" if not r["p95_ci95"] else f"[{r['p95_ci95'][0]:.2f}, {r['p95_ci95'][1]:.2f}]"
            basis = r["basis"] + (f" (n={r['n_pairs']})" if r["n_pairs"] else "")
            lines.append(
                f"| {r['scenario']} | {r['fixture']} | {_fmt(r['p95_gateway_ms'])} | {_fmt(r['p95_direct_ms'])} | "
                f"{r['p95_delta_ms']:.3f} | {ci} | {_fmt(r['p99_delta_ms'])} | {_fmt(r['ttft_delta_ms'])} | "
                f"{basis} | {vs_baseline(r['gateway_scenario_key'])} |"
            )

    if paired:
        table(paired, "## Gateway Overhead (Gateway - Direct Upstream)")
        lines += ["",
                  "_Gateway path = HTTPS (TLS terminated at Kong, certificate verification skipped); direct path = "
                  "plain HTTP to the upstream. The delta therefore includes the TLS cost until the HTTP-entry "
                  "variants are measured. Paired = same-round gateway/direct runs, median of the differences._"]
    if derived:
        table(derived, "## Derived Overhead (scenarios compared with a reference control)")


def append_env_section(lines, drift):
    if drift is None:
        return
    lines += ["", "## Test Environment", ""]
    if drift["status"] == "missing":
        lines.append(f"_No env.json for: {', '.join(drift['missing'])} — environment drift cannot be checked._")
    elif drift["status"] == "ok":
        lines.append("Environment matches the baseline on the compared fields.")
    else:
        lines.append("**Environment differs from the baseline** — results may not be directly comparable "
                     "(informational, does not change the verdict):")
        lines.append("")
        lines.append("| Field | Current | Baseline |")
        lines.append("|---|---|---|")
        for d in drift["drift"]:
            lines.append(f"| {d['field']} | {d['current']} | {d['baseline']} |")


# --------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--current", required=True)
    ap.add_argument("--baseline", default=None)
    ap.add_argument("--gates", default=str(Path(__file__).parent / "release_gates.yaml"))
    ap.add_argument("--slo-config", default=str(Path(__file__).parent / "benchmark_config.yaml"))
    ap.add_argument("--confirm", default=None,
                    help="second run of the same release; a regression/unstable finding only counts "
                         "when it also flags there")
    ap.add_argument("--out", default=None, help="markdown report path")
    ap.add_argument("--json", default=None, help="machine-readable verdict path")
    args = ap.parse_args(argv)

    cur = load_metrics(args.current)
    with open(args.gates) as f:
        gates = yaml.safe_load(f) or {}
    slo_cfg = load_slo_config(args.slo_config)
    base = load_metrics(args.baseline) if args.baseline else None

    res = evaluate(cur, base, gates, slo_cfg)
    absolute_slo_violations = res["absolute_slo_violations"]
    paired = res["paired_overhead"]

    lines = [f"# Release Performance Evidence — {cur.get('version','?')}", ""]
    lines.append(f"**Captured:** {cur.get('captured_at','?')}  ")

    # ---- first release: nothing to compare against
    if base is None:
        lines += ["", "_No baseline provided — this run is recorded as the reference baseline._", ""]
        lines.append("| Scenario | p95 ms | p99 ms | TTFT p95 | RPS | Err % | repeats |")
        lines.append("|---|---|---|---|---|---|---|")
        for s, m in cur["scenarios"].items():
            lines.append(f"| {s} | {m.get('p95_ms','-')} | {m.get('p99_ms','-')} | "
                         f"{m.get('ttft_p95_ms') or '-'} | {m.get('throughput_rps','-')} | "
                         f"{m.get('error_rate_pct','-')} | {m.get('repeats','-')} |")
        overall = "ABSOLUTE_SLO_FAIL" if absolute_slo_violations else "BASELINE_PASS"
        if absolute_slo_violations:
            lines += ["", "## Absolute SLO Violations", ""]
            for v in absolute_slo_violations:
                lines.append(f"- `{v['scenario']}` / **{v['metric']}**: {v['detail']}")
        lines += ["", f"## Overall: {overall}", ""]
        append_overhead_section(lines, res, slo_cfg)
        append_not_measured(lines)

        report = "\n".join(lines) + "\n"
        if args.out:
            Path(args.out).write_text(report)
        print(report)
        if args.json:
            Path(args.json).write_text(json.dumps({
                "verdict": overall,
                "overall": overall,
                "regressions": [],
                "absolute_slo_violations": absolute_slo_violations,
                "gateway_overhead_estimates": paired,
                "derived_overhead": res["derived_overhead"],
                "overhead_budgets": effective_overhead_budgets(slo_cfg),
            }, indent=2))
        return 1 if absolute_slo_violations else 0

    # ---- with a baseline
    lines.append(f"**Baseline:** {base.get('version','?')} ({base.get('captured_at','?')})")
    bases = sorted(res["bases"])
    if "median" in bases:
        lines.append("**Gate statistic:** median across repeats for latency and throughput "
                     "(error rate uses the mean so one bad repeat cannot hide)  ")
    elif bases:
        lines.append("**Gate statistic:** mean across repeats (per-run statistics are not available "
                     "for one of the two runs)  ")
    lines += ["", "## Regression Summary", ""]

    regressions, warnings, unstable = res["regressions"], res["warnings"], res["unstable"]
    unconfirmed_regs, unconfirmed_unst, confirm_note = [], [], None
    if args.confirm:
        confirm = load_metrics(args.confirm)
        confirm_res = evaluate(confirm, base, gates, slo_cfg)
        regressions, unconfirmed_regs, unstable, unconfirmed_unst = confirm_findings(res, confirm_res)
        confirm_note = (f"Confirmation run: {confirm.get('version','?')} ({confirm.get('captured_at','?')}) — "
                        f"{len(regressions)} regression(s) and {len(unstable)} unstable scenario(s) confirmed, "
                        f"{len(unconfirmed_regs)} regression(s) and {len(unconfirmed_unst)} unstable "
                        f"scenario(s) not reproduced.")

    lines.append("| Scenario | Verdict | Details | Stability |")
    lines.append("|---|---|---|---|")
    for r in res["rows"]:
        lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} |")

    overall = "REGRESSION" if regressions else ("UNSTABLE" if unstable else
              ("WARN" if warnings else "PASS"))
    if absolute_slo_violations:
        overall = "ABSOLUTE_SLO_FAIL"
    lines += ["", f"## Overall: {overall}", ""]
    if confirm_note:
        lines += [confirm_note, ""]
    if regressions:
        lines.append(f"**{len(regressions)} regression(s) — escalate to dev team:**")
        for s, m, ds, d in regressions:
            lines.append(f"- `{s}` / **{m}**: {d}")
    if unstable:
        lines.append(f"\n**{len(unstable)} unstable scenario(s) — re-run, do not trust:** "
                     + ", ".join(f"{s} (CV {cv:.1f}%)" for s, cv in unstable))
    if warnings and not regressions:
        lines.append(f"\n{len(warnings)} warning(s) — monitor, no action required.")
    if unconfirmed_regs or unconfirmed_unst:
        lines += ["", "**Not reproduced in the confirmation run (treated as noise):**"]
        for s, m, ds, d in unconfirmed_regs:
            lines.append(f"- `{s}` / **{m}**: {d}")
        for s, cv in unconfirmed_unst:
            lines.append(f"- `{s}` unstable (CV {cv:.1f}%)")

    if absolute_slo_violations:
        lines += ["", f"**{len(absolute_slo_violations)} absolute SLO violation(s):**"]
        for v in absolute_slo_violations:
            lines.append(f"- `{v['scenario']}` / **{v['metric']}**: {v['detail']}")
    if res["notes"]:
        lines += ["", "**Not checked:**"] + [f"- {n}" for n in res["notes"]]

    append_overhead_section(lines, res, slo_cfg)
    drift = env_drift(load_env(cur), load_env(base))
    append_env_section(lines, drift)
    append_not_measured(lines)

    report = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(report)
    print(report)
    if args.json:
        Path(args.json).write_text(json.dumps({
            "version": cur.get("version"), "baseline": base.get("version"),
            "overall": overall,
            "gate_statistic": bases,
            "regressions": [{"scenario": s, "metric": m, "delta": ds, "detail": d}
                            for s, m, ds, d in regressions],
            "warnings": [{"scenario": s, "metric": m, "delta": ds} for s, m, ds, _ in warnings],
            "unstable": [{"scenario": s, "cv_pct": cv} for s, cv in unstable],
            "unconfirmed": {
                "regressions": [{"scenario": s, "metric": m, "delta": ds, "detail": d}
                                for s, m, ds, d in unconfirmed_regs],
                "unstable": [{"scenario": s, "cv_pct": cv} for s, cv in unconfirmed_unst],
            } if args.confirm else None,
            "absolute_slo_violations": absolute_slo_violations,
            "gateway_overhead_estimates": paired,
            "derived_overhead": res["derived_overhead"],
            "overhead_checks": res["overhead_checks"],
            "overhead_budgets": effective_overhead_budgets(slo_cfg),
            "notes": res["notes"],
            "environment": drift,
        }, indent=2))

    # exit non-zero on absolute SLO failures, regression, or unstable runs.
    return 1 if (absolute_slo_violations or regressions or unstable) else 0


if __name__ == "__main__":
    sys.exit(main())
