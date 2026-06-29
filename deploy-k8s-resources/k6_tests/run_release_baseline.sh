#!/bin/bash
# =============================================================================
# run_release_baseline.sh — capture a full release benchmark for evidence.
#
# Runs the standard scenario matrix at a fixed load profile, REPEATS times each,
# saves every k6 summary, aggregates into metrics.json + summary.csv, and emits
# a release report. Store the output dir as a release record; point baseline.json
# at the first one. Re-run for each release, then diff with compare_release.py.
#
# Usage:
#   VERSION=ai-2.0.0-rc.2 ./run_release_baseline.sh
#   VERSION=... REPEATS=3 DURATION=3m ./run_release_baseline.sh
# =============================================================================
set -uo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

VERSION="${VERSION:-unknown}"
REPEATS="${REPEATS:-3}"
DURATION="${DURATION:-3m}"
INCLUDE_MLFLOW50_TRACK="${INCLUDE_MLFLOW50_TRACK:-true}"
GATES_FILE="${RELEASE_GATES_PATH:-$SCRIPT_DIR/release_gates.yaml}"
SLO_CONFIG_FILE="${SLO_CONFIG_PATH:-$SCRIPT_DIR/benchmark_config.yaml}"
CAPTURED_AT="${CAPTURED_AT:-$(date -u +"%Y-%m-%dT%H:%M:%SZ")}"
DATESTAMP="$(date -u +"%Y-%m-%d")"
OUTDIR="results/releases/${VERSION}_${DATESTAMP}"
mkdir -p "$OUTDIR"

if [ ! -f "$GATES_FILE" ]; then
  echo "ERROR: gates file not found: $GATES_FILE" >&2
  exit 2
fi

if [ ! -f "$SLO_CONFIG_FILE" ]; then
  echo "ERROR: SLO config file not found: $SLO_CONFIG_FILE" >&2
  exit 2
fi

# Standard release matrix: scenario:fixture:load.
# Streaming loads are VUs; others are request rate.
SCENARIOS=(
  "static-chat:short:50"
  "token-chat-openai:short:50"
  "token-chat-openai:medium:50"
  "direct-token-chat-openai:short:50"
  "direct-token-chat-openai:medium:50"
  "stream-openai:short:30"
  "stream-gemini:short:30"
  "embeddings-openai:short:50"
  "embeddings-openai:medium:50"
  "direct-embeddings-openai:short:50"
  "direct-embeddings-openai:medium:50"
  "routing-roundrobin-2:short:25"
  "routing-roundrobin-10:short:25"
  "routing-ewma:short:25"
  "routing-failover:short:25"
  "payload-logging:short:25"
)

# MLflow-style fixed-delay track for gateway-overhead gating.
if [[ "$INCLUDE_MLFLOW50_TRACK" == "true" ]]; then
  SCENARIOS+=(
    "token-chat-openai:mlflow50:50"
    "direct-token-chat-openai:mlflow50:50"
    "stream-openai:mlflow50:30"
    "direct-stream-openai:mlflow50:30"
    "embeddings-openai:mlflow50:50"
    "direct-embeddings-openai:mlflow50:50"
  )
fi

echo "=== Release baseline capture: $VERSION ($REPEATS repeats x $DURATION) -> $OUTDIR ==="
echo "=== Absolute SLO config: $SLO_CONFIG_FILE ==="
echo "=== INCLUDE_MLFLOW50_TRACK: $INCLUDE_MLFLOW50_TRACK ==="
for entry in "${SCENARIOS[@]}"; do
  IFS=':' read -r scenario fixture load <<< "$entry"
  if [[ -z "${load:-}" ]]; then
    load="$fixture"
    fixture="short"
  fi

  scenario_key="$scenario"
  if [[ "$fixture" != "short" ]]; then
    scenario_key="${scenario}__${fixture}"
  fi

  for r in $(seq 1 "$REPEATS"); do
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
  done
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
