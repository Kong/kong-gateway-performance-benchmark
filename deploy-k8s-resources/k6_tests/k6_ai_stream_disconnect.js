import http from 'k6/http'
import { check } from 'k6'
import { Rate } from 'k6/metrics'
import {
  buildOpenAiChatRequest,
  buildTunedHeaders,
  getFixture,
  parseDurationSeconds,
  recordStatus,
  renderAiSummary,
} from './k6_ai_shared.js'

// Client-disconnect scenario: every request is a streaming chat completion that the CLIENT abandons
// part-way through, by using a client timeout (K6_AI_TIMEOUT, default 300ms) that is shorter than the
// stream (~1.3s for the "short" fixture). k6 closes the connection when the timeout fires.
//
// What it measures is on the UPSTREAM side, not in this script: the release runner resets the fake
// provider's /__stats before the run and records them after it.
//   aborted ~ started    the gateway propagated the cancel and closed the upstream connection
//   completed ~ started  the gateway let the upstream stream run to the end for a client that was gone
// (a stream that is never cancelled keeps a connection, a worker slot and upstream capacity busy).
//
// A timeout is the EXPECTED outcome here, so status 0 is registered as an expected status and does not
// count towards http_req_failed. Any other failure (connection refused, 5xx...) still does.
http.setResponseCallback(http.expectedStatuses(0, { min: 200, max: 399 }))

const fixture = getFixture('stream', __ENV.K6_AI_FIXTURE || 'short')
const duration = __ENV.K6_AI_DURATION || '6m'
const disconnectAfter = __ENV.K6_AI_TIMEOUT || '300ms'

const hungUpAsIntended = new Rate('ai_disconnect_as_intended_rate')
const finishedFirst = new Rate('ai_stream_finished_before_disconnect_rate')

export const options = {
  scenarios: {
    stream_disconnect: {
      executor: 'constant-vus',
      vus: Number(__ENV.K6_AI_STREAM_VUS || 25),
      duration,
      tags: {
        benchmark: 'aigw-v2',
        scenario: 'stream-openai-disconnect',
        fixture: __ENV.K6_AI_FIXTURE || 'short',
      },
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)', 'count'],
  thresholds: {
    http_req_failed: ['rate<0.01'],
    checks: ['rate>0.99'],
    ai_disconnect_as_intended_rate: ['rate>0.99'],
  },
}

export default function () {
  const response = http.post(
    __ENV.K6_AI_CHAT_URL || 'https://kong-kong-proxy.kong.svc.cluster.local/bench/token/stream/openai',
    JSON.stringify(buildOpenAiChatRequest(fixture, true)),
    {
      timeout: disconnectAfter,
      headers: buildTunedHeaders(fixture),
      tags: {
        route: 'bench-token-stream-openai',
        mode: 'streaming-disconnect',
      },
    },
  )

  recordStatus(response)

  // k6 error 1050 is the request timeout: the client gave up before the stream finished.
  const hungUp = response.status === 0 && response.error_code === 1050
  hungUpAsIntended.add(hungUp)
  // A completed stream means the timeout was longer than the stream: the scenario measured nothing.
  finishedFirst.add(response.status === 200)

  check(response, {
    'client hung up mid-stream': () => hungUp,
  })
}

export function handleSummary(data) {
  const pct = name => (data.metrics?.[name]?.values?.rate ?? 0) * 100
  return renderAiSummary(
    `stream openai disconnect (${__ENV.K6_AI_FIXTURE || 'short'}, hang up after ${disconnectAfter})`,
    parseDurationSeconds(duration),
    data,
    [
      `disconnect as intended(%): ${pct('ai_disconnect_as_intended_rate').toFixed(2)}`,
      `stream finished before disconnect(%): ${pct('ai_stream_finished_before_disconnect_rate').toFixed(2)}`,
    ],
  )
}
