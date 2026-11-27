#!/usr/bin/env python3
"""Generate a Kong AI Gateway performance benchmark PDF report from a
release results directory (metrics.json + optional verdict.json).

Usage:
    python3 generate_perf_report.py <results_dir> <output_pdf> \
        [--cluster-name NAME] [--k8s-version X.Y] [--region us-west-2] \
        [--repeats N] [--duration 3m]

All headline numbers, tables and charts are computed from the actual
metrics/verdict data in <results_dir> — nothing here is hardcoded to a
specific past run.
"""

import argparse
import json
import os
import io
import base64
import datetime
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# ── colour palette ────────────────────────────────────────────────────────────
KONG_BLUE   = "#003459"
KONG_TEAL   = "#00B4D8"
KONG_GREEN  = "#2DC653"
KONG_ORANGE = "#FF6B35"
KONG_RED    = "#E03131"
KONG_GREY   = "#6C757D"
LIGHT_GREY  = "#F8F9FA"
MID_GREY    = "#E9ECEF"

VERDICT_COLOR = {
    "PASS": KONG_GREEN,
    "BASELINE_PASS": KONG_GREEN,
    "WARN": KONG_ORANGE,
    "REGRESSION": KONG_RED,
    "UNSTABLE": KONG_ORANGE,
    "ABSOLUTE_SLO_FAIL": KONG_RED,
    "N/A": KONG_GREY,
}


def load_json(path):
    with open(path) as f:
        return json.load(f)


def fig_to_png_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def fmt(v, unit="ms", decimals=2):
    if v is None:
        return "—"
    return f"{v:.{decimals}f} {unit}".strip()


# ── request size x response mode (AG-2333) ───────────────────────────────────
# Request-size fixtures (ai_benchmark_fixtures.json): reqNk, and mlflow50-reqNk on the mlflow50 track.
# The first row of each track is the short fixture that the older reports used (64 prompt tokens).
SIZE_TIERS = (("base", "~0.4 KB"), ("req1k", "1 KB"), ("req4k", "4 KB"), ("req10k", "10 KB"), ("req16k", "16 KB"))
SIZE_TRACKS = (
    ("core", "Core track (provider delay 100 to 120 ms)", "", "short"),
    ("mlflow50", "mlflow50 track (provider delay 50 ms)", "mlflow50-", "mlflow50"),
)
SIZE_MODES = (("buffered", "Buffered", "token-chat-openai"), ("sse", "SSE", "stream-openai"))


def size_matrix(estimates):
    """Overhead estimates arranged by request size and response mode.

    Returns None when the run has no request-size tier at all (older runs), so the report keeps its old shape.
    Otherwise a list of {track, title, rows}; each row is {tier, label, buffered, sse} and the two modes hold
    the matching estimate or None. A track without any request-size tier is left out.
    """
    index = {(e["scenario"], e["fixture"]): e for e in estimates}
    out = []
    for track, title, prefix, base in SIZE_TRACKS:
        rows = []
        for tier, label in SIZE_TIERS:
            fixture = base if tier == "base" else prefix + tier
            rows.append({"tier": tier, "label": label,
                         **{mode: index.get((scenario, fixture)) for mode, _, scenario in SIZE_MODES}})
        if any(r[mode] for r in rows if r["tier"] != "base" for mode, _, _ in SIZE_MODES):
            out.append({"track": track, "title": title, "rows": [r for r in rows if r["buffered"] or r["sse"]]})
    return out or None


# ── request profile: what each scenario actually sends ───────────────────────
# Mirrors the request builders of deploy-k8s-resources/k6_tests/k6_ai_shared.js (buildTokenText,
# buildOpenAiChatRequest, buildEmbeddingsRequest) so the report states the size of the request body.
FIXTURES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "deploy-k8s-resources", "k6_tests", "ai_benchmark_fixtures.json")
DEFAULT_CHAT_MODEL = "mock-gpt-4o-mini"
STATIC_CHAT_MODEL = "wiremock-static-chat"
EMBEDDING_MODEL = "text-embedding-3-small"
# scenario name -> (fixture group, request kind). kind: chat | chat_stream | chat_no_model | chat_static | embeddings
SCENARIO_REQUEST = {
    "token-chat-openai": ("chat", "chat"), "direct-token-chat-openai": ("chat", "chat"),
    "token-chat-openai-http": ("chat", "chat"), "token-chat-openai-churn": ("chat", "chat"),
    "token-chat-openai-http-churn": ("chat", "chat"), "payload-logging": ("chat", "chat"),
    "routing-roundrobin-2": ("chat", "chat_no_model"), "routing-roundrobin-10": ("chat", "chat_no_model"),
    "routing-ewma": ("chat", "chat_no_model"), "routing-failover": ("chat", "chat_no_model"),
    "static-chat": ("static", "chat_static"),
    "stream-openai": ("stream", "chat_stream"), "direct-stream-openai": ("stream", "chat_stream"),
    "stream-openai-disconnect": ("stream", "chat_stream"),
    # k6_ai_stream_gemini.js posts a non-streaming OpenAI-format body (buildOpenAiChatRequest(fixture, false))
    "stream-gemini": ("stream", "chat"), "direct-stream-gemini": ("stream", "chat"),
    "embeddings-openai": ("embeddings", "embeddings"), "embeddings-openai-http": ("embeddings", "embeddings"),
    "direct-embeddings-openai": ("embeddings", "embeddings"),
}


def token_text(n):
    return " ".join(f"t{i:04d}" for i in range(1, n + 1))


def request_body(kind, fx):
    """The JSON the k6 script sends, serialised like JSON.stringify (compact, undefined dropped)."""
    if kind == "embeddings":
        body = {"model": EMBEDDING_MODEL, "input": token_text(fx["prompt_tokens"])}
    else:
        body = {"model": STATIC_CHAT_MODEL if kind == "chat_static" else DEFAULT_CHAT_MODEL,
                "stream": kind == "chat_stream"}
        if kind == "chat_stream":
            body["stream_options"] = {"include_usage": True}
        body["max_tokens"] = fx["completion_tokens"]
        body["messages"] = [{"role": "user", "content": token_text(fx["prompt_tokens"])}]
        if kind == "chat_no_model":
            del body["model"]
    return json.dumps(body, separators=(",", ":"))


