#!/bin/bash
# =============================================================================
# run_gateway_comparison.sh — Multi-Gateway Performance Comparison
# =============================================================================
# Run the same benchmark scenario against multiple AI gateways and generate
# a side-by-side comparison report.
#
# Supported gateways:
#   - kong (Kong AI Gateway)
#   - litellm (LiteLLM Proxy)
#   - [future: envoy-ai, aws-bedrock-proxy, etc.]
#
# Prerequisites:
#   - Both gateways must be deployed in the cluster
#   - Upstream mocks must be accessible from both gateways
#   - k6 operator must be running
#
# Usage:
#   ./run_gateway_comparison.sh <scenario> [options]
#
# Examples:
#   ./run_gateway_comparison.sh token-chat-openai
#   ./run_gateway_comparison.sh stream-openai --gateways kong,litellm --repeats 5
# =============================================================================

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
SCENARIO=${1:-token-chat-openai}
GATEWAYS=${GATEWAYS:-kong}
FIXTURE=${FIXTURE:-short}
LOAD=${LOAD:-25}
DURATION=${DURATION:-6m}
REPEATS=${REPEATS:-3}
RESULTS_BASE_DIR=${RESULTS_BASE_DIR:-"./results/comparison-$(date -u +"%Y%m%dT%H%M%SZ")"}

usage() {
  cat <<'EOF'
Usage:
  ./run_gateway_comparison.sh <scenario> [options]

Arguments:
  scenario        Benchmark scenario (e.g., token-chat-openai, stream-openai)

Options:
  --gateways      Comma-separated list of gateways (default: kong)
  --fixture       Test fixture (default: short)
  --load          Load level (RPS or VUs depending on scenario)
  --duration      Test duration (default: 6m)
  --repeats       Number of repeats per gateway (default: 3)
  --results-dir   Results base directory

Environment Variables:
  GATEWAYS              Comma-separated gateway list
  FIXTURE               Test fixture
  LOAD                  Load level
  DURATION              Test duration
  REPEATS               Number of repeats
  SKIP_PREFLIGHT        Skip preflight checks (true/false)

Supported Gateways:
  kong      Kong AI Gateway (Enterprise)
  litellm   LiteLLM Proxy

Examples:
  ./run_gateway_comparison.sh token-chat-openai
  ./run_gateway_comparison.sh stream-openai --gateways kong,litellm --load 50
  GATEWAYS=kong,litellm ./run_gateway_comparison.sh embeddings-openai
EOF
  exit 0
}

# Parse arguments
shift || true
while [[ $# -gt 0 ]]; do
  case $1 in
    --gateways)
      GATEWAYS="$2"
      shift 2
      ;;
    --fixture)
      FIXTURE="$2"
      shift 2
      ;;
    --load)
      LOAD="$2"
      shift 2
      ;;
    --duration)
      DURATION="$2"
      shift 2
      ;;
    --repeats)
      REPEATS="$2"
      shift 2
      ;;
    --results-dir)
      RESULTS_BASE_DIR="$2"
      shift 2
      ;;
    --help|-h)
      usage
      ;;
    *)
      echo "Unknown option: $1"
      usage
      ;;
  esac
done

# Convert gateways to array
IFS=',' read -ra GATEWAY_LIST <<< "$GATEWAYS"

# -----------------------------------------------------------------------------
# Gateway Configuration
# -----------------------------------------------------------------------------
declare -A GATEWAY_PROXY_URL
declare -A GATEWAY_NAMESPACE
declare -A GATEWAY_DEPLOYMENT

# Kong configuration
GATEWAY_PROXY_URL[kong]="https://kong-kong-proxy.kong.svc.cluster.local"
GATEWAY_NAMESPACE[kong]="kong"
GATEWAY_DEPLOYMENT[kong]="kong-kong"

# LiteLLM configuration
GATEWAY_PROXY_URL[litellm]="http://litellm-proxy.litellm.svc.cluster.local:4000"
GATEWAY_NAMESPACE[litellm]="litellm"
GATEWAY_DEPLOYMENT[litellm]="litellm-proxy"

# Route mappings (gateway -> scenario -> path)
declare -A ROUTE_PATTERNS
# Kong routes (via ai-proxy-advanced plugin)
ROUTE_PATTERNS["kong:token-chat-openai"]="/bench/token/chat/openai"
ROUTE_PATTERNS["kong:stream-openai"]="/bench/token/stream/openai"
ROUTE_PATTERNS["kong:stream-gemini"]="/bench/token/stream/gemini/models/mock-gemini-2.5-flash:streamGenerateContent"
ROUTE_PATTERNS["kong:embeddings-openai"]="/bench/token/embeddings/openai"
ROUTE_PATTERNS["kong:static-chat"]="/bench/static/chat"

