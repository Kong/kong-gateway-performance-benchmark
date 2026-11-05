#!/usr/bin/env python3
"""
harness_calibration.py — how much can the load generator + the upstream sustain with the gateway
out of the way?

A gateway breakpoint ("max sustainable RPS") is only meaningful if the harness itself can push more
than the gateway can take. The fake provider is a single-threaded Node process on a shared node and
k6 parses every response, so either may run out first. This tool finds the harness ceiling:

  * it runs the `direct-calibration` scenario (k6 -> fake provider, zero delay, status-only checks,
    response bodies discarded) at increasing request rates, one TestRun per step;
  * a step PASSES when k6 achieved >= 97% of the target rate (completed iterations / step duration),
    dropped <= 1% of the iterations, failed <= 1% of the requests AND p95 stayed <= 10 ms. The upstream
    answers immediately, so a larger p95 means the harness is queueing: the rate may still be met, but a
    gateway measured through it would be charged for that queueing;
  * the sweep stops at the first failing step (--no-stop keeps going);
  * for every step it reads the fake provider's CPU (cgroup counter, cores = CPU seconds / step
    duration) and polls the k6 pod's CPU (`kubectl top`) to say WHO ran out first at the failing step:
    whichever component is the busier relative to its capacity (the fake provider is a single Node process,
    about 1 core; k6 has a 60-core limit), or neither when both are below half of theirs (then look at the
    network / kube-proxy / conntrack between them).

Output: calibration.json and calibration.md in --out-dir, and a table on stdout.
Gateway breakpoint results are only meaningful below `harness_ceiling_rps`.

Usage (from deploy-k8s-resources/k6_tests, against the cluster in the current kube context):
  python3 harness_calibration.py
  python3 harness_calibration.py --steps 500,2000,5000 --step-duration 60s
"""
import argparse
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aggregate_release as ag  # noqa: E402
import cpu_sample  # noqa: E402

DEFAULT_STEPS = [200, 500, 1000, 2000, 3000, 4000, 6000, 8000]
MIN_ACHIEVED_RATIO = 0.97
MAX_DROPPED_PCT = 1.0
MAX_ERROR_PCT = 1.0
MAX_P95_MS = 10.0                    # zero-delay upstream: more than this is queueing in the harness
UPSTREAM_CAPACITY_CORES = 1.0        # a single-threaded Node process
LOADGEN_CORES_LIMIT = 60.0           # runner limits.cpu in k6-ai-benchmark-test.yaml
MIN_BOTTLENECK_UTILIZATION = 0.5     # below this a component is not what ran out
LOG_PATTERN = ("AI benchmark summary|http_req_duration (avg|med|p90|p95|p99|max)|http_reqs/s|http_req_failed|"
               "checks pass|iterations total|http status counts")


# --------------------------------------------------------------------------- pure logic
def parse_duration_seconds(text):
    m = re.fullmatch(r"([1-9][0-9]*)(s|m)", str(text))
    if not m:
        raise ValueError(f"duration must look like 30s or 2m, got {text!r}")
    return int(m.group(1)) * (60 if m.group(2) == "m" else 1)


def parse_top_cores(text):
    """`kubectl top pod --no-headers` output -> the largest CPU value in cores ('1523m' / '2')."""
    best = None
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        m = re.fullmatch(r"(\d+)(m?)", parts[1])
        if m:
            cores = int(m.group(1)) / (1000.0 if m.group(2) else 1.0)
            best = cores if best is None else max(best, cores)
    return best


def step_from_log(target_rps, log_path, duration_s, upstream_cpu_seconds=None, loadgen_cores=None):
    p = ag.parse_log(log_path)
    # Achieved rate = iterations k6 completed / the step duration. k6's own http_reqs/s divides by the
    # wall-clock time of the whole test, start-up and shut-down included: ~1 s that is 20% of a 5 s step and
    # 3% of a 30 s step, which would fail a healthy harness. It is only the fallback.
    it = ag.ITERATIONS_RX.search(Path(log_path).read_text(errors="ignore"))
    achieved = int(it.group(1)) / duration_s if it else p.get("throughput_rps")
    return {
        "target_rps": target_rps,
        "achieved_rps": achieved,
        "k6_reqs_per_s": p.get("throughput_rps"),
        "p95_ms": p.get("p95_ms"),
        "p99_ms": p.get("p99_ms"),
        "error_pct": p.get("error_rate_pct"),
        "dropped_pct": p.get("dropped_pct"),
        "status": p.get("status"),
        "upstream_cpu_cores": None if upstream_cpu_seconds is None else round(upstream_cpu_seconds / duration_s, 3),
        "loadgen_cpu_cores": loadgen_cores,
    }


