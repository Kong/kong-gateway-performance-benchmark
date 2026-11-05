#!/usr/bin/env python3
"""
collect_env.py — record the test environment of a release run as env.json.

Why: a gateway-overhead number of ~1 ms can be moved by things that are not the
release (a node in another availability zone, another instance type, another
kernel, a different k6 or fake-provider config). compare_release.py reads the
env.json of both runs and warns about drift; the PDF report prints it.

The run is recorded twice by run_release_baseline.sh:
  --phase start   everything that exists before the first run (nodes, Kong, configs)
  --phase end     adds what only exists after it (the k6 runner image) and lists the
                  fields that changed while the run was going on

Collection is best effort: a failing kubectl command leaves that field null and is
listed in "collection_errors"; it never fails the run.

env.json
  schema_version, phase, cluster, started_at, finished_at
  kong            image, image_digest, version
  k6              image, image_digest, version   (tag; the digest when the tag is "latest")
  config_hash     sha256 of the kong-declarative ConfigMap data
  fake_provider   image, hash (script + env), env
  nodes[]         role, instance_type, zone, region, kernel, os, kubelet
  suite           git_sha, dirty
  changed_during_run, collection_errors

Usage:
  python3 collect_env.py --out results/releases/<dir>/env.json --phase start|end
"""
import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
ROLE_LABEL = "benchmark.konghq.com/node-role"
TIMEOUT_SECONDS = 60

# the kubectl commands, by name, so tests can feed canned output for each
COMMANDS = {
    "nodes": ["get", "nodes", "-o", "json"],
    "kong_deploy": ["get", "deploy", "kong-kong", "-n", "kong", "-o", "json"],
    "kong_pods": ["get", "pods", "-n", "kong", "-l", "app.kubernetes.io/name=kong", "-o", "json"],
    "kong_version": ["exec", "-n", "kong", "deploy/kong-kong", "-c", "proxy", "--", "kong", "version"],
    "declarative_cm": ["get", "cm", "kong-declarative", "-n", "kong", "-o", "json"],
    "fake_deploy": ["get", "deploy", "fake-provider", "-n", "upstream", "-o", "json"],
    "fake_script_cm": ["get", "cm", "fake-provider-script", "-n", "upstream", "-o", "json"],
    "k6_pods": ["get", "pods", "-n", "k6", "-o", "json"],
    "context": ["config", "current-context"],
}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_of(obj):
    return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def image_tag(image):
    """'repo:tag' / 'repo:tag@sha256:...' -> tag, or None when untagged."""
    if not image:
        return None
    name = image.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else None


def version_of(image, digest):
    """The tag when it identifies a version, else the digest (a 'latest' tag moves)."""
    tag = image_tag(image)
    if tag and tag != "latest":
        return tag
    return digest


def digest_of(image_id):
    if image_id and "@" in image_id:
        return image_id.split("@", 1)[1]
    return image_id or None