# LiteLLM routes (OpenAI-compatible API)
ROUTE_PATTERNS["litellm:token-chat-openai"]="/chat/completions"
ROUTE_PATTERNS["litellm:stream-openai"]="/chat/completions"
ROUTE_PATTERNS["litellm:stream-gemini"]="/chat/completions"  # LiteLLM normalizes to OpenAI format
ROUTE_PATTERNS["litellm:embeddings-openai"]="/embeddings"
ROUTE_PATTERNS["litellm:static-chat"]="/chat/completions"

# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------
log_info() {
  echo -e "\e[34m[INFO]\e[0m $*"
}

log_ok() {
  echo -e "\e[32m[OK]\e[0m $*"
}

log_warn() {
  echo -e "\e[93m[WARN]\e[0m $*"
}

log_error() {
  echo -e "\e[91m[ERROR]\e[0m $*"
}

get_route_for_gateway() {
  local gateway=$1
  local scenario=$2
  local key="${gateway}:${scenario}"
  
  if [[ -v "ROUTE_PATTERNS[$key]" ]]; then
    echo "${ROUTE_PATTERNS[$key]}"
  else
    # Default fallback
    echo "/chat/completions"
  fi
}

check_gateway_ready() {
  local gateway=$1
  local namespace="${GATEWAY_NAMESPACE[$gateway]}"
  local deployment="${GATEWAY_DEPLOYMENT[$gateway]}"
  
  if ! kubectl get namespace "$namespace" &>/dev/null; then
    log_error "Namespace '$namespace' for gateway '$gateway' does not exist"
    return 1
  fi
  
  if ! kubectl get deployment -n "$namespace" "$deployment" &>/dev/null; then
    log_error "Deployment '$deployment' for gateway '$gateway' does not exist"
    return 1
  fi
  
  local ready_replicas
  ready_replicas=$(kubectl get deployment -n "$namespace" "$deployment" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
  
  if [[ "$ready_replicas" -eq 0 ]]; then
    log_error "Gateway '$gateway' has no ready replicas"
    return 1
  fi
  
  log_ok "Gateway '$gateway' is ready ($ready_replicas replicas)"
  return 0
}

run_benchmark_for_gateway() {
  local gateway=$1
  local results_dir=$2
  
  local proxy_url="${GATEWAY_PROXY_URL[$gateway]}"
  local route=$(get_route_for_gateway "$gateway" "$SCENARIO")
  local full_url="${proxy_url}${route}"
  
  log_info "Running benchmark for gateway '$gateway'"
  log_info "  URL: $full_url"
  log_info "  Scenario: $SCENARIO"
  log_info "  Fixture: $FIXTURE"
  log_info "  Load: $LOAD"
  log_info "  Duration: $DURATION"
  log_info "  Repeats: $REPEATS"
  
  mkdir -p "$results_dir"
  
  # Run the benchmark using run_ai_benchmark.sh with gateway-specific URL
  for repeat_index in $(seq 1 "$REPEATS"); do
    local run_dir="$results_dir/run_${repeat_index}"
    mkdir -p "$run_dir"
    
    log_info "  Run $repeat_index/$REPEATS..."
    
    # Override the URL for this gateway
    K6_AI_CHAT_URL_OVERRIDE="$full_url" \
    GATEWAY="$gateway" \
    bash "$SCRIPT_DIR/run_ai_benchmark.sh" \
      "$SCENARIO" \
      "$FIXTURE" \
      "$LOAD" \
      "$DURATION" > "$run_dir/run.log" 2>&1 || true
    
    # Wait for completion and capture logs
    sleep 5
    local pod_name
    pod_name=$(kubectl get pods -n k6 --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1].metadata.name}' 2>/dev/null || true)
    
    if [[ -n "$pod_name" ]]; then
      # Wait for pod to complete (max 15 minutes)
      local timeout=900
      local elapsed=0
      while [[ $elapsed -lt $timeout ]]; do
        local phase
        phase=$(kubectl get pod -n k6 "$pod_name" -o jsonpath='{.status.phase}' 2>/dev/null || true)
        
        if [[ "$phase" == "Succeeded" || "$phase" == "Failed" || "$phase" == "Error" ]]; then
          break
        fi
        
        sleep 10
        ((elapsed += 10))
      done
      
      # Capture logs
      kubectl logs -n k6 "$pod_name" --all-containers=true > "$run_dir/runner.log" 2>&1 || true
      
      # Extract key metrics
      {
        echo "gateway=$gateway"
        echo "scenario=$SCENARIO"
        echo "fixture=$FIXTURE"
        echo "load=$LOAD"
        echo "duration=$DURATION"
        echo "repeat_index=$repeat_index"
        echo "pod_name=$pod_name"
        echo "pod_phase=$(kubectl get pod -n k6 "$pod_name" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
      } > "$run_dir/metadata.env"
    fi
  done
  
  log_ok "Completed benchmark for gateway '$gateway'"
}

