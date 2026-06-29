#!/usr/bin/env python3
"""
compare_release.py — Release regression gate for Kong AI Gateway benchmarks.

Compares a release's aggregated metrics against a baseline release, classifies
each scenario/metric as OK / WARN / REGRESSION per release_gates.yaml, writes a
human-readable evidence report, and exits non-zero if any REGRESSION is found
(so it can gate CI / release sign-off).

Inputs are metrics.json files produced by run_release_baseline.sh, shaped as:
  {
    "version": "ai-2.0.0-rc.3",
    "captured_at": "2026-06-23T...",
    "scenarios": {
      "token-chat-openai": {
        "p95_ms": 615.0, "p99_ms": 616.0, "ttft_p95_ms": null,
        "throughput_rps": 24.7, "error_rate_pct": 0.0,
        "repeats": 3, "cv_pct": {"p95_ms": 2.1, ...}
      }, ...
    }
  }

Usage:
  python3 compare_release.py --current <dir|metrics.json> --baseline <dir|metrics.json> \
      [--gates release_gates.yaml] [--out report.md] [--json verdict.json]
  # First release (no baseline): omit --baseline; it records and passes.
"""
import argparse
import json
import sys
from pathlib import Path

import yaml

# HIGHER-is-worse metrics; throughput is the exception (lower is worse).
LATENCY_METRICS = ["p95_ms", "p99_ms", "ttft_p95_ms"]


def load_slo_config(path_str):
    p = Path(path_str)
    if not p.exists():
        return {}
    with open(p) as f:
        return yaml.safe_load(f) or {}


def scenario_slo_thresholds(slo_cfg, scenario):
    base = slo_cfg.get("slo", {})
    overrides = slo_cfg.get("scenarios", {}).get(scenario, {})

    thresholds = {
        "max_error_rate_percent": base.get("max_error_rate_percent", 1.0),
        "min_checks_pass_rate_percent": base.get("min_checks_pass_rate_percent", 99.0),
        "max_p95_latency_ms": base.get("max_p95_latency_ms", 500),
        "max_p99_latency_ms": base.get("max_p99_latency_ms", 1000),
    }

    if "max_p95_latency_ms" in overrides:
        thresholds["max_p95_latency_ms"] = overrides["max_p95_latency_ms"]
    if "max_p99_latency_ms" in overrides:
        thresholds["max_p99_latency_ms"] = overrides["max_p99_latency_ms"]
    if "max_ttft_p95_ms" in overrides:
        thresholds["max_ttft_p95_ms"] = overrides["max_ttft_p95_ms"]

    return thresholds


def evaluate_absolute_slo(cur_metrics, slo_cfg):
    violations = []
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

    return violations


def load_metrics(path_str):
    p = Path(path_str)
    if p.is_dir():
        p = p / "metrics.json"
    with open(p) as f:
        return json.load(f)


