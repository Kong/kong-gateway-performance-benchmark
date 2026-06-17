# Release Performance Evidence — ai-2.0.0-rc.2

**Captured:** 2026-06-17T02:01:18Z  

_No baseline provided — this run is recorded as the reference baseline._

| Scenario | p95 ms | p99 ms | TTFT p95 | RPS | Err % | repeats |
|---|---|---|---|---|---|---|
| embeddings-openai | 52.497 | 52.803 | - | 49.99 | 0.0 | 3 |
| payload-logging | 614.638 | 615.036 | - | 24.92 | 0.0 | 3 |
| routing-ewma | 614.589 | 614.939 | - | 24.92 | 0.0 | 3 |
| routing-failover | 614.611 | 614.896 | - | 24.92 | 0.0 | 3 |
| routing-roundrobin-10 | 614.632 | 614.98 | - | 24.92 | 0.0 | 3 |
| routing-roundrobin-2 | 614.588 | 614.95 | - | 24.92 | 0.0 | 3 |
| static-chat | 122.45 | 122.816 | - | 49.97 | 0.0 | 3 |
| stream-gemini | 1326.178 | 1328.555 | 123.053 | 22.64 | 0.0 | 3 |
| stream-openai | 1326.341 | 1329.614 | 524.075 | 22.633 | 0.0 | 3 |
| token-chat-openai | 614.494 | 614.788 | - | 49.837 | 0.0 | 3 |
