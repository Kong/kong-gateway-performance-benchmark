#!/bin/bash
# =============================================================================
# run_release_baseline.sh — capture a full release benchmark for evidence.
#
# Runs the release scenario matrix (release_matrix.yaml) at a fixed load
# profile, REPEATS times each, saves every k6 summary, aggregates into
# metrics.json + summary.csv, and emits a release report. Store the output dir
# as a release record; point baseline.json at the first one. Re-run for each
# release, then diff with compare_release.py.
#
# Usage:
#   VERSION=ai-2.0.0-rc.2 ./run_release_baseline.sh
#   VERSION=... REPEATS=3 DURATION=3m ./run_release_baseline.sh
#
# Matrix selection / ordering (see release_matrix.py for details):
#   TIER=tier2|tier1        tier2 (default) = every enabled entry (per release);
#                           tier1 = the cheap per-build subset.
#   ORDER=interleaved       (default) round-major: every round runs every
#                           scenario once; gateway/direct pairs run back-to-back
#                           and swap order each round.
#         shuffle           like interleaved, group order shuffled per round (SEED).
#         sequential        legacy order: all repeats of one scenario, then the next.
#   SEED=1                  RNG seed for ORDER=shuffle.
#   MATRIX_FILE=PATH        alternative matrix file.
#   INCLUDE_MLFLOW50_TRACK  true (default) | false.
#   INCLUDE_PENDING=true    also run the entries that are implemented but not enabled yet
#                           (cluster smoke run for new scenarios), e.g.
#                             TIER=tier1 INCLUDE_PENDING=true REPEATS=1 ./run_release_baseline.sh
#   ONLY_KEYS=a,b           run only these result keys (e.g. token-chat-openai__mlflow50) plus the entries
#                           that share a group with them, i.e. their direct controls
#                           (EXPAND_GROUPS=false: only the listed keys, e.g. when only the gateway's CPU matters).
#   WARMUP_DURATION=30s     (default 30s) warm-up run before the FIRST timed run of every scenario/fixture pair: the same
#                           k6 run, with this duration, whose result is thrown away. It fills lazy caches (KFC
#                           code paths) so that round 1 is comparable with the later rounds. Default: the
#                           `global.warmup_duration` of the SLO config, 30s now. WARMUP_DURATION=0s turns the warm-up off. Gateway and direct
#                           entries are both warmed, so a pair stays symmetric. The warm-up is not in the CPU sample.
#   DRY_RUN=true            print the plan and the time estimate, run nothing
#                           (no cluster access, no output directory).
#   CONFIRM_RERUN=true      (default) when the comparison with the baseline flags REGRESSION,
#                           UNSTABLE or overhead-budget findings, re-run just those scenarios (plus their direct controls) once
#                           into <outdir>-confirm and compare again with --confirm: only findings that
#                           reproduce count. The first-pass report is kept as report.first-pass.md.
#                           false disables it.
#   CONFIRM_MAX_SCENARIOS=8 skip the confirmation run when more scenarios than this are flagged: that
#                           many findings usually mean an environment problem, re-run the release.
# =============================================================================
set -uo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

VERSION="${VERSION:-unknown}"
REPEATS="${REPEATS:-3}"
DURATION="${DURATION:-3m}"
INCLUDE_MLFLOW50_TRACK="${INCLUDE_MLFLOW50_TRACK:-true}"
INCLUDE_PENDING="${INCLUDE_PENDING:-false}"
ONLY_KEYS="${ONLY_KEYS:-}"
EXPAND_GROUPS="${EXPAND_GROUPS:-true}"
TIER="${TIER:-tier2}"
ORDER="${ORDER:-interleaved}"
SEED="${SEED:-1}"
WARMUP_DURATION="${WARMUP_DURATION:-}"
DRY_RUN="${DRY_RUN:-false}"
INTERRUPTED=0
CONFIRM_RERUN="${CONFIRM_RERUN:-true}"
CONFIRM_MAX_SCENARIOS="${CONFIRM_MAX_SCENARIOS:-8}"
# Settling pauses (seconds): after deleting the previous TestRun, and before reading the fake
# provider stats so streams that were not cancelled show up as completed.
DRIVER_SETTLE_SECONDS="${DRIVER_SETTLE_SECONDS:-4}"
UPSTREAM_SETTLE_SECONDS="${UPSTREAM_SETTLE_SECONDS:-3}"
MATRIX_FILE="${MATRIX_FILE:-$SCRIPT_DIR/release_matrix.yaml}"
GATES_FILE="${RELEASE_GATES_PATH:-$SCRIPT_DIR/release_gates.yaml}"
SLO_CONFIG_FILE="${SLO_CONFIG_PATH:-$SCRIPT_DIR/benchmark_config.yaml}"
CAPTURED_AT="${CAPTURED_AT:-$(date -u +"%Y-%m-%dT%H:%M:%SZ")}"
DATESTAMP="$(date -u +"%Y-%m-%d")"
OUTDIR="results/releases/${VERSION}_${DATESTAMP}"

