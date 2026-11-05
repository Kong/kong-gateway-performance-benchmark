#!/usr/bin/env python3
"""
paired_ab.py — compare two configurations measured in interleaved rounds.

An experiment that runs config A for a while and then config B for a while mixes the effect of the
configuration with everything that drifts between the two blocks (node state, noisy neighbours).
Run the configurations alternately instead — round 1: A then B, round 2: B then A, ... — and compare
them PER ROUND: the two measurements of a round are minutes apart, so their difference cancels most
of the drift.

Reads one directory per (configuration, round), named
    <prefix>-<config>-r<round>_<date>      e.g. ai-2.3.0-rc.2-cpuab-buffered-r3_2026-11-05
produced by run_release_baseline.sh with REPEATS=1, takes the metric of each scenario from the
per-run log, and reports for every scenario:
  * mean of A and of B over the rounds, the mean paired difference B - A (also as % of A)
  * the standard deviation of the paired differences and a bootstrap 95% CI of their mean
  * in how many rounds B > A, with the exact two-sided sign-test p-value
  * optionally the mean difference in the rounds where A ran first vs B ran first, which shows an
    order effect (e.g. the first run after a config reload being different)
It does not claim significance: with 6 rounds the smallest possible sign-test p-value is 0.031.

Metrics: cpu (Kong CPU seconds per 1k requests, the "kong cpu:" line) or any gate metric of the
aggregator: p95_ms, p99_ms, ttft_p95_ms, throughput_rps.

Usage:
  python3 paired_ab.py --prefix ai-2.3.0-rc.2-cpuab --a buffered --b unbuffered \\
      --scenarios stream-openai,stream-gemini --metric cpu --first-odd a
"""
import argparse
import math
import random
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aggregate_release as ag  # noqa: E402

CPU_RX = re.compile(r"^kong cpu: seconds=[\d.]+ iterations=\d+ seconds_per_1k=([\d.]+)", re.M)
BOOTSTRAP_SAMPLES = 5000


def round_dirs(root, prefix, config):
    """{round: directory} for <prefix>-<config>-r<N>_<date> under root."""
    rx = re.compile(rf"^{re.escape(prefix)}-{re.escape(config)}-r(\d+)_")
    out = {}
    for d in sorted(Path(root).iterdir()):
        m = rx.match(d.name)
        if m and d.is_dir():
            out[int(m.group(1))] = d
    return out


def read_metric(log_path, metric):
    text = Path(log_path).read_text(errors="ignore")
    if not text.strip():
        return None
    if metric == "cpu":
        m = CPU_RX.search(text)
        return float(m.group(1)) if m else None
    key = {"p95_ms": "p95_ms", "p99_ms": "p99_ms", "ttft_p95_ms": "ttft_p95_ms", "throughput_rps": "throughput_rps"}[metric]
    return ag.parse_log(log_path).get(key)


def collect(root, prefix, config, scenario, metric):
    """{round: value}; a round without a readable value is simply absent."""
    out = {}
    for rnd, d in round_dirs(root, prefix, config).items():
        log = d / f"{scenario}__run1.log"
        if log.exists():
            v = read_metric(log, metric)
            if v is not None:
                out[rnd] = v
    return out


def sign_test_p(positive, n):
    """Exact two-sided sign-test p-value for `positive` of `n` non-zero differences."""
    if n == 0:
        return None
    k = min(positive, n - positive)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def bootstrap_ci_of_mean(values, samples=BOOTSTRAP_SAMPLES, seed=20261105, alpha=0.05):
    if len(values) < 3:
        return None
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(samples))
    return means[int(samples * alpha / 2)], means[int(samples * (1 - alpha / 2)) - 1]


