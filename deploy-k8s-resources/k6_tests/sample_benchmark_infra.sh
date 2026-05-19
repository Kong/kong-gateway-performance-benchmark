#!/bin/bash

set -euo pipefail

DURATION_SECONDS=${1:-60}
INTERVAL_SECONDS=${2:-10}

normalize_cpu_m() {
  local raw=${1:-0}

  if [[ -z "$raw" || "$raw" == "0" ]]; then
    echo 0
    return
  fi

  if [[ "$raw" == *m ]]; then
    echo "${raw%m}"
    return
  fi

  awk -v value="$raw" 'BEGIN { printf "%.0f\n", value * 1000 }'
}

normalize_memory_mi() {
  local raw=${1:-0}

  if [[ -z "$raw" || "$raw" == "0" ]]; then
    echo 0
    return
  fi

  case "$raw" in
    *Ki)
      awk -v value="${raw%Ki}" 'BEGIN { printf "%.2f\n", value / 1024 }'
      ;;
    *Mi)
      echo "${raw%Mi}"
      ;;
    *Gi)
      awk -v value="${raw%Gi}" 'BEGIN { printf "%.2f\n", value * 1024 }'
      ;;
    *Ti)
      awk -v value="${raw%Ti}" 'BEGIN { printf "%.2f\n", value * 1048576 }'
      ;;
    *)
      echo "$raw"
      ;;
  esac
}

get_latest_runner_pod() {
  kubectl get pods -n k6 --sort-by=.metadata.creationTimestamp -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
    | grep '^k6-ai-benchmark-1-' \
    | tail -1 || true
}

get_cpu_mem_for_pod() {
  local namespace=$1
  local pod_name=${2:-}

  if [[ -z "$pod_name" ]]; then
    echo "0,0"
    return
  fi

  local line
  line=$(kubectl top pod -n "$namespace" "$pod_name" --no-headers 2>/dev/null || true)
  if [[ -z "$line" ]]; then
    echo "0,0"
    return
  fi

  local cpu memory
  cpu=$(echo "$line" | awk '{print $2}')
  memory=$(echo "$line" | awk '{print $3}')
  echo "$(normalize_cpu_m "$cpu"),$(normalize_memory_mi "$memory")"
}

get_cpu_mem_for_node() {
  local node_name=${1:-}

  if [[ -z "$node_name" ]]; then
    echo "0,0"
    return
  fi

  local line
  line=$(kubectl top node "$node_name" --no-headers 2>/dev/null || true)
  if [[ -z "$line" ]]; then
    echo "0,0"
    return
  fi

  local cpu memory
  cpu=$(echo "$line" | awk '{print $2}')
  memory=$(echo "$line" | awk '{print $4}')
  echo "$(normalize_cpu_m "$cpu"),$(normalize_memory_mi "$memory")"
}

get_node_for_pod() {
  local namespace=$1
  local pod_name=${2:-}

  if [[ -z "$pod_name" ]]; then
    echo ""
    return
  fi

  kubectl get pod -n "$namespace" "$pod_name" -o jsonpath='{.spec.nodeName}' 2>/dev/null || true
}

echo "timestamp,kong_pod_cpu_m,kong_pod_memory_mi,static_openai_mock_cpu_m,static_openai_mock_memory_mi,fake_provider_cpu_m,fake_provider_memory_mi,wiremock_cpu_m,wiremock_memory_mi,k6_runner_cpu_m,k6_runner_memory_mi,kong_node_cpu_m,kong_node_memory_mi,upstream_node_cpu_m,upstream_node_memory_mi,k6_node_cpu_m,k6_node_memory_mi"

END_TIME=$(( $(date +%s) + DURATION_SECONDS ))
while [[ $(date +%s) -lt $END_TIME ]]; do
  KONG_POD=$(kubectl get pod -n kong -l app.kubernetes.io/instance=kong,app.kubernetes.io/name=kong -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  STATIC_OPENAI_MOCK_POD=$(kubectl get pod -n upstream -l app=static-openai-mock -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  FAKE_PROVIDER_POD=$(kubectl get pod -n upstream -l app=fake-provider -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  WIREMOCK_POD=$(kubectl get pod -n upstream -l app=wiremock -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  K6_RUNNER_POD=$(get_latest_runner_pod)

  KONG_NODE=$(get_node_for_pod kong "$KONG_POD")
  UPSTREAM_NODE=$(get_node_for_pod upstream "${FAKE_PROVIDER_POD:-${STATIC_OPENAI_MOCK_POD:-$WIREMOCK_POD}}")
  K6_NODE=$(get_node_for_pod k6 "$K6_RUNNER_POD")

  KONG_POD_METRICS=$(get_cpu_mem_for_pod kong "$KONG_POD")
  STATIC_OPENAI_MOCK_METRICS=$(get_cpu_mem_for_pod upstream "$STATIC_OPENAI_MOCK_POD")
  FAKE_PROVIDER_METRICS=$(get_cpu_mem_for_pod upstream "$FAKE_PROVIDER_POD")
  WIREMOCK_METRICS=$(get_cpu_mem_for_pod upstream "$WIREMOCK_POD")
  K6_RUNNER_METRICS=$(get_cpu_mem_for_pod k6 "$K6_RUNNER_POD")

  KONG_NODE_METRICS=$(get_cpu_mem_for_node "$KONG_NODE")
  UPSTREAM_NODE_METRICS=$(get_cpu_mem_for_node "$UPSTREAM_NODE")
  K6_NODE_METRICS=$(get_cpu_mem_for_node "$K6_NODE")

  echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ"),$KONG_POD_METRICS,$STATIC_OPENAI_MOCK_METRICS,$FAKE_PROVIDER_METRICS,$WIREMOCK_METRICS,$K6_RUNNER_METRICS,$KONG_NODE_METRICS,$UPSTREAM_NODE_METRICS,$K6_NODE_METRICS"
  sleep "$INTERVAL_SECONDS"
done
