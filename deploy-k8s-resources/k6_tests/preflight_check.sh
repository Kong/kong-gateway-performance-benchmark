#!/bin/bash
# =============================================================================
# preflight_check.sh — Environment Stability Verification
# =============================================================================
# Run before any benchmark campaign to ensure the cluster is in a stable,
# ready state. This prevents running benchmarks on a degraded environment.
#
# Exit codes:
#   0 = All checks passed, safe to proceed
#   1 = Critical failure, do not proceed
#   2 = Warning, proceed with caution (use --strict to fail on warnings)
# =============================================================================

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
STRICT_MODE=${STRICT_MODE:-false}
GATEWAY=${GATEWAY:-kong}
QUIET=${QUIET:-false}

# Thresholds
MAX_POD_RESTARTS=${MAX_POD_RESTARTS:-0}
MAX_NODE_CPU_PERCENT=${MAX_NODE_CPU_PERCENT:-50}
MAX_NODE_MEMORY_PERCENT=${MAX_NODE_MEMORY_PERCENT:-60}
MIN_READY_PODS_PERCENT=${MIN_READY_PODS_PERCENT:-100}
STABILIZATION_WAIT_SECONDS=${STABILIZATION_WAIT_SECONDS:-30}

# Track issues
WARNINGS=0
ERRORS=0

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
log_info() {
  [[ "$QUIET" == "true" ]] || echo -e "\e[34m[INFO]\e[0m $*"
}

log_ok() {
  [[ "$QUIET" == "true" ]] || echo -e "\e[32m[OK]\e[0m $*"
}

log_warn() {
  echo -e "\e[93m[WARN]\e[0m $*"
  ((WARNINGS++)) || true
}

log_error() {
  echo -e "\e[91m[ERROR]\e[0m $*"
  ((ERRORS++)) || true
}

usage() {
  cat <<'EOF'
Usage: ./preflight_check.sh [OPTIONS]

Options:
  --gateway <name>     Gateway to check (kong, litellm). Default: kong
  --strict             Treat warnings as errors
  --quiet              Suppress info messages
  --help               Show this help

Environment Variables:
  MAX_POD_RESTARTS              Max recent pod restarts allowed (default: 0)
  MAX_NODE_CPU_PERCENT          Max idle node CPU usage (default: 50)
  MAX_NODE_MEMORY_PERCENT       Max idle node memory usage (default: 60)
  STABILIZATION_WAIT_SECONDS    Wait time for stability check (default: 30)

Examples:
  ./preflight_check.sh
  ./preflight_check.sh --gateway litellm --strict
  STRICT_MODE=true ./preflight_check.sh
EOF
  exit 0
}

# -----------------------------------------------------------------------------
# Parse Arguments
# -----------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case $1 in
    --gateway)
      GATEWAY="$2"
      shift 2
      ;;
    --strict)
      STRICT_MODE=true
      shift
      ;;
    --quiet)
      QUIET=true
      shift
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

# -----------------------------------------------------------------------------
# Gateway-Specific Configuration
# -----------------------------------------------------------------------------
case "$GATEWAY" in
  kong)
    GATEWAY_NAMESPACE="kong"
    GATEWAY_DEPLOYMENT="kong-kong"
    GATEWAY_NODE_ROLE="kong"
    ;;
  litellm)
    GATEWAY_NAMESPACE="litellm"
    GATEWAY_DEPLOYMENT="litellm-proxy"
    GATEWAY_NODE_ROLE="gateway"
    ;;
  *)
    log_error "Unknown gateway: $GATEWAY"
    exit 1
    ;;
esac

# -----------------------------------------------------------------------------
# Check Functions
# -----------------------------------------------------------------------------

check_kubectl_access() {
  log_info "Checking kubectl access..."
  if ! kubectl cluster-info &>/dev/null; then
    log_error "Cannot access Kubernetes cluster. Check your kubeconfig."
    return 1
  fi
  log_ok "Kubernetes cluster is accessible"
}

check_required_namespaces() {
  log_info "Checking required namespaces..."
  local required_namespaces=("$GATEWAY_NAMESPACE" "upstream" "k6" "observability")
  
  for ns in "${required_namespaces[@]}"; do
    if ! kubectl get namespace "$ns" &>/dev/null; then
      log_error "Namespace '$ns' does not exist"
      return 1
    fi
  done
  log_ok "All required namespaces exist"
}

