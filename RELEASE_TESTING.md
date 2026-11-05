# Release Performance Testing — Methodology & Regression Gating

How we produce per-release perf evidence and catch regressions version-over-version.
Run this every release (weekly/monthly); escalate to dev when a release regresses
beyond tolerance.

## Why a baseline matters
Every "is it worse?" judgment is relative to a reference. With only **v1** today, the
job is to (1) lock a repeatable methodology, (2) capture v1 as the **official baseline**,
and (3) prove the comparison harness — so future releases are a turnkey diff.

## The gates
1. **SLO gate (absolute)** — `benchmark_config.yaml`. Error rate, checks pass rate, dropped iterations (worst
   repeat, ≤ 1%), client-disconnect propagation (≥ 95% of hang-ups reach the upstream) and the **gateway-overhead budgets** (`overhead_slo`): what the gateway adds over the direct
   (gateway-bypass) path of the same run — p95 ≤ 5 ms, p99 ≤ 10 ms, streaming TTFT ≤ 20 ms. Fails a release on
   its own (`ABSOLUTE_SLO_FAIL`). The old absolute latency thresholds measured the fake provider's simulated
   delay (~614 ms for token chat), not the gateway, so they are off unless `legacy_absolute_latency_slo: true`.
2. **Regression gate (relative)** — `release_gates.yaml`. Per scenario, p95/p99/TTFT/throughput/error rate vs the
   baseline, plus the **overhead vs the baseline's overhead, in absolute milliseconds** (p95 +1 ms WARN / +3 ms
   REGRESSION; p99 +2 / +6; TTFT +1 / +3). A percentage gate cannot see a gateway-overhead regression: +4 ms on a
   614 ms path is 0.6%. When both runs carry it, **Kong CPU seconds per 1k requests** is gated too (+10% WARN /
   +25% REGRESSION): at low load latency barely moves, but the CPU burned per request is a sensitive efficiency signal.
   It is the Kong container's cumulative CPU (cgroup, read with `kubectl exec`; `cpu_sample.py`) over the measurement
   window divided by the iterations k6 completed — idle work and the k6 start-up are inside the window, so only the
   relative change between releases is meaningful.
3. **Stability gate** — ≥3 repeats, per-metric CV (sample standard deviation) < 15%. A noisy run is `UNSTABLE` →
   re-run, don't trust/escalate.
4. **Confirmation** — the release runner re-runs just the flagged scenarios (plus their direct controls, because the
   overhead is a paired difference) once into `<outdir>-confirm` and compares again with
   `compare_release.py --confirm`: a REGRESSION, UNSTABLE or overhead-budget finding only counts when the same
   scenario+metric also flags in the second run. Errors, checks, dropped iterations and disconnect propagation are
   never washed away by a lucky re-run. The first-pass report is kept as `report.first-pass.md`.
   `CONFIRM_RERUN=false` turns it off; when more than `CONFIRM_MAX_SCENARIOS` (8) scenarios are flagged it is skipped,
   because that many findings usually mean an environment problem — re-run the whole release instead.

Statistics: the relative gate uses the **median** across repeats (one cold or noisy run cannot trip it) when both
runs have per-run statistics, otherwise the mean; the error rate always uses the mean so one bad repeat cannot hide.
An older baseline is re-aggregated from its per-run logs when they sit next to its `metrics.json`.

Tolerances (defaults, tune with dev): p95 +15%, p99 +20%, TTFT +15%, throughput −10%,
error-rate +1.0pt. Latency regressions below a 20 ms absolute floor are ignored as noise.
The overhead budgets and tolerances are provisional until an A/A calibration (the same release twice on the same
topology) measures the real noise floor; so far the paired p95 overhead has a run-to-run sd of 0.07–0.33 ms.

