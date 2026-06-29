# Performance Benchmark Suite Documentation

This document describes the production-grade performance benchmarking infrastructure for AI gateways, designed for Kong AI Gateway with extensibility for LiteLLM and other gateways.

## Overview

The benchmark suite provides:
- **Environment Isolation** — Ensures EKS benchmarks run on dedicated node groups
- **Preflight Validation** — Verifies cluster health before benchmarks
- **SLO Gates** — Pass/fail evaluation against configurable thresholds
- **Multi-Gateway Comparison** — Side-by-side comparison of different AI gateways
- **Reproducibility** — Multiple repeats with statistical aggregation

## Quick Start

```bash
# Run a single benchmark
./run_ai_benchmark.sh token-chat-openai short 100 6m

# Run a campaign (multiple load points, multiple repeats)
./run_upper_token_campaign.sh 3

# Compare Kong vs LiteLLM
GATEWAYS=kong,litellm ./run_gateway_comparison.sh token-chat-openai
```

## Scripts Reference

### Core Benchmark Scripts

| Script | Purpose |
|--------|---------|
| `run_ai_benchmark.sh` | Single benchmark run |
| `run_k6_tests.sh` | Generic k6 test runner |

Direct upstream control scenarios are also available in `run_ai_benchmark.sh`
to estimate gateway-only overhead by bypassing Kong:

- `direct-token-chat-openai`
- `direct-stream-openai`
- `direct-embeddings-openai`

### Campaign Scripts

| Script | Purpose |
|--------|---------|
| `run_upper_token_campaign.sh` | Token throughput capacity testing |
| `run_stream_openai_campaign.sh` | Streaming response benchmarks |

### Infrastructure Scripts

| Script | Purpose |
|--------|---------|
| `preflight_check.sh` | Environment validation before benchmarks |
| `evaluate_benchmark_results.py` | SLO gate evaluation |
| `run_gateway_comparison.sh` | Multi-gateway performance comparison |

## Configuration

### benchmark_config.yaml

Unified configuration file for SLOs, thresholds, and gateway profiles.

```yaml
global:
  stability_cv_threshold: 0.15    # Max coefficient of variation (15%)
  warmup_ratio: 0.2               # First 20% of data is warmup

slos:
  error_rate_max_percent: 1.0     # Max acceptable error rate
  p95_latency_max_ms: 3000        # Max p95 latency
  p99_latency_max_ms: 5000        # Max p99 latency

gateways:
  kong:
    proxy_url: https://kong-kong-proxy.kong.svc.cluster.local
    namespace: kong
    deployment: kong-kong
  litellm:
    proxy_url: http://litellm-proxy.litellm.svc.cluster.local:4000
    namespace: litellm
    deployment: litellm-proxy
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY` | `kong` | Target gateway (kong, litellm) |
| `SKIP_PREFLIGHT` | `false` | Skip preflight checks |
| `SKIP_GATE_EVALUATION` | `false` | Skip SLO evaluation |
| `STRICT_GATES` | `true` | Fail build on SLO violations |
| `SKIP_EKS_ISOLATION_CHECK` | `false` | Skip node role isolation checks |

For MLflow-style overhead measurements, use the fake upstream `mlflow50`
latency profile and `mlflow50` fixture tier:

```bash
# fake_provider profile (fixed-delay style)
export FAKE_PROVIDER_LATENCY_PROFILE=mlflow50

# benchmark fixture profile
./run_ai_benchmark.sh token-chat-openai mlflow50 50 6m
./run_ai_benchmark.sh direct-token-chat-openai mlflow50 50 6m
```

`mlflow50` profile behavior:

- chat/stream default TTFT: 50ms
- chat/stream default TPOT: 0ms
- embeddings delay mode defaults to TTFT (50ms)

## Preflight Check

Validates environment before running benchmarks:

```bash
# Basic check
./preflight_check.sh

# Specify gateway
./preflight_check.sh --gateway litellm

# Strict mode (fail on warnings)
./preflight_check.sh --strict

# Quiet mode (only errors)
./preflight_check.sh --quiet
```

Checks performed:
- Kubernetes access
- Required namespaces exist
- Node roles configured correctly
- Pod health (no CrashLoopBackOff)
- Deployments ready
- Resource utilization below thresholds
- No conflicting benchmarks running

