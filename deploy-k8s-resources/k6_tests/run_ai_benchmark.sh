#!/bin/bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./run_ai_benchmark.sh <scenario> [fixture] [load] [duration]

Load semantics:
  - static-chat, token-chat-openai, embeddings-openai, policy-*:
      load = target request rate in requests/second (RPS)
  - stream-openai, stream-gemini:
      load = target constant concurrent virtual users (VUs), which represent
      active streaming sessions

Scenarios:
  static-chat          WireMock fixed-response comparison
  token-chat-openai    Non-streaming OpenAI-style token benchmark
  stream-openai        Streaming OpenAI-style token benchmark
  stream-gemini        Streaming Gemini-style token benchmark
  embeddings-openai    OpenAI-style embeddings benchmark
  policy-auth-openai   Key-auth overhead benchmark
  policy-rate-limit    Request rate limiting overhead benchmark
  policy-token-budget  AI token budget overhead benchmark
  policy-cache-hit     Semantic cache hit benchmark
  policy-cache-miss    Semantic cache miss benchmark

Examples:
  ./run_ai_benchmark.sh token-chat-openai short 25 6m
  ./run_ai_benchmark.sh stream-openai short 40 6m
  ./run_ai_benchmark.sh embeddings-openai medium 50 6m
EOF
  exit 1
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" || $# -lt 1 ]]; then
  usage
fi

if ! command -v yq >/dev/null 2>&1; then
  echo "Error: yq not found. Install it before running this script."
  exit 1
fi

SCENARIO=$1
FIXTURE=${2:-short}
LOAD=${3:-}
DURATION=${4:-}

RESOURCE_FILENAME=k6-ai-benchmark-test.yaml
NEW_RESOURCE_FILENAME="${RESOURCE_FILENAME%.yaml}-temp.yaml"
TAG_NAME="${SCENARIO}-$(date +%s)"

SCRIPT_FILE=
K6_AI_CHAT_URL=
K6_AI_RATE=
K6_AI_STREAM_VUS=
K6_AI_DURATION=
K6_AI_PRE_ALLOCATED_VUS=
K6_AI_MAX_VUS=
K6_AI_APIKEY=
K6_AI_SCENARIO_NAME=
K6_AI_CACHE_MODE=
K6_AI_MODEL=
LOAD_KIND=