def gate_for(gates, scenario, metric):
    g = dict(gates.get("defaults", {}).get(metric, {}))
    g.update(gates.get("scenarios", {}).get(scenario, {}).get(metric, {}))
    return g


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

    # latency-family: higher is worse, with absolute noise floor
    if metric in LATENCY_METRICS:
        floor = noise_floor.get("latency_ms", 0)
        if max(cur, base) < floor:
            return "SKIP", f"{base:.0f}→{cur:.0f}ms", f"below {floor}ms noise floor"
        if base == 0:
            return "SKIP", "n/a", "baseline zero"
        rise_pct = (cur - base) / base * 100.0
        warn, fail = gate.get("warn_pct", 5), gate.get("fail_pct", 15)
        ds = f"{base:.0f} → {cur:.0f} ms ({rise_pct:+.1f}%)"
        if rise_pct >= fail:
            return "REGRESSION", ds, f"{metric} up {rise_pct:.1f}% (limit {fail}%)"
        if rise_pct >= warn:
            return "WARN", ds, f"{metric} up {rise_pct:.1f}%"
        return "OK", ds, ""

    return "SKIP", "n/a", "unknown metric"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--current", required=True)
    ap.add_argument("--baseline", default=None)
    ap.add_argument("--gates", default=str(Path(__file__).parent / "release_gates.yaml"))
    ap.add_argument("--slo-config", default=str(Path(__file__).parent / "benchmark_config.yaml"))
    ap.add_argument("--out", default=None, help="markdown report path")
    ap.add_argument("--json", default=None, help="machine-readable verdict path")
    args = ap.parse_args()

    cur = load_metrics(args.current)
    gates = yaml.safe_load(open(args.gates))
    slo_cfg = load_slo_config(args.slo_config)
    absolute_slo_violations = evaluate_absolute_slo(cur, slo_cfg)
    noise_floor = gates.get("noise_floor", {})
    metrics = ["p95_ms", "p99_ms", "ttft_p95_ms", "throughput_rps", "error_rate_pct"]

    lines = [f"# Release Performance Evidence — {cur.get('version','?')}", ""]
    lines.append(f"**Captured:** {cur.get('captured_at','?')}  ")

    # First release: no baseline to compare against.
    if not args.baseline:
        lines += ["", "_No baseline provided — this run is recorded as the reference baseline._", ""]
        lines.append("| Scenario | p95 ms | p99 ms | TTFT p95 | RPS | Err % | repeats |")
        lines.append("|---|---|---|---|---|---|---|")
        for s, m in cur["scenarios"].items():
            lines.append(f"| {s} | {m.get('p95_ms','-')} | {m.get('p99_ms','-')} | "
                         f"{m.get('ttft_p95_ms') or '-'} | {m.get('throughput_rps','-')} | "
                         f"{m.get('error_rate_pct','-')} | {m.get('repeats','-')} |")

        if absolute_slo_violations:
            lines += ["", "## Absolute SLO Violations", ""]
            for v in absolute_slo_violations:
                lines.append(
                    f"- `{v['scenario']}` / **{v['metric']}**: {v['detail']}"
                )
            lines += ["", "## Overall: ABSOLUTE_SLO_FAIL", ""]
        else:
            lines += ["", "## Overall: BASELINE_PASS", ""]

        report = "\n".join(lines) + "\n"
        if args.out:
            Path(args.out).write_text(report)
        print(report)
        if args.json:
            Path(args.json).write_text(json.dumps({
                "verdict": "ABSOLUTE_SLO_FAIL" if absolute_slo_violations else "BASELINE_PASS",
                "regressions": [],
                "absolute_slo_violations": absolute_slo_violations,
            }, indent=2))
        return 1 if absolute_slo_violations else 0

    base = load_metrics(args.baseline)
    lines.append(f"**Baseline:** {base.get('version','?')} ({base.get('captured_at','?')})")
    lines += ["", "## Regression Summary", ""]

    regressions, warnings, unstable = [], [], []
    rows = []
    for s, cm in cur["scenarios"].items():
        bm = base["scenarios"].get(s)
        if not bm:
            rows.append((s, "—", "NEW", "no baseline entry"))
            continue
        # stability check
        cv = (cm.get("cv_pct") or {})
        worst_cv = max([v for v in cv.values() if v is not None] or [0])
        max_cv = gates.get("stability", {}).get("max_coefficient_of_variation_percent", 15)
        scen_verdict = "OK"
        detail_bits = []
        for metric in metrics:
            v, ds, detail = classify(metric, cm.get(metric), bm.get(metric),
                                     gate_for(gates, s, metric), noise_floor)
            if v == "REGRESSION":
                regressions.append((s, metric, ds, detail)); scen_verdict = "REGRESSION"
                detail_bits.append(f"{metric} {ds} ❌")
            elif v == "WARN":
                warnings.append((s, metric, ds, detail))
                if scen_verdict != "REGRESSION":
                    scen_verdict = "WARN"
                detail_bits.append(f"{metric} {ds} ⚠️")
        if worst_cv > max_cv:
            unstable.append((s, worst_cv))
            if scen_verdict == "OK":
                scen_verdict = "UNSTABLE"
        icon = {"OK": "✅", "WARN": "⚠️", "REGRESSION": "❌", "UNSTABLE": "🌀"}[scen_verdict]
        rows.append((s, f"{icon} {scen_verdict}", "; ".join(detail_bits) or "within tolerance",
                     f"CV {worst_cv:.1f}%"))

    lines.append("| Scenario | Verdict | Details | Stability |")
    lines.append("|---|---|---|---|")
    for r in rows:
        lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} |")

    overall = "REGRESSION" if regressions else ("UNSTABLE" if unstable else
              ("WARN" if warnings else "PASS"))
    if absolute_slo_violations:
        overall = "ABSOLUTE_SLO_FAIL"
    lines += ["", f"## Overall: {overall}", ""]
    if regressions:
        lines.append(f"**{len(regressions)} regression(s) — escalate to dev team:**")
        for s, m, ds, d in regressions:
            lines.append(f"- `{s}` / **{m}**: {d}")
    if unstable:
        lines.append(f"\n**{len(unstable)} unstable scenario(s) — re-run, do not trust:** "
                     + ", ".join(f"{s} (CV {cv:.1f}%)" for s, cv in unstable))
    if warnings and not regressions:
        lines.append(f"\n{len(warnings)} warning(s) — monitor, no action required.")

    if absolute_slo_violations:
        lines += ["", f"**{len(absolute_slo_violations)} absolute SLO violation(s):**"]
        for v in absolute_slo_violations:
            lines.append(f"- `{v['scenario']}` / **{v['metric']}**: {v['detail']}")

    report = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(report)
    print(report)
    if args.json:
        Path(args.json).write_text(json.dumps({
            "version": cur.get("version"), "baseline": base.get("version"),
            "overall": overall,
            "regressions": [{"scenario": s, "metric": m, "delta": ds, "detail": d}
                            for s, m, ds, d in regressions],
            "warnings": [{"scenario": s, "metric": m, "delta": ds} for s, m, ds, _ in warnings],
            "unstable": [{"scenario": s, "cv_pct": cv} for s, cv in unstable],
            "absolute_slo_violations": absolute_slo_violations,
        }, indent=2))

    # exit non-zero on absolute SLO failures, regression, or unstable runs.
    return 1 if (absolute_slo_violations or regressions or unstable) else 0


if __name__ == "__main__":
    sys.exit(main())
