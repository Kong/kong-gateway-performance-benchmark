"""Unit tests for collect_env.py (env.json) and its fit with compare_release.env_drift.

Run from deploy-k8s-resources/k6_tests:  python3 -m unittest discover -s tests -v
"""
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
K6_TESTS = HERE.parent
sys.path.insert(0, str(K6_TESTS))

import collect_env as ce  # noqa: E402
import compare_release as cr  # noqa: E402


def node(role, zone, itype="c5.4xlarge", kernel="5.10.1", os_="Amazon Linux 2"):
    return {"metadata": {"labels": {
                ce.ROLE_LABEL: role, "node.kubernetes.io/instance-type": itype,
                "topology.kubernetes.io/zone": zone, "topology.kubernetes.io/region": "us-west-2"}},
            "status": {"nodeInfo": {"kernelVersion": kernel, "osImage": os_, "kubeletVersion": "v1.31.13"}}}


def nodes_json(loadgen_zone="us-west-2b", kong_instance="c5.4xlarge"):
    return {"items": [
        node("loadgen", loadgen_zone, "c5.metal"),
        node("kong", "us-west-2b", kong_instance),
        node("support", "us-west-2b", "c5.2xlarge"),
    ]}


KONG_DEPLOY = {"spec": {"template": {"spec": {"containers": [
    {"name": "proxy", "image": "kong/kong-ai-gateway-dev:2.3.0-rc.2"}]}}}}
KONG_PODS = {"items": [{"status": {"containerStatuses": [
    {"name": "proxy", "imageID": "docker.io/kong/kong-ai-gateway-dev@sha256:aaa111"}]}}]}
DECL_CM = {"data": {"kong.yml": "_format_version: '3.0'\nservices: []\n"}}
FAKE_DEPLOY = {"spec": {"template": {"spec": {"containers": [
    {"image": "node:20-alpine", "env": [{"name": "FAKE_PROVIDER_LATENCY_PROFILE", "value": "realistic"}]}]}}}}
FAKE_CM = {"data": {"server.js": "console.log('fake')"}}
K6_PODS = {"items": [{"metadata": {"name": "k6-ai-benchmark-1-abcde"},
                      "spec": {"containers": [{"image": "ghcr.io/grafana/k6:0.56.0"}]},
                      "status": {"containerStatuses": [{"imageID": "ghcr.io/grafana/k6@sha256:bbb222"}]}}]}
GIT = {"git_sha": "abc123", "dirty": True}


def raw(**over):
    base = {"nodes": nodes_json(), "kong_deploy": KONG_DEPLOY, "kong_pods": KONG_PODS,
            "kong_version": "Kong Enterprise 3.10.0.0\n", "declarative_cm": DECL_CM,
            "fake_deploy": FAKE_DEPLOY, "fake_script_cm": FAKE_CM, "k6_pods": K6_PODS,
            "context": "arn:aws:eks:us-west-2:1:cluster/kong-perf-x\n"}
    base.update(over)
    return base


