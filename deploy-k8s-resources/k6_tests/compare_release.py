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
    ap.add_argument("--out", default=None, help="markdown report path")
    ap.add_argument("--json", default=None, help="machine-readable verdict path")
    args = ap.parse_args()

    cur = load_metrics(args.current)
    gates = yaml.safe_load(open(args.gates))
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
        report = "\n".join(lines) + "\n"
        if args.out:
            Path(args.out).write_text(report)
        print(report)
        if args.json:
            Path(args.json).write_text(json.dumps({"verdict": "BASELINE", "regressions": []}, indent=2))
        return 0

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
        }, indent=2))

    # exit non-zero on regression OR unstable so CI gates / surfaces it
    return 1 if (regressions or unstable) else 0


if __name__ == "__main__":
    sys.exit(main())
