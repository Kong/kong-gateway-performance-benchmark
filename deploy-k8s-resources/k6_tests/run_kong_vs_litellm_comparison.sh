#!/bin/bash
# =============================================================================
# run_kong_vs_litellm_comparison.sh — Kong vs LiteLLM Benchmark Comparison
# =============================================================================
# Run standardized benchmark scenarios against Kong AI Gateway and LiteLLM Proxy
# to produce a side-by-side performance comparison report.
#
# Prerequisites:
#   - Kong running on https://localhost:8443 with ai-proxy-advanced routes
#   - LiteLLM running on http://localhost:4000
#   - fake_provider running on 8080/8081
#   - k6 installed locally
#
# Usage:
#   ./run_kong_vs_litellm_comparison.sh [scenario] [options]
#
# Examples:
#   ./run_kong_vs_litellm_comparison.sh              # Run all scenarios
#   ./run_kong_vs_litellm_comparison.sh token-chat   # Run specific scenario
#   ./run_kong_vs_litellm_comparison.sh --quick      # Quick test (shorter duration)
# =============================================================================

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RESULTS_DIR=${RESULTS_DIR:-"./results/comparison-$(date -u +"%Y%m%dT%H%M%SZ")"}

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
# Kong Gateway
KONG_PROXY_URL="https://localhost:8443"
KONG_NAME="kong"

# LiteLLM Proxy  
LITELLM_PROXY_URL="http://localhost:4000"
LITELLM_AUTH_TOKEN="sk-litellm-master-key"
LITELLM_NAME="litellm"

# Test Parameters
DURATION=${DURATION:-"30s"}
QUICK_DURATION="15s"
REPEATS=${REPEATS:-1}

# Prometheus
PROMETHEUS_URL="http://localhost:9090/api/v1/write"

# -----------------------------------------------------------------------------
# Scenario Definitions
# -----------------------------------------------------------------------------
declare -A SCENARIOS

# Scenario: token-chat-openai (non-streaming chat)
SCENARIOS["token-chat-openai"]="
  script=k6_ai_token_chat.js
  kong_url=${KONG_PROXY_URL}/bench/token/chat/openai
  litellm_url=${LITELLM_PROXY_URL}/chat/completions
  fixture=short
  load_type=rps
  load_points=10,25,50
  description=Non-streaming chat proxy overhead
  expected_winner=Kong (lower latency at high RPS due to nginx event loop)
"

# Scenario: stream-openai (streaming chat)
SCENARIOS["stream-openai"]="
  script=k6_ai_stream_openai.js
  kong_url=${KONG_PROXY_URL}/bench/token/stream/openai
  litellm_url=${LITELLM_PROXY_URL}/chat/completions
  fixture=short
  load_type=vus
  load_points=5,10,25
  description=Streaming SSE pass-through
  expected_winner=Kong (lower TTFT due to less per-request overhead)
"

# Scenario: large-prompt (64KB payload)
SCENARIOS["large-prompt"]="
  script=k6_ai_large_prompt.js
  kong_url=${KONG_PROXY_URL}/bench/large/prompt
  litellm_url=${LITELLM_PROXY_URL}/chat/completions
  fixture=64kb
  load_type=rps
  load_points=5,10,20
  description=Large request payload buffering (64KB)
  expected_winner=Similar (depends on buffering strategy)
"

# Scenario: large-response (512KB response)
SCENARIOS["large-response"]="
  script=k6_ai_large_response.js
  kong_url=${KONG_PROXY_URL}/bench/large/response
  litellm_url=${LITELLM_PROXY_URL}/chat/completions
  fixture=512kb
  load_type=rps
  load_points=5,10,20
  description=Large response buffering (512KB)
  expected_winner=Kong (nginx zero-copy paths)
"

# Scenario: embeddings-openai
SCENARIOS["embeddings-openai"]="
  script=k6_ai_embeddings.js
  kong_url=${KONG_PROXY_URL}/bench/token/embeddings/openai
  litellm_url=${LITELLM_PROXY_URL}/embeddings
  fixture=short
  load_type=rps
  load_points=10,25,50
  description=Embeddings endpoint efficiency
  expected_winner=Kong (lower overhead)
