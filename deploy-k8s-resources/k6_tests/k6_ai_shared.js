import { SharedArray } from 'k6/data'
import { Counter } from 'k6/metrics'

const fixtureData = new SharedArray('ai-benchmark-fixtures', () => [JSON.parse(open('./ai_benchmark_fixtures.json'))])[0]

export function getFixture(group, tier) {
  const selectedGroup = fixtureData[group]
  if (!selectedGroup) {
    throw new Error(`unknown fixture group: ${group}`)
  }

  const fixture = selectedGroup[tier]
  if (!fixture) {
    throw new Error(`unknown fixture tier '${tier}' for group '${group}'`)
  }

  return fixture
}

// ---------------------------------------------------------------------------
// Response taxonomy. Call recordStatus(response) once per request so the run
// summary can say *what kind* of failures happened, not just how many:
//   2xx / 3xx / 429 / other 4xx / 5xx   by HTTP status
//   timeout                             k6 error 1050 (request) or 1211 (dial)
//   conn_err                            any other transport failure (status 0)
// ---------------------------------------------------------------------------
const statusCounters = {
  '2xx': new Counter('ai_status_2xx'),
  '3xx': new Counter('ai_status_3xx'),
  '429': new Counter('ai_status_429'),
  '4xx': new Counter('ai_status_4xx'),
  '5xx': new Counter('ai_status_5xx'),
  timeout: new Counter('ai_err_timeout'),
  conn_err: new Counter('ai_err_conn'),
}
const STATUS_ORDER = ['2xx', '3xx', '429', '4xx', '5xx', 'timeout', 'conn_err']
const STATUS_METRIC = {
  '2xx': 'ai_status_2xx',
  '3xx': 'ai_status_3xx',
  '429': 'ai_status_429',
  '4xx': 'ai_status_4xx',
  '5xx': 'ai_status_5xx',
  timeout: 'ai_err_timeout',
  conn_err: 'ai_err_conn',
}

export function classifyStatus(status, errorCode) {
  if (status >= 200 && status < 300) return '2xx'
  if (status >= 300 && status < 400) return '3xx'
  if (status === 429) return '429'
  if (status >= 400 && status < 500) return '4xx'
  if (status >= 500) return '5xx'
  return errorCode === 1050 || errorCode === 1211 ? 'timeout' : 'conn_err'
}

export function recordStatus(response) {
  statusCounters[classifyStatus(response.status, response.error_code)].add(1)
}

export function buildTokenText(tokenCount) {
  return Array.from({ length: tokenCount }, (_, index) => `t${String(index + 1).padStart(4, '0')}`).join(' ')
}

export function buildOpenAiChatRequest(fixture, stream = false) {
  return {
    model: __ENV.K6_AI_MODEL || 'mock-gpt-4o-mini',
    stream,
    stream_options: stream ? { include_usage: true } : undefined,
    max_tokens: fixture.completion_tokens,
    messages: [{
      role: 'user',
      content: buildTokenText(fixture.prompt_tokens),
    }],
  }
}

export function buildGeminiChatRequest(fixture) {
  return {
    contents: [{
      role: 'user',
      parts: [{
        text: buildTokenText(fixture.prompt_tokens),
      }],
    }],
    generationConfig: {
      maxOutputTokens: fixture.completion_tokens,
    },
  }
}

export function buildEmbeddingsRequest(fixture) {
  return {
    model: __ENV.K6_AI_EMBEDDING_MODEL || 'text-embedding-3-small',
    input: buildTokenText(fixture.prompt_tokens),
  }
}

export function buildTunedHeaders(fixture) {
  const headers = {
    'Content-Type': 'application/json',
    'x-llm-prompt-tokens': String(fixture.prompt_tokens),
  }

  // Support custom Authorization header for gateways like LiteLLM. The runner
  // plumbs the token end-to-end as K6_AI_APIKEY; accept K6_AI_AUTH_TOKEN too.
  const authToken = __ENV.K6_AI_APIKEY || __ENV.K6_AI_AUTH_TOKEN
  if (authToken) {
    headers['Authorization'] = `Bearer ${authToken}`
  }

  if (fixture.completion_tokens !== undefined) {
    headers['x-llm-completion-tokens'] = String(fixture.completion_tokens)
  }

  if (fixture.ttft_ms !== undefined) {
    headers['x-llm-ttft-ms'] = String(fixture.ttft_ms)
  }

  if (fixture.tpot_ms !== undefined) {
    headers['x-llm-tpot-ms'] = String(fixture.tpot_ms)
  }

  if (fixture.tokens_per_chunk !== undefined) {
    headers['x-llm-tokens-per-chunk'] = String(fixture.tokens_per_chunk)
  }

  return headers
}

export function parseDurationSeconds(rawValue) {
  const value = String(rawValue || '0').trim()
  const match = value.match(/^(\d+)(ms|s|m|h)$/)
  if (!match) {
    return 0
  }

  const amount = Number(match[1])
  const unit = match[2]
  if (unit === 'ms') {
    return amount / 1000
  }

  if (unit === 's') {
    return amount
  }

  if (unit === 'm') {
    return amount * 60
  }

  return amount * 3600
}

export function countWords(text) {
  if (!text || typeof text !== 'string') {
    return 0
  }

  const trimmed = text.trim()
  return trimmed ? trimmed.split(/\s+/).length : 0
}

export function parseJsonBody(response) {
  try {
    return JSON.parse(response.body)
  } catch (_error) {
    return null
  }
}

