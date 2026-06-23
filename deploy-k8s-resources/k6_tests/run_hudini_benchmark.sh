#!/bin/bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./run_hudini_benchmark.sh <scenario> [fixture] [load] [duration]

Scenarios:
  openai-stream      OpenAI-compatible streaming chat on /v1/chat/completions
  openai-chat        OpenAI-compatible non-streaming chat on /v1/chat/completions
  anthropic-stream   Anthropic-compatible streaming chat
  anthropic-chat     Anthropic-compatible non-streaming chat
  anthropic-native-stream  Native Anthropic streaming chat on /v1/messages
  anthropic-native-chat    Native Anthropic non-streaming chat on /v1/messages

Environment variables:
  HUDINI_URL               Base URL for the local gateway (default: http://localhost:8888)
  HUDINI_SKIP_PRECHECK     Skip precheck probe if set to true (default: false)
  HUDINI_PRECHECK_TIMEOUT_SEC  curl timeout for precheck in seconds (default: 10)
  K6_BIN                   k6 binary path (default: ~/bin/k6)
  K6_AI_APIKEY             API key passed as Authorization: Bearer ...
  K6_AI_MODEL              Override the model name for the selected profile
  K6_AI_ANTHROPIC_VERSION  Override Anthropic version header
  K6_AI_DURATION           Test duration override

Examples:
  ./run_hudini_benchmark.sh openai-stream short 25 6m
  ./run_hudini_benchmark.sh anthropic-stream short 20 6m
  ./run_hudini_benchmark.sh anthropic-native-stream short 20 6m
  HUDINI_URL=http://localhost:8888 ./run_hudini_benchmark.sh openai-chat
EOF
  exit 1
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" || $# -lt 1 ]]; then
  usage
fi

SCENARIO=$1
FIXTURE=${2:-short}
LOAD=${3:-}
DURATION=${4:-}
HUDINI_URL=${HUDINI_URL:-http://localhost:8888}
K6_BIN=${K6_BIN:-~/bin/k6}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

K6_AI_PROFILE=
K6_AI_STREAM=
K6_AI_CHAT_URL=
K6_AI_RATE=
K6_AI_STREAM_VUS=
K6_AI_DURATION=

resolve_api_key() {
  local candidate="${K6_AI_APIKEY:-${ANTHROPIC_API_KEY:-${OPENAI_API_KEY:-}}}"
  candidate="${candidate#\"}"
  candidate="${candidate%\"}"
  candidate="${candidate#\'}"
  candidate="${candidate%\'}"
  printf '%s' "$candidate"
}

EFFECTIVE_API_KEY=$(resolve_api_key)

run_precheck() {
  local url=$1
  local profile_name=$2
  local timeout_sec=${HUDINI_PRECHECK_TIMEOUT_SEC:-10}
  local model_name
  local body_file
  local status

  model_name=${K6_AI_MODEL:-}
  if [[ -z "$model_name" ]]; then
    if [[ "$profile_name" == "anthropic" ]]; then
      model_name="anthropic/claude-haiku-4-5-20251001"
    else
      model_name="openai/mock-gpt-4o-mini"
    fi
  fi

  body_file=$(mktemp)

  local -a curl_cmd
  curl_cmd=(curl -sS -o "$body_file" -w '%{http_code}' --max-time "$timeout_sec" -X POST "$url")
  curl_cmd+=(-H 'Content-Type: application/json')

  if [[ -n "$EFFECTIVE_API_KEY" ]]; then
    curl_cmd+=(-H "Authorization: Bearer $EFFECTIVE_API_KEY")
  fi

  if [[ "$profile_name" == "anthropic" ]]; then
    curl_cmd+=(-H "anthropic-version: ${K6_AI_ANTHROPIC_VERSION:-2023-06-01}")
  fi

  curl_cmd+=(-d "{\"model\":\"$model_name\",\"max_tokens\":64,\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":\"precheck\"}]}")

  if ! status=$("${curl_cmd[@]}"); then
    echo "Precheck failed: curl transport error for $url"
    rm -f "$body_file"
    return 1
  fi

  if [[ "$status" -lt 200 || "$status" -ge 300 ]]; then
    echo "Precheck failed: HTTP $status from $url"
    echo "Precheck response (first 300 chars):"
    head -c 300 "$body_file" || true
    echo
    rm -f "$body_file"
    return 1
  fi

  rm -f "$body_file"
  echo "Precheck passed: $url"
  return 0
}

case "$SCENARIO" in
  openai-stream)
    K6_AI_PROFILE="openai"
    K6_AI_STREAM="true"
    K6_AI_CHAT_URL="$HUDINI_URL/v1/chat/completions"
    K6_AI_STREAM_VUS=${LOAD:-25}
    K6_AI_DURATION=${DURATION:-6m}
    ;;
  openai-chat)
    K6_AI_PROFILE="openai"
    K6_AI_STREAM="false"
    K6_AI_CHAT_URL="$HUDINI_URL/v1/chat/completions"
    K6_AI_RATE=${LOAD:-25}
    K6_AI_DURATION=${DURATION:-6m}
    ;;
  anthropic-stream)
    K6_AI_PROFILE="anthropic"
    K6_AI_STREAM="true"
    K6_AI_CHAT_URL="$HUDINI_URL/v1/chat/completions"
    K6_AI_STREAM_VUS=${LOAD:-25}
    K6_AI_DURATION=${DURATION:-6m}
    ;;
  anthropic-chat)
    K6_AI_PROFILE="anthropic"
    K6_AI_STREAM="false"
    K6_AI_CHAT_URL="$HUDINI_URL/v1/chat/completions"
    K6_AI_RATE=${LOAD:-25}
    K6_AI_DURATION=${DURATION:-6m}
    ;;
  anthropic-native-stream)
    K6_AI_PROFILE="anthropic"
    K6_AI_STREAM="true"
    K6_AI_CHAT_URL="$HUDINI_URL/v1/messages"
    K6_AI_STREAM_VUS=${LOAD:-25}
    K6_AI_DURATION=${DURATION:-6m}
    ;;
  anthropic-native-chat)
    K6_AI_PROFILE="anthropic"
    K6_AI_STREAM="false"
    K6_AI_CHAT_URL="$HUDINI_URL/v1/messages"
    K6_AI_RATE=${LOAD:-25}
    K6_AI_DURATION=${DURATION:-6m}
    ;;
  *)
    echo "Unknown scenario: $SCENARIO"
    usage
    ;;
