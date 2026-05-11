import http from 'k6/http'
import { check } from 'k6'
import { Counter, Rate } from 'k6/metrics'

const payload = open('./chat-short.json')
const expectedResponseText = __ENV.K6_AI_EXPECTED_TEXT || 'kong-benchmark-ok'

const requestFailureCount = new Counter('ai_request_failure_count')
const non200Rate = new Rate('ai_non_200_rate')
const invalidJsonRate = new Rate('ai_invalid_json_rate')
const unexpectedBodyRate = new Rate('ai_unexpected_body_rate')

export const options = {
  scenarios: {
    ai_chat_baseline: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.K6_AI_RATE || 25),
      timeUnit: __ENV.K6_AI_TIME_UNIT || '1s',
      duration: __ENV.K6_AI_DURATION || '6m',
      preAllocatedVUs: Number(__ENV.K6_AI_PRE_ALLOCATED_VUS || 50),
      maxVUs: Number(__ENV.K6_AI_MAX_VUS || 200),
      tags: {
        benchmark: 'aigw-phase1',
        scenario: 'ai-chat-baseline',
      },
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)', 'count'],
  thresholds: {
    http_req_failed: ['rate<0.01'],
    checks: ['rate>0.99'],
    ai_non_200_rate: ['rate<0.01'],
    ai_invalid_json_rate: ['rate<0.01'],
    ai_unexpected_body_rate: ['rate<0.01'],
  },
}

export default function () {
  const url = __ENV.K6_AI_CHAT_URL || 'https://kong-kong-proxy.kong.svc.cluster.local/ai-chat'
  const timeout = __ENV.K6_AI_TIMEOUT || '30s'

  const response = http.post(url, payload, {
    timeout,
    headers: {
      'Content-Type': 'application/json',
    },
    tags: {
      route: 'ai-chat',
      workload: 'chat-short',
    },
  })

  if (response.status === 0) {
    requestFailureCount.add(1)
  }

  non200Rate.add(response.status !== 200)

  let parsed = null
  try {
    parsed = JSON.parse(response.body)
  } catch (_error) {
    invalidJsonRate.add(true)
  }

  if (parsed) {
    invalidJsonRate.add(false)
  }

  const responseText = parsed?.choices?.[0]?.message?.content
  const usage = parsed?.usage
  const unexpectedBody =
    response.status === 200 &&
    (!responseText ||
      responseText !== expectedResponseText ||
      !usage ||
      typeof usage.prompt_tokens !== 'number' ||
      typeof usage.completion_tokens !== 'number' ||
      typeof usage.total_tokens !== 'number')

  unexpectedBodyRate.add(unexpectedBody)

  check(response, {
    'status is 200': r => r.status === 200,
    'response body is valid json': () => parsed !== null,
    'response text matches expected payload': () => responseText === expectedResponseText,
    'usage payload exists': () =>
      Boolean(
        usage &&
          typeof usage.prompt_tokens === 'number' &&
          typeof usage.completion_tokens === 'number' &&
          typeof usage.total_tokens === 'number',
      ),
  })
}