case "$SCENARIO" in
  static-chat)
    SCRIPT_FILE="k6_ai_static_chat.js"
    K6_AI_CHAT_URL="https://kong-kong-proxy.kong.svc.cluster.local/bench/static/chat"
    K6_AI_RATE=${LOAD:-25}
    K6_AI_DURATION=${DURATION:-3m}
    K6_AI_PRE_ALLOCATED_VUS=50
    K6_AI_MAX_VUS=200
    K6_AI_STREAM_VUS=25
    LOAD_KIND="rps"
    ;;
  token-chat-openai)
    SCRIPT_FILE="k6_ai_token_chat.js"
    K6_AI_CHAT_URL="https://kong-kong-proxy.kong.svc.cluster.local/bench/token/chat/openai"
    K6_AI_RATE=${LOAD:-25}
    K6_AI_DURATION=${DURATION:-6m}
    K6_AI_PRE_ALLOCATED_VUS=50
    K6_AI_MAX_VUS=200
    K6_AI_STREAM_VUS=25
    LOAD_KIND="rps"
    ;;
  stream-openai)
    SCRIPT_FILE="k6_ai_stream_openai.js"
    K6_AI_CHAT_URL="https://kong-kong-proxy.kong.svc.cluster.local/bench/token/stream/openai"
    K6_AI_RATE=25
    K6_AI_STREAM_VUS=${LOAD:-30}
    K6_AI_DURATION=${DURATION:-6m}
    K6_AI_PRE_ALLOCATED_VUS=50
    K6_AI_MAX_VUS=200
    LOAD_KIND="vus"
    ;;
  stream-gemini)
    SCRIPT_FILE="k6_ai_stream_gemini.js"
    K6_AI_CHAT_URL="https://kong-kong-proxy.kong.svc.cluster.local/bench/token/stream/gemini/models/mock-gemini-2.5-flash:streamGenerateContent"
    K6_AI_RATE=25
    K6_AI_STREAM_VUS=${LOAD:-30}
    K6_AI_DURATION=${DURATION:-6m}
    K6_AI_PRE_ALLOCATED_VUS=50
    K6_AI_MAX_VUS=200
    LOAD_KIND="vus"
    ;;
  embeddings-openai)
    SCRIPT_FILE="k6_ai_embeddings.js"
    K6_AI_CHAT_URL="https://kong-kong-proxy.kong.svc.cluster.local/bench/token/embeddings/openai"
    K6_AI_RATE=${LOAD:-40}
    K6_AI_DURATION=${DURATION:-6m}
    K6_AI_PRE_ALLOCATED_VUS=50
    K6_AI_MAX_VUS=200
    K6_AI_STREAM_VUS=25
    LOAD_KIND="rps"
    ;;
  policy-auth-openai)
    SCRIPT_FILE="k6_ai_policy_chat.js"
    K6_AI_CHAT_URL="https://kong-kong-proxy.kong.svc.cluster.local/bench/policy/auth/openai"
    K6_AI_RATE=${LOAD:-25}
    K6_AI_DURATION=${DURATION:-6m}
    K6_AI_PRE_ALLOCATED_VUS=50
    K6_AI_MAX_VUS=200
    K6_AI_STREAM_VUS=25
    K6_AI_APIKEY="ai-benchmark-policy-key"
    K6_AI_SCENARIO_NAME="policy-auth-openai"
    LOAD_KIND="rps"
    ;;
  policy-rate-limit)
    SCRIPT_FILE="k6_ai_policy_chat.js"
    K6_AI_CHAT_URL="https://kong-kong-proxy.kong.svc.cluster.local/bench/policy/rate-limit/openai"
    K6_AI_RATE=${LOAD:-25}
    K6_AI_DURATION=${DURATION:-6m}
    K6_AI_PRE_ALLOCATED_VUS=50
    K6_AI_MAX_VUS=200
    K6_AI_STREAM_VUS=25
    K6_AI_SCENARIO_NAME="policy-rate-limit-openai"
    LOAD_KIND="rps"
    ;;
  policy-token-budget)
    SCRIPT_FILE="k6_ai_policy_chat.js"
    K6_AI_CHAT_URL="https://kong-kong-proxy.kong.svc.cluster.local/bench/policy/token-budget/openai"
    K6_AI_RATE=${LOAD:-25}
    K6_AI_DURATION=${DURATION:-6m}
    K6_AI_PRE_ALLOCATED_VUS=50
    K6_AI_MAX_VUS=200
    K6_AI_STREAM_VUS=25
    K6_AI_SCENARIO_NAME="policy-token-budget-openai"
    LOAD_KIND="rps"
    ;;
  policy-cache-hit)
    SCRIPT_FILE="k6_ai_policy_semantic_cache.js"
    K6_AI_CHAT_URL="https://kong-kong-proxy.kong.svc.cluster.local/bench/policy/cache/openai"
    K6_AI_RATE=${LOAD:-25}
    K6_AI_DURATION=${DURATION:-6m}
    K6_AI_PRE_ALLOCATED_VUS=50
    K6_AI_MAX_VUS=200
    K6_AI_STREAM_VUS=25
    K6_AI_SCENARIO_NAME="policy-semantic-cache-hit"
    K6_AI_CACHE_MODE="hit"
    LOAD_KIND="rps"
    ;;
  policy-cache-miss)
    SCRIPT_FILE="k6_ai_policy_semantic_cache.js"
    K6_AI_CHAT_URL="https://kong-kong-proxy.kong.svc.cluster.local/bench/policy/cache/openai"
    K6_AI_RATE=${LOAD:-25}
    K6_AI_DURATION=${DURATION:-6m}
    K6_AI_PRE_ALLOCATED_VUS=50
    K6_AI_MAX_VUS=200
    K6_AI_STREAM_VUS=25
    K6_AI_SCENARIO_NAME="policy-semantic-cache-miss"
    K6_AI_CACHE_MODE="miss"
    LOAD_KIND="rps"
    ;;
  *)
    echo "Unknown scenario: $SCENARIO"
    usage
    ;;