"

# Scenario: policy-auth (authentication overhead)
SCENARIOS["policy-auth"]="
  script=k6_ai_token_chat.js
  kong_url=${KONG_PROXY_URL}/bench/policy/auth/openai
  litellm_url=${LITELLM_PROXY_URL}/chat/completions
  fixture=short
  load_type=rps
  load_points=10,25,50
  description=Authentication overhead comparison
  expected_winner=Interesting (Kong key-auth vs LiteLLM master_key)
"

# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------
log_info() { echo -e "\e[34m[INFO]\e[0m $*"; }
log_ok() { echo -e "\e[32m[OK]\e[0m $*"; }
log_warn() { echo -e "\e[93m[WARN]\e[0m $*"; }
log_error() { echo -e "\e[91m[ERROR]\e[0m $*"; }

parse_scenario_config() {
  local config="$1"
  local key="$2"
  echo "$config" | grep -oP "${key}=\K[^\s]+" | head -1
}

check_gateway_health() {
  local name=$1
  local url=$2
  local auth_token=${3:-}
  
  local curl_args=(-s -o /dev/null -w "%{http_code}" --max-time 5)
  if [[ "$url" == https://* ]]; then
    curl_args+=(-k)
  fi
  if [[ -n "$auth_token" ]]; then
    curl_args+=(-H "Authorization: Bearer $auth_token")
  fi
  
  local status
  status=$(curl "${curl_args[@]}" "$url/health" 2>/dev/null || echo "000")
  
  # LiteLLM returns 401 without auth, Kong returns various codes
  if [[ "$status" == "200" || "$status" == "401" || "$status" == "404" ]]; then
    log_ok "$name is reachable ($url)"
    return 0
  else
    log_error "$name is not reachable ($url, status: $status)"
    return 1
  fi
}

run_single_benchmark() {
  local gateway=$1
  local scenario=$2
  local load_value=$3
  local run_dir=$4
  
  local config="${SCENARIOS[$scenario]}"
  local script=$(parse_scenario_config "$config" "script")
  local fixture=$(parse_scenario_config "$config" "fixture")
  local load_type=$(parse_scenario_config "$config" "load_type")
  
  local url auth_token=""
  if [[ "$gateway" == "kong" ]]; then
    url=$(parse_scenario_config "$config" "kong_url")
  else
    url=$(parse_scenario_config "$config" "litellm_url")
    auth_token="$LITELLM_AUTH_TOKEN"
  fi
  
  mkdir -p "$run_dir"
  
  local env_vars=(
    "K6_AI_CHAT_URL=$url"
    "K6_AI_FIXTURE=$fixture"
    "K6_AI_DURATION=$DURATION"
  )
  
  if [[ -n "$auth_token" ]]; then
    env_vars+=("K6_AI_AUTH_TOKEN=$auth_token")
  fi
  
  if [[ "$load_type" == "rps" ]]; then
    env_vars+=("K6_AI_RATE=$load_value")
    env_vars+=("K6_AI_PRE_ALLOCATED_VUS=$((load_value * 2))")
    env_vars+=("K6_AI_MAX_VUS=$((load_value * 4))")
  else
    env_vars+=("K6_AI_VUS=$load_value")
  fi
  
  local k6_cmd="k6 run --insecure-skip-tls-verify"
  k6_cmd+=" -o experimental-prometheus-rw"
  k6_cmd+=" --tag gateway=$gateway"
  k6_cmd+=" --tag scenario=$scenario"
  k6_cmd+=" --tag load=$load_value"
  k6_cmd+=" $SCRIPT_DIR/$script"
  
  # Export environment and run
  (
    export K6_PROMETHEUS_RW_SERVER_URL="$PROMETHEUS_URL"
    for var in "${env_vars[@]}"; do
      export "$var"
    done
    cd "$SCRIPT_DIR"
    eval "$k6_cmd" > "$run_dir/output.log" 2>&1
  )
  
  # Extract metrics from output
  local output_file="$run_dir/output.log"
  if [[ -f "$output_file" ]]; then
    local p95 p99 error_rate rps ttft
    p95=$(grep -oP 'http_req_duration p95\(ms\): \K[\d.]+' "$output_file" | tail -1 || echo "N/A")
    p99=$(grep -oP 'http_req_duration p99\(ms\): \K[\d.]+' "$output_file" | tail -1 || echo "N/A")
    error_rate=$(grep -oP 'http_req_failed\(%\): \K[\d.]+' "$output_file" | tail -1 || echo "N/A")
    rps=$(grep -oP 'http_reqs/s: \K[\d.]+' "$output_file" | tail -1 || echo "N/A")
    ttft=$(grep -oP 'ai_time_to_first_token_ms p95: \K[\d.]+' "$output_file" | tail -1 || echo "N/A")
    
    echo "$gateway,$scenario,$load_value,$p95,$p99,$error_rate,$rps,$ttft" >> "$RESULTS_DIR/all_results.csv"
  fi
}

run_scenario_comparison() {
  local scenario=$1
  local config="${SCENARIOS[$scenario]}"
  
  local description=$(parse_scenario_config "$config" "description")
  local expected=$(parse_scenario_config "$config" "expected_winner")
  local load_points=$(parse_scenario_config "$config" "load_points")
  local load_type=$(parse_scenario_config "$config" "load_type")
  
  echo ""
  echo "=============================================="
  echo "  Scenario: $scenario"
  echo "  Description: $description"
  echo "  Expected: $expected"
  echo "  Load type: $load_type"
  echo "  Load points: $load_points"
  echo "=============================================="
  
  IFS=',' read -ra LOAD_ARRAY <<< "$load_points"
  
  for load_value in "${LOAD_ARRAY[@]}"; do
    echo ""
    log_info "Testing at $load_type=$load_value..."
    
    for repeat in $(seq 1 "$REPEATS"); do
      # Test Kong
      local kong_dir="$RESULTS_DIR/$scenario/kong/${load_type}_${load_value}/run_${repeat}"
      log_info "  Kong ($load_type=$load_value, run $repeat)..."
      run_single_benchmark "kong" "$scenario" "$load_value" "$kong_dir" || true
      
      # Brief pause between gateways
      sleep 2
      
      # Test LiteLLM
      local litellm_dir="$RESULTS_DIR/$scenario/litellm/${load_type}_${load_value}/run_${repeat}"
      log_info "  LiteLLM ($load_type=$load_value, run $repeat)..."
      run_single_benchmark "litellm" "$scenario" "$load_value" "$litellm_dir" || true
      
      sleep 2
    done
  done
}

generate_comparison_report() {
  local report_file="$RESULTS_DIR/comparison_report.md"
  
  log_info "Generating comparison report..."
  
  cat > "$report_file" << 'HEADER'
# Kong vs LiteLLM Performance Comparison Report

HEADER
  
  echo "**Generated:** $(date -u +"%Y-%m-%dT%H:%M:%SZ")" >> "$report_file"
  echo "" >> "$report_file"
  
  if [[ -f "$RESULTS_DIR/all_results.csv" ]]; then
    echo "## Summary Table" >> "$report_file"
    echo "" >> "$report_file"
    echo "| Gateway | Scenario | Load | p95 (ms) | p99 (ms) | Error % | RPS | TTFT p95 |" >> "$report_file"
    echo "|---------|----------|------|----------|----------|---------|-----|----------|" >> "$report_file"
    
    while IFS=',' read -r gateway scenario load p95 p99 error rps ttft; do
      echo "| $gateway | $scenario | $load | $p95 | $p99 | $error | $rps | $ttft |" >> "$report_file"
    done < "$RESULTS_DIR/all_results.csv"
    
    echo "" >> "$report_file"
  fi
  
  # Add scenario analysis
  echo "## Scenario Analysis" >> "$report_file"
  echo "" >> "$report_file"
  
  for scenario in "${!SCENARIOS[@]}"; do
    local config="${SCENARIOS[$scenario]}"
    local description=$(parse_scenario_config "$config" "description")
    local expected=$(parse_scenario_config "$config" "expected_winner")
    
    echo "### $scenario" >> "$report_file"
    echo "" >> "$report_file"
    echo "- **Description:** $description" >> "$report_file"
    echo "- **Expected Winner:** $expected" >> "$report_file"
    echo "" >> "$report_file"
  done
  
  log_ok "Report saved to: $report_file"
}

usage() {
  cat << 'EOF'
Usage:
  ./run_kong_vs_litellm_comparison.sh [scenario] [options]

Scenarios:
  token-chat-openai   Non-streaming chat (RPS sweep)
  stream-openai       Streaming chat with SSE (VU sweep)
  large-prompt        Large request payload (64KB)
  large-response      Large response payload (512KB)
  embeddings-openai   Embeddings endpoint
  policy-auth         Authentication overhead

Options:
  --quick             Use shorter test duration (15s)
  --repeats N         Number of repeats per load point (default: 1)
  --duration D        Test duration (default: 30s)
  --results-dir DIR   Results directory
  --all               Run all scenarios (default if no scenario specified)
  --help, -h          Show this help

Environment Variables:
  KONG_PROXY_URL      Kong proxy URL (default: https://localhost:8443)
  LITELLM_PROXY_URL   LiteLLM proxy URL (default: http://localhost:4000)
  LITELLM_AUTH_TOKEN  LiteLLM auth token (default: sk-litellm-master-key)

Examples:
  ./run_kong_vs_litellm_comparison.sh                    # Run all scenarios
  ./run_kong_vs_litellm_comparison.sh token-chat-openai  # Single scenario
  ./run_kong_vs_litellm_comparison.sh --quick            # Quick test
  ./run_kong_vs_litellm_comparison.sh stream-openai --repeats 3
EOF
  exit 0
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
  local scenarios_to_run=()
  local run_all=false
  
  # Parse arguments
  while [[ $# -gt 0 ]]; do
    case $1 in
      --quick)
        DURATION="$QUICK_DURATION"
        shift
        ;;
      --repeats)
        REPEATS="$2"
        shift 2
        ;;
      --duration)
        DURATION="$2"
        shift 2
        ;;
      --results-dir)
        RESULTS_DIR="$2"
        shift 2
        ;;
      --all)
        run_all=true
        shift
        ;;
      --help|-h)
        usage
        ;;
      -*)
        log_error "Unknown option: $1"
        usage
        ;;
      *)
        if [[ -v "SCENARIOS[$1]" ]]; then
          scenarios_to_run+=("$1")
        else
          log_error "Unknown scenario: $1"
          echo "Available scenarios: ${!SCENARIOS[*]}"
          exit 1
        fi
        shift
        ;;
    esac
  done
  
  # Default to all scenarios if none specified
  if [[ ${#scenarios_to_run[@]} -eq 0 ]] || [[ "$run_all" == "true" ]]; then
    scenarios_to_run=("token-chat-openai" "stream-openai" "embeddings-openai")
    # Uncomment below to include all scenarios by default
    # scenarios_to_run=("${!SCENARIOS[@]}")
  fi
  
  echo "=============================================="
  echo "  Kong vs LiteLLM Performance Comparison"
  echo "=============================================="
  echo "  Scenarios: ${scenarios_to_run[*]}"
  echo "  Duration: $DURATION"
  echo "  Repeats: $REPEATS"
  echo "  Results: $RESULTS_DIR"
  echo "=============================================="
  echo ""
  
  mkdir -p "$RESULTS_DIR"
  echo "gateway,scenario,load,p95_ms,p99_ms,error_rate,rps,ttft_p95" > "$RESULTS_DIR/all_results.csv"
  
  # Check gateway health
  log_info "Checking gateway availability..."
  check_gateway_health "Kong" "$KONG_PROXY_URL" || exit 1
  check_gateway_health "LiteLLM" "$LITELLM_PROXY_URL" "$LITELLM_AUTH_TOKEN" || exit 1
  
  # Run scenarios
  for scenario in "${scenarios_to_run[@]}"; do
    run_scenario_comparison "$scenario"
  done
  
  # Generate report
  generate_comparison_report
  
  echo ""
  echo "=============================================="
  echo "  Comparison Complete!"
  echo "=============================================="
  echo "  Results: $RESULTS_DIR"
  echo "  Report: $RESULTS_DIR/comparison_report.md"
  echo "  CSV: $RESULTS_DIR/all_results.csv"
  echo "=============================================="
}

main "$@"
