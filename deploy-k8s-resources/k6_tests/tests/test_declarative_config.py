"""Consistency of the Kong declarative config with the benchmark scenarios.

Both problems below only showed up on a real cluster: the routes were loaded https-only, so the HTTP-entry
scenarios got 426; and the generator that produces the file had drifted from the committed file.
"""
import re
import sys
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
K6_TESTS = HERE.parent
sys.path.insert(0, str(K6_TESTS))

import release_matrix as rm  # noqa: E402

DECLARATIVE = K6_TESTS.parent / "kong_helm" / "declarative" / "kong-full.yaml"
RUNNER = K6_TESTS / "run_ai_benchmark.sh"
GATEWAY = re.compile(r'K6_AI_CHAT_URL="(https?)://kong-kong-proxy[^/"]*(/[^"]*)"')


def routes():
    cfg = yaml.safe_load(DECLARATIVE.read_text())
    return [r for s in cfg["services"] for r in s["routes"]]


def scenario_urls():
    """scenario name -> (scheme, path) for every scenario of run_ai_benchmark.sh that goes through the gateway."""
    text = RUNNER.read_text()
    out = {}
    for m in re.finditer(r"^  ([a-z0-9|-]+)\)\n(.*?)^    ;;", text, re.M | re.S):
        names, body = m.group(1).split("|"), m.group(2)
        u = GATEWAY.search(body)
        if not u:
            continue
        for name in names:
            path = u.group(2).replace("${ROUTING_VARIANT}", name[len("routing-"):] if name.startswith("routing-") else "")
            out[name] = (u.group(1), path)
    return out


class Routes(unittest.TestCase):
    def test_every_route_serves_http_and_https(self):
        # Without `protocols` this Kong image loaded the routes https-only and answered plain HTTP with 426.
        for r in routes():
            self.assertEqual(sorted(r["protocols"]), ["http", "https"], r["name"])

    def test_generator_emits_protocols_so_a_regeneration_does_not_lose_them(self):
        self.assertIn('"protocols": ["http", "https"]',
                      (K6_TESTS.parent / "kong_helm" / "declarative" / "build_declarative.py").read_text())

    def test_route_names_and_paths_are_unique(self):
        names = [r["name"] for r in routes()]
        paths = [p for r in routes() for p in r["paths"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(paths), len(set(paths)))


class StreamingRoutes(unittest.TestCase):
    """Kong buffers the response by default, which delivers a whole SSE stream in one burst at the end. Measured on the
    cluster: all 18 events arrived at once after ~1325 ms while the direct upstream delivered the first at 121 ms."""

    def test_streaming_routes_do_not_buffer_the_response(self):
        streaming = [r for r in routes() if "stream" in r["name"]]
        self.assertGreaterEqual(len(streaming), 2)
        for r in streaming:
            self.assertIs(r.get("response_buffering"), False, r["name"])

    def test_other_routes_keep_kongs_default_so_their_results_stay_comparable(self):
        for r in routes():
            if "stream" not in r["name"]:
                self.assertNotIn("response_buffering", r, r["name"])

    def test_the_generator_sets_it_too_so_a_regeneration_does_not_lose_it(self):
        text = (K6_TESTS.parent / "kong_helm" / "declarative" / "build_declarative.py").read_text()
        self.assertIn('route["response_buffering"] = False', text)
        self.assertIn('"stream" in meta["name"]', text)

    def test_every_streaming_scenario_goes_through_a_route_that_streams(self):
        urls = scenario_urls()
        streaming_paths = [p for r in routes() if r.get("response_buffering") is False for p in r["paths"]]
        checked = 0
        for e in rm.load_matrix(K6_TESTS / "release_matrix.yaml"):
            if "stream" in e["scenario"] and e["scenario"] in urls:       # gateway streaming scenarios
                checked += 1
                path = urls[e["scenario"]][1]
                self.assertTrue(any(path.startswith(sp) for sp in streaming_paths),
                                f"{e['scenario']} requests {path}, which no non-buffering route serves")
        self.assertGreaterEqual(checked, 3)       # stream-openai, stream-gemini, stream-openai-disconnect


class ScenarioPaths(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.urls = scenario_urls()
        cls.route_paths = [p for r in routes() for p in r["paths"]]

    def test_the_parser_finds_the_gateway_scenarios(self):
        for name in ("token-chat-openai", "stream-gemini", "embeddings-openai", "routing-ewma", "token-chat-openai-http"):
            self.assertIn(name, self.urls)
        self.assertNotIn("direct-token-chat-openai", self.urls)           # direct paths do not use the gateway
        self.assertEqual(self.urls["routing-roundrobin-2"][1], "/bench/routing/roundrobin-2")

    def test_every_release_scenario_requests_a_path_that_a_route_serves(self):
        # Kong matches route paths by prefix (strip_path is false), so the route path must prefix the request path.
        entries = rm.load_matrix(K6_TESTS / "release_matrix.yaml")        # enabled and pending alike
        checked = 0
        for e in entries:
            if e["scenario"] not in self.urls:
                continue
            checked += 1
            path = self.urls[e["scenario"]][1]
            self.assertTrue(any(path.startswith(rp) for rp in self.route_paths),
                            f"{e['scenario']} requests {path}, but no route serves it")
        self.assertGreater(checked, 10)

    def test_http_entry_scenarios_use_http_and_every_other_gateway_scenario_https(self):
        for name, (scheme, _) in self.urls.items():
            expected = "http" if name.endswith("-http") or "-http-" in name else "https"
            self.assertEqual(scheme, expected, name)

    def test_http_variants_hit_the_same_path_as_their_https_twin(self):
        pairs = {"token-chat-openai-http": "token-chat-openai", "embeddings-openai-http": "embeddings-openai",
                 "token-chat-openai-http-churn": "token-chat-openai-churn"}
        for http_name, https_name in pairs.items():
            self.assertEqual(self.urls[http_name][1], self.urls[https_name][1], http_name)

    def test_known_gap_policy_and_large_scenarios_have_no_route(self):
        # Documents the gap found while reviewing the suite: these k6 scripts exist, the routes do not. When the
        # routes are added this test fails on purpose, as a reminder to put the scenarios into the matrix.
        for name in ("policy-auth-openai", "large-prompt", "large-response"):
            self.assertIn(name, self.urls)
            self.assertFalse(any(self.urls[name][1].startswith(rp) for rp in self.route_paths), name)


if __name__ == "__main__":
    unittest.main()