if [ ! -f "$GATES_FILE" ]; then
  echo "ERROR: gates file not found: $GATES_FILE" >&2
  exit 2
fi

if [ ! -f "$SLO_CONFIG_FILE" ]; then
  echo "ERROR: SLO config file not found: $SLO_CONFIG_FILE" >&2
  exit 2
fi

if [ ! -f "$MATRIX_FILE" ]; then
  echo "ERROR: matrix file not found: $MATRIX_FILE" >&2
  exit 2
fi

# Warm-up length: WARMUP_DURATION, else global.warmup_duration of the SLO config, else 0s.
if [[ -z "$WARMUP_DURATION" ]]; then
  WARMUP_DURATION=$(python3 - "$SLO_CONFIG_FILE" <<'PY'
import sys
try:
    import yaml
    print(yaml.safe_load(open(sys.argv[1])).get("global", {}).get("warmup_duration", "0s"))
except Exception:
    print("0s")
PY
  ) || WARMUP_DURATION="0s"
fi
if ! [[ "$WARMUP_DURATION" =~ ^(0|[0-9]+[smh])$ ]]; then
  echo "ERROR: WARMUP_DURATION must be 0 or a number with s, m or h (for example 30s), got '$WARMUP_DURATION'" >&2
  exit 2
fi
warmup_enabled() { [[ "$WARMUP_DURATION" != "0" && "$WARMUP_DURATION" != 0[smh] ]]; }

if ! [[ "$REPEATS" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: REPEATS must be a positive integer, got '$REPEATS'" >&2
  exit 2
fi

# Build the ordered run plan: one TSV line per run
#   round <TAB> scenario <TAB> fixture <TAB> load <TAB> result-key
PLAN_ARGS=(--matrix "$MATRIX_FILE" --tier "$TIER" --repeats "$REPEATS"
           --include-mlflow50 "$INCLUDE_MLFLOW50_TRACK" --include-pending "$INCLUDE_PENDING"
           --order "$ORDER" --seed "$SEED")
[[ -n "$ONLY_KEYS" ]] && PLAN_ARGS+=(--only-keys "$ONLY_KEYS" --expand-groups "$EXPAND_GROUPS")
if ! PLAN_TEXT=$(python3 release_matrix.py plan "${PLAN_ARGS[@]}"); then
  echo "ERROR: could not build the run plan from $MATRIX_FILE" >&2
  exit 2
fi
SUMMARY=$(python3 release_matrix.py summary "${PLAN_ARGS[@]}" --duration "$DURATION") || exit 2
mapfile -t PLAN <<< "$PLAN_TEXT"

echo "=== Release baseline capture: $VERSION ($REPEATS repeats x $DURATION) -> $OUTDIR ==="
echo "=== Absolute SLO config: $SLO_CONFIG_FILE ==="
echo "=== INCLUDE_MLFLOW50_TRACK: $INCLUDE_MLFLOW50_TRACK  INCLUDE_PENDING: $INCLUDE_PENDING ==="
echo "=== Matrix: $MATRIX_FILE ==="
echo "=== Plan: $SUMMARY (seed=$SEED) ==="
if warmup_enabled; then echo "=== Warm-up: $WARMUP_DURATION before the first timed run of every scenario/fixture pair (not counted) ==="; else echo "=== Warm-up: off ==="; fi

if [[ "$DRY_RUN" == "true" ]]; then
  echo "=== DRY_RUN: nothing will be executed ==="
  for line in "${PLAN[@]}"; do
    IFS=$'\t' read -r r scenario fixture load scenario_key status <<< "$line"
    mark=""; [[ "$status" == "pending" ]] && mark="  [pending: not verified on a cluster yet]"
    printf '  round %-3s %-44s (%s %s load=%s)%s\n' "$r" "$scenario_key" "$scenario" "$fixture" "$load" "$mark"
  done
  exit 0
fi

# Lines of the k6 summary kept in each per-run log (parsed by aggregate_release.py).
# p90/p95/p99 and avg/med/max latency, achieved rate, failures, checks, TTFT,
# iterations + dropped_iterations (the "iterations total" alternative matches
# both), and the HTTP status / error taxonomy.
RUN_LOG_PATTERN='AI benchmark summary|http_req_duration (avg|med|p90|p95|p99|max)|http_reqs/s|http_req_failed|checks pass|ai_time_to_first_token_ms p95|iterations total|http status counts|disconnect as intended|stream finished before disconnect'

# Client-disconnect scenarios: the interesting numbers are on the upstream side. The fake provider
# counts started / completed / aborted responses; reset them before the run, read them after it.
fake_provider_stats() {  # $1: "" -> read, "/reset" -> zero the counters
  kubectl exec -n upstream deploy/fake-provider -- wget -qO- "http://127.0.0.1:8080/__stats$1" 2>/dev/null
}

record_upstream_stats() {  # $1: per-run log to append the "upstream stats:" line to
  local json line
  sleep "$UPSTREAM_SETTLE_SECONDS"   # let streams that were not cancelled finish, so they show up as completed
  json=$(fake_provider_stats "") || json=""
  line=$(python3 - "$json" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1])
    print("upstream stats: " + " ".join(f"{k}={int(d[k])}" for k in ("started", "completed", "aborted", "active")))
except Exception:
    sys.exit(1)
PY
) && echo "$line" >> "$1" && echo "    $line" || echo "    WARNING: could not read the fake provider stats"
}

