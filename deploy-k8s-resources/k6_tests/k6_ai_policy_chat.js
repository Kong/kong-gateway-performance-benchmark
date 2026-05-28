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

// Shared script for policy overhead benchmarks:
//   policy-auth-openai    → measures key-auth plugin overhead
//   policy-rate-limit     → measures ai-rate-limiting-advanced overhead
//   policy-token-budget   → measures token budget plugin overhead
//
// The active scenario is selected via K6_AI_SCENARIO_NAME. Each scenario
// points to a different Kong route via K6_AI_CHAT_URL.
const fixture = getFixture('chat', __ENV.K6_AI_FIXTURE || 'short')
const duration = __ENV.K6_AI_DURATION || '6m'
const scenarioName = __ENV.K6_AI_SCENARIO_NAME || 'policy-chat'
const apiKey = __ENV.K6_AI_APIKEY || ''

const invalidJsonRate = new Rate('ai_invalid_json_rate')
const unexpectedBodyRate = new Rate('ai_unexpected_body_rate')
const non200Rate = new Rate('ai_non_200_rate')
const inputTokensTotal = new Counter('ai_input_tokens_total')
const outputTokensTotal = new Counter('ai_output_tokens_total')
const totalTokensTotal = new Counter('ai_total_tokens_total')

export const options = {
  scenarios: {
    policy_chat: {
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
  const url = __ENV.K6_AI_CHAT_URL || 'https://kong-kong-proxy.kong.svc.cluster.local/bench/policy/auth/openai'

  const headers = buildTunedHeaders(fixture)
  if (apiKey) {
    headers['apikey'] = apiKey
  }

  const response = http.post(
    url,
    JSON.stringify(buildOpenAiChatRequest(fixture, false)),
    {
      timeout: __ENV.K6_AI_TIMEOUT || '60s',
      headers,
      tags: {
        route: scenarioName,
        mode: 'non-streaming',
      },
    },
  )

  non200Rate.add(response.status !== 200)

  const parsed = parseJsonBody(response)
  invalidJsonRate.add(parsed === null)

  const usage = parsed?.usage
  const responseText = parsed?.choices?.[0]?.message?.content
  const responseTokenCount = countWords(responseText)
  const validBody =
    response.status === 200 &&
    usage &&
    usage.prompt_tokens === fixture.prompt_tokens &&
    usage.completion_tokens === fixture.completion_tokens &&
    usage.total_tokens === fixture.prompt_tokens + fixture.completion_tokens &&
    responseTokenCount === fixture.completion_tokens

  unexpectedBodyRate.add(!validBody)

  if (usage) {
    inputTokensTotal.add(usage.prompt_tokens)
    outputTokensTotal.add(usage.completion_tokens)
    totalTokensTotal.add(usage.total_tokens)
  }

  check(response, {
    'status is 200': r => r.status === 200,
    'response body is valid json': () => parsed !== null,
    'usage prompt tokens match fixture': () => usage?.prompt_tokens === fixture.prompt_tokens,
    'usage completion tokens match fixture': () => usage?.completion_tokens === fixture.completion_tokens,
    'response token count matches fixture': () => responseTokenCount === fixture.completion_tokens,
  })
}

export function handleSummary(data) {
  return renderAiSummary(
    `${scenarioName} (${__ENV.K6_AI_FIXTURE || 'short'})`,
    parseDurationSeconds(duration),
    data,
  )
}
