# Release Performance Evidence — Hudini Local

**Captured:** 2026-06-23  
**Gateway:** `http://localhost:8888`  
**Mock upstream:** `http://localhost:8081`  
**Fixture:** `short`

## Verification Summary

| Scenario | Load | Duration | Verdict | Notes |
|---|---:|---:|---|---|
| anthropic-stream | 8 VUs | 5m | ✅ PASS | Anthropic-compatible streaming path stayed stable |
| anthropic-native-stream | 8 VUs | 5m | ✅ PASS | Native `/v1/messages` path stayed stable |
| openai-chat | 50 VUs max | 5m | ✅ PASS | Non-streaming OpenAI path stayed stable |
| openai-stream | 5 VUs | 5m | ✅ PASS | Streaming OpenAI path stayed stable |
| soak test | 8 VUs | 10m | ✅ PASS | Separate long soak completed with 0% failures |

## Overall

**Current status: PASS for the unified 5-minute main scenarios, plus the separate 10-minute Anthropic stream soak.**

## Environment Notes

1. Local fake provider is healthy on `localhost:8081` for OpenAI, Gemini, and native Anthropic.
2. Hudini supports both `POST /v1/chat/completions` and `POST /v1/messages` in the local gateway script.
3. The Anthropic SSE parser regression did not reappear in the refreshed 5-minute traffic.
4. The benchmark runner defaults OpenAI traffic to provider-prefixed `openai/mock-gpt-4o-mini` for stable route detection.

## Commands Run

```bash
# Unified 5-minute main scenarios
./run_hudini_benchmark.sh anthropic-stream short 8 5m
./run_hudini_benchmark.sh anthropic-native-stream short 8 5m
./run_hudini_benchmark.sh openai-chat short 50 5m
./run_hudini_benchmark.sh openai-stream short 5 5m

# Separate soak evidence
./run_hudini_benchmark.sh anthropic-stream short 8 10m
```

## Main Scenario Results

### Anthropic Stream, 8 VUs, 5m

| Metric | Value |
|---|---:|
| http_req_failed | `0.00%` |
| checks pass rate | `100.00%` |
| http_reqs total | `2248` |
| http_reqs/s | `7.33` |
| output tokens total | `575488` |
| output tokens/s | `1918.29` |
| http_req_duration p95 | `1084.61 ms` |
| http_req_duration p99 | `1813.54 ms` |
| ai_time_to_first_token_ms p95 | `2.09 ms` |

### Anthropic Native Stream, 8 VUs, 5m

| Metric | Value |
|---|---:|
| Route | `http://localhost:8888/v1/messages` |
| http_req_failed | `0.00%` |
| checks pass rate | `100.00%` |
| http_reqs total | `2248` |
| http_reqs/s | `7.35` |
| output tokens total | `575488` |
| output tokens/s | `1918.29` |
| http_req_duration p95 | `1088.08 ms` |
| http_req_duration p99 | `1715.24 ms` |
| ai_time_to_first_token_ms p95 | `1.90 ms` |

### OpenAI Chat, 50 VUs max, 5m

| Metric | Value |
|---|---:|
| http_req_failed | `0.00%` |
| checks pass rate | `100.00%` |
| http_reqs total | `1800` |
| http_reqs/s | `5.85` |
| input tokens total | `115200` |
| output tokens total | `230400` |
| total tokens total | `345600` |
| input tokens/s | `384.00` |
| output tokens/s | `768.00` |
| total tokens/s | `1152.00` |
| http_req_duration p95 | `615.18 ms` |
| http_req_duration p99 | `1452.19 ms` |

### OpenAI Stream, 5 VUs, 5m

| Metric | Value |
|---|---:|
| http_req_failed | `0.00%` |
| checks pass rate | `100.00%` |
| http_reqs total | `1130` |
| http_reqs/s | `3.68` |
| input tokens total | `72320` |
| output tokens total | `289280` |
| total tokens total | `361600` |
| input tokens/s | `241.07` |
| output tokens/s | `964.27` |
| total tokens/s | `1205.33` |
| http_req_duration p95 | `1329.78 ms` |
| http_req_duration p99 | `2127.77 ms` |
| ai_time_to_first_token_ms p95 | `122.71 ms` |

## Soak Evidence

### Anthropic Stream Soak, 8 VUs, 10m

**Status:** Completed (PASS)  
**Artifacts:**

- `results/hudini-local-soak-anthropic-stream-short-8vu-10m-2026-06-23-full.log`
- `results/hudini-local-soak-anthropic-stream-short-8vu-10m-2026-06-23-summary.json`
- `results/hudini-local-soak-anthropic-stream-short-8vu-10m-2026-06-23.log` (interrupted attempt, kept for traceability)

| Metric | Final Value |
|---|---:|
| duration | `600s` |
| http_req_failed | `0.00%` |
| checks pass rate | `100.00%` |
| http_reqs total | `4504` |
| http_reqs/s | `7.29` |
| dropped_iterations | `0` |
| output tokens total | `1153024` |
| output tokens/s | `1921.71` |
| http_req_duration p95 | `1070.71 ms` |
| http_req_duration p99 | `1981.88 ms` |
| ai_time_to_first_token_ms p95 | `1.84 ms` |

## Key Findings

1. The 5-minute main scenarios now give a consistent evidence set across Anthropic and OpenAI paths.
2. Anthropic streaming stayed stable through both `/v1/chat/completions` and native `/v1/messages`.
3. OpenAI chat and OpenAI streaming both passed precheck and k6 assertions in the refreshed 5-minute runs.
4. The separate 10-minute Anthropic stream soak also completed successfully, so the report now has both steady-state and soak evidence.
