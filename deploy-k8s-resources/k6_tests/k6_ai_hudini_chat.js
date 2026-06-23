import http from 'k6/http'
import { check } from 'k6'
import { Counter, Rate, Trend } from 'k6/metrics'
import {
  buildTokenText,
  countWords,
  getFixture,
  parseDurationSeconds,
  parseJsonBody,
  parseOpenAiSseUsage,
  renderAiSummary,
} from './k6_ai_shared.js'

const profile = String(__ENV.K6_AI_PROFILE || 'openai').toLowerCase()
const streamEnabled = String(__ENV.K6_AI_STREAM || 'true').toLowerCase() !== 'false'
const anthropicEndpoint = String(__ENV.K6_AI_ANTHROPIC_ENDPOINT || 'chat-completions').toLowerCase()
const fixture = getFixture(streamEnabled ? 'stream' : 'chat', __ENV.K6_AI_FIXTURE || 'short')
const duration = __ENV.K6_AI_DURATION || '6m'

const defaultUrl = profile === 'anthropic'
  ? (anthropicEndpoint === 'messages' ? 'http://localhost:8888/v1/messages' : 'http://localhost:8888/v1/chat/completions')
  : 'http://localhost:8888/v1/chat/completions'

const invalidJsonRate = new Rate('ai_invalid_json_rate')
const invalidStreamRate = new Rate('ai_invalid_stream_rate')
const streamCompletionRate = new Rate('ai_stream_completion_rate')
const non200Rate = new Rate('ai_non_200_rate')
const ttftTrend = new Trend('ai_time_to_first_token_ms')
const inputTokensTotal = new Counter('ai_input_tokens_total')
const outputTokensTotal = new Counter('ai_output_tokens_total')
const totalTokensTotal = new Counter('ai_total_tokens_total')

export const options = {
  scenarios: streamEnabled
    ? {
        hudini_stream: {
          executor: 'constant-vus',
          vus: Number(__ENV.K6_AI_STREAM_VUS || 25),
          duration,
          tags: {
            benchmark: 'hudini',
            scenario: `hudini-${profile}-stream`,
            fixture: __ENV.K6_AI_FIXTURE || 'short',
          },
        },
      }
    : {
        hudini_chat: {
          executor: 'constant-arrival-rate',
          rate: Number(__ENV.K6_AI_RATE || 25),
          timeUnit: __ENV.K6_AI_TIME_UNIT || '1s',
          duration,
          preAllocatedVUs: Number(__ENV.K6_AI_PRE_ALLOCATED_VUS || 50),
          maxVUs: Number(__ENV.K6_AI_MAX_VUS || 200),
          tags: {
            benchmark: 'hudini',
            scenario: `hudini-${profile}-chat`,
            fixture: __ENV.K6_AI_FIXTURE || 'short',
          },
        },
      },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)', 'count'],
  thresholds: {
    http_req_failed: ['rate<0.01'],
    checks: ['rate>0.99'],
    ai_invalid_json_rate: ['rate<0.01'],
    ai_invalid_stream_rate: ['rate<0.01'],
    ai_stream_completion_rate: ['rate>0.99'],
    ai_non_200_rate: ['rate<0.01'],
  },
}

function buildRequestBody() {
  const prompt = buildTokenText(fixture.prompt_tokens)
  const model = __ENV.K6_AI_MODEL || (profile === 'anthropic' ? 'anthropic/claude-haiku-4-5-20251001' : 'openai/mock-gpt-4o-mini')

  return {
    model,
    max_tokens: fixture.completion_tokens,
    stream: streamEnabled,
    stream_options: profile === 'openai' && streamEnabled ? { include_usage: true } : undefined,
    messages: [{ role: 'user', content: prompt }],
  }
}

