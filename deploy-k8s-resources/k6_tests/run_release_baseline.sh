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
#   DRY_RUN=true            print the plan and the time estimate, run nothing
#                           (no cluster access, no output directory).
# =============================================================================
set -uo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

VERSION="${VERSION:-unknown}"
REPEATS="${REPEATS:-3}"
DURATION="${DURATION:-3m}"
INCLUDE_MLFLOW50_TRACK="${INCLUDE_MLFLOW50_TRACK:-true}"
TIER="${TIER:-tier2}"
ORDER="${ORDER:-interleaved}"
SEED="${SEED:-1}"
DRY_RUN="${DRY_RUN:-false}"
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

if ! [[ "$REPEATS" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: REPEATS must be a positive integer, got '$REPEATS'" >&2
  exit 2
fi

# Build the ordered run plan: one TSV line per run
#   round <TAB> scenario <TAB> fixture <TAB> load <TAB> result-key
PLAN_ARGS=(--matrix "$MATRIX_FILE" --tier "$TIER" --repeats "$REPEATS"
           --include-mlflow50 "$INCLUDE_MLFLOW50_TRACK" --order "$ORDER" --seed "$SEED")
if ! PLAN_TEXT=$(python3 release_matrix.py plan "${PLAN_ARGS[@]}"); then
  echo "ERROR: could not build the run plan from $MATRIX_FILE" >&2
  exit 2
fi
SUMMARY=$(python3 release_matrix.py summary "${PLAN_ARGS[@]}" --duration "$DURATION") || exit 2
mapfile -t PLAN <<< "$PLAN_TEXT"

echo "=== Release baseline capture: $VERSION ($REPEATS repeats x $DURATION) -> $OUTDIR ==="
echo "=== Absolute SLO config: $SLO_CONFIG_FILE ==="
echo "=== INCLUDE_MLFLOW50_TRACK: $INCLUDE_MLFLOW50_TRACK ==="
echo "=== Matrix: $MATRIX_FILE ==="
echo "=== Plan: $SUMMARY (seed=$SEED) ==="

if [[ "$DRY_RUN" == "true" ]]; then
  echo "=== DRY_RUN: nothing will be executed ==="
  for line in "${PLAN[@]}"; do
    IFS=$'\t' read -r r scenario fixture load scenario_key <<< "$line"
    printf '  round %-3s %-44s (%s %s load=%s)\n' "$r" "$scenario_key" "$scenario" "$fixture" "$load"
  done
  exit 0
fi

mkdir -p "$OUTDIR"
# Keep the executed order next to the results as evidence.
printf '%s\n' "${PLAN[@]}" > "$OUTDIR/plan.tsv"

run_one() {
  local r="$1" scenario="$2" fixture="$3" load="$4" scenario_key="$5"
  local run_driver_log rp ph
  echo "--- $scenario ($fixture) repeat $r/$REPEATS  (load=$load, $DURATION) ---"
  run_driver_log="$OUTDIR/${scenario_key}__run${r}.driver.log"
  {
    echo "[driver] deleting previous testruns"
    kubectl delete testrun -n k6 --all --ignore-not-found
    sleep 4
    echo "[driver] running scenario=$scenario fixture=$fixture load=$load duration=$DURATION"
    bash run_ai_benchmark.sh "$scenario" "$fixture" "$load" "$DURATION"
  } >"$run_driver_log" 2>&1
  # wait for the runner pod to finish
  rp=""
  for i in $(seq 1 80); do
    rp=$(kubectl get pods -n k6 --no-headers 2>/dev/null | grep -E "k6-ai-benchmark-1" | head -1 | awk '{print $1}')
    [ -z "$rp" ] && { sleep 5; continue; }
    ph=$(kubectl get pod -n k6 "$rp" -o jsonpath='{.status.phase}' 2>/dev/null)
    { [ "$ph" = "Succeeded" ] || [ "$ph" = "Failed" ]; } && break
    sleep 6
  done
  # save this repeat's summary
  kubectl logs -n k6 "$rp" 2>/dev/null | \
    grep -E "AI benchmark summary|http_req_duration p9|http_reqs/s|http_req_failed|checks pass|ai_time_to_first_token_ms p95" \
    > "$OUTDIR/${scenario_key}__run${r}.log"
  echo "    saved $OUTDIR/${scenario_key}__run${r}.log (phase=$ph)"
}

current_round=0
for line in "${PLAN[@]}"; do
  IFS=$'\t' read -r r scenario fixture load scenario_key <<< "$line"
  if [[ "$r" != "$current_round" ]]; then
    current_round="$r"
    echo "=== Round $r/$REPEATS ==="
  fi
  run_one "$r" "$scenario" "$fixture" "$load" "$scenario_key"
done

echo "=== aggregating ==="
python3 aggregate_release.py --dir "$OUTDIR" --version "$VERSION" --captured-at "$CAPTURED_AT"

# Report: if a baseline exists, diff against it; otherwise record as baseline.
BASELINE_PTR="results/releases/baseline.json"
if [ -f "$BASELINE_PTR" ]; then
  BASE_DIR=$(python3 -c "import json;print(json.load(open('$BASELINE_PTR'))['dir'])")
  echo "=== comparing vs baseline $BASE_DIR (gates: $GATES_FILE) ==="
  python3 compare_release.py --current "$OUTDIR" --baseline "$BASE_DIR" \
    --gates "$GATES_FILE" --slo-config "$SLO_CONFIG_FILE" --out "$OUTDIR/report.md" --json "$OUTDIR/verdict.json"
  echo "verdict exit: $?"
else
  echo "=== no baseline yet — recording $OUTDIR as the reference baseline (gates: $GATES_FILE) ==="
  python3 compare_release.py --current "$OUTDIR" --gates "$GATES_FILE" \
    --slo-config "$SLO_CONFIG_FILE" --out "$OUTDIR/report.md" --json "$OUTDIR/verdict.json"
  printf '{"dir": "%s", "version": "%s"}\n' "$OUTDIR" "$VERSION" > "$BASELINE_PTR"
  echo "baseline.json -> $OUTDIR"
fi
echo "BASELINE_CAPTURE_DONE: $OUTDIR"
