import http from 'k6/http'
import { check } from 'k6'
import { Counter, Rate, Trend } from 'k6/metrics'
import {
  buildOpenAiChatRequest,
  buildTunedHeaders,
  getFixture,
  parseDurationSeconds,
  parseOpenAiSseUsage,
  renderAiSummary,
} from './k6_ai_shared.js'

const fixture = getFixture('stream', __ENV.K6_AI_FIXTURE || 'short')
const duration = __ENV.K6_AI_DURATION || '6m'

const invalidStreamRate = new Rate('ai_invalid_stream_rate')
const streamCompletionRate = new Rate('ai_stream_completion_rate')
const non200Rate = new Rate('ai_non_200_rate')
const ttftTrend = new Trend('ai_time_to_first_token_ms')
const inputTokensTotal = new Counter('ai_input_tokens_total')
const outputTokensTotal = new Counter('ai_output_tokens_total')
const totalTokensTotal = new Counter('ai_total_tokens_total')

export const options = {
  scenarios: {
    stream_openai: {
      executor: 'constant-vus',
      vus: Number(__ENV.K6_AI_STREAM_VUS || 25),
      duration,
      tags: {
        benchmark: 'aigw-v2',
        scenario: 'stream-openai',
        fixture: __ENV.K6_AI_FIXTURE || 'short',
      },
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)', 'count'],
  thresholds: {
    http_req_failed: ['rate<0.01'],
    checks: ['rate>0.99'],
    ai_invalid_stream_rate: ['rate<0.01'],
    ai_stream_completion_rate: ['rate>0.99'],
    ai_non_200_rate: ['rate<0.01'],
  },
}

export default function () {
  const response = http.post(
    __ENV.K6_AI_CHAT_URL || 'https://kong-kong-proxy.kong.svc.cluster.local/bench/token/stream/openai',
    JSON.stringify(buildOpenAiChatRequest(fixture, true)),
    {
      timeout: __ENV.K6_AI_TIMEOUT || '120s',
      headers: buildTunedHeaders(fixture),
      tags: {
        route: 'bench-token-stream-openai',
        mode: 'streaming',
      },
    },
  )

  non200Rate.add(response.status !== 200)
  ttftTrend.add(response.timings.waiting)

  const parsed = parseOpenAiSseUsage(response.body)
  const usage = parsed.usage
  const validStream =
    response.status === 200 &&
    parsed.valid &&
    usage?.prompt_tokens === fixture.prompt_tokens &&
    usage?.completion_tokens === fixture.completion_tokens &&
    usage?.total_tokens === fixture.prompt_tokens + fixture.completion_tokens

  invalidStreamRate.add(!validStream)
  streamCompletionRate.add(validStream)

  if (usage) {
    inputTokensTotal.add(usage.prompt_tokens)
    outputTokensTotal.add(usage.completion_tokens)
    totalTokensTotal.add(usage.total_tokens)
  }

  check(response, {
    'status is 200': r => r.status === 200,
    'openai stream usage is present': () => usage !== null,
    'openai stream usage prompt tokens match fixture': () => usage?.prompt_tokens === fixture.prompt_tokens,
    'openai stream usage completion tokens match fixture': () => usage?.completion_tokens === fixture.completion_tokens,
  })
}

export function handleSummary(data) {
  return renderAiSummary(
    `stream openai (${__ENV.K6_AI_FIXTURE || 'short'})`,
    parseDurationSeconds(duration),
    data,
  )
}
