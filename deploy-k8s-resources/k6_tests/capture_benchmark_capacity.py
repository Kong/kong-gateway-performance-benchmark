#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from decimal import Decimal


CPU_UNITS = {
    "n": Decimal("0.000001"),
    "u": Decimal("0.001"),
    "m": Decimal("1"),
    "": Decimal("1000"),
    "k": Decimal("1000000"),
}

MEMORY_UNITS = {
    "Ki": Decimal("0.0009765625"),
    "Mi": Decimal("1"),
    "Gi": Decimal("1024"),
    "Ti": Decimal("1048576"),
    "Pi": Decimal("1073741824"),
    "Ei": Decimal("1099511627776"),
    "K": Decimal("0.00095367431640625"),
    "M": Decimal("0.95367431640625"),
    "G": Decimal("976.5625"),
    "T": Decimal("1000000"),
    "P": Decimal("1000000000"),
    "E": Decimal("1000000000000"),
    "": Decimal("0.00000095367431640625"),
}


def kubectl_json(args: list[str]) -> dict | None:
    result = subprocess.run(
        ["kubectl", *args, "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def parse_cpu_m(raw: str | None) -> Decimal:
    if not raw:
        return Decimal("0")

    raw = raw.strip()
    for suffix in ("n", "u", "m", "k"):
        if raw.endswith(suffix):
            return Decimal(raw[: -len(suffix)]) * CPU_UNITS[suffix]

    return Decimal(raw) * CPU_UNITS[""]


def parse_memory_mi(raw: str | None) -> Decimal:
    if not raw:
        return Decimal("0")

    raw = raw.strip()
    for suffix in ("Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "K", "M", "G", "T", "P", "E"):
        if raw.endswith(suffix):
            return Decimal(raw[: -len(suffix)]) * MEMORY_UNITS[suffix]

    return Decimal(raw) * MEMORY_UNITS[""]


def format_decimal(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.01"))
    if normalized == normalized.to_integral():
        return str(int(normalized))
    return format(normalized.normalize(), "f")


def first_pod_name(namespace: str, selector: str) -> str:
    data = kubectl_json(["get", "pods", "-n", namespace, "-l", selector])
    items = data.get("items", []) if data else []
    return items[0]["metadata"]["name"] if items else ""


def latest_runner_pod_name() -> str:
    data = kubectl_json(["get", "pods", "-n", "k6"])
    items = data.get("items", []) if data else []
    candidates = [
        item
        for item in items
        if item.get("metadata", {}).get("name", "").startswith("k6-ai-benchmark-1-")
    ]
    candidates.sort(key=lambda item: item.get("metadata", {}).get("creationTimestamp", ""))
    return candidates[-1]["metadata"]["name"] if candidates else ""


def pod_info(namespace: str, pod_name: str) -> dict[str, str]:
    if not pod_name:
        return {}

    data = kubectl_json(["get", "pod", "-n", namespace, pod_name])
    if not data:
        return {}

    cpu_request = Decimal("0")
    cpu_limit = Decimal("0")
    memory_request = Decimal("0")
    memory_limit = Decimal("0")

    for container in data.get("spec", {}).get("containers", []):
        resources = container.get("resources", {})
        requests = resources.get("requests", {})
        limits = resources.get("limits", {})
        cpu_request += parse_cpu_m(requests.get("cpu"))
        cpu_limit += parse_cpu_m(limits.get("cpu"))
        memory_request += parse_memory_mi(requests.get("memory"))
        memory_limit += parse_memory_mi(limits.get("memory"))

    return {
        "POD_NAME": pod_name,
        "NODE_NAME": data.get("spec", {}).get("nodeName", ""),
        "POD_CPU_REQUEST_M": format_decimal(cpu_request),
        "POD_CPU_LIMIT_M": format_decimal(cpu_limit),
        "POD_MEMORY_REQUEST_MI": format_decimal(memory_request),
        "POD_MEMORY_LIMIT_MI": format_decimal(memory_limit),
    }


def node_info(node_name: str) -> dict[str, str]:
    if not node_name:
        return {}

    data = kubectl_json(["get", "node", node_name])
    if not data:
        return {}

    allocatable = data.get("status", {}).get("allocatable", {})
    return {
        "NODE_NAME": node_name,
        "NODE_ALLOCATABLE_CPU_M": format_decimal(parse_cpu_m(allocatable.get("cpu"))),
        "NODE_ALLOCATABLE_MEMORY_MI": format_decimal(parse_memory_mi(allocatable.get("memory"))),
    }


def emit(prefix: str, values: dict[str, str]) -> None:
    for key, value in values.items():
        print(f"{prefix}_{key}={shlex.quote(value)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner-pod", default="")
    args = parser.parse_args()

    kong_pod = first_pod_name("kong", "app.kubernetes.io/instance=kong,app.kubernetes.io/name=kong")
    static_pod = first_pod_name("upstream", "app=static-openai-mock")
    fake_provider_pod = first_pod_name("upstream", "app=fake-provider")
    wiremock_pod = first_pod_name("upstream", "app=wiremock")
    runner_pod = args.runner_pod or latest_runner_pod_name()

    domains = {
        "KONG": pod_info("kong", kong_pod),
        "STATIC_OPENAI_MOCK": pod_info("upstream", static_pod),
        "FAKE_PROVIDER": pod_info("upstream", fake_provider_pod),
        "WIREMOCK": pod_info("upstream", wiremock_pod),
        "K6_RUNNER": pod_info("k6", runner_pod),
    }

    for prefix, pod_values in domains.items():
        emit(prefix, pod_values)
        emit(prefix, node_info(pod_values.get("NODE_NAME", "")))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