## Test cluster topology
- **One availability zone.** All managed node groups run in a single AZ (`pin_nodes_to_single_az`, default true;
  `node_az_index` picks which, 0 = the NAT gateway's AZ). The subnets span three AZs and each node group's node used
  to land in a random one, so a cluster could have the load generator, Kong and the upstream in different AZs:
  ~0.5–1 ms of cross-AZ round trip, the same size as the ~1 ms gateway overhead being measured, and different from
  cluster to cluster. The EKS control plane keeps all subnets (it needs at least two AZs). `terraform output
  node_availability_zone` shows the zone.
- **Kong sizing.** CPU/memory requests of 12 CPU / 8 GiB and no limits (limits would add CFS throttling jitter), and
  `kong_worker_processes` defaults to 14 = vCPU − 2 on the 16-vCPU node, which also runs the kubelet and DaemonSets.
  All provisional until a calibration run shows the real usage.
- **Baselines.** Results recorded before this change come from clusters with a random AZ layout and 16 workers. Compare
  a release against a baseline from the same topology; `env.json` records the zones and the report warns when the
  layout differs from the baseline's.

## Standard run profile
Fixed so every release is apples-to-apples. Defined in `release_matrix.yaml` (read by `run_release_baseline.sh`):

| Scenario | Load |
|---|---|
| static-chat, token-chat-openai, embeddings-openai | 50 RPS |
| stream-openai, stream-gemini | 30 VUs |
| routing-roundrobin-2/-10, ewma, failover, payload-logging | 25 RPS |

`REPEATS=3`, `DURATION=3m` (raise to 5–6 m for the official record once dev signs off on
the load profile). Same cluster, isolated node groups (loadgen/kong/support).

## Run matrix, tiers and ordering
The scenario list lives in `deploy-k8s-resources/k6_tests/release_matrix.yaml`; `release_matrix.py` plans the runs.

| Tier | Contents | When |
|---|---|---|
| `tier1` | cheap subset: the gateway/direct overhead pairs | every build |
| `tier2` (default) | every enabled entry | every release |

Entries marked `enabled: false` are implemented but stay out of the default plan until they have run cleanly on a
cluster. `INCLUDE_PENDING=true` plans them for that smoke run — `TIER=tier1 INCLUDE_PENDING=true REPEATS=1
./run_release_baseline.sh` — and then they are switched to `enabled: true` in the matrix.

Scenarios added with the HTTP-entry / churn / disconnect work (they ran cleanly on the cluster on 2026-11-05 and are
part of the default plan; a future new entry starts disabled and goes through the same smoke run):
- `token-chat-openai-http`, `embeddings-openai-http` — the same routes entered over plain HTTP. HTTPS − HTTP is the
  TLS cost of the client hop; HTTP − direct is the pure gateway overhead.
- `direct-stream-gemini` — the direct control for `stream-gemini`.
- `token-chat-openai-churn`, `token-chat-openai-http-churn` — a new TCP (+TLS) connection per request (k6
  `noConnectionReuse`); churn(HTTPS) − churn(HTTP) is the TLS handshake cost.
- `stream-openai-disconnect` — the client hangs up mid-stream (`K6_AI_DISCONNECT_AFTER`, default 300 ms). The runner
  resets the fake provider's `/__stats` before the run and records them after it (`upstream stats:` in the per-run
  log). The gateway should cancel the upstream: at least 95% of the hang-ups must show up as aborted upstream
  responses (`slo.min_disconnect_propagation_percent`). If the stats cannot be read the report says the check was
  not performed instead of passing silently.

- `ORDER=interleaved` (default): every round runs every scenario once, then the next round. Gateway/direct pairs run
  back-to-back and swap order each round, so slow environment drift cancels out of the pair difference, and an
  interrupted run leaves every scenario with the same number of repeats. `ORDER=shuffle` (seeded by `SEED`) shuffles
  the group order; `ORDER=sequential` is the legacy order.
- `DRY_RUN=true` prints the plan and a time estimate (each run ≈ `DURATION` + ~1 min) without touching the cluster.
- The executed order is saved as `plan.tsv` in the results directory.
- No warm-up is discarded: at 50 RPS the first repeat differs from the later ones by < 0.5 ms, so every repeat
  counts. A k6 warm-up is planned for high-load tests only.

```bash
cd deploy-k8s-resources/k6_tests
TIER=tier1 REPEATS=3 DRY_RUN=true VERSION=ai-x ./run_release_baseline.sh   # preview the plan
python3 -m unittest discover -s tests                                       # planner / aggregator / gate tests
```

## Capturing a release
```bash
cd deploy-k8s-resources/k6_tests
VERSION=ai-2.0.0-rc.2 ./run_release_baseline.sh
```
Produces `results/releases/<version>_<date>/`:
- `*__run<N>.log` — raw k6 summary per repeat (evidence): latency avg/med/p90/p95/p99/max, rate, errors,
  dropped iterations, status / error counts, and for gateway runs the `kong cpu:` line (seconds, iterations,
  seconds per 1k requests)
- `plan.tsv` — the order in which the runs were executed
- `env.json` — the test environment, recorded at the start and end of the run (`collect_env.py`): nodes (role, instance
  type, zone, kernel, OS), Kong image digest and version, k6 runner version, hashes of the Kong declarative config and
  of the fake provider (script + env), suite git sha. Collection is best effort and never fails a run; anything it
  could not read is listed in `collection_errors`, and fields that changed during the run in `changed_during_run`.