esac

echo "SCENARIO=$SCENARIO"
echo "FIXTURE=$FIXTURE"
echo "K6_AI_PROFILE=$K6_AI_PROFILE"
echo "K6_AI_STREAM=$K6_AI_STREAM"
echo "K6_AI_CHAT_URL=$K6_AI_CHAT_URL"
echo "K6_AI_RATE=${K6_AI_RATE:-}"
echo "K6_AI_STREAM_VUS=${K6_AI_STREAM_VUS:-}"
echo "K6_AI_DURATION=$K6_AI_DURATION"

if [[ "${HUDINI_SKIP_PRECHECK:-false}" != "true" ]]; then
  run_precheck "$K6_AI_CHAT_URL" "$K6_AI_PROFILE"
else
  echo "Warning: skipping precheck because HUDINI_SKIP_PRECHECK=true"
fi

cd "$SCRIPT_DIR"

env \
  K6_AI_PROFILE="$K6_AI_PROFILE" \
  K6_AI_STREAM="$K6_AI_STREAM" \
  K6_AI_CHAT_URL="$K6_AI_CHAT_URL" \
  K6_AI_RATE="${K6_AI_RATE:-25}" \
  K6_AI_STREAM_VUS="${K6_AI_STREAM_VUS:-25}" \
  K6_AI_DURATION="$K6_AI_DURATION" \
  K6_AI_FIXTURE="$FIXTURE" \
  K6_AI_MODEL="${K6_AI_MODEL:-}" \
  K6_AI_APIKEY="$EFFECTIVE_API_KEY" \
  K6_AI_ANTHROPIC_VERSION="${K6_AI_ANTHROPIC_VERSION:-}" \
  "$K6_BIN" run k6_ai_hudini_chat.js