generate_comparison_report() {
  local results_base=$1
  local report_file="$results_base/comparison_report.md"
  
  log_info "Generating comparison report..."
  
  python3 - "$results_base" "$report_file" "${GATEWAY_LIST[@]}" <<'PY'
import csv
import json
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

results_base = sys.argv[1]
report_file = sys.argv[2]
gateways = sys.argv[3:]

def extract_metric(log_file, label):
    """Extract a metric from k6 runner log."""
    try:
        with open(log_file) as f:
            for line in f:
                if label in line:
                    parts = line.split(':')
                    if len(parts) >= 2:
                        return float(parts[-1].strip().replace('%', '').replace('ms', ''))
    except:
        pass
    return None

def collect_gateway_metrics(gateway, base_dir):
    """Collect all metrics for a gateway."""
    gateway_dir = Path(base_dir) / gateway
    if not gateway_dir.exists():
        return None
    
    metrics = defaultdict(list)
    
    for run_dir in sorted(gateway_dir.iterdir()):
        if not run_dir.is_dir() or not run_dir.name.startswith('run_'):
            continue
        
        log_file = run_dir / 'runner.log'
        if not log_file.exists():
            continue
        
        # Extract metrics
        p95 = extract_metric(log_file, 'http_req_duration p95(ms)')
        p99 = extract_metric(log_file, 'http_req_duration p99(ms)')
        error_rate = extract_metric(log_file, 'http_req_failed(%)')
        rps = extract_metric(log_file, 'http_reqs/s')
        
        if p95 is not None:
            metrics['p95_ms'].append(p95)
        if p99 is not None:
            metrics['p99_ms'].append(p99)
        if error_rate is not None:
            metrics['error_rate'].append(error_rate)
        if rps is not None:
            metrics['rps'].append(rps)
    
    # Calculate statistics
    result = {}
    for key, values in metrics.items():
        if values:
            result[f'{key}_median'] = statistics.median(values)
            result[f'{key}_min'] = min(values)
            result[f'{key}_max'] = max(values)
            result[f'{key}_stddev'] = statistics.stdev(values) if len(values) > 1 else 0
    
    result['runs'] = len(metrics.get('p95_ms', []))
    return result

# Collect metrics for all gateways
all_metrics = {}
for gw in gateways:
    metrics = collect_gateway_metrics(gw, results_base)
    if metrics:
        all_metrics[gw] = metrics

# Generate report
lines = [
    '# AI Gateway Performance Comparison Report',
    '',
    f'**Generated:** {__import__("datetime").datetime.utcnow().isoformat()}Z',
    f'**Gateways:** {", ".join(gateways)}',
    '',
    '## Summary',
    '',
]

if not all_metrics:
    lines.append('*No metrics collected. Check run logs for errors.*')
else:
    lines.extend([
        '| Gateway | Runs | p95 (ms) | p99 (ms) | Error Rate (%) | RPS |',
        '|---------|------|----------|----------|----------------|-----|',
    ])
    
    for gw, m in sorted(all_metrics.items()):
        lines.append(
            f'| {gw} | {m.get("runs", 0)} | '
            f'{m.get("p95_ms_median", 0):.2f} | '
            f'{m.get("p99_ms_median", 0):.2f} | '
            f'{m.get("error_rate_median", 0):.2f} | '
            f'{m.get("rps_median", 0):.2f} |'
        )
    
    lines.append('')
    
    # Determine winner
    if len(all_metrics) >= 2:
        lines.append('## Winner Analysis')
        lines.append('')
        
        # Score by p95 latency (lower is better)
        p95_scores = {gw: m.get('p95_ms_median', float('inf')) for gw, m in all_metrics.items()}
        p95_winner = min(p95_scores, key=p95_scores.get)
        
        # Score by error rate (lower is better)
        error_scores = {gw: m.get('error_rate_median', float('inf')) for gw, m in all_metrics.items()}
        error_winner = min(error_scores, key=error_scores.get)
        
        # Score by RPS (higher is better)
        rps_scores = {gw: m.get('rps_median', 0) for gw, m in all_metrics.items()}
        rps_winner = max(rps_scores, key=rps_scores.get)
        
        lines.append(f'- **Lowest p95 latency:** {p95_winner} ({p95_scores[p95_winner]:.2f} ms)')
        lines.append(f'- **Lowest error rate:** {error_winner} ({error_scores[error_winner]:.2f}%)')
        lines.append(f'- **Highest throughput:** {rps_winner} ({rps_scores[rps_winner]:.2f} RPS)')
        lines.append('')
        
        # Overall recommendation
        # Simple scoring: p95 weight=0.4, error=0.3, rps=0.3
        overall_scores = {}
        for gw in all_metrics:
            # Normalize scores (0-1, lower is better for p95/error, higher is better for rps)
            p95_norm = p95_scores[gw] / max(p95_scores.values()) if max(p95_scores.values()) > 0 else 0
            error_norm = error_scores[gw] / max(error_scores.values()) if max(error_scores.values()) > 0 else 0
            rps_norm = 1 - (rps_scores[gw] / max(rps_scores.values())) if max(rps_scores.values()) > 0 else 0
            
            overall_scores[gw] = p95_norm * 0.4 + error_norm * 0.3 + rps_norm * 0.3
        
        overall_winner = min(overall_scores, key=overall_scores.get)
        lines.append(f'**Overall Recommendation:** {overall_winner}')
        lines.append('')
        lines.append('*Note: This is a weighted comparison (p95: 40%, error rate: 30%, RPS: 30%). Consider your specific requirements.*')
        lines.append('')

lines.append('## Detailed Metrics')
lines.append('')

for gw, m in sorted(all_metrics.items()):
    lines.append(f'### {gw.upper()}')
    lines.append('')
    lines.append(f'- Runs completed: {m.get("runs", 0)}')
    lines.append(f'- p95 latency: {m.get("p95_ms_median", 0):.2f} ms (range: {m.get("p95_ms_min", 0):.2f} - {m.get("p95_ms_max", 0):.2f}, stddev: {m.get("p95_ms_stddev", 0):.2f})')
    lines.append(f'- p99 latency: {m.get("p99_ms_median", 0):.2f} ms (range: {m.get("p99_ms_min", 0):.2f} - {m.get("p99_ms_max", 0):.2f})')
    lines.append(f'- Error rate: {m.get("error_rate_median", 0):.2f}% (range: {m.get("error_rate_min", 0):.2f} - {m.get("error_rate_max", 0):.2f})')
    lines.append(f'- Throughput: {m.get("rps_median", 0):.2f} RPS (range: {m.get("rps_min", 0):.2f} - {m.get("rps_max", 0):.2f})')
    lines.append('')

with open(report_file, 'w') as f:
    f.write('\n'.join(lines) + '\n')

print(f'Comparison report saved to: {report_file}')
PY

  log_ok "Comparison report generated: $report_file"
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
  echo "=============================================="
  echo "  AI Gateway Performance Comparison"
  echo "=============================================="
  echo "  Scenario: $SCENARIO"
  echo "  Gateways: ${GATEWAY_LIST[*]}"
  echo "  Fixture: $FIXTURE"
  echo "  Load: $LOAD"
  echo "  Duration: $DURATION"
  echo "  Repeats: $REPEATS"
  echo "  Results: $RESULTS_BASE_DIR"
  echo "=============================================="
  echo ""
  
  mkdir -p "$RESULTS_BASE_DIR"
  
  # Check all gateways are ready
  log_info "Checking gateway availability..."
  for gateway in "${GATEWAY_LIST[@]}"; do
    if ! check_gateway_ready "$gateway"; then
      log_error "Gateway '$gateway' is not ready. Aborting."
      exit 1
    fi
  done
  echo ""
  
  # Run benchmarks for each gateway
  for gateway in "${GATEWAY_LIST[@]}"; do
    echo ""
    echo "=============================================="
    echo "  Benchmarking: $gateway"
    echo "=============================================="
    
    gateway_results_dir="$RESULTS_BASE_DIR/$gateway"
    run_benchmark_for_gateway "$gateway" "$gateway_results_dir"
  done
  
  echo ""
  echo "=============================================="
  echo "  Generating Comparison Report"
  echo "=============================================="
  
  generate_comparison_report "$RESULTS_BASE_DIR"
  
  echo ""
  echo "=============================================="
  echo "  Comparison Complete"
  echo "=============================================="
  echo "  Results: $RESULTS_BASE_DIR"
  echo "  Report: $RESULTS_BASE_DIR/comparison_report.md"
  echo "=============================================="
}

main "$@"
