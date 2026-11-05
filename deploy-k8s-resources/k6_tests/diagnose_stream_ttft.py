#!/usr/bin/env python3
"""
diagnose_stream_ttft.py — why is the gateway's streaming TTFT what it is?

k6 reports time-to-first-byte (http_req_waiting), which cannot say whether the gateway delays the
first bytes, holds back the first few chunks and then releases them in a burst, or just adds latency
to everything. This tool sends real streaming requests to the gateway route and to the direct
upstream route and timestamps every SSE event as it arrives, then compares the two.

Per request: time of the response headers, time of every `data:` event, total time.
Per target (median over --runs requests): first event, gap between events, and the size of the
BURST at the start of the stream (events arriving within 5 ms of the first one). An upstream that
streams evenly has a burst of 1; a gateway that buffers the start of the stream has a bigger one.

The verdict is a heuristic that names the pattern; the numbers are the evidence.

Run it from a place that can reach both routes, e.g. through kubectl port-forward:
  kubectl -n kong port-forward svc/kong-kong-proxy 8443:443 &
  kubectl -n upstream port-forward svc/fake-provider 8080:8080 &
  python3 diagnose_stream_ttft.py --kind openai \
      --gateway-url https://127.0.0.1:8443/bench/token/stream/openai --insecure \
      --direct-url  http://127.0.0.1:8080/v1/chat/completions
Absolute times include the port-forward hop; the first-event DELTA and the burst size are what matter.
"""
import argparse
import http.client
import json
import ssl
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
BURST_WINDOW_MS = 5.0
DELTA_SIGNIFICANT_MS = 50.0
DELTA_NEGLIGIBLE_MS = 10.0


def token_text(count):
    return " ".join(f"t{i + 1:04d}" for i in range(count))


def load_fixture(name, path=None):
    data = json.loads(Path(path or HERE / "ai_benchmark_fixtures.json").read_text())
    return data["stream"][name]


def build_request(kind, fixture):
    """Body and headers, the same as k6_ai_shared.js builds them."""
    headers = {"Content-Type": "application/json", "x-llm-prompt-tokens": str(fixture["prompt_tokens"]),
               "x-llm-completion-tokens": str(fixture["completion_tokens"])}
    for key, header in (("ttft_ms", "x-llm-ttft-ms"), ("tpot_ms", "x-llm-tpot-ms"),
                        ("tokens_per_chunk", "x-llm-tokens-per-chunk")):
        if key in fixture:
            headers[header] = str(fixture[key])
    text = token_text(fixture["prompt_tokens"])
    if kind == "gemini":
        body = {"contents": [{"role": "user", "parts": [{"text": text}]}],
                "generationConfig": {"maxOutputTokens": fixture["completion_tokens"]}}
    else:
        body = {"model": "mock-gpt-4o-mini", "stream": True, "stream_options": {"include_usage": True},
                "max_tokens": fixture["completion_tokens"], "messages": [{"role": "user", "content": text}]}
    return json.dumps(body), headers


