# Kong AI Gateway Performance Benchmark — Implementation Reference

> This document covers the full implementation of the AI Gateway benchmark test suite:
> new test scenarios, issues encountered, root-cause analysis, and fixes applied.
> Aligned with: AIGW Performance Test Case Design V1.0 (2026-05)

---

## 1. Architecture Overview

### Local Debug Environment

| Component | Address | Notes |
|-----------|---------|-------|
| Kong EE 3.14.0.3 Admin API | `http://localhost:8001` + `Kong-Admin-Token: handyshake` | Control Plane |
| Kong Proxy (HTTPS only) | `https://localhost:8443` | Data Plane — **HTTPS required** |
| ai_upstream mock | `http://localhost:8080` / `http://172.19.0.1:8080` (from container) | Static responder, always returns `"kong-benchmark-ok"` |
| fake_provider mock | `http://localhost:8081` / `http://172.19.0.1:8081` (from container) | High-fidelity mock: OpenAI / Gemini / Embeddings |
| k6 | `~/bin/k6` (v0.56.0) | Requires `K6_INSECURE_SKIP_TLS_VERIFY=true` |

> **Important**: Kong runs inside Docker containers. The host IP visible to containers is `172.19.0.1`
> (the gateway of Kong's Docker network). To look it up:
> ```bash
> docker network inspect gateway-docker-compose-generator_kong-ee-net \
>   --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}'
> ```

### Mock Service Comparison

| Feature | ai_upstream (`:8080`) | fake_provider (`:8081`) |
|---------|-----------------------|------------------------|
| OpenAI chat (non-streaming) | ✓ Fixed response: `kong-benchmark-ok` | ✓ Returns N token words: `t0001 t0002 …` |
| OpenAI chat (streaming) | ✗ | ✓ |
| Gemini chat (streaming) | ✗ | ✓ |
| Embeddings | ✗ | ✓ |
| Exact token count control | completion only (via `max_tokens`) | ✓ via request headers |
| TTFT / TPOT control | ✗ | ✓ via request headers |

**fake_provider control headers:**
```
x-llm-prompt-tokens: 64        # number of prompt tokens to simulate
x-llm-completion-tokens: 128   # number of completion tokens to return
x-llm-ttft-ms: 100             # time-to-first-token delay in ms
x-llm-tpot-ms: 4               # per-token delay in ms (non-streaming: totalDelay = ttft + tpot×tokens)
x-llm-tokens-per-chunk: 16     # tokens per SSE chunk (streaming only)
```

---

## 2. New Test Scripts

The test design document defines 37 scenarios. The following were newly implemented:

| Script | Scenario | PERF-ID |
|--------|----------|---------|
| `k6_ai_static_chat.js` | Static WireMock baseline | AIGW-PERF-002 |
| `k6_ai_stream_gemini.js` | Gemini streaming output | AIGW-PERF-005 |
| `k6_ai_embeddings.js` | OpenAI Embeddings | AIGW-PERF-006 |
| `k6_ai_policy_chat.js` | Policy overhead (auth / rate-limit / token-budget) | AIGW-PERF-201/202/203 |
| `k6_ai_policy_semantic_cache.js` | Semantic cache hit / miss | AIGW-PERF-301/302 |
| `k6_ai_large_prompt.js` | Large prompt forwarding (8 kb / 64 kb / 256 kb) | AIGW-PERF-003 |
| `k6_ai_large_response.js` | Large response forwarding (64 kb / 512 kb / 2 mb) | AIGW-PERF-004 |
| `k6_ai_routing.js` | Routing algorithms (round-robin / ewma / failover) | AIGW-PERF-101/102/104 |

### Other Files Modified

- `ai_benchmark_fixtures.json` — added `large_prompt` and `large_response` fixture groups
- `run_ai_benchmark.sh` — added 7 new `case` entries
- `kong_helm/ai-routing-benchmark.yaml` — Kong plugin and Ingress configs for routing scenarios
- `deploy-k8s-resources/kubernetes.tf` — 3 new scripts mounted in the k6 ConfigMap

---

## 3. Issues Encountered and Fixes

### Issue 1: Gemini `upstream_url` must include the full model path

**Symptom:** Gemini route returns `{"error": "unknown path: /"}` or Kong responds with 404.

**Root cause:**
`ai-proxy-advanced` plugin behaviour for the Gemini provider:

| `upstream_url` value | What happens |
|----------------------|--------------|
| `http://host:port` (base URL only) | Plugin does **not** append the Gemini model path; the stripped path `/` is forwarded instead |
| `http://host:port/v1beta/models/model-name:streamGenerateContent` (full path) | Forwarded correctly |

**Wrong config:**
```json
"options": { "upstream_url": "http://172.19.0.1:8081" }
```

**Correct config (matches production YAML):**
```json
"options": { "upstream_url": "http://172.19.0.1:8081/v1beta/models/mock-gemini-2.5-flash:streamGenerateContent" }
```

> Production reference — `kong_helm/ai-benchmark-suite.yaml` line 131:
> ```yaml
> upstream_url: http://fake-provider.upstream.svc.cluster.local:8080/v1beta/models/mock-gemini:streamGenerateContent
> ```

---

### Issue 2: `llm_format: gemini` controls the *response* format — the request must still be OpenAI format

**Symptom:** `k6_ai_stream_gemini.js` originally called `buildGeminiChatRequest` (sending native Gemini `contents` format), resulting in 100% request failures.

**Root cause:**
`llm_format: gemini` in `ai-proxy-advanced` means:
- **Input** (from client): OpenAI format (`messages`) — always
- **Output** (to client): Gemini SSE format (`candidates`, `usageMetadata`)

The k6 script must send an OpenAI-format request body and parse the Gemini-format response.

**Fix:**
```js
// Wrong — native Gemini request format
JSON.stringify(buildGeminiChatRequest(fixture))

// Correct — OpenAI request; Gemini SSE response parsed by parseGeminiSseUsage
JSON.stringify(buildOpenAiChatRequest(fixture, false))
```

**Verification — single curl before running k6:**
```bash
curl -sk -X POST 'https://localhost:8443/bench/stream/gemini' \
  -H "Content-Type: application/json" \
  -d '{"model":"mock-gemini-2.5-flash","messages":[{"role":"user","content":"test"}]}' | head -3
# Expected output: data: {"candidates":[...]}
```

---

### Issue 3: `tpot_ms` in `large_response` fixtures caused Kong proxy timeout

**Symptom:** `k6_ai_large_response.js` — checks pass rate 0%; response body only 102 bytes (Kong error response).

**Root cause:**
fake_provider calculates the total non-streaming delay as:
```
totalDelay = ttft_ms + (tpot_ms × completion_tokens)
```
Original `large_response/64kb` fixture: `tpot_ms: 4`, `completion_tokens: 16384`  
Computed delay: `100 + 4 × 16384 = 65,636 ms ≈ 65.6 s` — exceeds Kong's default 60 s proxy timeout.

**Fix:** Set `tpot_ms: 0` for all `large_response` fixtures:

```json
"large_response": {
  "64kb":  { "prompt_tokens": 64, "completion_tokens": 16384,  "ttft_ms": 50, "tpot_ms": 0 },
  "512kb": { "prompt_tokens": 64, "completion_tokens": 131072, "ttft_ms": 50, "tpot_ms": 0 },
  "2mb":   { "prompt_tokens": 64, "completion_tokens": 524288, "ttft_ms": 50, "tpot_ms": 0 }
}
```

> **Design rationale:** The `large_response` test measures Kong's ability to buffer and forward large response bodies — not realistic LLM latency. A fixed 50 ms TTFT with 0 ms TPOT keeps response time predictable and well within the timeout.

---

### Issue 4: Gemini targets require `auth.allow_override: false` explicitly

**Symptom:** Creating an `ai-proxy-advanced` plugin with a Gemini provider fails with:
```
schema violation: "gemini only support auth.allow_override = false"
```

**Fix:** Always set the field explicitly on every Gemini target:
```json
"targets": [{
  "auth": { "allow_override": false },
  "model": { "provider": "gemini", ... }
}]
```

---

### Issue 5: Token count validation requires fake_provider — ai_upstream is insufficient

**Symptom:** Scripts such as `k6_ai_policy_chat.js` connected to `ai_upstream` reported only 60% checks pass rate.

**Root cause:**
`ai_upstream` always returns the string `"kong-benchmark-ok"` (1 word) regardless of `max_tokens`. Scripts that check `responseTokenCount === fixture.completion_tokens` (e.g. 128) will always fail against this mock.

**Affected scripts** (require a fake_provider-backed route):
`k6_ai_policy_chat.js`, `k6_ai_routing.js`, `k6_ai_large_prompt.js`, `k6_ai_large_response.js`

**Set up the fake_provider chat route locally:**
```bash
# Start fake_provider
cd deploy-k8s-resources/fake_provider && PORT=8081 node server.js &

# Create Kong Service + Route + Plugin
curl -s -X POST http://localhost:8001/services \
  -H "Kong-Admin-Token: handyshake" \
  -d name=fake-chat-mock -d url=http://172.19.0.1:8081

curl -s -X POST http://localhost:8001/services/fake-chat-mock/routes \
  -H "Kong-Admin-Token: handyshake" \
  -d name=bench-token-chat \
  -d 'paths[]=/bench/token/chat/openai' -d 'methods[]=POST'

curl -s -X POST http://localhost:8001/routes/bench-token-chat/plugins \
  -H "Kong-Admin-Token: handyshake" -H "Content-Type: application/json" \
  -d '{
    "name": "ai-proxy-advanced",
    "config": {
      "llm_format": "openai",
      "response_streaming": "deny",
      "balancer": {"algorithm": "round-robin"},
      "targets": [{
        "route_type": "llm/v1/chat",
        "auth": {"allow_override": false},
        "model": {
          "provider": "openai",
          "name": "mock-gpt-4o-mini",
          "options": { "upstream_url": "http://172.19.0.1:8081/v1/chat/completions" }
        },
        "logging": { "log_payloads": false, "log_statistics": false }
      }]
    }
  }'
```

---

## 4. Local Run Command Reference

Common env vars for all scripts:
```bash
K6_INSECURE_SKIP_TLS_VERIFY=true   # required — Kong local proxy is HTTPS-only
K6_AI_RATE=5                        # override default RPS
K6_AI_DURATION=20s                  # override default duration (production uses 6m)
K6_AI_PRE_ALLOCATED_VUS=5          # pre-allocated VUs
K6_AI_MAX_VUS=10                    # max VUs
```

### Per-script commands

```bash
# Embeddings
K6_AI_CHAT_URL=https://localhost:8443/bench/token/embeddings/openai \
K6_AI_RATE=5 K6_AI_DURATION=20s K6_INSECURE_SKIP_TLS_VERIFY=true \
~/bin/k6 run k6_ai_embeddings.js

# Policy Chat (requires fake_provider route at /bench/token/chat/openai)
K6_AI_CHAT_URL=https://localhost:8443/bench/token/chat/openai \
K6_AI_SCENARIO_NAME=policy-rate-limit \
K6_AI_RATE=5 K6_AI_DURATION=20s K6_INSECURE_SKIP_TLS_VERIFY=true \
~/bin/k6 run k6_ai_policy_chat.js

# Large Prompt (requires fake_provider route)
K6_AI_CHAT_URL=https://localhost:8443/bench/token/chat/openai \
K6_AI_FIXTURE=8kb K6_AI_RATE=5 K6_AI_DURATION=20s K6_INSECURE_SKIP_TLS_VERIFY=true \
~/bin/k6 run k6_ai_large_prompt.js

# Large Response (requires fake_provider route; tpot_ms=0 already in fixture)
K6_AI_CHAT_URL=https://localhost:8443/bench/token/chat/openai \
K6_AI_FIXTURE=64kb K6_AI_RATE=3 K6_AI_DURATION=20s K6_INSECURE_SKIP_TLS_VERIFY=true \
~/bin/k6 run k6_ai_large_response.js

# Gemini Streaming (requires dedicated Gemini route — see setup below)
K6_AI_CHAT_URL='https://localhost:8443/bench/stream/gemini' \
K6_AI_STREAM_VUS=3 K6_AI_DURATION=20s K6_INSECURE_SKIP_TLS_VERIFY=true \
~/bin/k6 run k6_ai_stream_gemini.js

# Static Chat (reuse ai_upstream route; override expected text)
K6_AI_CHAT_URL='https://localhost:8443/ai-chat' \
K6_AI_EXPECTED_TEXT='kong-benchmark-ok' \
K6_AI_RATE=5 K6_AI_DURATION=20s K6_INSECURE_SKIP_TLS_VERIFY=true \
~/bin/k6 run k6_ai_static_chat.js

# Semantic Cache — miss mode (no Redis needed to validate script logic)
K6_AI_CHAT_URL=https://localhost:8443/bench/token/chat/openai \
K6_AI_CACHE_MODE=miss \
K6_AI_RATE=5 K6_AI_DURATION=20s K6_INSECURE_SKIP_TLS_VERIFY=true \
~/bin/k6 run k6_ai_policy_semantic_cache.js

# Routing (reuse fake_provider route to validate script logic)
K6_AI_CHAT_URL=https://localhost:8443/bench/token/chat/openai \
K6_AI_ROUTING_SCENARIO=roundrobin-2 \
K6_AI_RATE=5 K6_AI_DURATION=20s K6_INSECURE_SKIP_TLS_VERIFY=true \
~/bin/k6 run k6_ai_routing.js
```

### Set up the Gemini Streaming route locally

```bash
# Start fake_provider (Gemini support included)
cd deploy-k8s-resources/fake_provider && PORT=8081 node server.js &

# Create Service + Route
curl -s -X POST http://localhost:8001/services \
  -H "Kong-Admin-Token: handyshake" \
  -d name=gemini-mock -d url=http://172.19.0.1:8081

curl -s -X POST http://localhost:8001/services/gemini-mock/routes \
  -H "Kong-Admin-Token: handyshake" \
  -d name=bench-gemini-stream \
  -d 'paths[]=/bench/stream/gemini' -d 'methods[]=POST'

# Attach plugin — upstream_url MUST include the full Gemini model path
curl -s -X POST http://localhost:8001/routes/bench-gemini-stream/plugins \
  -H "Kong-Admin-Token: handyshake" -H "Content-Type: application/json" \
  -d '{
    "name": "ai-proxy-advanced",
    "config": {
      "llm_format": "gemini",
      "response_streaming": "always",
      "balancer": {"algorithm": "round-robin"},
      "targets": [{
        "route_type": "llm/v1/chat",
        "auth": {"allow_override": false},
        "model": {
          "provider": "gemini",
          "name": "mock-gemini-2.5-flash",
          "options": { "upstream_url": "http://172.19.0.1:8081/v1beta/models/mock-gemini-2.5-flash:streamGenerateContent" }
        },
        "logging": { "log_payloads": false, "log_statistics": false }
      }]
    }
  }'
```

---

## 5. Code Quality Fixes

### Dead code removed — `k6_ai_routing.js`

The variable `upstreamStatus` was declared but never referenced:

```js
// Before
const upstreamStatus = response.headers['X-Kong-Upstream-Status']
if (routingScenario === 'failover') { ... }

// After
if (routingScenario === 'failover') { ... }
```

### Request format fix — `k6_ai_stream_gemini.js`

```js
// Before — wrong import and native Gemini request format
import { buildGeminiChatRequest, ... } from './k6_ai_shared.js'
JSON.stringify(buildGeminiChatRequest(fixture))

// After — OpenAI request format; Gemini SSE response parsed by parseGeminiSseUsage
import { buildOpenAiChatRequest, ... } from './k6_ai_shared.js'
JSON.stringify(buildOpenAiChatRequest(fixture, false))
```

### Duration consistency fix — `k6_ai_static_chat.js`

```js
// Before — inconsistent with all other scripts
const duration = __ENV.K6_AI_DURATION || '3m'

// After — aligned with the rest of the suite
const duration = __ENV.K6_AI_DURATION || '6m'
```

---

## 6. Fixture Design Principles

### `large_prompt` fixtures

Goal: measure Kong's throughput when forwarding large prompt bodies.  
Keep `completion_tokens` small (256) so response processing does not dominate latency.  
Default `tpot_ms: 4` is acceptable because 4 ms × 256 tokens = ~1 s, well within the proxy timeout.

```json
"8kb":  { "prompt_tokens": 2048,  "completion_tokens": 256, "ttft_ms": 150, "tpot_ms": 4 }
"64kb": { "prompt_tokens": 16384, "completion_tokens": 256, "ttft_ms": 200, "tpot_ms": 4 }
```

### `large_response` fixtures

Goal: measure Kong's throughput when buffering and forwarding large response bodies.  
**`tpot_ms` must be 0** — otherwise `4 ms × 16 384 tokens = 65.6 s` exceeds Kong's 60 s proxy timeout.  
A fixed `ttft_ms: 50` preserves a small but realistic first-byte delay.

```json
"64kb":  { "prompt_tokens": 64, "completion_tokens": 16384,  "ttft_ms": 50, "tpot_ms": 0 }
"512kb": { "prompt_tokens": 64, "completion_tokens": 131072, "ttft_ms": 50, "tpot_ms": 0 }
"2mb":   { "prompt_tokens": 64, "completion_tokens": 524288, "ttft_ms": 50, "tpot_ms": 0 }
```

---

## 7. Scenarios Not Yet Covered

| PERF-ID | Description | Blocker |
|---------|-------------|---------|
| AIGW-PERF-103 | Semantic routing | Requires `ai-proxy-advanced` semantic balancer config |
| AIGW-PERF-105 | Upstream health-check eviction | Requires multi-upstream + active health checks |
| AIGW-PERF-202 | High consumer cardinality for AI rate limiting | Requires 100+ consumers pre-created |
| AIGW-PERF-203 | Token budget enforcement | Requires `ai-rate-limiting-advanced` plugin |
| AIGW-PERF-303–307 | Advanced cache scenarios | Requires Redis + semantic similarity tuning |
| AIGW-PERF-401–405 | Prompt guard | Requires `ai-prompt-guard` plugin |
| AIGW-PERF-501–506 | Observability overhead | Requires Prometheus + structured logging |
| AIGW-PERF-601–606 | High-concurrency stress | Requires EKS cluster environment |
| AIGW-PERF-703–705 | Mixed workload | Requires parallel multi-scenario execution |

> **Note:** The Kong config file for AIGW-PERF-006 (payload logging) — `kong_helm/ai-logging-benchmark.yaml` — has not been created yet. The corresponding `case` entry in `run_ai_benchmark.sh` is already in place.
