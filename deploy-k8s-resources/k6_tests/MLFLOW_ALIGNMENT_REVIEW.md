# MLflow Alignment Review and Optimization Plan

This document reviews the Kong AI Gateway benchmark suite against the methodology in MLflow AI Gateway Performance and Benchmarks, and tracks actionable optimizations.

## Review Summary

### Reasonable Design Choices

1. Environment isolation and preflight checks are strong.
2. Scenario coverage is broad (non-streaming, streaming, routing, failover, policy overhead).
3. Release evidence is reproducible via repeat runs and archived reports.
4. Relative regression gates and stability checks already exist.

### Unreasonable or Risky Gaps

1. Release gating focuses on relative regression and can miss absolute SLO violations.
2. Gateway-only overhead is not surfaced as a first-class metric, unlike MLflow headers.
3. Driver execution logs are too silent for fast triage.
4. Release baseline uses only short fixture, which narrows representativeness.
5. Aggregation is mean-heavy and can hide tail instability.

## Optimization Plan

### Phase 1 (implemented in this change)

1. Enforce absolute SLO gates during release comparison using benchmark_config.yaml.
2. Fail release verdict when absolute SLO is violated, even if relative comparison passes.
3. Improve run-level driver observability by preserving per-run driver logs.

### Phase 2 (next)

1. Add direct-upstream control-path scenarios to estimate gateway overhead delta:
   overhead_ms = p95_gateway_path_ms - p95_direct_upstream_ms.
2. Publish explicit "Not measured" section in benchmark outputs for interpretation safety.
3. Expand release matrix beyond short fixture (at least short + medium for key scenarios).

### Phase 3 (next)

1. Add median/IQR/CI output in release aggregation and gate on robust statistics.
2. Split final verdict into four layers:
   - Functional correctness
   - Absolute SLO
   - Relative regression
   - Stability confidence

## Tracking Checklist

- [x] Add absolute SLO gate to release comparator
- [x] Wire absolute SLO gate into release baseline script
- [x] Preserve per-run driver logs in release output
- [x] Add direct-upstream overhead scenario pair
- [x] Add mlflow50 fixed-delay release track (gateway/direct pairs)
- [x] Add robust statistics (median/IQR/sample stdev in metrics.json; gate on the median; bootstrap CI on the paired gateway overhead)
- [x] Expand release fixture matrix
- [x] Gate on gateway overhead (gateway - direct, paired by run) instead of absolute latency
- [x] Four-layer verdict inputs: correctness (checks/errors/dropped), overhead SLO, relative regression, stability confidence
- [x] Run matrix in release_matrix.yaml with interleaved, pair-adjacent execution

## Update — gateway-overhead SLO

The absolute p95/p99 SLOs measured the fake provider's simulated delay (~614 ms for token chat), so even the
gateway-bypass control paths violated them and every release ended `ABSOLUTE_SLO_FAIL`. The latency SLO is now a
budget on what the gateway adds over the direct path of the same run (`overhead_slo` in `benchmark_config.yaml`;
the old thresholds only apply with `legacy_absolute_latency_slo: true`). See `RELEASE_TESTING.md`.
