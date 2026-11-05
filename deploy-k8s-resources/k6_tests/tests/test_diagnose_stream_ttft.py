"""Tests for diagnose_stream_ttft.py: the analysis on synthetic timestamps, and the capture/CLI against
local servers that stream evenly or hold back the start of the stream like a buffering gateway."""
import io
import json
import shutil
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
K6_TESTS = HERE.parent
sys.path.insert(0, str(K6_TESTS))

import diagnose_stream_ttft as dg  # noqa: E402


def sample(events, headers=5.0, total=None, status=200):
    return {"status": status, "headers_ms": headers, "events_ms": events,
            "total_ms": total if total is not None else (events[-1] + 1 if events else 10)}


class Analysis(unittest.TestCase):
    def test_even_stream_has_a_burst_of_one(self):
        a = dg.analyze(sample([100, 180, 260, 340, 420]))
        self.assertEqual((a["first_event_ms"], a["burst_events"], a["events"]), (100, 1, 5))
        self.assertEqual(a["median_gap_ms"], 80)

    def test_buffered_start_shows_as_a_burst(self):
        a = dg.analyze(sample([400.0, 400.5, 401.0, 401.2, 402.0, 480, 560]))
        self.assertEqual(a["burst_events"], 5)
        self.assertEqual(a["first_event_ms"], 400.0)

    def test_stream_without_events(self):
        a = dg.analyze(sample([], headers=12.0))
        self.assertIsNone(a["first_event_ms"])
        self.assertEqual(a["events"], 0)

    def test_summarize_takes_the_median_and_skips_empty_runs(self):
        runs = [dg.analyze(sample([100 + d, 180 + d])) for d in (0, 10, 500)] + [dg.analyze(sample([]))]
        s = dg.summarize(runs)
        self.assertEqual((s["runs"], s["first_event_ms"]), (3, 110))
        self.assertIsNone(dg.summarize([dg.analyze(sample([]))]))


def summary(first, total, burst, headers=5.0):
    return {"first_event_ms": first, "total_ms": total, "burst_events": burst, "headers_ms": headers}


class Verdict(unittest.TestCase):
    def test_no_overhead(self):
        v, facts, _ = dg.diagnose(summary(125, 1330, 1), summary(120, 1326, 1))
        self.assertEqual(v, "NO_TTFT_OVERHEAD")
        self.assertAlmostEqual(facts["first_event_delta_ms"], 5)

    def test_buffering_of_the_stream_start(self):
        v, facts, text = dg.diagnose(summary(524, 1330, 6), summary(123, 1326, 1))
        self.assertEqual(v, "BUFFERS_STREAM_START")
        self.assertEqual((facts["burst_gateway"], facts["burst_direct"]), (6, 1))
        self.assertIn("burst", text)

    def test_late_first_bytes_but_the_stream_ends_on_time(self):
        v, _, _ = dg.diagnose(summary(524, 1330, 1), summary(123, 1326, 1))
        self.assertEqual(v, "DELAYS_FIRST_BYTES_ONLY")

    def test_everything_is_later(self):
        v, _, _ = dg.diagnose(summary(524, 1800, 1), summary(123, 1326, 1))
        self.assertEqual(v, "ADDS_LATENCY")

    def test_small_difference(self):
        self.assertEqual(dg.diagnose(summary(150, 1330, 1), summary(123, 1326, 1))[0], "SMALL_TTFT_OVERHEAD")

    def test_faster_than_direct_is_not_an_overhead(self):
        self.assertEqual(dg.diagnose(summary(100, 1300, 1), summary(105, 1326, 1))[0], "NO_TTFT_OVERHEAD")