class Parsing(unittest.TestCase):
    def test_nodes(self):
        nodes = ce.parse_nodes(nodes_json())
        self.assertEqual([n["role"] for n in nodes], ["kong", "loadgen", "support"])
        kong = nodes[0]
        self.assertEqual((kong["instance_type"], kong["zone"], kong["kernel"], kong["os"]),
                         ("c5.4xlarge", "us-west-2b", "5.10.1", "Amazon Linux 2"))

    def test_kong(self):
        k = ce.parse_kong(KONG_DEPLOY, KONG_PODS, "Kong Enterprise 3.10.0.0\n")
        self.assertEqual(k, {"image": "kong/kong-ai-gateway-dev:2.3.0-rc.2",
                             "image_digest": "sha256:aaa111", "version": "Kong Enterprise 3.10.0.0"})

    def test_k6_version_is_the_tag_unless_the_tag_is_latest(self):
        self.assertEqual(ce.parse_k6(K6_PODS)["version"], "0.56.0")
        latest = json.loads(json.dumps(K6_PODS).replace("k6:0.56.0", "k6:latest"))
        k = ce.parse_k6(latest)
        self.assertEqual(k["version"], "sha256:bbb222")   # a moving tag identifies nothing
        self.assertEqual(k["image"], "ghcr.io/grafana/k6:latest")

    def test_k6_missing_before_the_first_run(self):
        self.assertEqual(ce.parse_k6({"items": []})["version"], None)
        self.assertEqual(ce.parse_k6(None)["image"], None)
        other = {"items": [{"metadata": {"name": "k6-k6-operator-controller-manager-1"}, "spec": {"containers": [{}]}}]}
        self.assertIsNone(ce.parse_k6(other)["version"])

    def test_image_tag(self):
        self.assertEqual(ce.image_tag("repo/x:1.2@sha256:ff"), "1.2")
        self.assertEqual(ce.image_tag("localhost:5000/k6"), None)     # a registry port is not a tag
        self.assertEqual(ce.image_tag("localhost:5000/k6:9"), "9")
        self.assertIsNone(ce.image_tag(None))

    def test_config_hash_is_stable_and_sensitive(self):
        a = ce.config_hash(DECL_CM)
        self.assertEqual(a, ce.config_hash(json.loads(json.dumps(DECL_CM))))
        self.assertNotEqual(a, ce.config_hash({"data": {"kong.yml": "different"}}))
        self.assertIsNone(ce.config_hash(None))
        self.assertIsNone(ce.config_hash({}))

    def test_fake_provider_hash_depends_on_script_and_env(self):
        base = ce.parse_fake_provider(FAKE_DEPLOY, FAKE_CM)["hash"]
        changed_script = ce.parse_fake_provider(FAKE_DEPLOY, {"data": {"server.js": "x"}})["hash"]
        env2 = json.loads(json.dumps(FAKE_DEPLOY))
        env2["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "mlflow50"
        changed_env = ce.parse_fake_provider(env2, FAKE_CM)["hash"]
        self.assertEqual(len({base, changed_script, changed_env}), 3)
        self.assertEqual(ce.parse_fake_provider(FAKE_DEPLOY, FAKE_CM)["env"],
                         {"FAKE_PROVIDER_LATENCY_PROFILE": "realistic"})


class BuildAndMerge(unittest.TestCase):
    def test_build_env_has_every_section(self):
        e = ce.build_env(raw(), "start", GIT, started_at="2026-01-01T00:00:00Z")
        self.assertEqual(e["schema_version"], 1)
        self.assertEqual(e["cluster"], "arn:aws:eks:us-west-2:1:cluster/kong-perf-x")
        self.assertEqual(len(e["nodes"]), 3)
        self.assertTrue(e["config_hash"].startswith("sha256:"))
        self.assertEqual(e["suite"], GIT)
        self.assertEqual(e["collection_errors"], [])

    def test_failed_commands_are_listed_but_do_not_break_the_record(self):
        e = ce.build_env(raw(nodes=None, kong_version=None, k6_pods=None), "start", GIT)
        self.assertEqual(e["nodes"], [])
        self.assertIsNone(e["kong"]["version"])
        self.assertEqual(e["collection_errors"], ["kong_version", "nodes"])   # k6_pods is expected to be absent early

    def test_merge_end_adds_k6_and_reports_nothing_changed_for_a_stable_run(self):
        start = ce.build_env(raw(k6_pods={"items": []}), "start", GIT, started_at="2026-01-01T00:00:00Z")
        end = ce.build_env(raw(), "end", GIT, started_at="2026-01-01T08:00:00Z")
        merged = ce.merge_end(start, end)
        self.assertEqual(merged["phase"], "end")
        self.assertEqual(merged["started_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(merged["finished_at"], "2026-01-01T08:00:00Z")
        self.assertEqual(merged["k6"]["version"], "0.56.0")
        self.assertEqual(merged["changed_during_run"], [])

    def test_merge_end_detects_changes_during_the_run(self):
        start = ce.build_env(raw(), "start", GIT)
        end = ce.build_env(raw(nodes=nodes_json(loadgen_zone="us-west-2c"),
                               declarative_cm={"data": {"kong.yml": "changed"}}), "end", GIT)
        merged = ce.merge_end(start, end)
        self.assertIn("nodes", merged["changed_during_run"])
        self.assertIn("config_hash", merged["changed_during_run"])


class FitsCompareRelease(unittest.TestCase):
    """The collector's field names must be the ones compare_release.env_drift reads."""

    def env(self, **over):
        return ce.build_env(raw(**over), "end", GIT)

    def test_identical_environments_do_not_drift(self):
        self.assertEqual(cr.env_drift(self.env(), self.env())["status"], "ok")

    def test_kong_version_difference_is_ignored(self):
        other = self.env(kong_version="Kong Enterprise 9.9.9.9\n",
                         kong_pods={"items": [{"status": {"containerStatuses": [
                             {"name": "proxy", "imageID": "x@sha256:zzz999"}]}}]})
        self.assertEqual(cr.env_drift(other, self.env())["status"], "ok")

    def test_instance_type_zone_layout_config_and_k6_changes_are_seen(self):
        other = self.env(nodes=nodes_json(loadgen_zone="us-west-2c", kong_instance="c5.2xlarge"),
                         declarative_cm={"data": {"kong.yml": "changed"}},
                         k6_pods=json.loads(json.dumps(K6_PODS).replace("k6:0.56.0", "k6:0.57.0")))
        fields = {d["field"] for d in cr.env_drift(other, self.env())["drift"]}
        self.assertIn("nodes.kong.instance_type", fields)
        self.assertIn("config_hash", fields)
        self.assertIn("k6.version", fields)
        self.assertTrue(any("zone_layout" in f for f in fields))

    def test_fake_provider_config_change_is_seen(self):
        env2 = json.loads(json.dumps(FAKE_DEPLOY))
        env2["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "mlflow50"
        fields = {d["field"] for d in cr.env_drift(self.env(fake_deploy=env2), self.env())["drift"]}
        self.assertIn("fake_provider.hash", fields)


class Cli(unittest.TestCase):
    def make_stub(self, d, k6_present):
        """A kubectl stub that answers every collector command with canned output."""
        answers = {
            "get nodes -o json": json.dumps(nodes_json()),
            "get deploy kong-kong -n kong -o json": json.dumps(KONG_DEPLOY),
            "get pods -n kong -l app.kubernetes.io/name=kong -o json": json.dumps(KONG_PODS),
            "exec -n kong deploy/kong-kong -c proxy -- kong version": "Kong Enterprise 3.10.0.0",
            "get cm kong-declarative -n kong -o json": json.dumps(DECL_CM),
            "get deploy fake-provider -n upstream -o json": json.dumps(FAKE_DEPLOY),
            "get cm fake-provider-script -n upstream -o json": json.dumps(FAKE_CM),
            "get pods -n k6 -o json": json.dumps(K6_PODS if k6_present else {"items": []}),
            "config current-context": "ctx-test",
        }
        stub = Path(d) / "kubectl"
        stub.write_text("#!/usr/bin/env python3\nimport sys, json\nA=" + repr(answers) +
                        "\nk=' '.join(sys.argv[1:])\nif k in A:\n    print(A[k])\nelse:\n    sys.exit(1)\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        return str(stub)

    def run_cli(self, d, phase, k6_present):
        return subprocess.run([sys.executable, "-B", str(K6_TESTS / "collect_env.py"), "--out", str(Path(d) / "env.json"),
                               "--phase", phase, "--kubectl", self.make_stub(d, k6_present), "--repo", str(K6_TESTS)],
                              capture_output=True, text=True)

    def test_start_then_end(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_cli(d, "start", k6_present=False)
            self.assertEqual(r.returncode, 0, r.stderr)
            start = json.loads((Path(d) / "env.json").read_text())
            self.assertEqual(start["phase"], "start")
            self.assertEqual(len(start["nodes"]), 3)
            self.assertIsNone(start["k6"]["version"])
            self.assertEqual(start["cluster"], "ctx-test")
            self.assertEqual(start["collection_errors"], [])

            r = self.run_cli(d, "end", k6_present=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            end = json.loads((Path(d) / "env.json").read_text())
            self.assertEqual((end["phase"], end["k6"]["version"]), ("end", "0.56.0"))
            self.assertEqual(end["changed_during_run"], [])
            self.assertIsNotNone(end["finished_at"])

    def test_a_missing_kubectl_never_fails_the_run(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, "-B", str(K6_TESTS / "collect_env.py"), "--out", str(Path(d) / "env.json"),
                                "--phase", "start", "--kubectl", "/nonexistent/kubectl", "--repo", str(K6_TESTS)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            e = json.loads((Path(d) / "env.json").read_text())
            self.assertEqual(e["nodes"], [])
            self.assertIn("nodes", e["collection_errors"])
            self.assertIn("could not read", r.stdout)


if __name__ == "__main__":
    unittest.main()
