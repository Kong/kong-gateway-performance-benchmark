# Release Performance Evidence — ai-2.0.0-rc.3

**Captured:** 2026-06-20T05:44:13Z  
**Baseline:** ai-2.0.0-rc.2 (2026-06-17T02:01:18Z)

## Regression Summary

| Scenario | Verdict | Details | Stability |
|---|---|---|---|
| embeddings-openai | ✅ OK | within tolerance | CV 0.2% |
| payload-logging | ✅ OK | within tolerance | CV 0.0% |
| routing-ewma | ✅ OK | within tolerance | CV 0.0% |
| routing-failover | ✅ OK | within tolerance | CV 0.0% |
| routing-roundrobin-10 | ✅ OK | within tolerance | CV 0.0% |
| routing-roundrobin-2 | ✅ OK | within tolerance | CV 0.0% |
| static-chat | ✅ OK | within tolerance | CV 0.1% |
| stream-gemini | ✅ OK | within tolerance | CV 0.1% |
| stream-openai | ✅ OK | within tolerance | CV 0.0% |
| token-chat-openai | ✅ OK | within tolerance | CV 0.0% |

## Overall: PASS

