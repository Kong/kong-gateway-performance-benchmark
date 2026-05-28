import http from 'k6/http'
import { check } from 'k6'
import { Counter, Rate } from 'k6/metrics'
import {
  buildEmbeddingsRequest,
  buildTunedHeaders,
  getFixture,
  parseDurationSeconds,
  parseJsonBody,
  renderAiSummary,
} from './k6_ai_shared.js'

const fixture = getFixture('embeddings', __ENV.K6_AI_FIXTURE || 'short')
const duration = __ENV.K6_AI_DURATION || '6m'

const invalidJsonRate = new Rate('ai_invalid_json_rate')
const unexpectedBodyRate = new Rate('ai_unexpected_body_rate')
const non200Rate = new Rate('ai_non_200_rate')
const inputTokensTotal = new Counter('ai_input_tokens_total')

export const options = {
  scenarios: {
    embeddings_openai: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.K6_AI_RATE || 40),
      timeUnit: __ENV.K6_AI_TIME_UNIT || '1s',
      duration,
      preAllocatedVUs: Number(__ENV.K6_AI_PRE_ALLOCATED_VUS || 50),
      maxVUs: Number(__ENV.K6_AI_MAX_VUS || 200),
      tags: {
        benchmark: 'aigw-v2',
        scenario: 'embeddings-openai',
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
  const response = http.post(
    __ENV.K6_AI_CHAT_URL || 'https://kong-kong-proxy.kong.svc.cluster.local/bench/token/embeddings/openai',
    JSON.stringify(buildEmbeddingsRequest(fixture)),
    {
      timeout: __ENV.K6_AI_TIMEOUT || '60s',
      headers: buildTunedHeaders(fixture),
      tags: {
        route: 'bench-token-embeddings-openai',
        mode: 'embeddings',
      },
    },
  )

  non200Rate.add(response.status !== 200)

  const parsed = parseJsonBody(response)
  invalidJsonRate.add(parsed === null)

  // Validate the response follows OpenAI embeddings format:
  // { object: 'list', data: [{ object: 'embedding', index: 0, embedding: [...] }], usage: {...} }
  const embeddingData = parsed?.data?.[0]
  const usage = parsed?.usage
  const validBody =
    response.status === 200 &&
    parsed?.object === 'list' &&
    Array.isArray(embeddingData?.embedding) &&
    embeddingData.embedding.length > 0 &&
    usage &&
    typeof usage.prompt_tokens === 'number'

  unexpectedBodyRate.add(!validBody)

  if (usage) {
    inputTokensTotal.add(usage.prompt_tokens)
  }

  check(response, {
    'status is 200': r => r.status === 200,
    'response body is valid json': () => parsed !== null,
    'response object is list': () => parsed?.object === 'list',
    'embedding vector present': () => Array.isArray(embeddingData?.embedding) && embeddingData.embedding.length > 0,
    'usage prompt tokens present': () => typeof usage?.prompt_tokens === 'number',
  })
}

export function handleSummary(data) {
  return renderAiSummary(
    `embeddings openai (${__ENV.K6_AI_FIXTURE || 'short'})`,
    parseDurationSeconds(duration),
    data,
  )
}
