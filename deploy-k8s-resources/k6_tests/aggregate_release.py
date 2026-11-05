#!/usr/bin/env python3
"""
aggregate_release.py — parse k6 release-run summaries into one metrics.json.

Reads a directory of per-repeat k6 summary logs named
<scenario>__run<N>.log, extracts the key metrics from each, aggregates across
repeats and writes metrics.json + summary.csv in the release dir.

Per scenario, metrics.json holds:
  * the six gate metrics as flat keys (p95_ms, p99_ms, throughput_rps,
    error_rate_pct, ttft_p95_ms, checks_pass_pct) = MEAN across repeats, plus
    cv_pct (coefficient of variation, SAMPLE standard deviation) — unchanged
    layout, so compare_release.py and old baselines keep working;
  * median / iqr / stdev / min / max across repeats for those six metrics and
    for the extra latency stats (avg_ms, p50_ms, p90_ms, max_ms) when the logs
    carry them (stdev is the sample standard deviation; iqr uses inclusive
    quartiles, statistics.quantiles(..., method="inclusive"));
  * dropped_pct {mean, max}: dropped_iterations / (iterations + dropped_iterations)
    per repeat, in percent — null when the logs predate that field;
  * status_totals: HTTP status / error counts summed over repeats — null when
    the logs predate that field;
  * infra: {cpu_seconds_per_1k_req: median across repeats} from the "kong cpu:" line the runner
    appends to gateway runs (cpu_sample.py); null when the logs have none. compare_release.py gates
    on it (a sensitive efficiency signal at low load, where latency barely moves);
  * upstream_totals: for client-disconnect scenarios, the fake provider's started /
    completed / aborted counts summed over repeats (the runner appends an
    "upstream stats:" line to the per-run log) and aborted_pct = aborted / started;
    null for every other scenario;
  * per_run: the raw per-repeat values, ordered by run number. compare_release.py
    pairs gateway and direct runs of the same round (they run back-to-back) to
    estimate gateway overhead from paired differences.

The extra statistics are deliberately NOT part of cv_pct: compare_release.py
takes the worst value in cv_pct as the scenario's stability, and noisy-by-nature
metrics such as max latency would make every scenario look unstable.

Logs written before the extra fields existed (7-line logs) still aggregate; the
fields that need the missing lines are simply null.

Usage:
  python3 aggregate_release.py --dir results/releases/<ver>_<date> \
      --version <ver> --captured-at <iso8601>
"""
import argparse
import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

SCHEMA_VERSION = 2

# Gate metrics (flat keys, mean + cv_pct). Do not add keys here without
# thinking about compare_release.py's stability check.
PATTERNS = {
    "p95_ms": re.compile(r"http_req_duration p95\(ms\):\s*([\d.]+)"),
    "p99_ms": re.compile(r"http_req_duration p99\(ms\):\s*([\d.]+)"),
    "throughput_rps": re.compile(r"http_reqs/s:\s*([\d.]+)"),
    "error_rate_pct": re.compile(r"http_req_failed\(%\):\s*([\d.]+)"),
    "ttft_p95_ms": re.compile(r"ai_time_to_first_token_ms p95:\s*([\d.]+)"),
    "checks_pass_pct": re.compile(r"checks pass rate\(%\):\s*([\d.]+)"),
}

# Extra per-run latency stats: summarised with median/iqr/stdev/min/max only.
EXTRA_LATENCY_PATTERNS = {
    "avg_ms": re.compile(r"http_req_duration avg\(ms\):\s*([\d.]+)"),
    "p50_ms": re.compile(r"http_req_duration med\(ms\):\s*([\d.]+)"),
    "p90_ms": re.compile(r"http_req_duration p90\(ms\):\s*([\d.]+)"),
    "max_ms": re.compile(r"http_req_duration max\(ms\):\s*([\d.]+)"),
}

# "dropped_iterations total:" also contains "iterations total:", so anchor both.
ITERATIONS_RX = re.compile(r"^iterations total:\s*(\d+)", re.M)
DROPPED_RX = re.compile(r"^dropped_iterations total:\s*(\d+)", re.M)
STATUS_RX = re.compile(r"^http status counts:\s*(.+)$", re.M)
CPU_RX = re.compile(r"^kong cpu: seconds=([\d.]+) iterations=(\d+) seconds_per_1k=([\d.]+)", re.M)
UPSTREAM_RX = re.compile(
    r"^upstream stats: started=(\d+) completed=(\d+) aborted=(\d+) active=(\d+)", re.M)

STAT_METRICS = list(PATTERNS) + list(EXTRA_LATENCY_PATTERNS)


def parse_status(text):
    m = STATUS_RX.search(text)
    if not m:
        return None
    out = {}
    for token in m.group(1).split():
        name, _, count = token.partition("=")
        if count.isdigit():
            out[name] = int(count)
    return out or None


def parse_log(path):
    text = Path(path).read_text(errors="ignore")
    out = {}
    for metric, rx in {**PATTERNS, **EXTRA_LATENCY_PATTERNS}.items():
        m = rx.search(text)
        out[metric] = float(m.group(1)) if m else None

    it, dr = ITERATIONS_RX.search(text), DROPPED_RX.search(text)
    if it and dr:
        iterations, dropped = int(it.group(1)), int(dr.group(1))
        total = iterations + dropped
        out["dropped_pct"] = (dropped / total * 100.0) if total else 0.0
    else:
        out["dropped_pct"] = None
    out["status"] = parse_status(text)
    cpu = CPU_RX.search(text)
    out["cpu_seconds_per_1k_req"] = float(cpu.group(3)) if cpu else None
    up = UPSTREAM_RX.search(text)
    out["upstream"] = (
        dict(zip(("started", "completed", "aborted", "active"), map(int, up.groups()))) if up else None
    )
    return out


