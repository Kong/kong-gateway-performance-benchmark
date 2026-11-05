#!/usr/bin/env python3
"""
cpu_sample.py — Kong CPU seconds per 1k requests, from the container's own cgroup.

At low load latency barely moves between releases, but the CPU the gateway burns per request is
a sensitive efficiency signal. The release runner reads the cumulative CPU time of the Kong
`proxy` container before and after every gateway run:

  cgroup v2   /sys/fs/cgroup/cpu.stat          usage_usec
  cgroup v1   /sys/fs/cgroup/cpuacct/cpuacct.usage   nanoseconds

and appends one line to the per-run log:

  kong cpu: seconds=<delta> iterations=<completed iterations> seconds_per_1k=<delta / iterations * 1000>

(read straight from the container, so there is no dependency on Prometheus scraping cAdvisor).

What the number is: the container's total CPU over the measurement window, idle work included
(timers, health checks, the metrics endpoint), divided by the iterations k6 completed. The window
runs from just before the TestRun is applied to just after it finished, so it also contains the
k6 start-up, which is roughly constant from release to release; compare_release.py gates on the
RELATIVE change, which cancels that constant. It is not a pure per-request cost.

Usage:
  cpu_sample.py read   [--kubectl kubectl]            print the cumulative CPU seconds of the Kong proxy
                       [--namespace N --target deploy/X --container C]   ... or of another container
  cpu_sample.py record --before S --after S --log F   append the "kong cpu:" line to the per-run log
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

SNIPPET = (
    "if [ -r /sys/fs/cgroup/cpu.stat ]; then "
    "echo v2 $(sed -n 's/^usage_usec //p' /sys/fs/cgroup/cpu.stat); "
    "else echo v1 $(cat /sys/fs/cgroup/cpuacct/cpuacct.usage 2>/dev/null "
    "|| cat /sys/fs/cgroup/cpu,cpuacct/cpuacct.usage); fi"
)
KUBECTL_ARGS = ["exec", "-n", "kong", "deploy/kong-kong", "-c", "proxy", "--", "sh", "-c", SNIPPET]


def kubectl_args(namespace="kong", target="deploy/kong-kong", container="proxy"):
    """The kubectl exec command that prints the CPU counter of a container (Kong's proxy by default;
    harness_calibration.py reads the fake provider's). An empty container means the pod's only one."""
    args = ["exec", "-n", namespace, target]
    if container:
        args += ["-c", container]
    return args + ["--", "sh", "-c", SNIPPET]
ITERATIONS_RX = re.compile(r"^iterations total:\s*(\d+)", re.M)


def parse_cpu_sample(text):
    """'v2 123456789' (microseconds) or 'v1 123456789000' (nanoseconds) -> seconds; None if unreadable."""
    parts = (text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    value = int(parts[1])
    if parts[0] == "v2":
        return value / 1e6
    if parts[0] == "v1":
        return value / 1e9
    return None


def read_seconds(kubectl="kubectl", namespace="kong", target="deploy/kong-kong", container="proxy"):
    try:
        r = subprocess.run([kubectl, *kubectl_args(namespace, target, container)],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_cpu_sample(r.stdout) if r.returncode == 0 else None


def iterations_in(log_text):
    m = ITERATIONS_RX.search(log_text)
    return int(m.group(1)) if m else None


def format_line(before, after, iterations):
    """The log line, or (None, reason) when the sample cannot be trusted."""
    if iterations is None:
        return None, "no 'iterations total' line in the per-run log"
    if iterations <= 0:
        return None, "k6 completed no iterations"
    delta = after - before
    if delta < 0:
        return None, "the CPU counter went backwards (the Kong container restarted during the run?)"
    return f"kong cpu: seconds={delta:.3f} iterations={iterations} seconds_per_1k={delta / iterations * 1000:.4f}", None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read")
    r.add_argument("--kubectl", default="kubectl")
    r.add_argument("--namespace", default="kong")
    r.add_argument("--target", default="deploy/kong-kong", help="what to exec into, e.g. deploy/fake-provider")
    r.add_argument("--container", default="proxy", help="container name; empty for the pod's only container")
    rec = sub.add_parser("record")
    rec.add_argument("--before", type=float, required=True)
    rec.add_argument("--after", type=float, required=True)
    rec.add_argument("--log", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "read":
        seconds = read_seconds(args.kubectl, args.namespace, args.target, args.container)
        if seconds is None:
            return 1
        print(f"{seconds:.6f}")
        return 0

    log = Path(args.log)
    line, why = format_line(args.before, args.after, iterations_in(log.read_text(errors="ignore")))
    if line is None:
        print(f"cpu_sample.py: not recording: {why}", file=sys.stderr)
        return 1
    with log.open("a") as fh:
        fh.write(line + "\n")
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