def k6_error_line(logs):
    """The first line in a k6 pod log that looks like an error, trimmed; None when there is none."""
    for line in (logs or "").splitlines():
        if re.search(r"level=error|ERRO|invalid|must be|cannot|failed to", line, re.I):
            return re.sub(r"\s+", " ", line).strip()[:200]
    return None


def evaluate_step(step, min_ratio=MIN_ACHIEVED_RATIO, max_dropped=MAX_DROPPED_PCT, max_error=MAX_ERROR_PCT,
                  max_p95=MAX_P95_MS):
    """(passed, reasons). A step with no readable result fails."""
    reasons = []
    achieved = step.get("achieved_rps")
    if achieved is None:
        why = step.get("k6_error")
        return False, ["no k6 summary could be read" + (f" (k6 said: {why})" if why else "")]
    if achieved < step["target_rps"] * min_ratio:
        reasons.append(f"achieved {achieved:.0f} of {step['target_rps']} rps ({achieved / step['target_rps']:.0%})")
    if step.get("dropped_pct") is not None and step["dropped_pct"] > max_dropped:
        reasons.append(f"dropped {step['dropped_pct']:.1f}% of the iterations")
    if step.get("error_pct") is not None and step["error_pct"] > max_error:
        reasons.append(f"{step['error_pct']:.1f}% of the requests failed")
    if step.get("p95_ms") is not None and step["p95_ms"] > max_p95:
        reasons.append(f"p95 {step['p95_ms']:.1f} ms > {max_p95:g} ms: the harness is queueing")
    return not reasons, reasons


def attribute_bottleneck(step, upstream_capacity=UPSTREAM_CAPACITY_CORES, loadgen_limit=LOADGEN_CORES_LIMIT,
                         min_utilization=MIN_BOTTLENECK_UTILIZATION):
    """Who ran out first at a failing step: (who, explanation).

    Compares how busy each component is RELATIVE to its capacity (an absolute CPU threshold is fragile:
    one Node process already queues at 70-80% of a core) and blames the busier one, provided it is at
    least `min_utilization` busy."""
    up, lg = step.get("upstream_cpu_cores"), step.get("loadgen_cpu_cores")
    up_u = None if up is None else up / upstream_capacity
    lg_u = None if lg is None else lg / loadgen_limit
    seen = []
    if up is not None:
        seen.append(f"the fake provider used {up:.2f} of ~{upstream_capacity:g} core ({up_u:.0%})")
    if lg is not None:
        seen.append(f"k6 used {lg:.1f} of its {loadgen_limit:g}-core limit ({lg_u:.0%})")
    detail = "; ".join(seen) if seen else "no CPU data"
    candidates = [(u, who) for u, who in ((up_u, "upstream"), (lg_u, "load generator"))
                  if u is not None and u >= min_utilization]
    if not candidates:
        return "undetermined", (f"neither component is at least {min_utilization:.0%} busy ({detail}): "
                                f"look at the network, kube-proxy / conntrack or the node between them")
    _, who = max(candidates)
    advice = ("scale the fake provider out (more replicas, its own node) before measuring gateway capacity"
              if who == "upstream" else
              "use a lighter script, more k6 runners or a bigger load-generator node")
    return who, f"{detail}; {advice}"


def sweep(steps, run_step, stop_on_fail=True, **thresholds):
    """Run `run_step(rps) -> step dict` for every rate, evaluating each, optionally stopping at the first failure."""
    results = []
    for rps in steps:
        step = run_step(rps)
        passed, reasons = evaluate_step(step, **{k: v for k, v in thresholds.items()
                                                 if k in ("min_ratio", "max_dropped", "max_error", "max_p95")})
        step["passed"], step["reasons"] = passed, reasons
        results.append(step)
        if not passed and stop_on_fail:
            break
    return results


