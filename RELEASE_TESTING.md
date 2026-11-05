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
4. **Confirmation (optional)** — `compare_release.py --confirm <second run>`: a REGRESSION/UNSTABLE only counts when
   the same scenario+metric also flags in the second run.

Statistics: the relative gate uses the **median** across repeats (one cold or noisy run cannot trip it) when both
runs have per-run statistics, otherwise the mean; the error rate always uses the mean so one bad repeat cannot hide.
An older baseline is re-aggregated from its per-run logs when they sit next to its `metrics.json`.

Tolerances (defaults, tune with dev): p95 +15%, p99 +20%, TTFT +15%, throughput −10%,
error-rate +1.0pt. Latency regressions below a 20 ms absolute floor are ignored as noise.
The overhead budgets and tolerances are provisional until an A/A calibration (the same release twice on the same
topology) measures the real noise floor; so far the paired p95 overhead has a run-to-run sd of 0.07–0.33 ms.

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

Scenarios behind that switch (all verified locally against the fake provider, not yet on a cluster):
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