# A long run outlives short-lived credentials (an SSO session expired during an 8 h benchmark and the
# runner crashed on an unset variable without aggregating anything). Check before every run, so an
# unreachable cluster stops the run with a clear message and what was collected is still reported.
#
# One failed call is not "unreachable": the API server connection from a laptop/WSL is occasionally slow
# or times out for a moment (two runs were stopped at 13/93 and 75/93 and a retry minutes later worked, with
# valid credentials). Try CLUSTER_CHECK_TRIES times, CLUSTER_CHECK_PAUSE seconds apart, before giving up.
cluster_reachable() {
  local i tries="${CLUSTER_CHECK_TRIES:-5}" pause="${CLUSTER_CHECK_PAUSE:-10}"
  for ((i = 1; i <= tries; i++)); do
    kubectl get ns k6 -o name --request-timeout=20s >/dev/null 2>&1 && return 0
    ((i < tries)) && sleep "$pause"
  done
  return 1
}

# Wait until the k6 runner pod of the current TestRun has finished. Sets RUNNER_POD and RUNNER_PHASE
# (phase stays empty when the pod never showed up or did not finish in time).
wait_for_runner_pod() {
  local i
  RUNNER_POD=""; RUNNER_PHASE=""
  for i in $(seq 1 80); do
    RUNNER_POD=$(kubectl get pods -n k6 --no-headers 2>/dev/null | grep -E "k6-ai-benchmark-1" | head -1 | awk '{print $1}')
    if [ -z "$RUNNER_POD" ]; then
      # no pod yet: normal at the start of a run, but not worth 400 s of polling when the cluster is gone
      cluster_reachable || { echo "    WARNING: the cluster became unreachable while waiting for the runner pod" >&2; break; }
      sleep 5
      continue
    fi
    RUNNER_PHASE=$(kubectl get pod -n k6 "$RUNNER_POD" -o jsonpath='{.status.phase}' 2>/dev/null)
    { [ "$RUNNER_PHASE" = "Succeeded" ] || [ "$RUNNER_PHASE" = "Failed" ]; } && break
    sleep 6
  done
}

# Warm up once for each scenario/fixture pair, before its first timed run. The result is not saved.
declare -A WARMED=()
warm_up() {
  local scenario="$1" fixture="$2" load="$3" scenario_key="$4"
  warmup_enabled || return 0
  local wkey="$scenario:$fixture"
  [[ -n "${WARMED[$wkey]:-}" ]] && return 0
  WARMED["$wkey"]=1
  echo "--- warm-up $scenario ($fixture)  (load=$load, $WARMUP_DURATION, not counted) ---"
  {
    echo "[driver] deleting previous testruns"
    kubectl delete testrun -n k6 --all --ignore-not-found
    sleep "$DRIVER_SETTLE_SECONDS"
    echo "[driver] warm-up scenario=$scenario fixture=$fixture load=$load duration=$WARMUP_DURATION"
    bash run_ai_benchmark.sh "$scenario" "$fixture" "$load" "$WARMUP_DURATION"
  } >"$OUTDIR/${scenario_key}__warmup.driver.log" 2>&1
  wait_for_runner_pod
  echo "    warm-up finished (phase=${RUNNER_PHASE:-unknown})"
}

