import http from 'k6/http'
import { check } from 'k6'
import { Counter, Rate } from 'k6/metrics'
import {
  buildTunedHeaders,
  buildTokenText,
  countWords,
  getFixture,
  parseDurationSeconds,
  parseJsonBody,
  renderAiSummary,
} from './k6_ai_shared.js'

// Semantic cache benchmark.
//
// K6_AI_CACHE_MODE controls the prompt strategy:
//   'hit'  → every VU sends the identical prompt so the cache will be warm
//            after the first request. Measures cached-response overhead.
//   'miss' → each iteration generates a unique prompt via iteration counter
//            so the cache is never warm. Measures cache-miss + upstream path.
//
// Both modes run against the same Kong route (K6_AI_CHAT_URL). The route
// must have ai-semantic-cache enabled and ai-proxy-advanced as the fallback.
const fixture = getFixture('chat', __ENV.K6_AI_FIXTURE || 'short')
const duration = __ENV.K6_AI_DURATION || '6m'
const cacheMode = __ENV.K6_AI_CACHE_MODE || 'miss'
const scenarioName = __ENV.K6_AI_SCENARIO_NAME || `policy-semantic-cache-${cacheMode}`

// Fixed prompt for cache-hit mode: all VUs send the same text so the cache
// warms on the first request and every subsequent one is a hit.
const fixedPrompt = buildTokenText(fixture.prompt_tokens)

const invalidJsonRate = new Rate('ai_invalid_json_rate')
const unexpectedBodyRate = new Rate('ai_unexpected_body_rate')
const non200Rate = new Rate('ai_non_200_rate')
const cacheHitRate = new Rate('ai_cache_hit_rate')
const inputTokensTotal = new Counter('ai_input_tokens_total')
const outputTokensTotal = new Counter('ai_output_tokens_total')
const totalTokensTotal = new Counter('ai_total_tokens_total')

export const options = {
  scenarios: {
    semantic_cache: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.K6_AI_RATE || 25),
      timeUnit: __ENV.K6_AI_TIME_UNIT || '1s',
      duration,
      preAllocatedVUs: Number(__ENV.K6_AI_PRE_ALLOCATED_VUS || 50),
      maxVUs: Number(__ENV.K6_AI_MAX_VUS || 200),
      tags: {
        benchmark: 'aigw-v2',
        scenario: scenarioName,
        fixture: __ENV.K6_AI_FIXTURE || 'short',
        cache_mode: cacheMode,
      },
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)', 'count'],
  thresholds: {
    http_req_failed: ['rate<0.01'],
    checks: ['rate>0.99'],
    ai_invalid_json_rate: ['rate<0.01'],
    ai_unexpected_body_rate: ['rate<0.01'],
    ai_non_200_rate: ['rate<0.01'],
  },
}

export default function () {
  // For cache-miss mode each iteration uses a unique prompt so nothing is
  // ever reused from the semantic cache. __ITER is the global iteration counter
  // across all VUs, giving each request a distinct seed.
  const prompt =
    cacheMode === 'hit'
      ? fixedPrompt
      : `perf-unique-${__ITER} ` + buildTokenText(Math.max(1, fixture.prompt_tokens - 1))

  const body = {
    model: __ENV.K6_AI_MODEL || 'mock-gpt-4o-mini',
    stream: false,
    max_tokens: fixture.completion_tokens,
    messages: [{ role: 'user', content: prompt }],
  }

  const headers = buildTunedHeaders(fixture)

  const response = http.post(
    __ENV.K6_AI_CHAT_URL || 'https://kong-kong-proxy.kong.svc.cluster.local/bench/policy/cache/openai',
    JSON.stringify(body),
    {
      timeout: __ENV.K6_AI_TIMEOUT || '60s',
      headers,
      tags: {
        route: 'bench-policy-cache',
        cache_mode: cacheMode,
      },
    },
  )

  non200Rate.add(response.status !== 200)

  const parsed = parseJsonBody(response)
  invalidJsonRate.add(parsed === null)

  // Kong ai-semantic-cache sets X-Cache-Status: Hit on cache hits
  const isCacheHit = response.headers['X-Cache-Status'] === 'Hit'
  cacheHitRate.add(isCacheHit)

  const usage = parsed?.usage
  const responseText = parsed?.choices?.[0]?.message?.content
  const responseTokenCount = countWords(responseText)

  // For hit mode the response must be valid JSON with usage.
  // For miss mode also verify token counts match the fixture.
  const validBody =
    response.status === 200 &&
    parsed !== null &&
    usage &&
    typeof usage.prompt_tokens === 'number' &&
    typeof usage.completion_tokens === 'number' &&
    typeof usage.total_tokens === 'number'

  unexpectedBodyRate.add(!validBody)

  if (usage) {
    inputTokensTotal.add(usage.prompt_tokens)
    outputTokensTotal.add(usage.completion_tokens)
    totalTokensTotal.add(usage.total_tokens)
  }

  check(response, {
    'status is 200': r => r.status === 200,
    'response body is valid json': () => parsed !== null,
    'usage payload exists': () =>
      Boolean(
        usage &&
          typeof usage.prompt_tokens === 'number' &&
          typeof usage.completion_tokens === 'number' &&
          typeof usage.total_tokens === 'number',
      ),
    ...(cacheMode === 'hit'
      ? { 'cache hit': () => isCacheHit }
      : { 'response token count matches fixture': () => responseTokenCount === fixture.completion_tokens }),
  })
}

export function handleSummary(data) {
  return renderAiSummary(
    `semantic cache ${cacheMode} (${__ENV.K6_AI_FIXTURE || 'short'})`,
    parseDurationSeconds(duration),
    data,
  )
}