def request_profile(scenario_keys, fixtures_path=FIXTURES_PATH):
    """One row per result key: request body size and the response the fake provider is told to produce.

    Keys are '<scenario>' (fixture short) or '<scenario>__<fixture>'. Returns [] when the fixtures file is
    missing or a key is unknown, so the report is never blocked by this section.
    """
    try:
        fixtures = load_json(fixtures_path)
    except (OSError, ValueError):
        return []
    rows = []
    for key in sorted(scenario_keys):
        scenario, _, fixture = key.partition("__")
        fixture = fixture or "short"
        if scenario == "static-chat":
            fixture = "chat"
        group_kind = SCENARIO_REQUEST.get(scenario)
        if not group_kind:
            continue
        group, kind = group_kind
        fx = fixtures.get(group, {}).get(fixture)
        if not fx:
            continue
        body = request_body(kind, fx)
        rows.append({"key": key, "group": group, "fixture": fixture, "prompt_tokens": fx["prompt_tokens"],
                     "completion_tokens": fx.get("completion_tokens"), "body_bytes": len(body.encode()),
                     "streaming": kind == "chat_stream", "ttft_ms": fx.get("ttft_ms"),
                     "tpot_ms": fx.get("tpot_ms"), "tokens_per_chunk": fx.get("tokens_per_chunk")})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir")
    ap.add_argument("output_pdf")
    ap.add_argument("--cluster-name", default="kong-perf-pIrSMIsE")
    ap.add_argument("--k8s-version", default="1.31")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--duration", default="3m")
    ap.add_argument("--prepared-by", default="liangwen.chen@konghq.com")
    ap.add_argument("--html-out", default=None, help="also write the rendered HTML to this path")
    args = ap.parse_args()

    metrics = load_json(os.path.join(args.results_dir, "metrics.json"))
    verdict_path = os.path.join(args.results_dir, "verdict.json")
    verdict = load_json(verdict_path) if os.path.exists(verdict_path) else {}

    VERSION = metrics["version"]
    CAPTURED_AT = metrics["captured_at"]
    SCENARIOS = metrics["scenarios"]

    def s(key):
        return SCENARIOS.get(key, {})

    overhead_estimates = verdict.get("gateway_overhead_estimates", [])
    mlflow_estimates = [e for e in overhead_estimates if e.get("fixture") == "mlflow50"]

    overall = verdict.get("overall", "N/A")
    baseline_version = verdict.get("baseline")
    regressions = verdict.get("regressions", [])
    warn_list = verdict.get("warnings", [])
    unstable_list = verdict.get("unstable", [])
    slo_violations = verdict.get("absolute_slo_violations", [])
    derived_overhead = verdict.get("derived_overhead", [])
    overhead_checks = verdict.get("overhead_checks", [])
    overhead_budgets = verdict.get("overhead_budgets") or {}
    gate_statistic = verdict.get("gate_statistic") or []
    unconfirmed = verdict.get("unconfirmed") or {}
    env_drift = verdict.get("environment")
    env_path = os.path.join(args.results_dir, "env.json")
    env_record = load_json(env_path) if os.path.exists(env_path) else None

    # baseline captured_at (best effort, non-fatal if unavailable)
    baseline_captured_at = None
    baseline_scenarios = None    # None = baseline metrics could not be read
    try:
        baseline_ptr = load_json(os.path.join(os.path.dirname(args.results_dir), "baseline.json"))
        if baseline_ptr.get("version") == baseline_version:
            # baseline.json holds a path relative to k6_tests/ (results/releases/<dir>), whatever the cwd is
            k6_tests_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(args.results_dir))))
            baseline_dir = baseline_ptr["dir"]
            if not os.path.isabs(baseline_dir):
                baseline_dir = os.path.join(k6_tests_dir, baseline_dir)
            baseline_metrics = load_json(os.path.join(baseline_dir, "metrics.json"))
            baseline_captured_at = baseline_metrics.get("captured_at")
            baseline_scenarios = set(baseline_metrics.get("scenarios", {}))
    except Exception:
        pass

    # ── headline KPIs (all computed from actual data) ──────────────────────
    scenario_count = len(SCENARIOS)
    total_runs = scenario_count * args.repeats

    error_rates = [d.get("error_rate_pct", 0) or 0 for d in SCENARIOS.values()]
    max_error_rate = max(error_rates) if error_rates else 0

    cv_values = [v for d in SCENARIOS.values() for v in (d.get("cv_pct") or {}).values() if v is not None]
    max_cv = max(cv_values) if cv_values else 0

    mlflow_deltas = [e["p95_delta_ms"] for e in mlflow_estimates if e.get("p95_delta_ms") is not None]
    max_mlflow_overhead = max(mlflow_deltas) if mlflow_deltas else None
    min_mlflow_overhead = min(mlflow_deltas) if mlflow_deltas else None

    chat_rps = s("token-chat-openai").get("throughput_rps")

    ttft_deltas = [e["ttft_delta_ms"] for e in overhead_estimates if e.get("ttft_delta_ms") is not None]
    streaming_ttft_finding = (
        f"<li><strong>Streaming TTFT overhead:</strong> {fmt(min(ttft_deltas))} to {fmt(max(ttft_deltas))} "
        f"(gateway &minus; direct, paired median)</li>\n    " if ttft_deltas else ""
    )

    # ══════════════════════════════════════════════════════════════════════
    # CHARTS
    # ══════════════════════════════════════════════════════════════════════

    def bar_chart_overhead_mlflow():
        if not mlflow_estimates:
            return None
        labels = [f'{e["scenario"]}' for e in mlflow_estimates]
        deltas = [e["p95_delta_ms"] for e in mlflow_estimates]

        fig, ax = plt.subplots(figsize=(7, 4))
        fig.patch.set_facecolor("white")
        ax.set_facecolor(LIGHT_GREY)

        bars = ax.bar(labels, deltas, color=KONG_ORANGE, zorder=3, width=0.4)
        for bar, d in zip(bars, deltas):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(deltas) * 0.02,
                    f"{d:.2f} ms", ha="center", va="bottom", fontsize=10, fontweight="bold")

        ax.set_ylabel("Absolute Gateway Overhead p95 (ms)", fontsize=9)
        ax.set_title("Kong AI Gateway Pure Overhead\n(mlflow50 track: 50 ms fixed upstream)",
                      fontsize=11, fontweight="bold")
        ax.set_ylim(0, max(deltas) * 2.2 if max(deltas) > 0 else 1)
        ax.axhline(5, color=KONG_GREEN, linestyle="--", linewidth=1.2, label="5 ms reference")
        ax.legend(fontsize=8.5)
        ax.grid(axis="y", linestyle="--", alpha=0.5, zorder=0)
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        return fig_to_png_b64(fig)

    def bar_chart_overhead_all():
        if not overhead_estimates:
            return None
        labels = [f'{e["scenario"]} ({e["fixture"]})' for e in overhead_estimates]
        gw_vals = [e["p95_gateway_ms"] for e in overhead_estimates]
        dir_vals = [e["p95_direct_ms"] for e in overhead_estimates]
        delta = [e["p95_delta_ms"] for e in overhead_estimates]

        x = np.arange(len(labels))
        width = 0.35

        fig, ax = plt.subplots(figsize=(10, 5))
        fig.patch.set_facecolor("white")
        ax.set_facecolor(LIGHT_GREY)

        b2 = None
        ax.bar(x - width / 2, dir_vals, width, label="Direct Upstream (p95)", color=KONG_TEAL, zorder=3)
        b2 = ax.bar(x + width / 2, gw_vals, width, label="Via Kong Gateway (p95)", color=KONG_BLUE, zorder=3)

        for bar, d in zip(b2, delta):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(gw_vals) * 0.01,
                    f"+{d:.1f} ms", ha="center", va="bottom", fontsize=7.5,
                    color=KONG_ORANGE, fontweight="bold")

        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8.5)
        ax.set_ylabel("Latency p95 (ms)", fontsize=9)
        ax.set_title("Gateway vs Direct Upstream — p95 Latency & Overhead", fontsize=11, fontweight="bold", pad=12)
        ax.legend(fontsize=8.5)
        ax.grid(axis="y", linestyle="--", alpha=0.5, zorder=0)
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        return fig_to_png_b64(fig)

    ROUTING_CHART_KEYS = [
        ("Round-Robin (2)", "routing-roundrobin-2"),
        ("Round-Robin (10)", "routing-roundrobin-10"),
        ("EWMA", "routing-ewma"),
        ("Failover", "routing-failover"),
        ("Payload Logging", "payload-logging"),
        ("Static Chat", "static-chat"),
    ]
    THROUGHPUT_CHART_KEYS = ["static-chat", "token-chat-openai", "embeddings-openai",
                             "routing-roundrobin-2", "stream-openai", "stream-gemini"]

    def bar_chart_routing():
        keys = ROUTING_CHART_KEYS
        labels = [l for l, k in keys if s(k).get("p95_ms") is not None]
        vals = [s(k).get("p95_ms") for l, k in keys if s(k).get("p95_ms") is not None]
        if not labels:
            return None

        fig, ax = plt.subplots(figsize=(9, 4))
        fig.patch.set_facecolor("white")
        ax.set_facecolor(LIGHT_GREY)

        colors = [KONG_GREY if l == "Static Chat" else KONG_BLUE for l in labels]
        bars = ax.bar(labels, vals, color=colors, zorder=3, width=0.5)
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(vals) * 0.01,
                    f"{v:.0f} ms", ha="center", va="bottom", fontsize=9)

        ax.set_ylabel("Latency p95 (ms)", fontsize=9)
        ax.set_title("Routing & Policy Scenarios — p95 Latency", fontsize=11, fontweight="bold")
        ax.grid(axis="y", linestyle="--", alpha=0.5, zorder=0)
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        return fig_to_png_b64(fig)

    def bar_chart_ttft():
        candidates = {k: v.get("ttft_p95_ms") for k, v in SCENARIOS.items() if v.get("ttft_p95_ms") is not None}
        if not candidates:
            return None
        labels = list(candidates.keys())
        vals = list(candidates.values())

        fig, ax = plt.subplots(figsize=(8, 4))
        fig.patch.set_facecolor("white")
        ax.set_facecolor(LIGHT_GREY)

        colors = [KONG_TEAL if "direct" in k else KONG_BLUE for k in labels]
        bars = ax.bar(labels, vals, color=colors, zorder=3, width=0.45)
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(vals) * 0.01,
                    f"{v:.1f} ms", ha="center", va="bottom", fontsize=9)

        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=8)
        ax.set_ylabel("TTFT p95 (ms)", fontsize=9)
        ax.set_title("Streaming — Time-To-First-Token (TTFT) p95", fontsize=11, fontweight="bold")
        ax.grid(axis="y", linestyle="--", alpha=0.5, zorder=0)
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        return fig_to_png_b64(fig)

    def bar_chart_throughput():
        keys = THROUGHPUT_CHART_KEYS
        labels = [k for k in keys if s(k).get("throughput_rps") is not None]
        vals = [s(k).get("throughput_rps") for k in labels]
        if not labels:
            return None

        fig, ax = plt.subplots(figsize=(9, 4))
        fig.patch.set_facecolor("white")
        ax.set_facecolor(LIGHT_GREY)

        bars = ax.bar(labels, vals, color=KONG_TEAL, zorder=3, width=0.5)
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(vals) * 0.01,
                    f"{v:.1f}", ha="center", va="bottom", fontsize=9)

        ax.set_ylabel("Throughput (RPS)", fontsize=9)
        ax.set_title("Throughput by Scenario (RPS)", fontsize=11, fontweight="bold")
        ax.grid(axis="y", linestyle="--", alpha=0.5, zorder=0)
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        return fig_to_png_b64(fig)

    size_data = size_matrix(overhead_estimates)

    def line_chart_overhead_by_size():
        if not size_data:
            return None
        fig, axes = plt.subplots(1, len(size_data), figsize=(6.2 * len(size_data), 4), squeeze=False)
        fig.patch.set_facecolor("white")
        budget = overhead_budgets.get("p99_ms")
        for ax, block in zip(axes[0], size_data):
            ax.set_facecolor(LIGHT_GREY)
            labels = [r["label"] for r in block["rows"]]
            xs = list(range(len(labels)))
            for mode, name, _ in SIZE_MODES:
                color = KONG_BLUE if mode == "buffered" else KONG_ORANGE
                pts = [(x, r[mode]["p99_delta_ms"]) for x, r in zip(xs, block["rows"])
                       if r[mode] and r[mode].get("p99_delta_ms") is not None]
                if not pts:
                    continue
                ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o", color=color, label=name, zorder=3)
                dy = -14 if mode == "buffered" else 7   # buffered labels below its line, SSE above, so they do not overlap
                for x, y in pts:
                    ax.annotate(f"{y:.2f}", (x, y), textcoords="offset points", xytext=(0, dy), ha="center",
                                fontsize=8, color=color)
            if budget is not None:
                ax.axhline(budget, color=KONG_GREEN, linestyle="--", linewidth=1.2, label=f"{budget:g} ms budget")
            ax.axhline(0, color=KONG_GREY, linewidth=0.8)
            ax.set_xticks(xs)
            ax.set_xticklabels(labels, fontsize=8.5)
            ax.set_xlabel("Request body size", fontsize=9)
            ax.set_ylabel("Gateway overhead p99 (ms)", fontsize=9)
            ax.set_title(block["title"], fontsize=10, fontweight="bold")
            ax.legend(fontsize=8)
            ax.grid(axis="y", linestyle="--", alpha=0.5, zorder=0)
            ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        return fig_to_png_b64(fig)

    img_overhead_by_size = line_chart_overhead_by_size()
    img_overhead_mlflow = bar_chart_overhead_mlflow()
    img_overhead_all = bar_chart_overhead_all()
    img_routing = bar_chart_routing()
    img_ttft = bar_chart_ttft()
    img_throughput = bar_chart_throughput()

    # A chart reads fixed scenarios. When none of them is part of the run (a partial run such as the request-size
    # tiers) the chart does not apply: say so plainly. When the scenarios ran but the data is missing it is a
    # fault, and the warning stays.
    has_mlflow_fixture = any(k.partition("__")[2] == "mlflow50" for k in SCENARIOS)
    has_routing_scenarios = any(k in SCENARIOS for _, k in ROUTING_CHART_KEYS)
    has_throughput_scenarios = any(k in SCENARIOS for k in THROUGHPUT_CHART_KEYS)

    def fig_block(img, alt, caption, applicable=True, not_run=""):
        if img is None and not applicable:
            return f'<div class="callout info">Not part of this run: {not_run}</div>'
        if img is None:
            return f'<div class="callout warn">No data available for: {alt}</div>'
        return f"""
        <div class="fig">
          <img src="data:image/png;base64,{img}" alt="{alt}"/>
          <div class="fig-cap">{caption}</div>
        </div>"""

    # ══════════════════════════════════════════════════════════════════════
    # TABLE BUILDERS
    # ══════════════════════════════════════════════════════════════════════

    def overhead_vs_baseline(key):
        """Worst overhead check vs the baseline for one gateway scenario key."""
        checks = [c for c in overhead_checks if c["scenario"] == key]
        if not checks:
            return "—"
        order = ["OK", "WARN", "REGRESSION"]
        worst = max(checks, key=lambda c: (order.index(c["verdict"]), abs(c["delta"])))
        color = {"OK": KONG_GREEN, "WARN": KONG_ORANGE, "REGRESSION": KONG_RED}[worst["verdict"]]
        label = {"overhead_p95_ms": "p95", "overhead_p99_ms": "p99", "overhead_ttft_ms": "TTFT"}[worst["metric"]]
        return f'<span style="color:{color};font-weight:700">{worst["verdict"]}</span> {label} {worst["delta"]:+.2f} ms'

    def overhead_rows_html(rows, empty_msg, show_reference=False):
        budget = overhead_budgets.get("p95_ms", 5)
        out = ""
        for e in rows:
            delta = e["p95_delta_ms"]
            color = KONG_GREEN if delta < budget else KONG_ORANGE
            ci = e.get("p95_ci95")
            ci_txt = f"[{ci[0]:.2f}, {ci[1]:.2f}]" if ci else "—"
            p99 = e.get("p99_delta_ms")
            ttft = e.get("ttft_delta_ms")
            label = f'{e["scenario"]} ({e["fixture"]})'
            if show_reference:
                label += f'<br><small>vs {e["direct_scenario_key"]}</small>'
            out += f"""
            <tr>
              <td>{label}</td>
              <td class="num">{fmt(e["p95_direct_ms"], decimals=3)}</td>
              <td class="num">{fmt(e["p95_gateway_ms"], decimals=3)}</td>
              <td class="num" style="color:{color};font-weight:700">+{delta:.3f} ms</td>
              <td class="num">{ci_txt}</td>
              <td class="num">{"—" if p99 is None else f"+{p99:.3f} ms"}</td>
              <td class="num">{"—" if ttft is None else f"+{ttft:.3f} ms"}</td>
              <td class="num">{overhead_vs_baseline(e["gateway_scenario_key"])}</td>
            </tr>"""
        return out or f'<tr><td colspan="8">{empty_msg}</td></tr>'

    def overhead_table_rows():
        return overhead_rows_html(overhead_estimates, "No paired direct-upstream scenarios found.")

    def derived_overhead_rows():
        return overhead_rows_html(derived_overhead, "No reference-control scenarios found.", show_reference=True)

    def request_profile_html():
        rows = request_profile(SCENARIOS.keys())
        if not rows:
            return ""
        body = ""
        for r in rows:
            comp = "&mdash;" if r["completion_tokens"] is None else r["completion_tokens"]
            delay = "&mdash;" if r["ttft_ms"] is None else (
                f'{r["ttft_ms"]} ms' + ("" if r["tpot_ms"] is None else f' + {r["tpot_ms"]} ms/tok'))
            body += f"""
      <tr><td>{r["key"]}</td><td>{r["fixture"]}</td><td class="num">{r["prompt_tokens"]}</td>
          <td class="num">{comp}</td><td class="num">{r["body_bytes"]:,} B ({r["body_bytes"] / 1024:.2f} KB)</td>
          <td>{"SSE" if r["streaming"] else "buffered"}</td><td>{delay}</td></tr>"""
        return f"""
  <h2 class="sub">2.3.1 Request Profile</h2>
  <p>Size of the JSON request body each scenario sends (counted in bytes, compact JSON as k6 serialises it) and what
  the fake provider is told to return. The body is a single user message of numbered pseudo-tokens
  (<code>t0001 t0002 ...</code>, 5 characters and one space each). Request headers are not counted. The gateway and direct variants of a scenario send the same body.</p>
  <table class="nowrap" style="font-size:8.5px">
    <thead><tr><th>Scenario</th><th>Fixture</th><th class="num">Prompt tokens</th><th class="num">Completion tokens</th>
      <th class="num">Request body</th><th>Response</th><th>Provider delay</th></tr></thead>
    <tbody>{body}</tbody>
  </table>"""

    def size_cell(est, key, budget):
        if not est or est.get(key) is None:
            return '<td class="num">—</td>'
        v = est[key]
        color = KONG_GREEN if budget is None or v < budget else KONG_ORANGE
        return f'<td class="num" style="color:{color};font-weight:700;white-space:nowrap">{v:+.3f} ms</td>'

    def size_section_html():
        """Section 3.4. Empty for a run without request-size tiers."""
        if not size_data:
            return ""
        b95, b99 = overhead_budgets.get("p95_ms"), overhead_budgets.get("p99_ms")
        head = """
    <thead>
      <tr><th rowspan="2">Request body</th>
          <th colspan="3" style="text-align:center">Buffered response</th>
          <th colspan="3" style="text-align:center">SSE response</th></tr>
      <tr><th class="num">p95</th><th class="num">p99</th><th class="num">vs baseline</th>
          <th class="num">p95</th><th class="num">p99</th><th class="num">vs baseline</th></tr>
    </thead>"""
        html = """
  <h2 class="sub">3.4 Overhead by Request Size and Response Mode</h2>
  <p>Gateway overhead (gateway &minus; direct, median over rounds) for request bodies of different size.
  Buffered is a non-streaming chat response. SSE is a streaming chat response with frames of about 100 to 200 bytes.
  The first row of each table (~0.4 KB) is the short fixture of the older reports. A small body shows the fixed per-request
  cost of the gateway. The question is whether the overhead stays small for a body of the size that real agents send
  (about 10 KB).</p>"""
        for block in size_data:
            rows = ""
            for r in block["rows"]:
                cells = ""
                for mode, _, _ in SIZE_MODES:
                    est = r[mode]
                    cells += size_cell(est, "p95_delta_ms", b95) + size_cell(est, "p99_delta_ms", b99)
                    cells += f'<td class="num">{overhead_vs_baseline(est["gateway_scenario_key"]) if est else "—"}</td>'
                rows += f"<tr><td>{r['label']}</td>{cells}</tr>"
            html += f"""
  <h3>{block["title"]}</h3>
  <table>{head}
    <tbody>{rows}</tbody>
  </table>"""
        html += fig_block(img_overhead_by_size, "Overhead by request size chart",
                          "Figure 2b &mdash; p99 gateway overhead for each request size, buffered and SSE. "
                          "The dashed line is the p99 overhead budget.")
        return html

    OVERHEAD_TABLE_HEAD = """
    <thead>
      <tr>
        <th>Scenario (fixture)</th>
        <th class="num">Direct p95 (ms)</th>
        <th class="num">Gateway p95 (ms)</th>
        <th class="num">Overhead p95</th>
        <th class="num">95% CI</th>
        <th class="num">p99</th>
        <th class="num">TTFT</th>
        <th class="num">vs baseline</th>
      </tr>
    </thead>"""

    def env_block_html():
        if env_record is None:
            note = "No <code>env.json</code> was recorded for this run, so the exact node types, zones, image digest " \
                   "and kernel are not part of the evidence."
            return f"<p>{note}</p>"
        rows = ""
        kong = env_record.get("kong") or {}
        for label, value in (("Kong image", kong.get("image")), ("Kong image digest", kong.get("image_digest")),
                             ("Kong version", kong.get("version")),
                             ("k6 version", (env_record.get("k6") or {}).get("version")),
                             ("Declarative config hash", env_record.get("config_hash")),
                             ("Fake provider config hash", (env_record.get("fake_provider") or {}).get("hash")),
                             ("Suite git sha", env_record.get("suite_git_sha"))):
            if value:
                rows += f"<tr><td>{label}</td><td colspan='3'><code>{value}</code></td></tr>"
        for n in env_record.get("nodes") or []:
            rows += (f"<tr><td>Node: {n.get('role','?')}</td><td>{n.get('instance_type','?')}</td>"
                     f"<td>{n.get('zone','?')}</td><td>{n.get('kernel','?')} / {n.get('os','?')}</td></tr>")
        return ("<table><thead><tr><th>Item</th><th>Value / instance type</th><th>Zone</th><th>Kernel / OS</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>")

    def env_drift_html():
        if not env_drift:
            return ""
        if env_drift.get("status") == "drift":
            items = "".join(f"<li><code>{d['field']}</code>: {d['current']} (baseline: {d['baseline']})</li>"
                            for d in env_drift["drift"])
            return ('<div class="callout warn"><strong>Environment differs from the baseline</strong> — '
                    'results may not be directly comparable (informational; does not change the verdict).'
                    f"<ul>{items}</ul></div>")
        if env_drift.get("status") == "ok":
            return "<p>Environment matches the baseline on the compared fields.</p>"
        return ""

    def unconfirmed_html():
        regs = unconfirmed.get("regressions") or []
        unst = unconfirmed.get("unstable") or []
        if not regs and not unst:
            return ""
        items = "".join(f"<li><code>{r['scenario']}</code> / {r['metric']}: {r['delta']}</li>" for r in regs)
        items += "".join(f"<li><code>{u['scenario']}</code> unstable (CV {u['cv_pct']:.1f}%)</li>" for u in unst)
        return ('<p><strong>Not reproduced in the confirmation run (treated as noise):</strong></p>'
                f"<ul>{items}</ul>")

    def stability_rows():
        rows = ""
        for key in sorted(SCENARIOS.keys()):
            d = SCENARIOS[key]
            cv = d.get("cv_pct") or {}
            p95_cv = cv.get("p95_ms") or 0
            thr_cv = cv.get("throughput_rps") or 0
            worst = max(p95_cv, thr_cv)
            is_unstable = key in {u["scenario"] for u in unstable_list}
            status = f'<span style="color:{KONG_RED}">&#9888; UNSTABLE</span>' if is_unstable \
                else f'<span style="color:{KONG_GREEN}">&#10003; STABLE</span>'
            rows += f"""
            <tr>
              <td>{key}</td>
              <td class="num">{p95_cv:.2f}%</td>
              <td class="num">{thr_cv:.2f}%</td>
              <td class="num">{status}</td>
            </tr>"""
        return rows

    def all_scenarios_rows():
        rows = ""
        for key, data in sorted(SCENARIOS.items()):
            p95 = fmt(data.get("p95_ms"), decimals=3)
            p99 = fmt(data.get("p99_ms"), decimals=3)
            rps = fmt(data.get("throughput_rps"), "rps")
            ttft = fmt(data.get("ttft_p95_ms"), decimals=1) if data.get("ttft_p95_ms") else "—"
            err = f"{data.get('error_rate_pct', 0) or 0:.1f}%"
            cv = f"{(data.get('cv_pct') or {}).get('p95_ms', 0) or 0:.1f}%"
            err_color = f"color:{KONG_RED}" if (data.get("error_rate_pct", 0) or 0) > 0 else ""
            rows += f"""
            <tr>
              <td>{key}</td>
              <td class="num">{p95}</td>
              <td class="num">{p99}</td>
              <td class="num">{ttft}</td>
              <td class="num">{rps}</td>
              <td class="num" style="{err_color}">{err}</td>
              <td class="num">{cv}</td>
            </tr>"""
        return rows

    def verdict_detail_rows():
        rows = ""
        for r in regressions:
            rows += f"""<tr><td>{r["scenario"]}</td><td>{r["metric"]}</td>
              <td style="color:{KONG_RED};font-weight:700">REGRESSION</td><td>{r["delta"]}</td></tr>"""
        for r in warn_list:
            rows += f"""<tr><td>{r["scenario"]}</td><td>{r["metric"]}</td>
              <td style="color:{KONG_ORANGE};font-weight:700">WARN</td><td>{r["delta"]}</td></tr>"""
        for r in unstable_list:
            rows += f"""<tr><td>{r["scenario"]}</td><td>—</td>
              <td style="color:{KONG_ORANGE};font-weight:700">UNSTABLE</td><td>CV {r["cv_pct"]:.1f}%</td></tr>"""
        return rows

    def slo_rows():
        rows = ""
        for v in slo_violations:
            rows += f"""<tr><td>{v["scenario"]}</td><td>{v["metric"]}</td>
              <td class="num">{v["actual"]:.2f}</td><td class="num">{v["threshold"]:.2f}</td>
              <td>{v["detail"]}</td></tr>"""
        return rows

    # ── verdict badge / callout ──────────────────────────────────────────
    verdict_color = VERDICT_COLOR.get(overall, KONG_GREY)
    baseline_line = ""
    compared_count = None if baseline_scenarios is None else len(baseline_scenarios & set(SCENARIOS))
    if baseline_version and compared_count == 0:
        baseline_line = (f'baseline <strong>{baseline_version}</strong> has none of the scenarios of this run, '
                         f'so nothing was compared with it')
    elif baseline_version and compared_count is None:
        baseline_line = (f'baseline <strong>{baseline_version}</strong>; its metrics could not be read here, so which '
                         f'scenarios were compared is not stated in this report')
    elif baseline_version:
        baseline_line = f'compared against baseline <strong>{baseline_version}</strong>'
        if baseline_captured_at:
            baseline_line += f' (captured {baseline_captured_at})'
    else:
        baseline_line = "no baseline comparison available for this run"

    n_reg, n_warn, n_unstable, n_slo = len(regressions), len(warn_list), len(unstable_list), len(slo_violations)

    if overall in ("PASS", "BASELINE_PASS"):
        verdict_callout_class = "good"
        if compared_count == 0:
            verdict_summary = (f"All {scenario_count} scenarios are new, with no baseline entry to compare against. "
                               "The run is the first record of them: no gateway-overhead SLO violation and no instability "
                               "within the run.")
        elif compared_count is not None and compared_count < scenario_count:
            verdict_summary = (f"{compared_count} of {scenario_count} scenarios passed within tolerance vs. baseline; the other "
                               f"{scenario_count - compared_count} are new (no baseline entry). No regressions, no instability.")
        else:
            verdict_summary = f"All {scenario_count} scenarios passed within tolerance vs. baseline. No regressions, no instability."
    elif overall == "N/A":
        verdict_callout_class = "warn"
        verdict_summary = "No verdict.json found for this run — regression comparison was not performed."
    else:
        verdict_callout_class = "warn" if overall in ("WARN", "UNSTABLE") else "warn"
        parts = []
        if n_reg:
            parts.append(f"{n_reg} regression(s)")
        if n_warn:
            parts.append(f"{n_warn} warning(s)")
        if n_unstable:
            parts.append(f"{n_unstable} unstable scenario(s)")
        if n_slo:
            parts.append(f"{n_slo} absolute SLO violation(s)")
        verdict_summary = "Flagged: " + ", ".join(parts) if parts else "See details below."

    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Inter', 'Helvetica Neue', Arial, sans-serif;
    font-size: 9.5pt;
    color: #212529;
    background: white;
    line-height: 1.55;
  }}
  .cover {{
    background: linear-gradient(135deg, {KONG_BLUE} 0%, #005580 100%);
    color: white;
    padding: 80px 60px 60px;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    justify-content: center;
  }}
  .cover .logo {{ font-size: 11pt; letter-spacing: 3px; text-transform: uppercase;
                  opacity: 0.7; margin-bottom: 40px; }}
  .cover h1 {{ font-size: 28pt; font-weight: 700; line-height: 1.2; margin-bottom: 18px; }}
  .cover h2 {{ font-size: 14pt; font-weight: 400; opacity: 0.85; margin-bottom: 50px; }}
  .cover .meta {{ font-size: 9pt; opacity: 0.7; border-top: 1px solid rgba(255,255,255,0.3);
                  padding-top: 24px; margin-top: auto; }}
  .cover .badge {{
    display: inline-block;
    background: {verdict_color};
    color: white;
    font-size: 10pt;
    font-weight: 700;
    padding: 6px 18px;
    border-radius: 20px;
    margin-bottom: 30px;
  }}
  .page {{ padding: 48px 52px; page-break-after: always; }}
  .page:last-child {{ page-break-after: avoid; }}
  h1.section {{ font-size: 16pt; font-weight: 700; color: {KONG_BLUE};
                border-bottom: 3px solid {KONG_TEAL}; padding-bottom: 8px;
                margin-bottom: 20px; }}
  h2.sub {{ font-size: 11pt; font-weight: 700; color: {KONG_BLUE};
             margin: 24px 0 10px; }}
  h3.subsub {{ font-size: 9.5pt; font-weight: 700; color: {KONG_GREY};
               margin: 16px 0 8px; }}
  .callout {{
    padding: 14px 18px;
    border-radius: 6px;
    margin: 14px 0;
    font-size: 9pt;
  }}
  .callout.info  {{ background: #e7f5ff; border-left: 4px solid {KONG_TEAL}; }}
  .callout.warn  {{ background: #fff3bf; border-left: 4px solid {KONG_ORANGE}; }}
  .callout.good  {{ background: #ebfbee; border-left: 4px solid {KONG_GREEN}; }}
  .kpi-row {{
    display: flex; gap: 14px; margin: 18px 0;
  }}
  .kpi {{
    flex: 1;
    background: {LIGHT_GREY};
    border: 1px solid {MID_GREY};
    border-radius: 8px;
    padding: 16px 12px;
    text-align: center;
  }}
  .kpi .val {{ font-size: 18pt; font-weight: 700; color: {KONG_BLUE}; }}
  .kpi .lbl {{ font-size: 7.5pt; color: {KONG_GREY}; margin-top: 4px; }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 8.5pt;
    margin: 12px 0;
  }}
  thead tr {{ background: {KONG_BLUE}; color: white; }}
  thead th {{ padding: 8px 10px; text-align: left; font-weight: 600; }}
  table.nowrap td, table.nowrap th {{ white-space: nowrap; padding: 4px 6px; }}
  tbody tr:nth-child(odd)  {{ background: {LIGHT_GREY}; }}
  tbody tr:nth-child(even) {{ background: white; }}
  tbody td {{ padding: 7px 10px; border-bottom: 1px solid {MID_GREY}; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .fig {{ margin: 18px 0; }}
  .fig img {{ width: 100%; border-radius: 6px;
               box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
  .fig-cap {{ font-size: 7.5pt; color: {KONG_GREY}; text-align: center; margin-top: 6px; }}
  .arch {{
    background: {LIGHT_GREY};
    border: 1px solid {MID_GREY};
    border-radius: 8px;
    padding: 20px;
    margin: 16px 0;
    font-size: 8.5pt;
    text-align: center;
  }}
  .arch-row {{ display: flex; align-items: center; justify-content: center; gap: 0; }}
  .arch-box {{
    background: {KONG_BLUE}; color: white;
    padding: 10px 16px; border-radius: 6px;
    font-weight: 600; font-size: 8pt;
    min-width: 110px;
  }}
  .arch-box.green {{ background: {KONG_GREEN}; }}
  .arch-box.teal  {{ background: {KONG_TEAL}; }}
  .arch-box.grey  {{ background: {KONG_GREY}; }}
  .arch-arrow {{ padding: 0 8px; color: {KONG_GREY}; font-size: 14pt; }}
  .arch-label {{ font-size: 7pt; color: {KONG_GREY}; margin-top: 6px; }}
  p {{ margin: 8px 0; }}
  ul {{ margin: 8px 0 8px 20px; }}
  li {{ margin: 3px 0; }}
  strong {{ font-weight: 700; }}
</style>
</head>
<body>

<div class="cover">
  <div class="logo">Kong &nbsp;·&nbsp; AI Gateway Performance</div>
  <div class="badge">{overall}</div>
  <h1>AI Gateway Performance<br>Testing Report</h1>
  <h2>Version {VERSION} &nbsp;|&nbsp; AWS EKS Benchmark</h2>
  <div class="meta">
    <strong>Captured:</strong> {CAPTURED_AT} &nbsp;&nbsp;
    <strong>Environment:</strong> AWS EKS {args.region} &nbsp;&nbsp;
    <strong>Generated:</strong> {now_str}<br>
    <strong>Test Runner:</strong> k6 via k6-operator &nbsp;&nbsp;
    <strong>Repeats per scenario:</strong> {args.repeats} runs &times; {args.duration} &nbsp;&nbsp;
    <strong>Prepared by:</strong> {args.prepared_by}
  </div>
</div>

<div class="page">
  <h1 class="section">1. Executive Summary</h1>

  <p>This report presents the performance benchmark results for <strong>Kong AI Gateway {VERSION}</strong>,
  measured on a dedicated AWS EKS cluster ({args.cluster_name}). The primary objective is to quantify the
  <strong>processing overhead added by Kong</strong> over a direct upstream call, independent of provider latency,
  and to check for regressions relative to a known baseline.</p>

  <div class="callout {verdict_callout_class}">
    <strong>Regression verdict: {overall}</strong> — {baseline_line}. {verdict_summary}
  </div>

  <h2 class="sub">Headline Metrics</h2>
  <div class="kpi-row">
    <div class="kpi">
      <div class="val">{fmt(max_mlflow_overhead) if max_mlflow_overhead is not None else "—"}</div>
      <div class="lbl">Max gateway overhead p95<br>(mlflow50 track){"" if max_mlflow_overhead is not None else "<br>not part of this run"}</div>
    </div>
    <div class="kpi">
      <div class="val">{max_error_rate:.1f}%</div>
      <div class="lbl">Max error rate<br>across all scenarios</div>
    </div>
    <div class="kpi">
      <div class="val">{fmt(chat_rps, "rps", 1) if chat_rps is not None else "—"}</div>
      <div class="lbl">Chat throughput<br>(token-chat-openai){"" if chat_rps is not None else "<br>not part of this run"}</div>
    </div>
    <div class="kpi">
      <div class="val">{max_cv:.1f}%</div>
      <div class="lbl">Max coefficient of variation<br>(result stability)</div>
    </div>
    <div class="kpi">
      <div class="val">{scenario_count}</div>
      <div class="lbl">Scenarios tested<br>{args.repeats} repeats each</div>
    </div>
  </div>

  <div class="callout warn">
    <strong>Important — Fake Provider Latency:</strong> All benchmark scenarios route through a
    <strong>fake OpenAI-compatible provider</strong> with a fixed simulated delay (not a real LLM).
    Observed end-to-end latency figures include this provider delay and <em>do not represent real LLM
    response times</em>. Gateway overhead is isolated by subtracting direct-upstream measurements
    (Section 3); the SLOs in Section 4 are budgets on that overhead, not on absolute latency.
  </div>
</div>

<div class="page">
  <h1 class="section">2. Methodology &amp; Test Architecture</h1>

  <h2 class="sub">2.1 Benchmark Architecture</h2>
  <div class="arch">
    <div class="arch-row">
      <div class="arch-box green">k6 Load Generator<br><small>(c5.metal node)</small></div>
      <div class="arch-arrow">&rarr;</div>
      <div class="arch-box">Kong AI Gateway<br><small>(c5.4xlarge node)</small></div>
      <div class="arch-arrow">&rarr;</div>
      <div class="arch-box teal">Fake Provider<br><small>(fixed delay)</small></div>
    </div>
    <div class="arch-label">Gateway path (measured: end-to-end including provider delay)</div>
    <br>
    <div class="arch-row">
      <div class="arch-box green">k6 Load Generator<br><small>(c5.metal node)</small></div>
      <div class="arch-arrow">&rarr;</div>
      <div class="arch-box grey">Direct Upstream<br><small>(bypass Kong)</small></div>
      <div class="arch-arrow">&rarr;</div>
      <div class="arch-box teal">Fake Provider<br><small>(fixed delay)</small></div>
    </div>
    <div class="arch-label">Direct path (baseline: provider latency only)</div>
  </div>

  <div class="callout info">
    <strong>Gateway Overhead = Gateway p95 &minus; Direct Upstream p95</strong><br>
    By running paired gateway and direct-upstream scenarios against the same fake provider,
    we isolate the exact latency contribution of Kong's processing pipeline. Gateway and direct runs of the
    same round run back-to-back (and swap order every round), so the overhead is the <em>median of the
    per-round differences</em>, with a bootstrap 95% confidence interval.
  </div>

  <h2 class="sub">2.2 Infrastructure</h2>
  <table>
    <thead><tr><th>Component</th><th>AWS Instance</th><th>Role</th></tr></thead>
    <tbody>
      <tr><td>Load Generator</td><td>c5.metal</td><td>k6 via k6-operator (dedicated, tainted)</td></tr>
      <tr><td>Kong Gateway</td><td>c5.4xlarge (16 vCPU)</td><td>Kong AI Gateway — dedicated, tainted</td></tr>
      <tr><td>Support</td><td>c5.2xlarge</td><td>Fake provider, mock upstreams, observability</td></tr>
      <tr><td>Region</td><td colspan="2">AWS {args.region}</td></tr>
      <tr><td>Kubernetes</td><td colspan="2">EKS {args.k8s_version} (cluster: {args.cluster_name})</td></tr>
      <tr><td>Kong version</td><td colspan="2">{VERSION} (kong/kong-ai-gateway-dev)</td></tr>
    </tbody>
  </table>

  <h2 class="sub">2.3 Load Profile</h2>
  <table>
    <thead><tr><th>Scenario Type</th><th>Load</th><th>Duration</th><th>Repeats</th></tr></thead>
    <tbody>
      <tr><td>Chat, Embeddings, Static</td><td class="num">50 RPS</td><td>{args.duration}</td><td>{args.repeats}</td></tr>
      <tr><td>Routing, Policy</td><td class="num">25 RPS</td><td>{args.duration}</td><td>{args.repeats}</td></tr>
      <tr><td>Streaming</td><td class="num">30 VUs</td><td>{args.duration}</td><td>{args.repeats}</td></tr>
    </tbody>
  </table>
  {request_profile_html()}

  <h2 class="sub">2.4 Test Environment Record</h2>
  {env_block_html()}
  {env_drift_html()}
</div>

<div class="page">
  <h1 class="section">3. Gateway Overhead Analysis</h1>

  <h2 class="sub">3.1 Absolute Overhead — mlflow50 Track (Primary Metric)</h2>
  {fig_block(img_overhead_mlflow, "Absolute gateway overhead chart",
             "Figure 1 — Pure Kong AI Gateway overhead (p95). Upstream fixed at 50 ms. "
             "Gateway overhead = gateway p95 &minus; direct p95.",
             applicable=has_mlflow_fixture,
             not_run="no mlflow50-fixture scenario was run (the mlflow50 request-size tiers are in section 3.4).")}

  <h2 class="sub">3.2 Gateway vs Direct — All Fixtures</h2>
  {fig_block(img_overhead_all, "Overhead chart all fixtures",
             "Figure 2 — Gateway (blue) vs direct upstream (teal) p95 latency per scenario. "
             "Orange labels show the gateway overhead delta.")}

  <table>{OVERHEAD_TABLE_HEAD}
    <tbody>{overhead_table_rows()}</tbody>
  </table>
  <p><small>Overhead = median over rounds of (gateway &minus; direct) for the same round. The gateway path is
  HTTPS (TLS terminated at Kong, certificate verification skipped) while the direct path is plain HTTP, so the
  overhead includes the TLS cost on the client hop until the HTTP-entry variants are measured.</small></p>

  <h2 class="sub">3.3 Scenarios Compared With a Reference Control</h2>
  <p>Routing and logging scenarios have no direct-upstream twin of their own; they are compared with the
  same-fixture direct token-chat control (same fake-provider delay).</p>
  <table>{OVERHEAD_TABLE_HEAD}
    <tbody>{derived_overhead_rows()}</tbody>
  </table>
  {size_section_html()}
</div>

<div class="page">
  <h1 class="section">4. Regression Verdict vs Baseline</h1>

  <div class="callout {verdict_callout_class}">
    <strong>Overall: {overall}</strong> — {baseline_line}.
  </div>
  <p><small>Statistic: {"median across repeats for latency and throughput (error rate uses the mean)" if "median" in gate_statistic else ("mean across repeats" if gate_statistic else "n/a")}.</small></p>

  <table>
    <thead><tr><th>Scenario</th><th>Metric</th><th>Status</th><th>Detail</th></tr></thead>
    <tbody>{verdict_detail_rows() or '<tr><td colspan="4">No regressions, warnings, or instability flagged.</td></tr>'}</tbody>
  </table>

  {unconfirmed_html()}

  <h2 class="sub">Absolute SLO Violations</h2>
  <p>Absolute SLOs (from <code>benchmark_config.yaml</code>) are independent of the baseline comparison:
  error rate, checks pass rate, dropped iterations (worst repeat) and the <strong>gateway-overhead budgets</strong>
  (p95 {overhead_budgets.get("p95_ms", "—")} ms, p99 {overhead_budgets.get("p99_ms", "—")} ms,
  streaming TTFT {overhead_budgets.get("ttft_p95_ms", "—")} ms, measured against the direct path of the same run).
  The old absolute latency thresholds are no longer applied: they measured the fake provider's simulated delay,
  not the gateway.</p>
  <table>
    <thead><tr><th>Scenario</th><th>Metric</th><th class="num">Actual</th><th class="num">Threshold</th><th>Detail</th></tr></thead>
    <tbody>{slo_rows() or '<tr><td colspan="5">No absolute SLO violations.</td></tr>'}</tbody>
  </table>
</div>

<div class="page">
  <h1 class="section">5. Routing &amp; Policy Scenarios</h1>

  {fig_block(img_routing, "Routing scenarios chart",
             "Figure 3 — Routing &amp; policy scenario p95 latency.",
             applicable=has_routing_scenarios,
             not_run="no routing, payload-logging or static-chat scenario was run.")}

  <h2 class="sub">Streaming Performance</h2>
  {fig_block(img_ttft, "TTFT chart",
             "Figure 4 — Time-To-First-Token (TTFT) p95 for streaming scenarios.")}
</div>

<div class="page">
  <h1 class="section">6. Throughput &amp; Stability</h1>

  {fig_block(img_throughput, "Throughput chart",
             "Figure 5 — Sustained throughput (RPS) across key scenarios.",
             applicable=has_throughput_scenarios,
             not_run="none of the scenarios this chart plots (static-chat, token-chat-openai, embeddings-openai, "
                     "routing-roundrobin-2, stream-openai, stream-gemini, short fixture) was run.")}

  <h2 class="sub">Stability (Coefficient of Variation)</h2>
  <p>Scenarios flagged UNSTABLE exceeded the stability gate's max CV threshold and should be re-run
  before being trusted for a release decision.</p>
  <table>
    <thead><tr><th>Scenario</th><th class="num">p95 CV%</th><th class="num">Throughput CV%</th><th class="num">Stability</th></tr></thead>
    <tbody>{stability_rows()}</tbody>
  </table>
</div>

<div class="page">
  <h1 class="section">7. Full Scenario Results</h1>
  <table>
    <thead>
      <tr>
        <th>Scenario</th>
        <th class="num">p95 (ms)</th>
        <th class="num">p99 (ms)</th>
        <th class="num">TTFT p95</th>
        <th class="num">RPS</th>
        <th class="num">Error%</th>
        <th class="num">CV%</th>
      </tr>
    </thead>
    <tbody>{all_scenarios_rows()}</tbody>
  </table>

  <div class="callout warn">
    <strong>Reminder:</strong> Latency values above include fake provider simulation delay.
    The <code>direct-*</code> rows are the provider-only baseline. Subtract direct from
    gateway to get the Kong overhead for that scenario (Section 3).
  </div>
</div>

<div class="page">
  <h1 class="section">8. Conclusions &amp; What This Benchmark Does Not Measure</h1>

  <h2 class="sub">8.1 Key Findings</h2>
  <ul>
    <li><strong>Regression verdict:</strong> {overall} ({baseline_line})</li>
    {f"<li><strong>Gateway overhead (mlflow50 track):</strong> {fmt(min_mlflow_overhead)} to {fmt(max_mlflow_overhead)} p95</li>" if max_mlflow_overhead is not None else ""}
    {streaming_ttft_finding}<li><strong>Max error rate:</strong> {max_error_rate:.1f}% across {scenario_count} scenarios / {total_runs} planned runs</li>
    <li><strong>Max stability CV:</strong> {max_cv:.1f}%</li>
  </ul>

  <h2 class="sub">8.2 Absolute SLO Status</h2>
  <div class="callout {"good" if not slo_violations else "warn"}">
    <strong>{"All absolute SLOs and gateway-overhead budgets are met." if not slo_violations else f"{len(slo_violations)} absolute SLO violation(s) flagged (Section 4)."}</strong>
    The latency SLOs are budgets on what the gateway adds over the direct path, so they hold regardless of the
    fake provider's simulated delay.
  </div>

  <h2 class="sub">8.3 What This Benchmark Does Not Measure</h2>
  <table>
    <thead><tr><th>Not Measured</th><th>Reason</th></tr></thead>
    <tbody>
      <tr><td>Real LLM provider latency</td><td>Fake provider returns fixed synthetic response</td></tr>
      <tr><td>Internet / network distance to LLM API</td><td>All traffic stays within the EKS cluster</td></tr>
      <tr><td>Model generation time beyond fixed delay</td><td>Fake provider returns immediately after delay</td></tr>
      <tr><td>TLS handshake cost</td><td>k6 reuses connections, so handshakes are amortised; connection-churn scenarios are not part of this run</td></tr>
      <tr><td>TLS vs gateway split of the overhead</td><td>Client &rarr; Kong is TLS (self-signed, verification skipped); Kong &rarr; upstream and the direct path are plain HTTP, so the measured overhead includes the TLS cost of the client hop</td></tr>
      <tr><td>Authentication / RBAC flows</td><td>Auth is not exercised in these scenarios</td></tr>
    </tbody>
  </table>
</div>

</body>
</html>
"""

    if args.html_out:
        with open(args.html_out, "w") as fh:
            fh.write(HTML)

    from weasyprint import HTML as WP_HTML

    print(f"Rendering PDF -> {args.output_pdf} ...")
    WP_HTML(string=HTML, base_url=".").write_pdf(
        args.output_pdf,
        presentational_hints=True,
        optimize_images=True,
    )
    print(f"Done: {args.output_pdf}")


if __name__ == "__main__":
    main()
