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

// AIGW-PERF-101 / 102 / 104: Advanced routing benchmark.
//
// K6_AI_ROUTING_SCENARIO selects the test path:
//   roundrobin-2   → /bench/routing/roundrobin/2   (AIGW-PERF-101a, 2 targets)
//   roundrobin-10  → /bench/routing/roundrobin/10  (AIGW-PERF-101b, 10 targets)
//   ewma           → /bench/routing/ewma            (AIGW-PERF-102)
//   failover       → /bench/routing/failover        (AIGW-PERF-104)
//
// For AIGW-PERF-104 the primary target always returns 5xx; the test verifies
// that Kong's failover delivers a 200 from the fallback and measures the
// round-trip overhead of the retry.
const routingScenario = __ENV.K6_AI_ROUTING_SCENARIO || 'roundrobin-2'
const fixture = getFixture('chat', __ENV.K6_AI_FIXTURE || 'short')
const duration = __ENV.K6_AI_DURATION || '6m'

const BASE_URL = 'https://kong-kong-proxy.kong.svc.cluster.local'
const DEFAULT_URLS = {
  'roundrobin-2': `${BASE_URL}/bench/routing/roundrobin/2`,
  'roundrobin-10': `${BASE_URL}/bench/routing/roundrobin/10`,
  'ewma': `${BASE_URL}/bench/routing/ewma`,
  'failover': `${BASE_URL}/bench/routing/failover`,
}
const url = __ENV.K6_AI_CHAT_URL || DEFAULT_URLS[routingScenario] || DEFAULT_URLS['roundrobin-2']

const invalidJsonRate = new Rate('ai_invalid_json_rate')
const unexpectedBodyRate = new Rate('ai_unexpected_body_rate')
const non200Rate = new Rate('ai_non_200_rate')
const failoverRate = new Rate('ai_failover_triggered_rate')
const inputTokensTotal = new Counter('ai_input_tokens_total')
const outputTokensTotal = new Counter('ai_output_tokens_total')
const totalTokensTotal = new Counter('ai_total_tokens_total')

export const options = {
  scenarios: {
    routing: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.K6_AI_RATE || 25),
      timeUnit: __ENV.K6_AI_TIME_UNIT || '1s',
      duration,
      preAllocatedVUs: Number(__ENV.K6_AI_PRE_ALLOCATED_VUS || 50),
      maxVUs: Number(__ENV.K6_AI_MAX_VUS || 200),
      tags: {
        benchmark: 'aigw-v2',
        scenario: `routing-${routingScenario}`,
        fixture: __ENV.K6_AI_FIXTURE || 'short',
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
  // For multi-target balancer routing (round-robin/ewma/failover), the client
  // must NOT pin a model: ai-proxy-advanced assigns the selected target's model
  // itself, and a client-supplied model collides with target selection
  // ("cannot use own model - must be: <target>"). Strip it from the payload.
  const routingPayload = buildOpenAiChatRequest(fixture, false)
  delete routingPayload.model

  const response = http.post(
    url,
    JSON.stringify(routingPayload),
    {
      timeout: __ENV.K6_AI_TIMEOUT || '60s',
      headers: buildTunedHeaders(fixture),
      tags: {
        route: `bench-routing-${routingScenario}`,
        mode: 'non-streaming',
      },
    },
  )

  non200Rate.add(response.status !== 200)

  if (routingScenario === 'failover') {
    // Failover is expected: primary returns 5xx, fallback returns 200.
    // We record whether the final response is 200 (failover succeeded).
    failoverRate.add(response.status === 200)
  }

  const parsed = parseJsonBody(response)
  invalidJsonRate.add(parsed === null)

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
    inputTokensTotal.add(usage.prompt_tokens)
    outputTokensTotal.add(usage.completion_tokens)
    totalTokensTotal.add(usage.total_tokens)
  }

  const checks = {
    'status is 200': r => r.status === 200,
    'response body is valid json': () => parsed !== null,
    'completion tokens match fixture': () => usage?.completion_tokens === fixture.completion_tokens,
    'response token count matches fixture': () => responseTokenCount === fixture.completion_tokens,
  }

  if (routingScenario === 'failover') {
    checks['failover delivered 200'] = r => r.status === 200
  }

  check(response, checks)
}

export function handleSummary(data) {
  return renderAiSummary(
    `routing ${routingScenario} (${__ENV.K6_AI_FIXTURE || 'short'})`,
    parseDurationSeconds(duration),
    data,
  )
}
