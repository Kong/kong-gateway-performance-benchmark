#!/usr/bin/env python3
"""
Recreate all Kong services, routes, and plugins for local benchmark testing.
Run: python3 kong_setup.py
"""
import json, urllib.request, urllib.error, sys

ADMIN = "http://localhost:8001"
TOKEN = "handyshake"
FAKE  = "http://172.19.0.1:8081"
UPSTREAM = "http://172.19.0.1:8080"


def req(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request(
        f"{ADMIN}{path}", data=data, method=method,
        headers={"Kong-Admin-Token": TOKEN, "Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(r) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        # 409 = already exists, treat as OK
        if e.code == 409:
            return {"_conflict": True}
        print(f"  ERROR {e.code}: {err[:200]}", file=sys.stderr)
        return None


def create_service(name, host, port=80, protocol="http"):
    print(f"  service: {name} → {host}:{port}")
    return req("POST", "/services", {
        "name": name, "host": host, "port": port, "protocol": protocol,
        "connect_timeout": 60000, "read_timeout": 60000, "write_timeout": 60000
    })


def create_route(name, service_name, path, methods=None):
    print(f"  route:   {name} → {path}")
    return req("POST", f"/services/{service_name}/routes", {
        "name": name,
        "paths": [path],
        "methods": methods or ["POST", "GET"],
        "protocols": ["https"],
        "strip_path": False,
    })


def add_plugin(route_name, config):
    print(f"  plugin:  {route_name} → ai-proxy-advanced")
    return req("POST", f"/routes/{route_name}/plugins", {
        "name": "ai-proxy-advanced",
        "config": config
    })


def chat_target(url, model="mock-gpt-4o-mini", log_payloads=False, log_statistics=False):
    return {
        "route_type": "llm/v1/chat",
        "model": {"provider": "openai", "name": model, "options": {"upstream_url": url}},
        "auth": {"allow_override": False},
        "logging": {"log_payloads": log_payloads, "log_statistics": log_statistics},
    }


def base_balancer(algorithm="round-robin", retries=1, **kwargs):
    b = {
        "algorithm": algorithm,
        "retries": retries,
        "slots": 1000,
        "connect_timeout": 60000,
        "read_timeout": 60000,
        "write_timeout": 60000,
        "tokens_count_strategy": "total-tokens",
    }
    b.update(kwargs)
    return b


# ─── Services ────────────────────────────────────────────────────────────────
print("\n[1/3] Creating services...")
create_service("fake-chat-mock",   "172.19.0.1", 8081)
create_service("ai-upstream-mock", "172.19.0.1", 8080)

# ─── Routes + Plugins ────────────────────────────────────────────────────────
print("\n[2/3] Creating routes and plugins...")

# 1. ai-chat baseline (uses ai_upstream, static OpenAI mock)
create_route("ai-chat", "ai-upstream-mock", "/ai-chat")
add_plugin("ai-chat", {
    "llm_format": "openai",
    "genai_category": "text/generation",
    "response_streaming": "allow",
    "max_request_body_size": 1048576,
    "model_name_header": True,
    "balancer": base_balancer(),
    "targets": [chat_target(f"{UPSTREAM}/v1/chat/completions")],
})

# 2. token-chat-openai
create_route("bench-token-chat", "fake-chat-mock", "/bench/token/chat/openai")
add_plugin("bench-token-chat", {
    "llm_format": "openai",
    "genai_category": "text/generation",
    "response_streaming": "allow",
    "max_request_body_size": 10485760,
    "model_name_header": True,
    "balancer": base_balancer(),
    "targets": [chat_target(f"{FAKE}/v1/chat/completions")],
})

# 3. stream-openai
create_route("bench-stream-openai", "fake-chat-mock", "/bench/token/stream/openai")
add_plugin("bench-stream-openai", {
    "llm_format": "openai",
    "genai_category": "text/generation",
    "response_streaming": "allow",
    "max_request_body_size": 10485760,
    "model_name_header": True,
    "balancer": base_balancer(),
    "targets": [chat_target(f"{FAKE}/v1/chat/completions")],
})

# 4. stream-gemini
create_route("bench-gemini-stream", "fake-chat-mock", "/bench/stream/gemini")
add_plugin("bench-gemini-stream", {
    "llm_format": "gemini",
    "genai_category": "text/generation",
    "response_streaming": "always",
    "max_request_body_size": 10485760,
    "model_name_header": True,
    "balancer": base_balancer(),
    "targets": [{
        "route_type": "llm/v1/chat",
        "model": {
            "provider": "gemini",
            "name": "mock-gemini-2.5-flash",
            "options": {"upstream_url": f"{FAKE}/v1beta/models/mock-gemini:streamGenerateContent"}
        },
        "auth": {"allow_override": False},
        "logging": {"log_payloads": False, "log_statistics": False},
    }],
})

# 5. embeddings-openai
create_route("bench-embeddings", "fake-chat-mock", "/bench/token/embeddings/openai")
add_plugin("bench-embeddings", {
    "llm_format": "openai",
    "genai_category": "text/embeddings",
    "response_streaming": "deny",
    "max_request_body_size": 10485760,
    "model_name_header": True,
    "balancer": base_balancer(),
    "targets": [{
        "route_type": "llm/v1/embeddings",
        "model": {
            "provider": "openai",
            "name": "text-embedding-3-small",
            "options": {"upstream_url": f"{FAKE}/v1/embeddings"}
        },
        "auth": {"allow_override": False},
        "logging": {"log_payloads": False, "log_statistics": False},
    }],
})

# 6. static-chat (ai_upstream returns OpenAI format)
create_route("bench-static-chat", "ai-upstream-mock", "/bench/static/chat")
add_plugin("bench-static-chat", {
    "llm_format": "openai",
    "genai_category": "text/generation",
    "response_streaming": "deny",
    "max_request_body_size": 1048576,
    "model_name_header": True,
    "balancer": base_balancer(),
    "targets": [chat_target(f"{UPSTREAM}/v1/chat/completions")],
})

# 7. large-prompt
create_route("bench-large-prompt", "fake-chat-mock", "/bench/large/prompt")
add_plugin("bench-large-prompt", {
    "llm_format": "openai",
    "genai_category": "text/generation",
    "response_streaming": "allow",
    "max_request_body_size": 10485760,
    "model_name_header": True,
    "balancer": base_balancer(),
    "targets": [chat_target(f"{FAKE}/v1/chat/completions")],
})

# 8. large-response
create_route("bench-large-response", "fake-chat-mock", "/bench/large/response")
add_plugin("bench-large-response", {
    "llm_format": "openai",
    "genai_category": "text/generation",
    "response_streaming": "allow",
    "max_request_body_size": 10485760,
    "model_name_header": True,
    "balancer": base_balancer(),
    "targets": [chat_target(f"{FAKE}/v1/chat/completions")],
})

# 9. routing: round-robin 2
create_route("bench-routing-roundrobin-2", "fake-chat-mock", "/bench/routing/roundrobin/2")
add_plugin("bench-routing-roundrobin-2", {
    "llm_format": "openai",
    "genai_category": "text/generation",
    "response_streaming": "deny",
    "max_request_body_size": 1048576,
    "model_name_header": True,
    "balancer": base_balancer(),
    "targets": [
        chat_target(f"{FAKE}/v1/chat/completions"),
        chat_target(f"{FAKE}/v1/chat/completions"),
    ],
})

# 10. routing: round-robin 10
create_route("bench-routing-roundrobin-10", "fake-chat-mock", "/bench/routing/roundrobin/10")
add_plugin("bench-routing-roundrobin-10", {
    "llm_format": "openai",
    "genai_category": "text/generation",
    "response_streaming": "deny",
    "max_request_body_size": 1048576,
    "model_name_header": True,
    "balancer": base_balancer(),
    "targets": [
        chat_target(f"{FAKE}/v1/chat/completions")
        for _ in range(10)
    ],
})

# 11. routing: ewma (lowest-latency)
create_route("bench-routing-ewma", "fake-chat-mock", "/bench/routing/ewma")
add_plugin("bench-routing-ewma", {
    "llm_format": "openai",
    "genai_category": "text/generation",
    "response_streaming": "deny",
    "max_request_body_size": 1048576,
    "model_name_header": True,
    "balancer": base_balancer(algorithm="lowest-latency", latency_strategy="tpot"),
    "targets": [
        chat_target(f"{FAKE}/v1/chat/completions"),
        chat_target(f"{FAKE}/v1/chat/completions"),
    ],
})

# 12. routing: failover (primary returns 500, fallback succeeds)
create_route("bench-routing-failover", "fake-chat-mock", "/bench/routing/failover")
add_plugin("bench-routing-failover", {
    "llm_format": "openai",
    "genai_category": "text/generation",
    "response_streaming": "deny",
    "max_request_body_size": 1048576,
    "model_name_header": True,
    "balancer": {
        **base_balancer(retries=2, connect_timeout=5000, read_timeout=10000, write_timeout=10000),
        "failover_criteria": ["error", "timeout", "http_500", "http_502", "http_503", "http_504"],
    },
    "targets": [
        chat_target(f"{FAKE}/v1/chat/completions?status=500"),
        chat_target(f"{FAKE}/v1/chat/completions"),
    ],
})

# 13. payload-logging
create_route("bench-logging-chat", "fake-chat-mock", "/bench/logging/chat/openai")
add_plugin("bench-logging-chat", {
    "llm_format": "openai",
    "genai_category": "text/generation",
    "response_streaming": "deny",
    "max_request_body_size": 8388608,
    "model_name_header": True,
    "balancer": base_balancer(),
    "targets": [chat_target(f"{FAKE}/v1/chat/completions", log_payloads=True, log_statistics=True)],
})

# 14. policy-auth
create_route("bench-policy-auth", "fake-chat-mock", "/bench/policy/auth/openai")
add_plugin("bench-policy-auth", {
    "llm_format": "openai",
    "genai_category": "text/generation",
    "response_streaming": "allow",
    "max_request_body_size": 1048576,
    "model_name_header": True,
    "balancer": base_balancer(),
    "targets": [chat_target(f"{FAKE}/v1/chat/completions")],
})

# 15. policy-cache (semantic cache)
create_route("bench-policy-cache", "fake-chat-mock", "/bench/policy/cache/openai")
add_plugin("bench-policy-cache", {
    "llm_format": "openai",
    "genai_category": "text/generation",
    "response_streaming": "allow",
    "max_request_body_size": 1048576,
    "model_name_header": True,
    "balancer": base_balancer(),
    "targets": [chat_target(f"{FAKE}/v1/chat/completions")],
})

# ─── Verify ──────────────────────────────────────────────────────────────────
print("\n[3/3] Verifying...")
routes = req("GET", "/routes?size=100")
count = len(routes.get("data", []))
print(f"  Total routes in Kong: {count}")
for r in sorted(routes.get("data", []), key=lambda x: x["name"]):
    print(f"    {r['name']:40s} {r['paths']}")
print("\nDone!")