- `metrics.json` (schema 2) — means + CV (sample stdev) per gate metric, plus median/IQR/stdev/min/max,
  `dropped_pct`, `status_totals` and the raw `per_run` values
- `summary.csv` — flat table for spreadsheets/sign-off
- `report.md` — human-readable evidence report
- `verdict.json` — machine verdict (for CI)

The **first** run with no baseline writes `results/releases/baseline.json` pointing at
itself. Subsequent runs auto-diff against the baseline.

## Comparing / gating
`run_release_baseline.sh` auto-compares when a baseline exists. To diff manually:
```bash
python3 compare_release.py \
  --current  results/releases/ai-2.0.0-rc.3_2026-06-23 \
  --baseline results/releases/ai-2.0.0-rc.2_2026-06-16 \
  --out report.md --json verdict.json
```
Exit code: `0` = PASS/WARN, `1` = REGRESSION, UNSTABLE or ABSOLUTE_SLO_FAIL → fail the release pipeline.

`report.md` leads with a per-scenario verdict table (✅ OK / ⚠️ WARN / ❌ REGRESSION /
🌀 UNSTABLE) and an escalation list naming the offending scenario+metric.

## Reading the gateway-overhead table
`report.md` and the PDF list, per gateway scenario, `overhead = gateway − direct` as the **median of the per-round
differences** (the gateway and direct runs of one round run back-to-back), with a bootstrap 95% CI. Scenarios without
their own direct control (routing, payload logging) are compared with the same-fixture `direct-token-chat-openai` and
listed separately.

Caveat: the gateway path is HTTPS (TLS terminated at Kong, certificate verification skipped) while the direct path is
plain HTTP, so the overhead includes the TLS cost of the client hop until the HTTP-entry variants are measured.

When an `env.json` sits in both results directories it is compared: drift in instance types, k6 version, config hashes
or in whether each node shares Kong's availability zone is reported, but does not change the verdict.

## Comparing two configurations (A/B) and the noise floor (A/A)
Running config A for an hour and then config B mixes the effect with everything that drifts between the blocks. Run
them in interleaved rounds instead (round 1: A then B, round 2: B then A, ...; one `run_release_baseline.sh`
invocation per config and round with `REPEATS=1`, directories named `<prefix>-<config>-r<N>`) and compare per round:
`paired_ab.py --prefix <prefix> --a <config> --b <config> --scenarios ... --metric cpu --first-odd a` prints the mean
paired difference, its bootstrap 95% CI, in how many rounds B > A (exact sign test; with 6 rounds the smallest
possible p is 0.031) and the mean difference when A vs B ran first, which exposes an order effect. Use the same tool
with the same config twice (A/A) to measure the noise floor the gates' thresholds have to sit above.

## Harness calibration (before reading any capacity number)
A gateway breakpoint ("max sustainable RPS") only means something if the harness can push more than the gateway can
take. The fake provider is a single-threaded Node process on a shared node and k6 parses responses, so either may run
out first. `harness_calibration.py` finds that ceiling with the gateway out of the way:

