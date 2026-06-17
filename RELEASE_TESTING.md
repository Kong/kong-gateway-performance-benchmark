# Release Performance Testing — Methodology & Regression Gating

How we produce per-release perf evidence and catch regressions version-over-version.
Run this every release (weekly/monthly); escalate to dev when a release regresses
beyond tolerance.

## Why a baseline matters
Every "is it worse?" judgment is relative to a reference. With only **v1** today, the
job is to (1) lock a repeatable methodology, (2) capture v1 as the **official baseline**,
and (3) prove the comparison harness — so future releases are a turnkey diff.

## The three gates
1. **SLO gate (absolute floor)** — `benchmark_config.yaml`. e.g. p95 < 300 ms regardless. Fails a release on its own.
2. **Regression gate (relative)** — `release_gates.yaml`. e.g. p95 must not be >15% worse than baseline. This is the new layer.
3. **Stability gate** — ≥3 repeats, per-metric CV < 15%. A noisy run is `UNSTABLE` → re-run, don't trust/escalate.

Tolerances (defaults, tune with dev): p95 +15%, p99 +20%, TTFT +15%, throughput −10%,
error-rate +1.0pt. Latency regressions below a 20 ms absolute floor are ignored as noise.

## Standard run profile
Fixed so every release is apples-to-apples. Defined in `run_release_baseline.sh`:

| Scenario | Load |
|---|---|
| static-chat, token-chat-openai, embeddings-openai | 50 RPS |
| stream-openai, stream-gemini | 30 VUs |
| routing-roundrobin-2/-10, ewma, failover, payload-logging | 25 RPS |

`REPEATS=3`, `DURATION=3m` (raise to 5–6 m for the official record once dev signs off on
the load profile). Same cluster, isolated node groups (loadgen/kong/support).

## Capturing a release
```bash
cd deploy-k8s-resources/k6_tests
VERSION=ai-2.0.0-rc.2 ./run_release_baseline.sh
```
Produces `results/releases/<version>_<date>/`:
- `*__run<N>.log` — raw k6 summary per repeat (evidence)
- `metrics.json` — aggregated means + per-metric CV
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
Exit code: `0` = PASS/WARN, `1` = REGRESSION or UNSTABLE → fail the release pipeline.

`report.md` leads with a per-scenario verdict table (✅ OK / ⚠️ WARN / ❌ REGRESSION /
🌀 UNSTABLE) and an escalation list naming the offending scenario+metric.

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
- `release_gates.yaml` — relative tolerances + stability gate
- `benchmark_config.yaml` — absolute SLO floors (existing)
- `results/releases/` — versioned evidence store; `baseline.json` is the reference pointer