def measure_stream(url, body, headers, insecure=False, timeout=60.0):
    """One streaming request. Times are milliseconds since the request was sent."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
        conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout, context=ctx)
    else:
        conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    try:
        conn.connect()                      # exclude the connection setup from the timings
        t0 = time.perf_counter()
        conn.request("POST", target, body=body, headers=headers)
        resp = conn.getresponse()
        headers_ms = (time.perf_counter() - t0) * 1000
        events = []
        while True:
            line = resp.readline()
            if not line:
                break
            if line.startswith(b"data:"):
                events.append((time.perf_counter() - t0) * 1000)
                if line.strip().endswith(b"[DONE]"):
                    break
        return {"status": resp.status, "headers_ms": headers_ms, "events_ms": events,
                "total_ms": (time.perf_counter() - t0) * 1000}
    finally:
        conn.close()


def analyze(sample):
    """Shape of one stream from its event timestamps."""
    ev = sample["events_ms"]
    if not ev:
        return {"status": sample["status"], "events": 0, "first_event_ms": None, "headers_ms": sample["headers_ms"],
                "median_gap_ms": None, "max_gap_ms": None, "burst_events": 0, "total_ms": sample["total_ms"]}
    gaps = [b - a for a, b in zip(ev, ev[1:])]
    return {
        "status": sample["status"],
        "events": len(ev),
        "headers_ms": sample["headers_ms"],
        "first_event_ms": ev[0],
        "median_gap_ms": statistics.median(gaps) if gaps else None,
        "max_gap_ms": max(gaps) if gaps else None,
        "burst_events": sum(1 for t in ev if t - ev[0] <= BURST_WINDOW_MS),
        "total_ms": sample["total_ms"],
    }


def summarize(analyses):
    """Median over the runs, per field."""
    ok = [a for a in analyses if a["first_event_ms"] is not None]
    if not ok:
        return None
    out = {"runs": len(ok), "events": ok[0]["events"]}
    for key in ("headers_ms", "first_event_ms", "median_gap_ms", "max_gap_ms", "burst_events", "total_ms"):
        vals = [a[key] for a in ok if a[key] is not None]
        out[key] = statistics.median(vals) if vals else None
    return out


def diagnose(gateway, direct):
    """Compare two summaries and name the pattern. A heuristic: the numbers are the evidence."""
    first = gateway["first_event_ms"] - direct["first_event_ms"]
    total = gateway["total_ms"] - direct["total_ms"]
    burst_g, burst_d = gateway["burst_events"], direct["burst_events"]
    header_gap = gateway["first_event_ms"] - gateway["headers_ms"]
    facts = {"first_event_delta_ms": first, "total_delta_ms": total,
             "burst_gateway": burst_g, "burst_direct": burst_d,
             "gateway_headers_to_first_event_ms": header_gap}
    if abs(first) <= DELTA_NEGLIGIBLE_MS:
        return "NO_TTFT_OVERHEAD", facts, (
            f"The first event arrives {round(first):+d} ms after the direct path's: no meaningful TTFT overhead.")
    if first > DELTA_SIGNIFICANT_MS and burst_g >= max(3, 2 * burst_d):
        return "BUFFERS_STREAM_START", facts, (
            f"The first event is {first:.0f} ms later than direct and {burst_g} events arrive within "
            f"{BURST_WINDOW_MS:.0f} ms of each other (direct: {burst_d}): the gateway holds back the start of "
            f"the stream and releases it in a burst. Look for response buffering / chunk aggregation in the "
            f"AI proxy plugin or the nginx proxy_buffering settings of the route.")
    if first > DELTA_SIGNIFICANT_MS and total < first / 2:
        return "DELAYS_FIRST_BYTES_ONLY", facts, (
            f"The first event is {first:.0f} ms later than direct but the stream ends only {total:+.0f} ms "
            f"later: the gateway holds the first bytes for a while, then streams normally.")
    if first > DELTA_SIGNIFICANT_MS:
        return "ADDS_LATENCY", facts, (
            f"The first event is {first:.0f} ms later than direct and the whole stream is {total:+.0f} ms "
            f"later: the gateway adds latency to the stream, not just to its start.")
    return "SMALL_TTFT_OVERHEAD", facts, f"The first event is {round(first):+d} ms vs direct."


def run_target(label, url, kind, fixture, runs, insecure, timeout):
    body, headers = build_request(kind, fixture)
    analyses = []
    for i in range(runs):
        try:
            analyses.append(analyze(measure_stream(url, body, headers, insecure, timeout)))
        except OSError as exc:
            print(f"  {label} run {i + 1}: {exc}", file=sys.stderr)
    return analyses


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gateway-url", required=True)
    ap.add_argument("--direct-url", required=True)
    ap.add_argument("--kind", choices=("openai", "gemini"), default="openai")
    ap.add_argument("--fixture", default="short")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--insecure", action="store_true", help="do not verify the gateway's TLS certificate")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--fixtures-file", default=None)
    ap.add_argument("--json", default=None, help="also write the result to this file")
    args = ap.parse_args(argv)

    fixture = load_fixture(args.fixture, args.fixtures_file)
    results = {}
    for label, url in (("direct", args.direct_url), ("gateway", args.gateway_url)):
        analyses = run_target(label, url, args.kind, fixture, args.runs, args.insecure, args.timeout)
        results[label] = {"url": url, "summary": summarize(analyses), "runs": analyses}

    d, g = results["direct"]["summary"], results["gateway"]["summary"]
    if not d or not g:
        print("no stream could be measured on: " + ", ".join(k for k, v in results.items() if not v["summary"]),
              file=sys.stderr)
        return 2

    def fmt(v):
        return "-" if v is None else f"{v:8.1f}"

    print(f"{args.kind} stream, fixture '{args.fixture}', median of {args.runs} runs (ms)")
    print(f"{'':10}{'headers':>9}{'1st event':>11}{'med gap':>9}{'max gap':>9}{'burst':>7}{'total':>9}{'events':>8}")
    for label, s in (("direct", d), ("gateway", g)):
        print(f"{label:10}{fmt(s['headers_ms'])}{fmt(s['first_event_ms'])}{fmt(s['median_gap_ms'])}"
              f"{fmt(s['max_gap_ms'])}{s['burst_events']:7.0f}{fmt(s['total_ms'])}{s['events']:8d}")
    verdict, facts, text = diagnose(g, d)
    print(f"\nverdict: {verdict}\n{text}")
    results["verdict"] = {"pattern": verdict, "facts": facts, "text": text}
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