function buildHeaders() {
  const headers = {
    'Content-Type': 'application/json',
  }

  const authToken = __ENV.K6_AI_APIKEY || __ENV.K6_AI_AUTH_TOKEN
  if (authToken) {
    headers['Authorization'] = `Bearer ${authToken}`
  }

  if (profile === 'anthropic') {
    headers['anthropic-version'] = __ENV.K6_AI_ANTHROPIC_VERSION || '2023-06-01'
    return headers
  }

  // Keep mock-control headers only for OpenAI profile compatibility.
  headers['x-llm-prompt-tokens'] = String(fixture.prompt_tokens)
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

function extractUsageFromJson(parsed) {
  const usage = parsed?.usage
  if (!usage || typeof usage !== 'object') {
    return null
  }

  if (typeof usage.prompt_tokens === 'number' || typeof usage.completion_tokens === 'number') {
    return {
      promptTokens: usage.prompt_tokens || 0,
      completionTokens: usage.completion_tokens || 0,
      totalTokens: usage.total_tokens || (usage.prompt_tokens || 0) + (usage.completion_tokens || 0),
    }
  }

  if (typeof usage.input_tokens === 'number' || typeof usage.output_tokens === 'number') {
    return {
      promptTokens: usage.input_tokens || 0,
      completionTokens: usage.output_tokens || 0,
      totalTokens: usage.total_tokens || (usage.input_tokens || 0) + (usage.output_tokens || 0),
    }
  }

  return null
}

function extractTextFromJson(parsed) {
  const openAiText = parsed?.choices?.[0]?.message?.content
  if (typeof openAiText === 'string') {
    return openAiText
  }

  if (Array.isArray(openAiText)) {
    return openAiText
      .map(entry => (entry && typeof entry.text === 'string' ? entry.text : ''))
      .join(' ')
  }

  const anthropicContent = parsed?.content
  if (Array.isArray(anthropicContent)) {
    return anthropicContent
      .map(entry => {
        if (!entry || typeof entry !== 'object') {
          return ''
        }

        if (typeof entry.text === 'string') {
          return entry.text
        }

        if (typeof entry.content === 'string') {
          return entry.content
        }

        return ''
      })
      .join(' ')
  }

  if (typeof parsed?.content === 'string') {
    return parsed.content
  }

  return ''
}

function parseAnthropicSseUsage(body) {
  let usage = null
  let textCount = 0

  for (const line of String(body || '').split(/\r?\n/)) {
    if (!line.startsWith('data: ')) {
      continue
    }

    const data = line.slice(6)
    if (data === '[DONE]') {
      continue
    }

    try {
      const event = JSON.parse(data)
      const text =
        event?.delta?.text ??
        event?.content_block_delta?.delta?.text ??
        event?.content_block_delta?.text ??
        event?.text

      if (typeof text === 'string') {
        textCount += countWords(text)
      }

      if (event?.usage) {
        usage = event.usage
      }
    } catch (_error) {
      return { usage: null, contentTokens: 0, valid: false }
    }
  }

  return { usage, contentTokens: textCount, valid: usage !== null || textCount > 0 }
}

function normalizeUsage(usage) {
  if (!usage) {
    return null
  }

  if (typeof usage.prompt_tokens === 'number' || typeof usage.completion_tokens === 'number') {
    return {
      promptTokens: usage.prompt_tokens || 0,
      completionTokens: usage.completion_tokens || 0,
      totalTokens: usage.total_tokens || (usage.prompt_tokens || 0) + (usage.completion_tokens || 0),
    }
  }

  if (typeof usage.input_tokens === 'number' || typeof usage.output_tokens === 'number') {
    return {
      promptTokens: usage.input_tokens || 0,
      completionTokens: usage.output_tokens || 0,
      totalTokens: usage.total_tokens || (usage.input_tokens || 0) + (usage.output_tokens || 0),
    }
  }

  return null
}

export default function () {
  const response = http.post(
    __ENV.K6_AI_CHAT_URL || defaultUrl,
    JSON.stringify(buildRequestBody()),
    {
      timeout: __ENV.K6_AI_TIMEOUT || (streamEnabled ? '120s' : '60s'),
      headers: buildHeaders(),
      tags: {
        gateway: 'hudini',
        profile,
        mode: streamEnabled ? 'streaming' : 'non-streaming',
      },
    },
  )

  non200Rate.add(response.status !== 200)

  if (streamEnabled) {
    ttftTrend.add(response.timings.waiting)

    const openAiParsed = parseOpenAiSseUsage(response.body)
    const anthropicParsed = parseAnthropicSseUsage(response.body)
    const parsed = openAiParsed.valid ? openAiParsed : anthropicParsed
    const usage = normalizeUsage(parsed.usage)
    const validStream = response.status === 200 && parsed.valid && usage !== null

    invalidStreamRate.add(!validStream)
    streamCompletionRate.add(validStream)

    if (usage) {
      inputTokensTotal.add(usage.promptTokens)
      outputTokensTotal.add(usage.completionTokens)
      totalTokensTotal.add(usage.totalTokens)
    }

    check(response, {
      'status is 200': r => r.status === 200,
      'stream decoded': () => parsed.valid,
      'stream usage present': () => usage !== null,
    })

    return
  }

  const parsed = parseJsonBody(response)
  invalidJsonRate.add(parsed === null)

  const usage = extractUsageFromJson(parsed)
  const responseText = extractTextFromJson(parsed)
  const responseTokenCount = countWords(responseText)

  if (usage) {
    inputTokensTotal.add(usage.promptTokens)
    outputTokensTotal.add(usage.completionTokens)
    totalTokensTotal.add(usage.totalTokens)
  }

  check(response, {
    'status is 200': r => r.status === 200,
    'response body is valid json': () => parsed !== null,
    'usage present': () => usage !== null,
    'response text present': () => responseTokenCount > 0,
  })

  invalidStreamRate.add(false)
  streamCompletionRate.add(response.status === 200 && parsed !== null && usage !== null && responseTokenCount > 0)
}

export function handleSummary(data) {
  return renderAiSummary(
    `hudini ${profile} ${streamEnabled ? 'stream' : 'chat'} (${__ENV.K6_AI_FIXTURE || 'short'})`,
    parseDurationSeconds(duration),
    data,
  )
}