def sample_stdev(values):
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def cv_pct(values):
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return 0.0
    mean = statistics.mean(vals)
    if mean == 0:
        return 0.0
    return sample_stdev(vals) / mean * 100.0


def iqr(values):
    if len(values) < 2:
        return 0.0
    q = statistics.quantiles(values, n=4, method="inclusive")
    return q[2] - q[0]


def summarise(values):
    """median/iqr/stdev/min/max of the non-null values, or all-None."""
    vals = [v for v in values if v is not None]
    if not vals:
        return {"median": None, "iqr": None, "stdev": None, "min": None, "max": None}
    return {
        "median": round(statistics.median(vals), 3),
        "iqr": round(iqr(vals), 3),
        "stdev": round(sample_stdev(vals), 3),
        "min": round(min(vals), 3),
        "max": round(max(vals), 3),
    }


def aggregate_scenario(reps):
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

    per_stat = {m: summarise([r.get(m) for r in reps]) for m in STAT_METRICS}
    for stat in ("median", "iqr", "stdev", "min", "max"):
        agg[stat] = {m: per_stat[m][stat] for m in STAT_METRICS}

    dropped = [r["dropped_pct"] for r in reps if r.get("dropped_pct") is not None]
    agg["dropped_pct"] = (
        {"mean": round(statistics.mean(dropped), 3), "max": round(max(dropped), 3)} if dropped else None
    )

    ordered = sorted(reps, key=lambda r: (r.get("run") is None, r.get("run") or 0))
    agg["per_run"] = [
        {"run": r.get("run"),
         **{m: r.get(m) for m in STAT_METRICS},
         "dropped_pct": None if r.get("dropped_pct") is None else round(r["dropped_pct"], 3),
         "cpu_seconds_per_1k_req": r.get("cpu_seconds_per_1k_req")}
        for r in ordered
    ]

    totals = defaultdict(int)
    seen = False
    for r in reps:
        if r.get("status"):
            seen = True
            for name, count in r["status"].items():
                totals[name] += count
    agg["status_totals"] = dict(totals) if seen else None

    cpu = [r["cpu_seconds_per_1k_req"] for r in reps if r.get("cpu_seconds_per_1k_req") is not None]
    agg["infra"] = {"cpu_seconds_per_1k_req": round(statistics.median(cpu), 4)} if cpu else None

    ups = [r["upstream"] for r in reps if r.get("upstream")]
    if ups:
        started = sum(u["started"] for u in ups)
        agg["upstream_totals"] = {
            "started": started,
            "completed": sum(u["completed"] for u in ups),
            "aborted": sum(u["aborted"] for u in ups),
            "active_max": max(u["active"] for u in ups),
            "aborted_pct": round(sum(u["aborted"] for u in ups) / started * 100.0, 3) if started else None,
        }
    else:
        agg["upstream_totals"] = None
    return agg


def collect_scenarios(d):
    """Aggregate every <scenario>__run<N>.log in directory d (driver logs skipped)."""
    runs = defaultdict(list)  # scenario -> [per-repeat dicts]
    for log in sorted(Path(d).glob("*__run*.log")):
        if log.name.endswith(".driver.log"):
            continue
        if not log.read_text(errors="ignore").strip():
            # a run whose summary could not be read (e.g. kubectl lost its credentials): not a repeat
            print(f"aggregate_release.py: ignoring {log.name}: empty summary", file=sys.stderr)
            continue
        scenario = log.name.split("__run")[0]
        rep = parse_log(log)
        m = re.search(r"__run(\d+)\.log$", log.name)
        rep["run"] = int(m.group(1)) if m else None
        runs[scenario].append(rep)
    return {scenario: aggregate_scenario(reps) for scenario, reps in runs.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--captured-at", required=True)
    args = ap.parse_args()

    d = Path(args.dir)
    scenarios = collect_scenarios(d)

    metrics = {
        "schema_version": SCHEMA_VERSION,
        "version": args.version,
        "captured_at": args.captured_at,
        "aggregation": {
            "flat_metrics": "mean across repeats",
            "cv_pct": "sample standard deviation / mean * 100 (gate metrics only)",
            "iqr": "inclusive quartiles (statistics.quantiles, method=inclusive)",
            "dropped_pct": "dropped_iterations / (iterations + dropped_iterations) * 100 per repeat",
        },
        "scenarios": scenarios,
    }
    (d / "metrics.json").write_text(json.dumps(metrics, indent=2))

    def cell(m, stat, metric):
        v = (m.get(stat) or {}).get(metric)
        return "" if v is None else v

    # flat CSV for spreadsheets / archival evidence. The first nine columns are
    # unchanged; new columns are appended.
    with open(d / "summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "p95_ms", "p99_ms", "ttft_p95_ms", "throughput_rps",
                    "error_rate_pct", "checks_pass_pct", "repeats", "cv_p95_pct",
                    "p95_median_ms", "p99_median_ms", "throughput_median_rps", "dropped_pct_max"])
        for s, m in scenarios.items():
            dropped = m.get("dropped_pct")
            w.writerow([s, m["p95_ms"], m["p99_ms"], m["ttft_p95_ms"], m["throughput_rps"],
                        m["error_rate_pct"], m["checks_pass_pct"], m["repeats"],
                        (m["cv_pct"] or {}).get("p95_ms"),
                        cell(m, "median", "p95_ms"), cell(m, "median", "p99_ms"),
                        cell(m, "median", "throughput_rps"),
                        "" if dropped is None else dropped["max"]])

    print(f"wrote {d/'metrics.json'} and summary.csv — {len(scenarios)} scenarios")


if __name__ == "__main__":
    main()