run_one() {
  local r="$1" scenario="$2" fixture="$3" load="$4" scenario_key="$5"
  local run_driver_log rp="" ph="" cpu_before cpu_after
  warm_up "$scenario" "$fixture" "$load" "$scenario_key"
  echo "--- $scenario ($fixture) repeat $r/$REPEATS  (load=$load, $DURATION) ---"
  run_driver_log="$OUTDIR/${scenario_key}__run${r}.driver.log"
  if [[ "$scenario" == *-disconnect ]]; then
    fake_provider_stats /reset >/dev/null || echo "    WARNING: could not reset the fake provider stats"
  fi
  # Kong CPU before the run (gateway scenarios only; direct-* never touch Kong)
  cpu_before=""
  if [[ "$scenario" != direct-* ]]; then
    cpu_before=$(python3 cpu_sample.py read) || cpu_before=""
  fi
  {
    echo "[driver] deleting previous testruns"
    kubectl delete testrun -n k6 --all --ignore-not-found
    sleep "$DRIVER_SETTLE_SECONDS"
    echo "[driver] running scenario=$scenario fixture=$fixture load=$load duration=$DURATION"
    bash run_ai_benchmark.sh "$scenario" "$fixture" "$load" "$DURATION"
  } >"$run_driver_log" 2>&1
  # wait for the runner pod to finish
  wait_for_runner_pod
  rp="$RUNNER_POD"; ph="$RUNNER_PHASE"
  # save this repeat's summary
  kubectl logs -n k6 "$rp" 2>/dev/null | \
    grep -E "$RUN_LOG_PATTERN" \
    > "$OUTDIR/${scenario_key}__run${r}.log"
  echo "    saved $OUTDIR/${scenario_key}__run${r}.log (phase=${ph:-unknown})"
  grep -q . "$OUTDIR/${scenario_key}__run${r}.log" || echo "    WARNING: the summary of $scenario_key run $r is empty (kubectl logs failed?); it is ignored when aggregating"
  if [[ -n "$cpu_before" ]]; then
    if cpu_after=$(python3 cpu_sample.py read); then
      python3 cpu_sample.py record --before "$cpu_before" --after "$cpu_after" \
        --log "$OUTDIR/${scenario_key}__run${r}.log" | sed 's/^/    /' \
        || echo "    WARNING: Kong CPU not recorded for $scenario_key run $r"
    else
      echo "    WARNING: could not read the Kong CPU after the run"
    fi
  fi
  if [[ "$scenario" == *-disconnect ]]; then
    record_upstream_stats "$OUTDIR/${scenario_key}__run${r}.log"
  fi
}

# Run PLAN into OUTDIR: record the environment, execute every run, aggregate.
execute_runs() {
  local current_round=0 line r scenario fixture load scenario_key status
  mkdir -p "$OUTDIR"
  # Keep the executed order next to the results as evidence.
  printf '%s\n' "${PLAN[@]}" > "$OUTDIR/plan.tsv"

  # Record the test environment (nodes, zones, Kong image digest, configs, ...) for drift checks.
  # Best effort: it never fails the run.
  python3 collect_env.py --out "$OUTDIR/env.json" --phase start || echo "WARNING: could not record the environment (start)"

  local executed=0
  for line in "${PLAN[@]}"; do
    IFS=$'\t' read -r r scenario fixture load scenario_key status <<< "$line"
    if ! cluster_reachable; then
      echo "ERROR: kubectl cannot reach the cluster (expired credentials?). Stopping after $executed of ${#PLAN[@]} runs;" \
           "what was collected is aggregated and reported below, the run is incomplete." >&2
      INTERRUPTED=1
      break
    fi
    if [[ "$r" != "$current_round" ]]; then
      current_round="$r"
      echo "=== Round $r/$REPEATS ==="
    fi
    run_one "$r" "$scenario" "$fixture" "$load" "$scenario_key"
    executed=$((executed + 1))
  done

  python3 collect_env.py --out "$OUTDIR/env.json" --phase end || echo "WARNING: could not record the environment (end)"

  echo "=== aggregating ==="
  python3 aggregate_release.py --dir "$OUTDIR" --version "$VERSION" --captured-at "$CAPTURED_AT"
}

