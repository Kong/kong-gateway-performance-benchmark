#!/bin/bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./run_stream_openai_campaign.sh [repeats] [results-dir]

Defaults:
  repeats      3
  results-dir  ./results/stream-openai-campaign-<timestamp>

Default VU sweep:
  25 50 100 200 400
EOF
  exit 1
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
fi

REPEATS=${1:-3}
RESULTS_DIR=${2:-"./results/stream-openai-campaign-$(date -u +"%Y%m%dT%H%M%SZ")"}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
mkdir -p "$RESULTS_DIR"

SCENARIO="stream-openai"
FIXTURE=${STREAM_OPENAI_FIXTURE:-short}
DURATION=${STREAM_OPENAI_DURATION:-6m}
VU_POINTS=(${STREAM_OPENAI_VUS:-25 50 100 200 400})
INFRA_INTERVAL_SECONDS=${INFRA_INTERVAL_SECONDS:-10}
RSS_INTERVAL_SECONDS=${RSS_INTERVAL_SECONDS:-5}

duration_to_seconds() {
  local raw=${1:-0}
  local amount unit

  if [[ "$raw" =~ ^([0-9]+)(ms|s|m|h)$ ]]; then
    amount=${BASH_REMATCH[1]}
    unit=${BASH_REMATCH[2]}
  else
    echo 0
    return
  fi

  case "$unit" in
    ms) echo 1 ;;
    s) echo "$amount" ;;
    m) echo $(( amount * 60 )) ;;
    h) echo $(( amount * 3600 )) ;;
  esac
}

latest_runner_pod() {
  kubectl get pods -n k6 --sort-by=.metadata.creationTimestamp -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
    | grep '^k6-ai-benchmark-1-' \
    | tail -1
}

wait_for_new_runner_pod() {
  local previous_pod=${1:-}
  local timeout_seconds=${2:-180}
  local end_time=$(( $(date +%s) + timeout_seconds ))

  while [[ $(date +%s) -lt $end_time ]]; do
    local pod
    pod=$(latest_runner_pod)
    if [[ -n "$pod" && "$pod" != "$previous_pod" ]]; then
      echo "$pod"
      return 0
    fi
    sleep 5
  done

  return 1
}

wait_for_terminal_phase() {
  local pod_name=$1
  local timeout_seconds=$2
  local end_time=$(( $(date +%s) + timeout_seconds ))

  while [[ $(date +%s) -lt $end_time ]]; do
    local phase
    phase=$(kubectl get pod -n k6 "$pod_name" -o jsonpath='{.status.phase}' 2>/dev/null || true)

    case "$phase" in
      Succeeded|Failed|Error)
        echo "$phase"
        return 0
        ;;
      "")
        ;;
    esac

    sleep 10
  done

  echo "Timeout"
  return 1
}

extract_metric() {
  local label=$1
  local file=$2

  grep -m1 -F "$label" "$file" | sed 's/.*: //' | tr -d '\r' || true
}

csv_peak() {
  local file=$1
  local column=$2
  awk -F, -v col="$column" 'NR > 1 && $col != "" { value = $col + 0; if (NR == 2 || value > max) max = value } END { print max + 0 }' "$file"
}

csv_average() {
  local file=$1
  local column=$2
  awk -F, -v col="$column" 'NR > 1 && $col != "" { total += $col + 0; count += 1 } END { if (count == 0) print 0; else printf "%.2f\n", total / count }' "$file"
}

percent_of() {
  local numerator=${1:-0}
  local denominator=${2:-0}
  awk -v numerator="$numerator" -v denominator="$denominator" 'BEGIN { if (denominator <= 0) print 0; else printf "%.2f\n", (numerator / denominator) * 100 }'
}