def _containers(deploy, name=None):
    cs = (((deploy or {}).get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []
    if name:
        cs = [c for c in cs if c.get("name") == name] or cs
    return cs


def parse_nodes(nodes_json):
    out = []
    for item in (nodes_json or {}).get("items", []):
        meta, status = item.get("metadata", {}), item.get("status", {})
        labels, info = meta.get("labels", {}), status.get("nodeInfo", {})
        out.append({
            "role": labels.get(ROLE_LABEL),
            "instance_type": labels.get("node.kubernetes.io/instance-type"),
            "zone": labels.get("topology.kubernetes.io/zone"),
            "region": labels.get("topology.kubernetes.io/region"),
            "kernel": info.get("kernelVersion"),
            "os": info.get("osImage"),
            "kubelet": info.get("kubeletVersion"),
        })
    return sorted(out, key=lambda n: (str(n["role"]), str(n["zone"])))


def parse_kong(deploy_json, pods_json, version_text):
    containers = _containers(deploy_json, "proxy")
    image = containers[0].get("image") if containers else None
    digest = None
    for pod in (pods_json or {}).get("items", []):
        for cs in pod.get("status", {}).get("containerStatuses", []):
            if cs.get("name") == "proxy":
                digest = digest_of(cs.get("imageID"))
                break
        if digest:
            break
    version = (version_text or "").strip() or None
    return {"image": image, "image_digest": digest, "version": version}


def parse_k6(pods_json):
    """The k6 runner pod only exists once a run has started, so this is filled in the end phase."""
    for pod in (pods_json or {}).get("items", []):
        if not pod.get("metadata", {}).get("name", "").startswith("k6-ai-benchmark-"):
            continue
        spec_c = (pod.get("spec") or {}).get("containers") or [{}]
        image = spec_c[0].get("image")
        statuses = pod.get("status", {}).get("containerStatuses") or [{}]
        digest = digest_of(statuses[0].get("imageID"))
        return {"image": image, "image_digest": digest, "version": version_of(image, digest)}
    return {"image": None, "image_digest": None, "version": None}


def parse_fake_provider(deploy_json, script_cm_json):
    containers = _containers(deploy_json)
    image = containers[0].get("image") if containers else None
    env = {e["name"]: e.get("value") for e in (containers[0].get("env") or []) if "name" in e} if containers else {}
    script = ((script_cm_json or {}).get("data")) or {}
    return {"image": image, "env": env, "hash": sha256_of({"script": script, "env": env})} \
        if (script or env or image) else {"image": None, "env": {}, "hash": None}


def config_hash(cm_json):
    data = (cm_json or {}).get("data")
    return sha256_of(data) if data else None


def build_env(raw, phase, git_info, started_at=None, finished_at=None):
    """Assemble env.json from the raw command outputs (strings/parsed JSON, None when a command failed)."""
    errors = [name for name, value in raw.items() if value is None and name != "k6_pods"]
    env = {
        "schema_version": SCHEMA_VERSION,
        "phase": phase,
        "cluster": (raw.get("context") or "").strip() or None,
        "started_at": started_at or now(),
        "finished_at": finished_at,
        "kong": parse_kong(raw.get("kong_deploy"), raw.get("kong_pods"), raw.get("kong_version")),
        "k6": parse_k6(raw.get("k6_pods")),
        "config_hash": config_hash(raw.get("declarative_cm")),
        "fake_provider": parse_fake_provider(raw.get("fake_deploy"), raw.get("fake_script_cm")),
        "nodes": parse_nodes(raw.get("nodes")),
        "suite": git_info,
        "changed_during_run": [],
        "collection_errors": sorted(errors),
    }
    return env


def merge_end(start_env, end_env):
    """The end-phase record: keep the start record, add the k6 runner, and list what changed."""
    merged = dict(start_env)
    merged["phase"] = "end"
    merged["finished_at"] = end_env["started_at"]
    if end_env["k6"].get("version"):
        merged["k6"] = end_env["k6"]
    changed = []
    for field in ("config_hash",):
        if start_env.get(field) != end_env.get(field):
            changed.append(field)
    for field in ("kong", "fake_provider"):
        key = "image_digest" if field == "kong" else "hash"
        if (start_env.get(field) or {}).get(key) != (end_env.get(field) or {}).get(key):
            changed.append(f"{field}.{key}")
    if start_env.get("nodes") != end_env.get("nodes"):
        changed.append("nodes")
    merged["changed_during_run"] = changed
    merged["collection_errors"] = sorted(set(start_env.get("collection_errors", [])) |
                                         set(end_env.get("collection_errors", [])))
    return merged


def git_info(repo_dir):
    def git(*args):
        r = subprocess.run(["git", "-C", str(repo_dir), *args], capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else None
    sha = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    return {"git_sha": sha, "dirty": None if status is None else bool(status)}


def run_kubectl(args, kubectl="kubectl"):
    try:
        r = subprocess.run([kubectl, *args], capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def collect_raw(kubectl="kubectl"):
    raw = {}
    for name, args in COMMANDS.items():
        out = run_kubectl(args, kubectl)
        if out is not None and name not in ("kong_version", "context"):
            try:
                out = json.loads(out)
            except ValueError:
                out = None
        raw[name] = out
    return raw


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--phase", choices=("start", "end"), required=True)
    ap.add_argument("--kubectl", default="kubectl")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent))
    args = ap.parse_args(argv)

    out = Path(args.out)
    env = build_env(collect_raw(args.kubectl), args.phase, git_info(args.repo))
    if args.phase == "end" and out.exists():
        try:
            env = merge_end(json.loads(out.read_text()), env)
        except ValueError:
            pass
    out.write_text(json.dumps(env, indent=2) + "\n")
    errs = env["collection_errors"]
    print(f"env.json ({args.phase}): {len(env['nodes'])} nodes, kong={env['kong']['version']}, "
          f"k6={env['k6']['version']}" + (f", could not read: {', '.join(errs)}" if errs else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
