#!/usr/bin/env python3
"""
aggregate_release.py — parse k6 release-run summaries into one metrics.json.

Reads a directory of per-repeat k6 summary logs named
<scenario>__run<N>.log, extracts the key metrics from each, aggregates across
repeats (mean) with coefficient-of-variation, and writes metrics.json +
summary.csv in the release dir.

Usage:
  python3 aggregate_release.py --dir results/releases/<ver>_<date> \
      --version <ver> --captured-at <iso8601>
"""
import argparse
import csv
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path

PATTERNS = {
    "p95_ms": re.compile(r"http_req_duration p95\(ms\):\s*([\d.]+)"),
    "p99_ms": re.compile(r"http_req_duration p99\(ms\):\s*([\d.]+)"),
    "throughput_rps": re.compile(r"http_reqs/s:\s*([\d.]+)"),
    "error_rate_pct": re.compile(r"http_req_failed\(%\):\s*([\d.]+)"),
    "ttft_p95_ms": re.compile(r"ai_time_to_first_token_ms p95:\s*([\d.]+)"),
    "checks_pass_pct": re.compile(r"checks pass rate\(%\):\s*([\d.]+)"),
}


def parse_log(path):
    text = Path(path).read_text(errors="ignore")
    out = {}
    for metric, rx in PATTERNS.items():
        m = rx.search(text)
        out[metric] = float(m.group(1)) if m else None
    return out


def cv_pct(values):
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return 0.0
    mean = statistics.mean(vals)
    if mean == 0:
        return 0.0
    return statistics.pstdev(vals) / mean * 100.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--captured-at", required=True)
    args = ap.parse_args()

    d = Path(args.dir)
    runs = defaultdict(list)  # scenario -> [per-repeat dicts]
    for log in sorted(d.glob("*__run*.log")):
        scenario = log.name.split("__run")[0]
        runs[scenario].append(parse_log(log))

    scenarios = {}
    for scenario, reps in runs.items():
        agg = {}
        cvs = {}
        for metric in PATTERNS:
            vals = [r.get(metric) for r in reps if r.get(metric) is not None]
            if vals:
                agg[metric] = round(statistics.mean(vals), 3)
                cvs[metric] = round(cv_pct(vals), 2)
            else:
                agg[metric] = None
                cvs[metric] = None
        agg["repeats"] = len(reps)
        agg["cv_pct"] = cvs
        scenarios[scenario] = agg

    metrics = {"version": args.version, "captured_at": args.captured_at,
               "scenarios": scenarios}
    (d / "metrics.json").write_text(json.dumps(metrics, indent=2))

    # flat CSV for spreadsheets / archival evidence
    with open(d / "summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "p95_ms", "p99_ms", "ttft_p95_ms", "throughput_rps",
                    "error_rate_pct", "checks_pass_pct", "repeats", "cv_p95_pct"])
        for s, m in scenarios.items():
            w.writerow([s, m["p95_ms"], m["p99_ms"], m["ttft_p95_ms"], m["throughput_rps"],
                        m["error_rate_pct"], m["checks_pass_pct"], m["repeats"],
                        (m["cv_pct"] or {}).get("p95_ms")])

    print(f"wrote {d/'metrics.json'} and summary.csv — {len(scenarios)} scenarios")


if __name__ == "__main__":
    main()