check_node_roles() {
  log_info "Checking node role labels..."
  local required_roles=("loadgen" "$GATEWAY_NODE_ROLE" "support")
  
  for role in "${required_roles[@]}"; do
    local count
    count=$(kubectl get nodes -l "benchmark.konghq.com/node-role=${role}" --no-headers 2>/dev/null | wc -l | tr -d ' ')
    if [[ "$count" -eq 0 ]]; then
      log_error "No nodes found with role '$role'"
      return 1
    fi
    log_ok "Found $count node(s) with role '$role'"
  done
}

check_pod_health() {
  log_info "Checking pod health in critical namespaces..."
  local namespaces=("$GATEWAY_NAMESPACE" "upstream" "observability")
  
  for ns in "${namespaces[@]}"; do
    # Check for pods not in Running/Succeeded state
    local unhealthy
    unhealthy=$(kubectl get pods -n "$ns" --no-headers 2>/dev/null | grep -v -E 'Running|Succeeded|Completed' | wc -l | tr -d ' ')
    
    if [[ "$unhealthy" -gt 0 ]]; then
      log_error "Found $unhealthy unhealthy pods in namespace '$ns'"
      kubectl get pods -n "$ns" --no-headers | grep -v -E 'Running|Succeeded|Completed' || true
      return 1
    fi
    
    # Check for recent restarts
    local restart_info
    restart_info=$(kubectl get pods -n "$ns" -o jsonpath='{range .items[*]}{.metadata.name}{" "}{range .status.containerStatuses[*]}{.restartCount}{" "}{end}{"\n"}{end}' 2>/dev/null || true)
    
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      local pod_name restarts
      pod_name=$(echo "$line" | awk '{print $1}')
      restarts=$(echo "$line" | awk '{sum=0; for(i=2;i<=NF;i++) sum+=$i; print sum}')
      
      if [[ "$restarts" -gt "$MAX_POD_RESTARTS" ]]; then
        log_warn "Pod '$pod_name' in '$ns' has $restarts restarts"
      fi
    done <<< "$restart_info"
  done
  
  log_ok "All critical pods are healthy"
}