class Requests(unittest.TestCase):
    def test_openai_request_matches_what_k6_sends(self):
        fx = dg.load_fixture("short")
        body, headers = dg.build_request("openai", fx)
        b = json.loads(body)
        self.assertTrue(b["stream"])
        self.assertEqual(b["stream_options"], {"include_usage": True})
        self.assertEqual(b["max_tokens"], fx["completion_tokens"])
        self.assertEqual(len(b["messages"][0]["content"].split()), fx["prompt_tokens"])
        self.assertEqual(headers["x-llm-ttft-ms"], str(fx["ttft_ms"]))
        self.assertEqual(headers["x-llm-tpot-ms"], str(fx["tpot_ms"]))
        self.assertEqual(headers["x-llm-completion-tokens"], str(fx["completion_tokens"]))

    def test_gemini_request_shape(self):
        fx = dg.load_fixture("short")
        b = json.loads(dg.build_request("gemini", fx)[0])
        self.assertEqual(b["generationConfig"]["maxOutputTokens"], fx["completion_tokens"])
        self.assertEqual(len(b["contents"][0]["parts"][0]["text"].split()), fx["prompt_tokens"])


def make_server(mode, first_ms=80, gap_ms=30, events=12, buffered_prefix=6):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def chunk(self, data):
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.flush()
            held = []
            for i in range(events):
                time.sleep((first_ms if i == 0 else gap_ms) / 1000)
                payload = f"data: {json.dumps({'i': i})}\n\n".encode()
                if mode == "buffered" and i < buffered_prefix:
                    held.append(payload)
                    if i == buffered_prefix - 1:
                        for p in held:
                            self.chunk(p)
                else:
                    self.chunk(payload)
            self.chunk(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    class QuietServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):   # a client closing its keep-alive connection is not news
            pass

    server = QuietServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"


class Capture(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()

    def serve(self, mode, **kw):
        server, url = make_server(mode, **kw)
        self.servers.append(server)
        return url

    def cli(self, gateway, direct, runs=3):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = dg.main(["--gateway-url", gateway, "--direct-url", direct, "--runs", str(runs)])
        return rc, out.getvalue()

    def test_events_are_timestamped_as_they_arrive(self):
        url = self.serve("even")
        body, headers = dg.build_request("openai", dg.load_fixture("short"))
        a = dg.analyze(dg.measure_stream(url, body, headers))
        self.assertEqual(a["status"], 200)
        self.assertEqual(a["events"], 13)               # 12 events + [DONE]
        self.assertGreaterEqual(a["first_event_ms"], 70)
        self.assertLess(a["first_event_ms"], 200)
        self.assertGreaterEqual(a["median_gap_ms"], 25)
        self.assertEqual(a["burst_events"], 1)

    def test_a_server_that_holds_back_the_start_is_detected(self):
        rc, out = self.cli(self.serve("buffered"), self.serve("even"))
        self.assertEqual(rc, 0)
        self.assertIn("verdict: BUFFERS_STREAM_START", out)

    def test_two_even_streams_show_no_overhead(self):
        rc, out = self.cli(self.serve("even"), self.serve("even"))
        self.assertEqual(rc, 0)
        self.assertIn("verdict: NO_TTFT_OVERHEAD", out)

    def test_json_output(self):
        out = Path(self.id() + ".json")
        try:
            with redirect_stdout(io.StringIO()):
                dg.main(["--gateway-url", self.serve("even"), "--direct-url", self.serve("even"), "--runs", "2",
                         "--json", str(out)])
            data = json.loads(out.read_text())
            self.assertEqual(set(data), {"direct", "gateway", "verdict"})
            self.assertEqual(len(data["gateway"]["runs"]), 2)
        finally:
            out.unlink(missing_ok=True)

    def test_an_unreachable_target_is_reported_not_crashed(self):
        rc = dg.main(["--gateway-url", "http://127.0.0.1:9/x", "--direct-url", self.serve("even"), "--runs", "1"])
        self.assertEqual(rc, 2)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_against_the_real_fake_provider(self):
        import os
        port = 18095
        env = {**os.environ, "PORT": str(port)}
        proc = subprocess.Popen(["node", str(K6_TESTS.parent / "fake_provider" / "server.js")], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(1.0)
            url = f"http://127.0.0.1:{port}/v1/chat/completions"
            rc, out = self.cli(url, url, runs=2)
            self.assertEqual(rc, 0, out)
            self.assertIn("verdict: NO_TTFT_OVERHEAD", out)
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
