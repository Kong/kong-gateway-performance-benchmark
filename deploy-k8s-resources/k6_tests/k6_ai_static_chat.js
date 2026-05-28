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

// static-chat uses a WireMock fixed-response mock that always returns the same
// body regardless of the prompt, making it a zero-variance baseline for
// comparing against the token-chat path.
const fixture = getFixture('static', 'chat')
const duration = __ENV.K6_AI_DURATION || '6m'

const invalidJsonRate = new Rate('ai_invalid_json_rate')
const unexpectedBodyRate = new Rate('ai_unexpected_body_rate')
const non200Rate = new Rate('ai_non_200_rate')
const inputTokensTotal = new Counter('ai_input_tokens_total')
const outputTokensTotal = new Counter('ai_output_tokens_total')
const totalTokensTotal = new Counter('ai_total_tokens_total')

export const options = {
  scenarios: {
    static_chat: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.K6_AI_RATE || 25),
      timeUnit: __ENV.K6_AI_TIME_UNIT || '1s',
      duration,
      preAllocatedVUs: Number(__ENV.K6_AI_PRE_ALLOCATED_VUS || 50),
      maxVUs: Number(__ENV.K6_AI_MAX_VUS || 200),
      tags: {
        benchmark: 'aigw-v2',
        scenario: 'static-chat',
        fixture: 'static',
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
    __ENV.K6_AI_CHAT_URL || 'https://kong-kong-proxy.kong.svc.cluster.local/bench/static/chat',
    JSON.stringify(buildOpenAiChatRequest(fixture, false)),
    {
      timeout: __ENV.K6_AI_TIMEOUT || '60s',
      headers: buildTunedHeaders(fixture),
      tags: {
        route: 'bench-static-chat',
        mode: 'non-streaming',
      },
    },
  )

  non200Rate.add(response.status !== 200)

  const parsed = parseJsonBody(response)
  invalidJsonRate.add(parsed === null)

  const usage = parsed?.usage
  const responseText = parsed?.choices?.[0]?.message?.content
  const expectedText = __ENV.K6_AI_EXPECTED_TEXT || fixture.expected_text || 'wiremock-static-response'

  // Static mock returns a fixed response; verify content matches the known value.
  const validBody =
    response.status === 200 &&
    responseText === expectedText &&
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
    'response text matches static fixture': () => responseText === expectedText,
    'usage payload exists': () =>
      Boolean(
        usage &&
          typeof usage.prompt_tokens === 'number' &&
          typeof usage.completion_tokens === 'number' &&
          typeof usage.total_tokens === 'number',
      ),
  })
}

export function handleSummary(data) {
  return renderAiSummary('static chat', parseDurationSeconds(duration), data)
}