esac

K6_AI_PRE_ALLOCATED_VUS=${K6_AI_PRE_ALLOCATED_VUS_OVERRIDE:-$K6_AI_PRE_ALLOCATED_VUS}
K6_AI_MAX_VUS=${K6_AI_MAX_VUS_OVERRIDE:-$K6_AI_MAX_VUS}
K6_AI_CHAT_URL=${K6_AI_CHAT_URL_OVERRIDE:-$K6_AI_CHAT_URL}
K6_AI_MODEL=${K6_AI_MODEL_OVERRIDE:-$K6_AI_MODEL}

echo "SCENARIO=$SCENARIO"
echo "FIXTURE=$FIXTURE"
echo "SCRIPT_FILE=$SCRIPT_FILE"
echo "K6_AI_CHAT_URL=$K6_AI_CHAT_URL"
echo "LOAD_KIND=$LOAD_KIND"
echo "K6_AI_RATE=$K6_AI_RATE"
echo "K6_AI_STREAM_VUS=$K6_AI_STREAM_VUS"
echo "K6_AI_DURATION=$K6_AI_DURATION"
echo "K6_AI_PRE_ALLOCATED_VUS=$K6_AI_PRE_ALLOCATED_VUS"
echo "K6_AI_MAX_VUS=$K6_AI_MAX_VUS"
echo "K6_AI_MODEL=$K6_AI_MODEL"

if [[ -f "$NEW_RESOURCE_FILENAME" ]]; then
  kubectl delete -n k6 --ignore-not-found=true --wait=true -f "$NEW_RESOURCE_FILENAME" || true
fi

kubectl delete testrun.k6.io -n k6 k6-ai-benchmark --ignore-not-found=true --wait=true || true

yq -M eval-all ".spec.script.configMap.file = \"$SCRIPT_FILE\" |
  .spec.arguments = \"--tag testid=$TAG_NAME --tag benchmark=aigw-v2 --tag scenario=$SCENARIO --tag fixture=$FIXTURE\" |
  (.spec.runner.env[] | select(.name == \"K6_AI_CHAT_URL\").value) |= \"$K6_AI_CHAT_URL\" |
  (.spec.runner.env[] | select(.name == \"K6_AI_FIXTURE\").value) |= \"$FIXTURE\" |
  (.spec.runner.env[] | select(.name == \"K6_AI_RATE\").value) |= \"$K6_AI_RATE\" |
  (.spec.runner.env[] | select(.name == \"K6_AI_DURATION\").value) |= \"$K6_AI_DURATION\" |
  (.spec.runner.env[] | select(.name == \"K6_AI_PRE_ALLOCATED_VUS\").value) |= \"$K6_AI_PRE_ALLOCATED_VUS\" |
  (.spec.runner.env[] | select(.name == \"K6_AI_MAX_VUS\").value) |= \"$K6_AI_MAX_VUS\" |
  (.spec.runner.env[] | select(.name == \"K6_AI_STREAM_VUS\").value) |= \"$K6_AI_STREAM_VUS\" |
  (.spec.runner.env[] | select(.name == \"K6_AI_MODEL\").value) |= \"$K6_AI_MODEL\" |
  (.spec.runner.env[] | select(.name == \"K6_AI_APIKEY\").value) |= \"$K6_AI_APIKEY\" |
  (.spec.runner.env[] | select(.name == \"K6_AI_SCENARIO_NAME\").value) |= \"$K6_AI_SCENARIO_NAME\" |
  (.spec.runner.env[] | select(.name == \"K6_AI_CACHE_MODE\").value) |= \"$K6_AI_CACHE_MODE\"" "$RESOURCE_FILENAME" > "$NEW_RESOURCE_FILENAME"

kubectl apply -n k6 -f "$NEW_RESOURCE_FILENAME"

echo "Started benchmark run for scenario '$SCENARIO'."
echo "Validation commands:"
echo "  kubectl get pods -n k6"
echo "  kubectl logs -n k6 -l k6_cr=k6-ai-benchmark --all-containers=true"
