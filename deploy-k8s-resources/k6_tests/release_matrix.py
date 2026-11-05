#!/usr/bin/env python3
"""Release-matrix planner.

Turns release_matrix.yaml into the ordered list of (round, scenario, fixture,
load, key) runs that run_release_baseline.sh executes.

Ordering modes
--------------
interleaved (default)
    Round-major: every round runs every scenario once, then the next round.
    Entries that share a `group` (gateway/direct pairs, ...) run back-to-back
    and their internal order rotates each round (A B | B A | A B ...), which
    cancels order effects and slow environment drift out of the pair deltas.
    If a run is interrupted every scenario has the same number of repeats
    (+/- one round) instead of some scenarios having all repeats and others none.
    The order of groups is the same in every round and in every release, so
    position effects cancel in cross-release comparisons.
shuffle
    Like interleaved, but the order of groups is shuffled per round with a
    seeded RNG (SEED), for hunting position effects. Reproducible.
sequential
    Legacy order: scenario-major (all repeats of a scenario, then the next).

Usage
-----
    release_matrix.py plan    [--matrix F] [--tier tier1|tier2] [--repeats N]
                              [--include-mlflow50 true|false] [--include-pending true|false]
                              [--only-keys k1,k2,...] [--expand-groups true|false]
                              [--order interleaved|shuffle|sequential]
                              [--seed N] [--format tsv|json]
    release_matrix.py summary [same options] [--duration 3m]
"""
import argparse
import json
import random
import re
import sys
from pathlib import Path

import yaml

TRACKS = ("core", "mlflow50")
TIERS = ("tier1", "tier2")
ORDERS = ("interleaved", "shuffle", "sequential")
REQUIRED = ("scenario", "fixture", "load", "track")
# Per-run overhead on top of the k6 duration (testrun cleanup, pod start, log
# collection). Measured at ~0.8-1.0 min on the 2.3.0-rc.2 run; kept in sync with
# the CI runtime estimate.
RUN_OVERHEAD_MIN = 1


class MatrixError(ValueError):
    pass


def result_key(scenario, fixture):
    """Name used for per-run logs and metrics.json (unchanged from the old runner)."""
    return scenario if fixture == "short" else f"{scenario}__{fixture}"


def load_matrix(path):
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise MatrixError(f"{path}: expected a top-level 'entries' list")

    entries, seen = [], {}
    for i, raw in enumerate(data["entries"], 1):
        where = f"{path}: entry #{i}"
        if not isinstance(raw, dict):
            raise MatrixError(f"{where}: must be a mapping")
        missing = [f for f in REQUIRED if f not in raw]
        if missing:
            raise MatrixError(f"{where}: missing field(s) {', '.join(missing)}")
        if raw["track"] not in TRACKS:
            raise MatrixError(f"{where}: track must be one of {TRACKS}, got {raw['track']!r}")
        if isinstance(raw["load"], bool) or not isinstance(raw["load"], int) or raw["load"] <= 0:
            raise MatrixError(f"{where}: load must be a positive integer, got {raw['load']!r}")
        for name in ("scenario", "fixture"):
            if not re.fullmatch(r"[A-Za-z0-9._-]+", str(raw[name])):
                raise MatrixError(f"{where}: {name} {raw[name]!r} has characters outside [A-Za-z0-9._-]")
        group = raw.get("group")
        if group is not None and not re.fullmatch(r"[A-Za-z0-9._-]+", str(group)):
            raise MatrixError(f"{where}: group {group!r} has characters outside [A-Za-z0-9._-]")
        key = result_key(raw["scenario"], raw["fixture"])
        if key in seen:
            raise MatrixError(f"{where}: duplicate result key {key!r} (also entry #{seen[key]})")
        seen[key] = i
        entries.append({
            "scenario": str(raw["scenario"]),
            "fixture": str(raw["fixture"]),
            "load": raw["load"],
            "track": raw["track"],
            "group": group,
            "tier1": bool(raw.get("tier1", False)),
            "enabled": bool(raw.get("enabled", True)),
            "pending": raw.get("pending"),
            "key": key,
        })
    return entries


def select(entries, tier="tier2", include_mlflow50=True, include_pending=False):
    """Pick the entries to run. Disabled entries that carry a `pending` note are
    implemented but not yet verified on a cluster; include_pending=True runs them
    (first cluster smoke run). Disabled entries without a note are never planned."""
    if tier not in TIERS:
        raise MatrixError(f"tier must be one of {TIERS}, got {tier!r}")
    out = []
    for e in entries:
        if not e["enabled"] and not (include_pending and e["pending"]):
            continue
        if e["track"] == "mlflow50" and not include_mlflow50:
            continue
        if tier == "tier1" and not e["tier1"]:
            continue
        out.append(e)
    return out