export function parseSseEvents(body) {
  return String(body || '')
    .split(/\r?\n/)
    .filter(line => line.startsWith('data: '))
    .map(line => line.slice(6))
}

export function parseOpenAiSseUsage(body) {
  let usage = null
  let contentTokens = 0

  for (const data of parseSseEvents(body)) {
    if (data === '[DONE]') {
      continue
    }

    try {
      const event = JSON.parse(data)
      const deltaContent = event?.choices?.[0]?.delta?.content
      if (typeof deltaContent === 'string') {
        contentTokens += countWords(deltaContent)
      }

      if (event?.usage) {
        usage = event.usage
      }
    } catch (_error) {
      return { usage: null, contentTokens: 0, valid: false }
    }
  }

  return { usage, contentTokens, valid: usage !== null }
}

export function parseGeminiSseUsage(body) {
  let usage = null
  let contentTokens = 0

  for (const data of parseSseEvents(body)) {
    if (data === '[DONE]') {
      continue
    }

    try {
      const event = JSON.parse(data)
      const text = event?.candidates?.[0]?.content?.parts?.[0]?.text
      if (typeof text === 'string') {
        contentTokens += countWords(text)
      }

      if (event?.usageMetadata) {
        usage = event.usageMetadata
      }
    } catch (_error) {
      return { usage: null, contentTokens: 0, valid: false }
    }
  }

  return { usage, contentTokens, valid: usage !== null }
}

function metricValue(data, name, key = 'count', fallback = 0) {
  return data.metrics?.[name]?.values?.[key] ?? fallback
}

// Like metricValue, but undefined when the stat was not collected, so the
// summary can omit a line instead of printing a misleading 0.
function optionalValue(data, name, key) {
  return data.metrics?.[name]?.values?.[key]
}

function percent(data, name) {
  return metricValue(data, name, 'rate', 0) * 100
}

function tokensPerSecond(count, durationSeconds) {
  if (!durationSeconds) {
    return 0
  }

  return count / durationSeconds
}

export function renderAiSummary(title, durationSeconds, data, extraLines = []) {
  const inputTokens = metricValue(data, 'ai_input_tokens_total')
  const outputTokens = metricValue(data, 'ai_output_tokens_total')
  const totalTokens = metricValue(data, 'ai_total_tokens_total')
  const httpReqCount = metricValue(data, 'http_reqs')
  const httpReqRate = metricValue(data, 'http_reqs', 'rate')
  const httpP95 = metricValue(data, 'http_req_duration', 'p(95)')
  const httpP99 = metricValue(data, 'http_req_duration', 'p(99)')
  const iterationCount = metricValue(data, 'iterations')
  const iterationRate = metricValue(data, 'iterations', 'rate')
  const droppedIterations = metricValue(data, 'dropped_iterations')
  const droppedIterationRate = metricValue(data, 'dropped_iterations', 'rate')
  const vusMax = metricValue(data, 'vus_max', 'value')
  const ttftP95 = metricValue(data, 'ai_time_to_first_token_ms', 'p(95)')

  const httpAvg = optionalValue(data, 'http_req_duration', 'avg')
  const httpMed = optionalValue(data, 'http_req_duration', 'med')
  const httpP90 = optionalValue(data, 'http_req_duration', 'p(90)')
  const httpMax = optionalValue(data, 'http_req_duration', 'max')

  const lines = [
    `AI benchmark summary: ${title}`,
    `Duration(s): ${durationSeconds}`,
    `Input tokens total: ${inputTokens}`,
    `Output tokens total: ${outputTokens}`,
    `Total tokens total: ${totalTokens}`,
    `Input tokens/s: ${tokensPerSecond(inputTokens, durationSeconds).toFixed(2)}`,
    `Output tokens/s: ${tokensPerSecond(outputTokens, durationSeconds).toFixed(2)}`,
    `Total tokens/s: ${tokensPerSecond(totalTokens, durationSeconds).toFixed(2)}`,
    `http_reqs total: ${httpReqCount}`,
    `http_reqs/s: ${Number(httpReqRate).toFixed(2)}`,
    `iterations total: ${iterationCount}`,
    `iterations/s: ${Number(iterationRate).toFixed(2)}`,
    `dropped_iterations total: ${droppedIterations}`,
    `dropped_iterations/s: ${Number(droppedIterationRate).toFixed(2)}`,
    `vus_max: ${vusMax}`,
    `http_req_duration p95(ms): ${httpP95}`,
    `http_req_duration p99(ms): ${httpP99}`,
    ...[
      ['avg', httpAvg],
      ['med', httpMed],
      ['p90', httpP90],
      ['max', httpMax],
    ]
      .filter(([, value]) => value !== undefined)
      .map(([label, value]) => `http_req_duration ${label}(ms): ${Number(value).toFixed(3)}`),
    `http_req_failed(%): ${percent(data, 'http_req_failed').toFixed(2)}`,
    `checks pass rate(%): ${percent(data, 'checks').toFixed(2)}`,
  ]

  if (ttftP95) {
    lines.push(`ai_time_to_first_token_ms p95: ${ttftP95}`)
  }

  const statusCounts = STATUS_ORDER.map(name => [name, metricValue(data, STATUS_METRIC[name])])
  if (statusCounts.some(([, count]) => count > 0)) {
    lines.push(`http status counts: ${statusCounts.map(([name, count]) => `${name}=${count}`).join(' ')}`)
  }

  lines.push(...extraLines)
  lines.push('')

  return {
    stdout: `${lines.join('\n')}\n`,
  }
}