check_gateway_deployment() {
  log_info "Checking gateway deployment '$GATEWAY_DEPLOYMENT'..."
  
  local ready_replicas desired_replicas
  ready_replicas=$(kubectl get deployment -n "$GATEWAY_NAMESPACE" "$GATEWAY_DEPLOYMENT" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
  desired_replicas=$(kubectl get deployment -n "$GATEWAY_NAMESPACE" "$GATEWAY_DEPLOYMENT" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "0")
  
  if [[ "$ready_replicas" != "$desired_replicas" ]]; then
    log_error "Gateway deployment not fully ready: $ready_replicas/$desired_replicas replicas"
    return 1
  fi
  
  log_ok "Gateway deployment ready: $ready_replicas/$desired_replicas replicas"
}

check_upstream_deployment() {
  log_info "Checking upstream mock deployments..."
  
  local deployments=("fake-provider" "ai-openai-mock")
  for deploy in "${deployments[@]}"; do
    if kubectl get deployment -n upstream "$deploy" &>/dev/null; then
      local ready desired
      ready=$(kubectl get deployment -n upstream "$deploy" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
      desired=$(kubectl get deployment -n upstream "$deploy" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "0")
      
      if [[ "$ready" != "$desired" ]]; then
        log_error "Upstream '$deploy' not ready: $ready/$desired replicas"
        return 1
      fi
      log_ok "Upstream '$deploy' ready: $ready/$desired replicas"
    fi
  done
}

check_node_idle_resources() {
  log_info "Checking node resource utilization..."
  
  # Get node metrics
  local node_metrics
  node_metrics=$(kubectl top nodes --no-headers 2>/dev/null || true)
  
  if [[ -z "$node_metrics" ]]; then
    log_warn "Cannot get node metrics (metrics-server may not be ready)"
    return 0
  fi
  
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    local node_name cpu_percent mem_percent
    node_name=$(echo "$line" | awk '{print $1}')
    cpu_percent=$(echo "$line" | awk '{gsub(/%/,"",$3); print $3}')
    mem_percent=$(echo "$line" | awk '{gsub(/%/,"",$5); print $5}')
    
    if [[ "${cpu_percent:-0}" -gt "$MAX_NODE_CPU_PERCENT" ]]; then
      log_warn "Node '$node_name' CPU at ${cpu_percent}% (threshold: ${MAX_NODE_CPU_PERCENT}%)"
    fi
    
    if [[ "${mem_percent:-0}" -gt "$MAX_NODE_MEMORY_PERCENT" ]]; then
      log_warn "Node '$node_name' memory at ${mem_percent}% (threshold: ${MAX_NODE_MEMORY_PERCENT}%)"
    fi
  done <<< "$node_metrics"
  
  log_ok "Node resource check completed"
}

check_no_active_benchmarks() {
  log_info "Checking for active benchmark runs..."
  
  local active_testruns
  active_testruns=$(kubectl get testrun -n k6 --no-headers 2>/dev/null | grep -v -E 'finished|completed|error' | wc -l | tr -d ' ')
  
  if [[ "$active_testruns" -gt 0 ]]; then
    log_error "Found $active_testruns active TestRun(s) in k6 namespace"
    kubectl get testrun -n k6 --no-headers | grep -v -E 'finished|completed|error' || true
    return 1
  fi
  
  log_ok "No active benchmark runs detected"
}

check_prometheus_connectivity() {
  log_info "Checking Prometheus connectivity..."
  
  local prom_pod
  prom_pod=$(kubectl get pod -n observability -l app.kubernetes.io/name=prometheus -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  
  if [[ -z "$prom_pod" ]]; then
    log_warn "Prometheus pod not found"
    return 0
  fi
  
  # Check if Prometheus is responding
  if ! kubectl exec -n observability "$prom_pod" -c prometheus-server -- wget -qO- http://localhost:9090/-/ready &>/dev/null; then
    log_warn "Prometheus is not responding"
    return 0
  fi
  
  log_ok "Prometheus is ready"
}

wait_for_stabilization() {
  if [[ "$STABILIZATION_WAIT_SECONDS" -eq 0 ]]; then
    return 0
  fi
  
  log_info "Waiting ${STABILIZATION_WAIT_SECONDS}s for environment stabilization..."
  sleep "$STABILIZATION_WAIT_SECONDS"
  log_ok "Stabilization wait completed"
}

capture_baseline_snapshot() {
  log_info "Capturing baseline environment snapshot..."
  
  local snapshot_file="${1:-/tmp/preflight_snapshot.txt}"
  
  {
    echo "=== Preflight Snapshot: $(date -u +"%Y-%m-%dT%H:%M:%SZ") ==="
    echo ""
    echo "--- Nodes ---"
    kubectl get nodes -o wide
    echo ""
    echo "--- Node Metrics ---"
    kubectl top nodes 2>/dev/null || echo "(metrics unavailable)"
    echo ""
    echo "--- Gateway Pods ---"
    kubectl get pods -n "$GATEWAY_NAMESPACE" -o wide
    echo ""
    echo "--- Upstream Pods ---"
    kubectl get pods -n upstream -o wide
    echo ""
    echo "--- Observability Pods ---"
    kubectl get pods -n observability -o wide
    echo ""
    echo "--- k6 Namespace ---"
    kubectl get all -n k6
    echo ""
  } > "$snapshot_file"
  
  log_ok "Baseline snapshot saved to $snapshot_file"
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
  echo "=============================================="
  echo "  Benchmark Environment Preflight Check"
  echo "  Gateway: $GATEWAY"
  echo "  Strict Mode: $STRICT_MODE"
  echo "=============================================="
  echo ""
  
  check_kubectl_access
  check_required_namespaces
  check_node_roles
  check_pod_health
  check_gateway_deployment
  check_upstream_deployment
  check_node_idle_resources
  check_no_active_benchmarks
  check_prometheus_connectivity
  
  wait_for_stabilization
  
  capture_baseline_snapshot "/tmp/preflight_snapshot_$(date +%s).txt"
  
  echo ""
  echo "=============================================="
  
  if [[ "$ERRORS" -gt 0 ]]; then
    echo -e "\e[91m  PREFLIGHT FAILED: $ERRORS error(s), $WARNINGS warning(s)\e[0m"
    echo "=============================================="
    exit 1
  elif [[ "$WARNINGS" -gt 0 ]]; then
    if [[ "$STRICT_MODE" == "true" ]]; then
      echo -e "\e[91m  PREFLIGHT FAILED (strict): $WARNINGS warning(s)\e[0m"
      echo "=============================================="
      exit 2
    else
      echo -e "\e[93m  PREFLIGHT PASSED with $WARNINGS warning(s)\e[0m"
      echo "=============================================="
      exit 0
    fi
  else
    echo -e "\e[92m  PREFLIGHT PASSED: All checks OK\e[0m"
    echo "=============================================="
    exit 0
  fi
}

main "$@"