```bash
cd deploy-k8s-resources/k6_tests
python3 harness_calibration.py                                   # steps 200 ... 8000 rps, 30 s each
python3 harness_calibration.py --steps 500,2000,5000 --step-duration 60s --no-stop
```
- Each step is a `direct-calibration` TestRun (k6 → fake provider, zero delay, status-only checks, bodies discarded)
  at a higher rate. A step **passes** when k6 achieved ≥ 97% of the target (completed iterations ÷ step duration;
  k6's own `http_reqs/s` divides by the wall clock including start-up and would fail a healthy harness), dropped
  ≤ 1%, errored ≤ 1% **and p95 stayed ≤ 10 ms**: the upstream answers at once, so a larger p95 means the harness is
  queueing, and a gateway measured through it would be charged for that.
- At the failing step it names who ran out first by comparing how busy each side is relative to its capacity (the
  fake provider's CPU from its cgroup vs ~1 core, k6's CPU from `kubectl top` vs its 60-core limit): `upstream`,
  `load generator`, or `undetermined` when both are below half.
- Output in `results/calibration/<timestamp>/`: `calibration.md`, `calibration.json`, per-step logs. Treat gateway
  capacity numbers as valid only below ~80% of `harness_ceiling_rps`.
- Locally (10 cores, k6 + the Node fake provider, cluster side stubbed) it held 8000 rps; at 12000 rps p95 went from
  ~3 ms to ~30 ms with the fake provider at 0.8 of a core and k6 at 1 of 60, so it reported `upstream`. That is an
  illustration of the method, not a cluster number: run it on the cluster before sizing anything.

## Diagnosing streaming TTFT
k6's TTFT is time-to-first-byte; it cannot tell whether the gateway delays the first bytes, holds back the first few
chunks and releases them in a burst, or slows everything. `diagnose_stream_ttft.py` sends real streaming requests to
the gateway route and to the direct upstream route, timestamps every SSE event, and compares: first event, gap between
events and the size of the burst at the start of the stream (an even stream has a burst of 1). Its verdict is a
heuristic (`BUFFERS_STREAM_START`, `DELAYS_FIRST_BYTES_ONLY`, `ADDS_LATENCY`, `NO_TTFT_OVERHEAD`); the numbers are the
evidence. Run it through `kubectl port-forward` (see the header of the script); absolute times include the
port-forward hop, the first-event delta and the burst size are what matter. It is the way to find out why
`stream-openai` shows a TTFT of ~524 ms while `stream-gemini` shows ~123 ms with the same upstream settings.

**Result on the cluster (2026-11-05, run from a pod, 9 requests per target).** With the routes as committed — Kong's
default `response_buffering: true` — the gateway does not stream: over HTTPS all 18 events arrive in one burst at
~1325 ms (the length of the whole stream) while the direct upstream delivers the first one at 121 ms; over HTTP they
arrive in groups of ~4 (first event at ~363 ms). The TTFT k6 reports is the arrival of the response HEADERS, not of
the first token. With `response_buffering: false` on the streaming routes the gateway matches the direct path
(+1 ms OpenAI, +3 ms Gemini; events 80 ms apart, burst of 1). Running the diagnostic through `kubectl port-forward`
shows the same burst shape, but its first-event deltas are not usable: the tunnel set-up cost differs per target.
The streaming scenarios of every release up to 2026-11-05 therefore measured a buffered response.

**Decision (2026-11-05): the streaming routes carry `response_buffering: false`** (`kong-full.yaml` and
`build_declarative.py`; every other route keeps Kong's default). An A/B on the cluster (3 repeats per config)
showed the same total duration, errors, usage in the final event and client-disconnect propagation, and the k6
TTFT of `stream-openai` going from 524 ms to 123 ms (= direct). Consequences: streaming results recorded before
this change are **not comparable** with new ones (the k6 TTFT is the arrival of the headers), so rebuild the
baseline for `stream-*` before gating them; `env.json` carries a hash of the declarative config, so the report
flags the difference. CPU per request: a 6-round interleaved A/B (`paired_ab.py`) found no reliable difference
(`stream-openai` +8.8% on average but +0.33...+0.90 in rounds 1-4 and about 0 in rounds 5-6, sign test p = 0.69;
`stream-gemini` -0.8%). The paired difference of two runs minutes apart has an sd of 6-10% of the mean, so a CPU
tolerance tighter than ~15% will raise false alarms.

## When a regression fires
1. The report's escalation list names exactly what regressed (scenario, metric, Δ%, limit).
2. Confirm it's real, not noise: check the `cv_pct` — high CV → re-run before escalating.
3. File a ticket to dev (AG board) with `report.md` + `summary.csv` attached as evidence.
4. Dev investigates; once resolved (or accepted as a new normal), **move the baseline**
   by updating `results/releases/baseline.json` to the agreed known-good release.

## Moving / pinning the baseline
The baseline is a deliberate decision, not automatic. Update `baseline.json` only when a
release is blessed as the new reference (e.g. a GA, or an accepted intentional perf change):
```bash
echo '{"dir":"results/releases/<blessed>_<date>","version":"<blessed>"}' \
  > results/releases/baseline.json
```

## Files
- `run_release_baseline.sh` — capture orchestrator (matrix × repeats → aggregate → report)
- `aggregate_release.py` — parse k6 summaries → metrics.json + summary.csv
- `compare_release.py` — regression gate → report.md + verdict.json (exit 1 on regression)
- `release_matrix.yaml` / `release_matrix.py` — scenario matrix, tiers, pairing and run order
- `release_gates.yaml` — relative tolerances, overhead tolerances + stability gate
- `benchmark_config.yaml` — absolute SLOs (error rate, checks, dropped iterations) and the gateway-overhead
  budgets (`overhead_slo`)
- `tests/` — unit and golden tests: `python3 -m unittest discover -s tests`
- `results/releases/` — versioned evidence store; `baseline.json` is the reference pointer