def summarize(results, **attr_kwargs):
    first_fail = next((s for s in results if not s["passed"]), None)
    passing_before = [s["target_rps"] for s in results
                      if s["passed"] and (first_fail is None or s["target_rps"] < first_fail["target_rps"])]
    ceiling = max(passing_before) if passing_before else None
    out = {"harness_ceiling_rps": ceiling, "first_failing_rps": None if first_fail is None else first_fail["target_rps"],
           "limited_by": None, "explanation": None, "found_a_limit": first_fail is not None}
    if first_fail is None:
        out["explanation"] = (f"every step up to {results[-1]['target_rps']} rps held: the ceiling is at least that "
                              f"high; extend --steps to find it") if results else "no steps were run"
        return out
    out["limited_by"], out["explanation"] = attribute_bottleneck(first_fail, **attr_kwargs)
    if ceiling is None:
        out["explanation"] = (f"even the first step ({first_fail['target_rps']} rps) failed: " +
                              "; ".join(first_fail["reasons"]) + ". " + out["explanation"])
    return out


def render_markdown(results, summary, meta):
    lines = ["# Harness calibration — load generator + upstream, gateway out of the way", "",
             f"Run: {meta['started_at']}  |  step duration {meta['step_duration']}  |  "
             f"pass = achieved >= {MIN_ACHIEVED_RATIO:.0%} of target, dropped <= {MAX_DROPPED_PCT:g}%, "
             f"errors <= {MAX_ERROR_PCT:g}%, p95 <= {MAX_P95_MS:g} ms", "",
             "| Target rps | Achieved rps | p95 ms | Dropped % | Errors % | Fake provider cores | k6 cores | Result |",
             "|---|---|---|---|---|---|---|---|"]

    def cell(v, fmt):
        return "-" if v is None else format(v, fmt)

    for s in results:
        verdict = "PASS" if s["passed"] else "FAIL: " + "; ".join(s["reasons"])
        lines.append(f"| {s['target_rps']} | {cell(s['achieved_rps'], '.1f')} | {cell(s['p95_ms'], '.1f')} | "
                     f"{cell(s['dropped_pct'], '.2f')} | {cell(s['error_pct'], '.2f')} | "
                     f"{cell(s['upstream_cpu_cores'], '.2f')} | {cell(s['loadgen_cpu_cores'], '.1f')} | {verdict} |")
    ceiling = summary["harness_ceiling_rps"]
    lines += ["", "## Result", ""]
    if summary["found_a_limit"]:
        lines.append(f"- **Harness ceiling: {ceiling if ceiling is not None else 'below the first step'} rps** "
                     f"(first failing step: {summary['first_failing_rps']} rps).")
        lines.append(f"- **Limited by: {summary['limited_by']}.** {summary['explanation']}")
        if ceiling is not None:
            lines.append(f"- Gateway capacity numbers are only meaningful below ~{int(ceiling * 0.8)} rps "
                         f"(80% of the ceiling, to keep the harness out of the measurement) unless the limit above is lifted.")
    else:
        lines.append(f"- {summary['explanation']}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- cluster side
def kubectl(args, kube="kubectl", timeout=120):
    try:
        r = subprocess.run([kube, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def runner_pod(kube):
    out = kubectl(["get", "pods", "-n", "k6", "--no-headers"], kube) or ""
    for line in out.splitlines():
        if "k6-ai-benchmark-1" in line:
            return line.split()[0]
    return None


def make_cluster_step(args):
    """The real run_step: one TestRun at the given rate, results read from the runner pod's log."""
    duration_s = parse_duration_seconds(args.step_duration)
    out_dir = Path(args.out_dir)
    bench_dir = Path(args.bench_dir)

    def read_upstream_cpu():
        return cpu_sample.read_seconds(args.kubectl, "upstream", "deploy/fake-provider", "")

    def run_step(rps):
        before = read_upstream_cpu()
        kubectl(["delete", "testrun", "-n", "k6", "--all", "--ignore-not-found"], args.kubectl)
        time.sleep(args.settle_seconds)
        log = out_dir / f"step_{rps}.driver.log"
        with log.open("w") as fh:
            subprocess.run(["bash", args.bench_script, "direct-calibration", "zero", str(rps), args.step_duration],
                           cwd=bench_dir, stdout=fh, stderr=subprocess.STDOUT)
        # wait for the runner pod, polling its CPU while it runs
        peak = {"cores": None}
        stop = threading.Event()

        def poll_top():
            while not stop.is_set():
                cores = parse_top_cores(kubectl(["top", "pod", "-n", "k6", "--no-headers"], args.kubectl, 30))
                if cores is not None:
                    peak["cores"] = cores if peak["cores"] is None else max(peak["cores"], cores)
                stop.wait(args.top_interval)

        poller = threading.Thread(target=poll_top, daemon=True)
        poller.start()
        pod, phase = None, None
        for _ in range(args.max_polls):
            pod = runner_pod(args.kubectl)
            if pod:
                phase = (kubectl(["get", "pod", "-n", "k6", pod, "-o", "jsonpath={.status.phase}"], args.kubectl) or "").strip()
                if phase in ("Succeeded", "Failed"):
                    break
            time.sleep(args.poll_seconds)
        stop.set()
        poller.join(timeout=5)
        logs = kubectl(["logs", "-n", "k6", pod], args.kubectl) if pod else None
        step_log = out_dir / f"step_{rps}.log"
        step_log.write_text("\n".join(l for l in (logs or "").splitlines() if re.search(LOG_PATTERN, l)) + "\n")
        (out_dir / f"step_{rps}.k6.log").write_text(logs or "")      # the raw pod log, for when a step fails
        after = read_upstream_cpu()
        cpu = None if before is None or after is None or after < before else after - before
        result = step_from_log(rps, step_log, duration_s, cpu, peak["cores"])
        result["k6_error"] = k6_error_line(logs) if result["achieved_rps"] is None else None
        return result

    return run_step


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--steps", default=",".join(map(str, DEFAULT_STEPS)), help="target request rates, comma separated")
    ap.add_argument("--step-duration", default="30s")
    ap.add_argument("--out-dir", default=None, help="default: results/calibration/<timestamp>")
    ap.add_argument("--no-stop", action="store_true", help="keep going after the first failing step")
    ap.add_argument("--max-p95-ms", type=float, default=MAX_P95_MS,
                    help="p95 above this fails a step: the upstream answers at once, so it is harness queueing")
    ap.add_argument("--kubectl", default="kubectl")
    ap.add_argument("--bench-dir", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--bench-script", default="run_ai_benchmark.sh")
    ap.add_argument("--settle-seconds", type=float, default=4.0)
    ap.add_argument("--poll-seconds", type=float, default=5.0)
    ap.add_argument("--top-interval", type=float, default=10.0)
    ap.add_argument("--max-polls", type=int, default=80)
    args = ap.parse_args(argv)

    try:
        steps = [int(x) for x in args.steps.split(",") if x]
        parse_duration_seconds(args.step_duration)
    except ValueError as exc:
        print(f"harness_calibration.py: {exc}", file=sys.stderr)
        return 2
    if not steps or any(s <= 0 for s in steps) or steps != sorted(set(steps)):
        print("harness_calibration.py: --steps must be positive, ascending and unique", file=sys.stderr)
        return 2

    started = datetime.now(timezone.utc)
    out_dir = Path(args.out_dir or Path("results") / "calibration" / started.strftime("%Y%m%dT%H%M%SZ"))
    out_dir.mkdir(parents=True, exist_ok=True)
    args.out_dir = str(out_dir)
    meta = {"started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "step_duration": args.step_duration, "steps": steps}

    results = sweep(steps, make_cluster_step(args), stop_on_fail=not args.no_stop, max_p95=args.max_p95_ms)
    summary = summarize(results)
    (out_dir / "calibration.json").write_text(json.dumps({"meta": meta, "summary": summary, "steps": results}, indent=2))
    report = render_markdown(results, summary, meta)
    (out_dir / "calibration.md").write_text(report)
    print(report)
    print(f"written to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
