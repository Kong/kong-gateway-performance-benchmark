# AI Gateway Performance Comparison Report

**Generated:** 2026-06-20T05:24:23.856942Z
**Gateways:** kong, litellm

## Summary

| Gateway | Runs | p95 (ms) | p99 (ms) | Error Rate (%) | RPS |
|---------|------|----------|----------|----------------|-----|
| kong | 5 | 614.29 | 614.69 | 0.00 | 199.45 |
| litellm | 5 | 1856.24 | 1971.64 | 0.00 | 158.55 |

## Winner Analysis

- **Lowest p95 latency:** kong (614.29 ms)
- **Lowest error rate:** kong (0.00%)
- **Highest throughput:** kong (199.45 RPS)

**Overall Recommendation:** kong

*Note: This is a weighted comparison (p95: 40%, error rate: 30%, RPS: 30%). Consider your specific requirements.*

## Detailed Metrics

### KONG

- Runs completed: 5
- p95 latency: 614.29 ms (range: 614.28 - 614.34, stddev: 0.02)
- p99 latency: 614.69 ms (range: 614.66 - 614.78)
- Error rate: 0.00% (range: 0.00 - 0.00)
- Throughput: 199.45 RPS (range: 199.45 - 199.45)

### LITELLM

- Runs completed: 5
- p95 latency: 1856.24 ms (range: 1853.60 - 1882.86, stddev: 12.28)
- p99 latency: 1971.64 ms (range: 1961.07 - 2011.37)
- Error rate: 0.00% (range: 0.00 - 0.00)
- Throughput: 158.55 RPS (range: 156.53 - 159.57)