## Gate Evaluation

Evaluates benchmark results against SLOs:

```bash
# CLI usage
python3 evaluate_benchmark_results.py summary.csv \
  --scenario token-chat-openai \
  --gateway kong \
  --output markdown

# Programmatic usage
from evaluate_benchmark_results import BenchmarkEvaluator
evaluator = BenchmarkEvaluator('benchmark_config.yaml')
result = evaluator.evaluate_campaign(data, 'token-chat-openai', 'kong')
```

Exit codes:
- `0` — PASSED: All SLOs met
- `1` — FAILED: SLO violations detected
- `2` — WARNING: SLOs met with stability warnings

Release baseline comparison also enforces absolute SLOs from `benchmark_config.yaml`.
This means a release can fail even when relative regression gates pass, if any
scenario exceeds absolute limits (for example scenario `max_ttft_p95_ms`).

```bash
# Override absolute SLO config path when needed
SLO_CONFIG_PATH=./benchmark_config.yaml ./run_release_baseline.sh

# Include/exclude MLflow fixed-delay track in release matrix
INCLUDE_MLFLOW50_TRACK=true ./run_release_baseline.sh
INCLUDE_MLFLOW50_TRACK=false ./run_release_baseline.sh
```

Release run directories now include per-repeat driver logs for triage:

- `<scenario>__runN.driver.log` — orchestrator and kubectl driver output
- `<scenario>__runN.log` — extracted benchmark summary metrics

Release report now includes an estimated gateway overhead table when direct
control scenarios are present:

- `p95 delta (ms) = p95(gateway path) - p95(direct upstream path)`
- `p99 delta (ms) = p99(gateway path) - p99(direct upstream path)`

This gives a practical approximation of gateway-only latency contribution.

When `INCLUDE_MLFLOW50_TRACK=true`, the release matrix also runs
`mlflow50` fixture pairs for gateway and direct-upstream paths so the overhead
table can be interpreted under fixed-delay upstream assumptions.

## Multi-Gateway Comparison

Compare performance across different AI gateways:

```bash
# Compare Kong vs LiteLLM
./run_gateway_comparison.sh token-chat-openai \
  --gateways kong,litellm \
  --load 100 \
  --duration 6m \
  --repeats 5
```

Output:
- Per-gateway results directories
- `comparison_report.md` — Side-by-side comparison
- Winner analysis with weighted scoring

## EKS Environment

### Node Groups

| Role | Purpose | Taint |
|------|---------|-------|
| `loadgen` | k6 load generators | `role=loadgen:NoSchedule` |
| `kong` | Kong gateway pods | `role=kong:NoSchedule` |
| `support` | Mocks, observability | `role=support:NoSchedule` |

### Namespaces

| Namespace | Components |
|-----------|------------|
| `k6` | k6-operator, test runs |
| `kong` | Kong AI Gateway |
| `upstream` | Mock servers (fake_provider, wiremock) |
| `observability` | Prometheus, Grafana |
| `litellm` | LiteLLM Proxy (optional) |

## CI/CD Integration

Example GitHub Actions workflow:

```yaml
jobs:
  benchmark:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      
      - name: Configure kubectl
        run: aws eks update-kubeconfig --name benchmark-cluster
      
      - name: Run Preflight
        run: ./deploy-k8s-resources/k6_tests/preflight_check.sh --strict
      
      - name: Run Benchmark
        run: |
          cd deploy-k8s-resources/k6_tests
          ./run_upper_token_campaign.sh 3
      
      - name: Upload Results
        uses: actions/upload-artifact@v4
        with:
          name: benchmark-results
          path: deploy-k8s-resources/k6_tests/results/
```

## Troubleshooting

### Preflight Failures

1. **Namespace not found**: Deploy missing components
2. **Node role not found**: Check EKS node group labels
3. **Pods unhealthy**: Check pod logs for errors
4. **High resource utilization**: Wait or scale down other workloads

### Gate Failures

1. **Error rate too high**: Check mock server capacity
2. **Latency too high**: Review Kong plugin configuration
3. **High variance**: Increase repeats or duration

### Comparison Issues

1. **Gateway not ready**: Ensure deployment is complete
2. **Route not found**: Check route configuration in script