def analyze(a, b, first_odd=None):
    """Paired statistics of two {round: value} dicts (B - A), over the rounds present in both."""
    rounds = sorted(set(a) & set(b))
    if not rounds:
        return None
    diffs = [b[r] - a[r] for r in rounds]
    mean_a, mean_b = statistics.mean(a[r] for r in rounds), statistics.mean(b[r] for r in rounds)
    out = {
        "rounds": rounds,
        "n": len(rounds),
        "mean_a": mean_a,
        "mean_b": mean_b,
        "diffs": diffs,
        "mean_diff": statistics.mean(diffs),
        "pct_diff": statistics.mean(diffs) / mean_a * 100 if mean_a else None,
        "sd_diff": statistics.stdev(diffs) if len(diffs) > 1 else None,
        "sd_a": statistics.stdev([a[r] for r in rounds]) if len(rounds) > 1 else None,
        "sd_b": statistics.stdev([b[r] for r in rounds]) if len(rounds) > 1 else None,
        "ci95": bootstrap_ci_of_mean(diffs),
        "b_greater": sum(1 for d in diffs if d > 0),
        "nonzero": sum(1 for d in diffs if d != 0),
    }
    out["sign_p"] = sign_test_p(out["b_greater"], out["nonzero"])
    if first_odd in ("a", "b"):
        a_first = [d for r, d in zip(rounds, diffs) if (r % 2 == 1) == (first_odd == "a")]
        b_first = [d for r, d in zip(rounds, diffs) if (r % 2 == 1) != (first_odd == "a")]
        out["order"] = {"a_first": (statistics.mean(a_first) if a_first else None, len(a_first)),
                        "b_first": (statistics.mean(b_first) if b_first else None, len(b_first))}
    return out


def render(scenario, a_label, b_label, metric, s):
    if s is None:
        return f"{scenario}: no round has a value for both configurations"
    unit = "s/1k req" if metric == "cpu" else ("rps" if metric == "throughput_rps" else "ms")
    lines = [f"{scenario}  [{metric}, {unit}]  rounds {s['rounds']}",
             f"  {a_label:<12} mean {s['mean_a']:.3f}  (sd across rounds {s['sd_a']:.3f})" if s["sd_a"] is not None
             else f"  {a_label:<12} mean {s['mean_a']:.3f}",
             f"  {b_label:<12} mean {s['mean_b']:.3f}  (sd across rounds {s['sd_b']:.3f})" if s["sd_b"] is not None
             else f"  {b_label:<12} mean {s['mean_b']:.3f}",
             "  paired diff per round (B - A): " + ", ".join(f"{d:+.3f}" for d in s["diffs"])]
    pct = f" = {s['pct_diff']:+.1f}% of {a_label}" if s["pct_diff"] is not None else ""
    sd = f", sd {s['sd_diff']:.3f}" if s["sd_diff"] is not None else ""
    lines.append(f"  mean paired diff: {s['mean_diff']:+.3f}{pct}{sd}")
    if s["ci95"]:
        lo, hi = s["ci95"]
        lines.append(f"  bootstrap 95% CI of the mean diff: [{lo:+.3f}, {hi:+.3f}]  -> "
                     + ("excludes 0" if lo > 0 or hi < 0 else "includes 0"))
    if s["sign_p"] is not None:
        lines.append(f"  {b_label} > {a_label} in {s['b_greater']} of {s['nonzero']} rounds (sign test p = {s['sign_p']:.3f})")
    if "order" in s:
        (am, an), (bm, bn) = s["order"]["a_first"], s["order"]["b_first"]
        f = lambda v: "n/a" if v is None else f"{v:+.3f}"
        lines.append(f"  order effect: mean diff when {a_label} ran first {f(am)} (n={an}), when {b_label} ran first {f(bm)} (n={bn})")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="results/releases")
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--a", required=True, help="config label A (the reference)")
    ap.add_argument("--b", required=True, help="config label B (the difference is B - A)")
    ap.add_argument("--scenarios", required=True, help="comma separated result keys")
    ap.add_argument("--metric", default="cpu", choices=("cpu", "p95_ms", "p99_ms", "ttft_p95_ms", "throughput_rps"))
    ap.add_argument("--first-odd", choices=("a", "b"), default=None,
                    help="which config ran first in odd rounds (enables the order-effect line)")
    args = ap.parse_args(argv)

    any_data = False
    for scenario in [s for s in args.scenarios.split(",") if s]:
        a = collect(args.root, args.prefix, args.a, scenario, args.metric)
        b = collect(args.root, args.prefix, args.b, scenario, args.metric)
        stats = analyze(a, b, args.first_odd)
        any_data = any_data or stats is not None
        print(render(scenario, args.a, args.b, args.metric, stats))
        print()
    return 0 if any_data else 2


if __name__ == "__main__":
    sys.exit(main())