write_headers() {
  local summary_file=$1
  cat > "$summary_file" <<'EOF'
scenario,summary_title,fixture,repeat_index,load_kind,load_value,duration,duration_seconds,input_tokens_total,output_tokens_total,total_tokens_total,input_tokens_per_second,output_tokens_per_second,total_tokens_per_second,http_requests_total,http_requests_per_second,iterations_total,iterations_per_second,dropped_iterations_total,dropped_iterations_per_second,vus_max,http_p95_ms,http_p99_ms,http_failed_percent,checks_pass_percent,ttft_p95_ms,active_upstream_kind,kong_pod_cpu_peak_m,kong_pod_cpu_avg_m,kong_pod_memory_peak_mi,kong_pod_memory_avg_mi,kong_worker_total_rss_peak_kb,active_upstream_pod_cpu_peak_m,active_upstream_pod_cpu_avg_m,active_upstream_pod_memory_peak_mi,active_upstream_pod_memory_avg_mi,k6_runner_cpu_peak_m,k6_runner_cpu_avg_m,k6_runner_memory_peak_mi,k6_runner_memory_avg_mi,kong_node_cpu_peak_m,kong_node_cpu_avg_m,kong_node_memory_peak_mi,kong_node_memory_avg_mi,upstream_node_cpu_peak_m,upstream_node_cpu_avg_m,upstream_node_memory_peak_mi,upstream_node_memory_avg_mi,k6_node_cpu_peak_m,k6_node_cpu_avg_m,k6_node_memory_peak_mi,k6_node_memory_avg_mi,kong_pod_cpu_peak_of_limit_percent,kong_pod_memory_peak_of_limit_percent,active_upstream_pod_cpu_peak_of_limit_percent,active_upstream_pod_memory_peak_of_limit_percent,k6_runner_cpu_peak_of_limit_percent,k6_runner_memory_peak_of_limit_percent,kong_node_cpu_peak_of_allocatable_percent,kong_node_memory_peak_of_allocatable_percent,upstream_node_cpu_peak_of_allocatable_percent,upstream_node_memory_peak_of_allocatable_percent,k6_node_cpu_peak_of_allocatable_percent,k6_node_memory_peak_of_allocatable_percent,pod_phase
EOF
}

