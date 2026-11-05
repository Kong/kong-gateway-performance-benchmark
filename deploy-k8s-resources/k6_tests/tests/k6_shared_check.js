// Run with:  k6 run tests/k6_shared_check.js   (from deploy-k8s-resources/k6_tests)
// No network: exercises the pure helpers of k6_ai_shared.js and fails (throws)
// on the first mismatch. test_k6_shared.py runs this when k6 is installed.
import exec from 'k6/execution'
import { classifyStatus, renderAiSummary } from '../k6_ai_shared.js'

export const options = { vus: 1, iterations: 1 }

function eq(actual, expected, label) {
  if (actual !== expected) throw new Error(`${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`)
}

function metrics(values) {
  const out = {}
  for (const [name, v] of Object.entries(values)) out[name] = { values: v }
  return { metrics: out }
}

// k6 only logs an exception thrown inside a VU and still exits 0, so failures
// are turned into exec.test.abort(), which gives a non-zero exit code.
export default function () {
  try {
    runChecks()
  } catch (e) {
    exec.test.abort(`K6_SHARED_CHECK_FAILED: ${e.message}`)
  }
}

function runChecks() {
  // --- status taxonomy
  const table = [
    [200, 0, '2xx'], [204, 0, '2xx'], [301, 0, '3xx'], [404, 0, '4xx'], [429, 0, '429'],
    [500, 0, '5xx'], [503, 0, '5xx'],
    [0, 1050, 'timeout'], [0, 1211, 'timeout'], [0, 1212, 'conn_err'], [0, 1202, 'conn_err'],
    [0, undefined, 'conn_err'],
  ]
  for (const [status, code, want] of table) eq(classifyStatus(status, code), want, `classifyStatus(${status}, ${code})`)

  // --- summary with every stat present
  const full = metrics({
    http_req_duration: { 'p(95)': 614.5, 'p(99)': 615.1, avg: 613.64, med: 613.2, 'p(90)': 614.17, max: 627.65 },
    http_reqs: { count: 50, rate: 9.06 },
    iterations: { count: 50, rate: 9 },
    dropped_iterations: { count: 3, rate: 0.5 },
    http_req_failed: { rate: 0 },
    checks: { rate: 1 },
    ai_status_2xx: { count: 47 },
    ai_status_5xx: { count: 2 },
    ai_err_timeout: { count: 1 },
  })
  const text = renderAiSummary('t', 5, full).stdout
  for (const want of [
    'http_req_duration p95(ms): 614.5',
    'http_req_duration avg(ms): 613.640',
    'http_req_duration med(ms): 613.200',
    'http_req_duration p90(ms): 614.170',
    'http_req_duration max(ms): 627.650',
    'iterations total: 50',
    'dropped_iterations total: 3',
    'http status counts: 2xx=47 3xx=0 429=0 4xx=0 5xx=2 timeout=1 conn_err=0',
  ]) {
    if (!text.includes(want)) throw new Error(`summary is missing line: ${want}\n${text}`)
  }
  // the anchors aggregate_release.py relies on must be at the start of a line
  if (!/^iterations total: 50$/m.test(text)) throw new Error('iterations line not anchored')
  if (!/^dropped_iterations total: 3$/m.test(text)) throw new Error('dropped line not anchored')

  // --- stats that were not collected are omitted, not printed as 0
  const bare = metrics({ http_req_duration: { 'p(95)': 10, 'p(99)': 11 } })
  const bareText = renderAiSummary('t', 5, bare).stdout
  for (const label of ['avg(ms)', 'med(ms)', 'p90(ms)', 'max(ms)']) {
    if (bareText.includes(`http_req_duration ${label}`)) throw new Error(`unexpected ${label} line for missing stat`)
  }
  // --- no status counters recorded (e.g. a script that does not call recordStatus) -> no status line
  if (bareText.includes('http status counts')) throw new Error('status line printed without any counts')

  console.log('K6_SHARED_CHECK_OK')
}
