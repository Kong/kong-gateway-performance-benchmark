import http from 'k6/http'
import { check } from 'k6'
import {
  buildOpenAiChatRequest,
  buildTunedHeaders,
  parseDurationSeconds,
  recordStatus,
  renderAiSummary,
} from './k6_ai_shared.js'

// Harness calibration: how much can the load generator + the upstream sustain with the GATEWAY OUT OF THE
// WAY? Requests go straight to the fake provider, which answers immediately (ttft 0, tpot 0, a tiny
// response). The script does as little as possible per request (status check only, response bodies are
// discarded) so that the load generator is not the first thing to run out of CPU.
// harness_calibration.py runs it at increasing rates and reports the last rate that still held.
// A gateway breakpoint result is only meaningful below that ceiling.
const fixture = { prompt_tokens: 16, completion_tokens: 8, ttft_ms: 0, tpot_ms: 0 }
const duration = __ENV.K6_AI_DURATION || '30s'

export const options = {
  discardResponseBodies: true,
  scenarios: {
    calibration: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.K6_AI_RATE || 200),
      timeUnit: __ENV.K6_AI_TIME_UNIT || '1s',
      duration,
      preAllocatedVUs: Number(__ENV.K6_AI_PRE_ALLOCATED_VUS || 200),
      maxVUs: Number(__ENV.K6_AI_MAX_VUS || 2000),
      tags: { benchmark: 'aigw-v2', scenario: 'direct-calibration', fixture: 'zero' },
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)', 'count'],
}

export default function () {
  const response = http.post(
    __ENV.K6_AI_CHAT_URL || 'http://fake-provider.upstream.svc.cluster.local:8080/v1/chat/completions',
    JSON.stringify(buildOpenAiChatRequest(fixture, false)),
    {
      timeout: __ENV.K6_AI_TIMEOUT || '10s',
      headers: buildTunedHeaders(fixture),
      tags: { route: 'direct-calibration', mode: 'non-streaming' },
    },
  )
  recordStatus(response)
  check(response, { 'status is 200': r => r.status === 200 })
}

export function handleSummary(data) {
  return renderAiSummary('direct calibration (zero delay)', parseDurationSeconds(duration), data)
}