write_summary_line() {
  local summary_file=$1
  local load_value=$2
  local repeat_index=$3
  local run_dir=$4
  local pod_phase=$5

  local runner_log="$run_dir/runner.log"
  local infra_csv="$run_dir/infra_timeseries.csv"
  local worker_csv="$run_dir/worker_rss.csv"
  local capacity_file="$run_dir/capacity.env"

  if [[ -f "$capacity_file" ]]; then
    # shellcheck disable=SC1090
    source "$capacity_file"
  fi

  local title duration_s input_total output_total total_total input_ps output_ps total_ps
  local http_requests_total http_requests_per_second iterations_total iterations_per_second
  local dropped_iterations_total dropped_iterations_per_second vus_max
  local http_p95 http_p99 http_failed checks_pass ttft_p95
  title=$(extract_metric "AI benchmark summary" "$runner_log")
  duration_s=$(extract_metric "Duration(s)" "$runner_log")
  input_total=$(extract_metric "Input tokens total" "$runner_log")
  output_total=$(extract_metric "Output tokens total" "$runner_log")
  total_total=$(extract_metric "Total tokens total" "$runner_log")
  input_ps=$(extract_metric "Input tokens/s" "$runner_log")
  output_ps=$(extract_metric "Output tokens/s" "$runner_log")
  total_ps=$(extract_metric "Total tokens/s" "$runner_log")
  http_requests_total=$(extract_metric "http_reqs total" "$runner_log")
  http_requests_per_second=$(extract_metric "http_reqs/s" "$runner_log")
  iterations_total=$(extract_metric "iterations total" "$runner_log")
  iterations_per_second=$(extract_metric "iterations/s" "$runner_log")
  dropped_iterations_total=$(extract_metric "dropped_iterations total" "$runner_log")
  dropped_iterations_per_second=$(extract_metric "dropped_iterations/s" "$runner_log")
  vus_max=$(extract_metric "vus_max" "$runner_log")
  http_p95=$(extract_metric "http_req_duration p95(ms)" "$runner_log")
  http_p99=$(extract_metric "http_req_duration p99(ms)" "$runner_log")
  http_failed=$(extract_metric "http_req_failed(%)" "$runner_log")
  checks_pass=$(extract_metric "checks pass rate(%)" "$runner_log")
  ttft_p95=$(extract_metric "ai_time_to_first_token_ms p95" "$runner_log")

  local kong_cpu_peak kong_cpu_avg kong_mem_peak kong_mem_avg kong_worker_rss_peak
  local upstream_cpu_peak upstream_cpu_avg upstream_mem_peak upstream_mem_avg
  local k6_runner_cpu_peak k6_runner_cpu_avg k6_runner_mem_peak k6_runner_mem_avg
  local kong_node_cpu_peak kong_node_cpu_avg kong_node_mem_peak kong_node_mem_avg
  local upstream_node_cpu_peak upstream_node_cpu_avg upstream_node_mem_peak upstream_node_mem_avg
  local k6_node_cpu_peak k6_node_cpu_avg k6_node_mem_peak k6_node_mem_avg
  local kong_pod_cpu_peak_of_limit_pct kong_pod_memory_peak_of_limit_pct
  local upstream_pod_cpu_peak_of_limit_pct upstream_pod_memory_peak_of_limit_pct
  local k6_runner_cpu_peak_of_limit_pct k6_runner_memory_peak_of_limit_pct
  local kong_node_cpu_peak_of_allocatable_pct kong_node_memory_peak_of_allocatable_pct
  local upstream_node_cpu_peak_of_allocatable_pct upstream_node_memory_peak_of_allocatable_pct
  local k6_node_cpu_peak_of_allocatable_pct k6_node_memory_peak_of_allocatable_pct

  kong_cpu_peak=$(csv_peak "$infra_csv" 2)
  kong_cpu_avg=$(csv_average "$infra_csv" 2)
  kong_mem_peak=$(csv_peak "$infra_csv" 3)
  kong_mem_avg=$(csv_average "$infra_csv" 3)
  upstream_cpu_peak=$(csv_peak "$infra_csv" 6)
  upstream_cpu_avg=$(csv_average "$infra_csv" 6)
  upstream_mem_peak=$(csv_peak "$infra_csv" 7)
  upstream_mem_avg=$(csv_average "$infra_csv" 7)
  k6_runner_cpu_peak=$(csv_peak "$infra_csv" 10)
  k6_runner_cpu_avg=$(csv_average "$infra_csv" 10)
  k6_runner_mem_peak=$(csv_peak "$infra_csv" 11)
  k6_runner_mem_avg=$(csv_average "$infra_csv" 11)
  kong_node_cpu_peak=$(csv_peak "$infra_csv" 12)
  kong_node_cpu_avg=$(csv_average "$infra_csv" 12)
  kong_node_mem_peak=$(csv_peak "$infra_csv" 13)
  kong_node_mem_avg=$(csv_average "$infra_csv" 13)
  upstream_node_cpu_peak=$(csv_peak "$infra_csv" 14)
  upstream_node_cpu_avg=$(csv_average "$infra_csv" 14)
  upstream_node_mem_peak=$(csv_peak "$infra_csv" 15)
  upstream_node_mem_avg=$(csv_average "$infra_csv" 15)
  k6_node_cpu_peak=$(csv_peak "$infra_csv" 16)
  k6_node_cpu_avg=$(csv_average "$infra_csv" 16)
  k6_node_mem_peak=$(csv_peak "$infra_csv" 17)
  k6_node_mem_avg=$(csv_average "$infra_csv" 17)
  kong_worker_rss_peak=$(csv_peak "$worker_csv" 3)

  kong_pod_cpu_peak_of_limit_pct=$(percent_of "$kong_cpu_peak" "${KONG_POD_CPU_LIMIT_M:-0}")
  kong_pod_memory_peak_of_limit_pct=$(percent_of "$kong_mem_peak" "${KONG_POD_MEMORY_LIMIT_MI:-0}")
  upstream_pod_cpu_peak_of_limit_pct=$(percent_of "$upstream_cpu_peak" "${FAKE_PROVIDER_POD_CPU_LIMIT_M:-0}")
  upstream_pod_memory_peak_of_limit_pct=$(percent_of "$upstream_mem_peak" "${FAKE_PROVIDER_POD_MEMORY_LIMIT_MI:-0}")
  k6_runner_cpu_peak_of_limit_pct=$(percent_of "$k6_runner_cpu_peak" "${K6_RUNNER_POD_CPU_LIMIT_M:-0}")
  k6_runner_memory_peak_of_limit_pct=$(percent_of "$k6_runner_mem_peak" "${K6_RUNNER_POD_MEMORY_LIMIT_MI:-0}")
  kong_node_cpu_peak_of_allocatable_pct=$(percent_of "$kong_node_cpu_peak" "${KONG_NODE_ALLOCATABLE_CPU_M:-0}")
  kong_node_memory_peak_of_allocatable_pct=$(percent_of "$kong_node_mem_peak" "${KONG_NODE_ALLOCATABLE_MEMORY_MI:-0}")
  upstream_node_cpu_peak_of_allocatable_pct=$(percent_of "$upstream_node_cpu_peak" "${FAKE_PROVIDER_NODE_ALLOCATABLE_CPU_M:-0}")
  upstream_node_memory_peak_of_allocatable_pct=$(percent_of "$upstream_node_mem_peak" "${FAKE_PROVIDER_NODE_ALLOCATABLE_MEMORY_MI:-0}")
  k6_node_cpu_peak_of_allocatable_pct=$(percent_of "$k6_node_cpu_peak" "${K6_RUNNER_NODE_ALLOCATABLE_CPU_M:-0}")
  k6_node_memory_peak_of_allocatable_pct=$(percent_of "$k6_node_mem_peak" "${K6_RUNNER_NODE_ALLOCATABLE_MEMORY_MI:-0}")

  echo "\"$SCENARIO\",\"$title\",\"$FIXTURE\",$repeat_index,\"vus\",$load_value,\"$DURATION\",${duration_s:-0},${input_total:-0},${output_total:-0},${total_total:-0},${input_ps:-0},${output_ps:-0},${total_ps:-0},${http_requests_total:-0},${http_requests_per_second:-0},${iterations_total:-0},${iterations_per_second:-0},${dropped_iterations_total:-0},${dropped_iterations_per_second:-0},${vus_max:-0},${http_p95:-0},${http_p99:-0},${http_failed:-0},${checks_pass:-0},${ttft_p95:-0},\"fake-provider\",${kong_cpu_peak:-0},${kong_cpu_avg:-0},${kong_mem_peak:-0},${kong_mem_avg:-0},${kong_worker_rss_peak:-0},${upstream_cpu_peak:-0},${upstream_cpu_avg:-0},${upstream_mem_peak:-0},${upstream_mem_avg:-0},${k6_runner_cpu_peak:-0},${k6_runner_cpu_avg:-0},${k6_runner_mem_peak:-0},${k6_runner_mem_avg:-0},${kong_node_cpu_peak:-0},${kong_node_cpu_avg:-0},${kong_node_mem_peak:-0},${kong_node_mem_avg:-0},${upstream_node_cpu_peak:-0},${upstream_node_cpu_avg:-0},${upstream_node_mem_peak:-0},${upstream_node_mem_avg:-0},${k6_node_cpu_peak:-0},${k6_node_cpu_avg:-0},${k6_node_mem_peak:-0},${k6_node_mem_avg:-0},${kong_pod_cpu_peak_of_limit_pct:-0},${kong_pod_memory_peak_of_limit_pct:-0},${upstream_pod_cpu_peak_of_limit_pct:-0},${upstream_pod_memory_peak_of_limit_pct:-0},${k6_runner_cpu_peak_of_limit_pct:-0},${k6_runner_memory_peak_of_limit_pct:-0},${kong_node_cpu_peak_of_allocatable_pct:-0},${kong_node_memory_peak_of_allocatable_pct:-0},${upstream_node_cpu_peak_of_allocatable_pct:-0},${upstream_node_memory_peak_of_allocatable_pct:-0},${k6_node_cpu_peak_of_allocatable_pct:-0},${k6_node_memory_peak_of_allocatable_pct:-0},\"${pod_phase:-unknown}\"" >> "$summary_file"
}