def restrict(entries, keys, expand_groups=True):
    """Only the entries whose result key is in `keys`. With expand_groups, every entry that shares
    a group with one of them comes along: re-running a gateway scenario needs its direct control
    from the same round, because the overhead is a paired difference."""
    wanted = set(keys)
    chosen = [e for e in entries if e["key"] in wanted]
    if expand_groups:
        groups = {e["group"] for e in chosen if e["group"]}
        chosen = [e for e in entries if e["key"] in wanted or (e["group"] and e["group"] in groups)]
    return chosen


def build_groups(entries):
    """Ordered list of groups; an ungrouped entry is a group of one.
    A group sits at the position of its first member."""
    groups, index = [], {}
    for e in entries:
        gid = e["group"] if e["group"] else f"__single__{e['key']}"
        if gid not in index:
            index[gid] = len(groups)
            groups.append([])
        groups[index[gid]].append(e)
    return groups


def plan(entries, repeats, order="interleaved", seed=1):
    if order not in ORDERS:
        raise MatrixError(f"order must be one of {ORDERS}, got {order!r}")
    if repeats < 1:
        raise MatrixError("repeats must be >= 1")

    runs = []
    if order == "sequential":
        for e in entries:
            for r in range(1, repeats + 1):
                runs.append(_run(r, e))
        return runs

    groups = build_groups(entries)
    for r in range(1, repeats + 1):
        round_groups = list(groups)
        if order == "shuffle":
            random.Random(seed * 1000 + r).shuffle(round_groups)
        for members in round_groups:
            rot = (r - 1) % len(members)
            for e in members[rot:] + members[:rot]:
                runs.append(_run(r, e))
    return runs


def _run(round_no, e):
    return {"round": round_no, "scenario": e["scenario"], "fixture": e["fixture"],
            "load": e["load"], "key": e["key"], "pending": not e["enabled"]}


def parse_duration_minutes(text):
    m = re.fullmatch(r"([1-9][0-9]*)m", str(text))
    if not m:
        raise MatrixError(f"duration must be whole minutes like 3m, got {text!r}")
    return int(m.group(1))


def _common(ap):
    ap.add_argument("--matrix", default=str(Path(__file__).with_name("release_matrix.yaml")))
    ap.add_argument("--tier", default="tier2", choices=TIERS)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--include-mlflow50", default="true", choices=("true", "false"))
    ap.add_argument("--include-pending", default="false", choices=("true", "false"),
                    help="also plan entries that are implemented but not yet enabled (cluster smoke run)")
    ap.add_argument("--only-keys", default="",
                    help="comma-separated result keys: plan only these (used to re-run flagged scenarios)")
    ap.add_argument("--expand-groups", default="true", choices=("true", "false"),
                    help="with --only-keys, also plan the entries that share a group (direct controls)")
    ap.add_argument("--order", default="interleaved", choices=ORDERS)
    ap.add_argument("--seed", type=int, default=1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    _common(p)
    p.add_argument("--format", default="tsv", choices=("tsv", "json"))
    s = sub.add_parser("summary")
    _common(s)
    s.add_argument("--duration", default="3m")
    args = ap.parse_args(argv)

    try:
        entries = select(load_matrix(args.matrix), args.tier, args.include_mlflow50 == "true",
                         args.include_pending == "true")
        if args.only_keys:
            keys = [k for k in args.only_keys.split(",") if k]
            known = {e["key"] for e in entries}
            unknown = sorted(set(keys) - known)
            if unknown:
                print(f"release_matrix.py: ignoring keys that are not in the selected plan: {', '.join(unknown)}",
                      file=sys.stderr)
            entries = restrict(entries, keys, args.expand_groups == "true")
        if not entries:
            raise MatrixError("no runnable entries selected")
        runs = plan(entries, args.repeats, args.order, args.seed)
        if args.cmd == "plan":
            if args.format == "json":
                json.dump(runs, sys.stdout, indent=2)
                print()
            else:
                for r in runs:
                    print("\t".join(str(r[k]) for k in ("round", "scenario", "fixture", "load", "key")),
                          "pending" if r["pending"] else "enabled", sep="\t")
        else:
            minutes = len(runs) * (parse_duration_minutes(args.duration) + RUN_OVERHEAD_MIN)
            pending = sum(1 for e in entries if not e["enabled"])
            print(f"tier={args.tier} order={args.order} scenarios={len(entries)} repeats={args.repeats} "
                  f"runs={len(runs)} duration={args.duration} est_minutes={minutes} est_hours={minutes / 60:.1f}"
                  + (f" pending={pending}" if pending else ""))
    except MatrixError as exc:
        print(f"release_matrix.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
