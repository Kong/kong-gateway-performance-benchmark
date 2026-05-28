import http from 'k6/http'
import { check } from 'k6'
import { Counter, Rate } from 'k6/metrics'
import {
  buildOpenAiChatRequest,
  buildTunedHeaders,
  countWords,
  getFixture,
  parseDurationSeconds,
  parseJsonBody,
  renderAiSummary,
} from './k6_ai_shared.js'

// AIGW-PERF-004: Large response body forwarding.
//
// Tests Kong's outbound body buffering as completion size grows.
// Three response tiers (prompt is kept small at 64 tokens):
//   64kb  → ~16 384 completion tokens
//   512kb → ~131 072 completion tokens
//   2mb   → ~524 288 completion tokens
//
// The fake_provider mock uses x-llm-completion-tokens to return the
// requested number of tokens so response size is deterministic.
//
// Usage: K6_AI_FIXTURE=64kb|512kb|2mb
const tier = __ENV.K6_AI_FIXTURE || '64kb'
const fixture = getFixture('large_response', tier)
const duration = __ENV.K6_AI_DURATION || '6m'

const invalidJsonRate = new Rate('ai_invalid_json_rate')
const unexpectedBodyRate = new Rate('ai_unexpected_body_rate')
const non200Rate = new Rate('ai_non_200_rate')
const bytesReceivedTotal = new Counter('ai_bytes_received_total')
const outputTokensTotal = new Counter('ai_output_tokens_total')
const totalTokensTotal = new Counter('ai_total_tokens_total')

export const options = {
  scenarios: {
    large_response: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.K6_AI_RATE || 5),
      timeUnit: __ENV.K6_AI_TIME_UNIT || '1s',
      duration,
      preAllocatedVUs: Number(__ENV.K6_AI_PRE_ALLOCATED_VUS || 20),
      maxVUs: Number(__ENV.K6_AI_MAX_VUS || 50),
      tags: {
        benchmark: 'aigw-v2',
        scenario: 'large-response',
        fixture: tier,
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
  const response = http.post(
    __ENV.K6_AI_CHAT_URL || 'https://kong-kong-proxy.kong.svc.cluster.local/bench/large/response',
    JSON.stringify(buildOpenAiChatRequest(fixture, false)),
    {
      timeout: __ENV.K6_AI_TIMEOUT || '300s',
      headers: buildTunedHeaders(fixture),
      tags: {
        route: 'bench-large-response',
        response_tier: tier,
      },
    },
  )

  non200Rate.add(response.status !== 200)

  const parsed = parseJsonBody(response)
  invalidJsonRate.add(parsed === null)

  const bodyBytes = response.body ? response.body.length : 0
  bytesReceivedTotal.add(bodyBytes)

  const usage = parsed?.usage
  const responseText = parsed?.choices?.[0]?.message?.content
  const responseTokenCount = countWords(responseText)
  const validBody =
    response.status === 200 &&
    usage &&
    usage.completion_tokens === fixture.completion_tokens &&
    responseTokenCount === fixture.completion_tokens

  unexpectedBodyRate.add(!validBody)

  if (usage) {
    outputTokensTotal.add(usage.completion_tokens)
    totalTokensTotal.add(usage.total_tokens)
  }

  check(response, {
    'status is 200': r => r.status === 200,
    'response body is valid json': () => parsed !== null,
    'completion tokens match fixture': () => usage?.completion_tokens === fixture.completion_tokens,
    'response token count matches fixture': () => responseTokenCount === fixture.completion_tokens,
  })
}

export function handleSummary(data) {
  return renderAiSummary(
    `large response (${tier})`,
    parseDurationSeconds(duration),
    data,
  )
}