run_one() {
  local load_value=$1
  local repeat_index=$2
  local summary_file=$3

  local run_name="${SCENARIO}_vus${load_value}_r${repeat_index}"
  local run_dir="$RESULTS_DIR/$run_name"
  local duration_seconds sampler_seconds previous_pod
  local rss_pid infra_pid pod_name pod_phase

  mkdir -p "$run_dir"
  duration_seconds=$(duration_to_seconds "$DURATION")
  sampler_seconds=$(( duration_seconds + 60 ))

  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] starting $run_name"

  {
    echo "scenario=$SCENARIO"
    echo "fixture=$FIXTURE"
    echo "load_kind=vus"
    echo "load_value=$load_value"
    echo "duration=$DURATION"
    echo "repeat_index=$repeat_index"
    echo "started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  } > "$run_dir/metadata.env"

  bash "$SCRIPT_DIR/extract_infra_metrics.sh" > "$run_dir/infra_start.txt" 2>&1 || true
  bash "$SCRIPT_DIR/sample_kong_worker_memory.sh" "$sampler_seconds" "$RSS_INTERVAL_SECONDS" > "$run_dir/worker_rss.csv" 2>&1 &
  rss_pid=$!
  bash "$SCRIPT_DIR/sample_benchmark_infra.sh" "$sampler_seconds" "$INFRA_INTERVAL_SECONDS" > "$run_dir/infra_timeseries.csv" 2>&1 &
  infra_pid=$!

  previous_pod=$(latest_runner_pod || true)
  bash "$SCRIPT_DIR/run_ai_benchmark.sh" "$SCENARIO" "$FIXTURE" "$load_value" "$DURATION" > "$run_dir/start.log" 2>&1
  pod_name=$(wait_for_new_runner_pod "$previous_pod" 180)
  echo "runner_pod=$pod_name" >> "$run_dir/metadata.env"
  python3 "$SCRIPT_DIR/capture_benchmark_capacity.py" --runner-pod "$pod_name" > "$run_dir/capacity.env" 2>&1 || true
  pod_phase=$(wait_for_terminal_phase "$pod_name" $(( duration_seconds + 900 )) || true)

  kubectl logs -n k6 "$pod_name" --all-containers=true > "$run_dir/runner.log" 2>&1 || true
  bash "$SCRIPT_DIR/extract_infra_metrics.sh" > "$run_dir/infra_end.txt" 2>&1 || true

  kill "$rss_pid" "$infra_pid" 2>/dev/null || true
  wait "$rss_pid" "$infra_pid" 2>/dev/null || true

  write_summary_line "$summary_file" "$load_value" "$repeat_index" "$run_dir" "$pod_phase"
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] completed $run_name phase=$pod_phase"
}