# Re-run only the scenarios the comparison flagged as REGRESSION / UNSTABLE (with their direct
# controls: the overhead is a paired difference) and compare again, counting only findings that
# reproduce. That covers regressions, unstable scenarios and overhead-budget violations; errors,
# checks, dropped iterations and disconnect propagation are not re-judged.
confirm_flagged() {
  local flagged count main_outdir="$OUTDIR" main_plan=("${PLAN[@]}")
  flagged=$(python3 - "$OUTDIR/verdict.json" <<'PY'
import json, sys
try:
    v = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
keys = [r["scenario"] for r in v.get("regressions", [])] + [u["scenario"] for u in v.get("unstable", [])]
# overhead-budget violations come from the same noisy latency measurements and are confirmable too
keys += [x["scenario"] for x in v.get("absolute_slo_violations", []) if str(x.get("metric", "")).startswith("overhead_")]
print(",".join(dict.fromkeys(keys)))
PY
)
  [[ -z "$flagged" ]] && return 0
  count=$(tr ',' '\n' <<< "$flagged" | wc -l | tr -d ' ')
  if (( count > CONFIRM_MAX_SCENARIOS )); then
    echo "=== $count scenarios flagged (> CONFIRM_MAX_SCENARIOS=$CONFIRM_MAX_SCENARIOS): not re-running; that many findings usually mean an environment problem, re-run the release ==="
    return 0
  fi
  echo "=== confirmation run for: $flagged ==="
  if ! PLAN_TEXT=$(python3 release_matrix.py plan "${PLAN_ARGS[@]}" --only-keys "$flagged"); then
    echo "WARNING: could not plan the confirmation run; keeping the first-pass verdict"
    return 0
  fi
  mapfile -t PLAN <<< "$PLAN_TEXT"
  OUTDIR="${main_outdir}-confirm"
  [[ "$OUTDIR" == *-confirm ]] && rm -rf "$OUTDIR"   # stale results of an earlier attempt, never mix them in
  execute_runs
  OUTDIR="$main_outdir"
  PLAN=("${main_plan[@]}")
  cp "$OUTDIR/report.md" "$OUTDIR/report.first-pass.md"
  cp "$OUTDIR/verdict.json" "$OUTDIR/verdict.first-pass.json"
  python3 compare_release.py --current "$OUTDIR" --baseline "$BASE_DIR" --confirm "${main_outdir}-confirm" \
    --gates "$GATES_FILE" --slo-config "$SLO_CONFIG_FILE" --out "$OUTDIR/report.md" --json "$OUTDIR/verdict.json"
  echo "verdict exit (after confirmation): $?"
}

execute_runs

# Report: if a baseline exists, diff against it; otherwise record as baseline.
BASELINE_PTR="results/releases/baseline.json"
if [ -f "$BASELINE_PTR" ]; then
  BASE_DIR=$(python3 -c "import json;print(json.load(open('$BASELINE_PTR'))['dir'])")
  echo "=== comparing vs baseline $BASE_DIR (gates: $GATES_FILE) ==="
  python3 compare_release.py --current "$OUTDIR" --baseline "$BASE_DIR" \
    --gates "$GATES_FILE" --slo-config "$SLO_CONFIG_FILE" --out "$OUTDIR/report.md" --json "$OUTDIR/verdict.json"
  echo "verdict exit: $?"
  if [[ "$CONFIRM_RERUN" == "true" && "$INTERRUPTED" == "0" ]]; then
    confirm_flagged
  fi
else
  echo "=== no baseline yet — recording $OUTDIR as the reference baseline (gates: $GATES_FILE) ==="
  python3 compare_release.py --current "$OUTDIR" --gates "$GATES_FILE" \
    --slo-config "$SLO_CONFIG_FILE" --out "$OUTDIR/report.md" --json "$OUTDIR/verdict.json"
  printf '{"dir": "%s", "version": "%s"}\n' "$OUTDIR" "$VERSION" > "$BASELINE_PTR"
  echo "baseline.json -> $OUTDIR"
fi
echo "BASELINE_CAPTURE_DONE: $OUTDIR"
if [[ "$INTERRUPTED" == "1" ]]; then
  echo "RUN INCOMPLETE: the cluster became unreachable part-way through (exit 3)" >&2
  exit 3
fi