aggregate_results() {
  local summary_file=$1
  local aggregate_file=$2
  local findings_file=$3

  python3 - "$summary_file" "$aggregate_file" "$findings_file" <<'PY'
import csv
import statistics
import sys
from collections import defaultdict

summary_file, aggregate_file, findings_file = sys.argv[1:4]

with open(summary_file, newline='') as f:
    rows = list(csv.DictReader(f))

groups = defaultdict(list)
for row in rows:
    groups[row['load_value']].append(row)

metric_fields = [
    'http_requests_per_second',
    'dropped_iterations_total',
    'dropped_iterations_per_second',
    'vus_max',
    'input_tokens_per_second',
    'output_tokens_per_second',
    'total_tokens_per_second',
    'http_p95_ms',
    'http_p99_ms',
    'http_failed_percent',
    'checks_pass_percent',
    'ttft_p95_ms',
    'kong_pod_cpu_peak_m',
    'kong_pod_memory_peak_mi',
    'active_upstream_pod_cpu_peak_m',
    'active_upstream_pod_memory_peak_mi',
    'k6_runner_cpu_peak_m',
    'k6_runner_memory_peak_mi',
    'kong_node_cpu_peak_of_allocatable_percent',
    'kong_node_memory_peak_of_allocatable_percent',
    'upstream_node_cpu_peak_of_allocatable_percent',
    'upstream_node_memory_peak_of_allocatable_percent',
    'k6_node_cpu_peak_of_allocatable_percent',
    'k6_node_memory_peak_of_allocatable_percent',
]

def to_float(value):
    try:
        return float(value)
    except Exception:
        return 0.0

aggregate_rows = []
for load_value, group in sorted(groups.items(), key=lambda item: float(item[0])):
    output = {
        'load_value': load_value,
        'runs': len(group),
        'succeeded_runs': sum(1 for row in group if row['pod_phase'] == 'Succeeded'),
        'failed_runs': sum(1 for row in group if row['pod_phase'] != 'Succeeded'),
    }
    for field in metric_fields:
        values = [to_float(row[field]) for row in group]
        output[f'{field}_median'] = statistics.median(values) if values else 0.0
        output[f'{field}_min'] = min(values) if values else 0.0
        output[f'{field}_max'] = max(values) if values else 0.0
    aggregate_rows.append(output)

fieldnames = ['load_value', 'runs', 'succeeded_runs', 'failed_runs']
for field in metric_fields:
    fieldnames.extend([f'{field}_median', f'{field}_min', f'{field}_max'])

with open(aggregate_file, 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(aggregate_rows)

findings = ['# Stream OpenAI broad sweep findings', '']
findings.append(f'- Total completed runs: {len(rows)}')
findings.append('')
for row in aggregate_rows:
    findings.append(f'## {row["load_value"]} VUs')
    findings.append('')
    findings.append(f'- Pod phases: succeeded={row["succeeded_runs"]}, failed={row["failed_runs"]}')
    findings.append(f'- Achieved req/s median: **{row["http_requests_per_second_median"]:.2f}**')
    findings.append(f'- TTFT p95 median: **{row["ttft_p95_ms_median"]:.2f} ms**')
    findings.append(f'- p95 / p99 median: **{row["http_p95_ms_median"]:.2f} ms / {row["http_p99_ms_median"]:.2f} ms**')
    findings.append(f'- Failure rate median: **{row["http_failed_percent_median"]:.2f}%**')
    findings.append(f'- Kong node peak util median: cpu **{row["kong_node_cpu_peak_of_allocatable_percent_median"]:.2f}%**, mem **{row["kong_node_memory_peak_of_allocatable_percent_median"]:.2f}%**')
    findings.append(f'- k6 node peak util median: cpu **{row["k6_node_cpu_peak_of_allocatable_percent_median"]:.2f}%**, mem **{row["k6_node_memory_peak_of_allocatable_percent_median"]:.2f}%**')
    findings.append('')

findings.append('Use `aggregate.csv` as the source for medians and `summary.csv` for per-run details.')
with open(findings_file, 'w') as f:
    f.write('\n'.join(findings) + '\n')
PY
}

SUMMARY_FILE="$RESULTS_DIR/summary.csv"
AGGREGATE_FILE="$RESULTS_DIR/aggregate.csv"
FINDINGS_FILE="$RESULTS_DIR/findings.md"

write_headers "$SUMMARY_FILE"

for load_value in "${VU_POINTS[@]}"; do
  for repeat_index in $(seq 1 "$REPEATS"); do
    run_one "$load_value" "$repeat_index" "$SUMMARY_FILE"
  done
done

aggregate_results "$SUMMARY_FILE" "$AGGREGATE_FILE" "$FINDINGS_FILE"

echo "Stream OpenAI campaign completed."
echo "Results directory: $RESULTS_DIR"
echo "Per-run summary: $SUMMARY_FILE"
echo "Aggregated summary: $AGGREGATE_FILE"
echo "Findings: $FINDINGS_FILE"
