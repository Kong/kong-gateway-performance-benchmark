#!/bin/bash

set -euo pipefail

usage() {
  echo "Usage: ./run_ai_chat_baseline.sh [K6_AI_CHAT_URL] [K6_AI_RATE] [K6_AI_DURATION] [K6_AI_PRE_ALLOCATED_VUS] [K6_AI_MAX_VUS]"
  echo "Defaults:"
  echo "  K6_AI_CHAT_URL          https://kong-kong-proxy.kong.svc.cluster.local/ai-chat"
  echo "  K6_AI_RATE              25"
  echo "  K6_AI_DURATION          6m"
  echo "  K6_AI_PRE_ALLOCATED_VUS 50"
  echo "  K6_AI_MAX_VUS           200"
  exit 1
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
fi

check_yq() {
  if ! command -v yq >/dev/null 2>&1; then
    echo "Error: yq not found. Install it before running this script."
    exit 1
  fi
}

check_yq

K6_AI_CHAT_URL=${1:-https://kong-kong-proxy.kong.svc.cluster.local/ai-chat}
K6_AI_RATE=${2:-25}
K6_AI_DURATION=${3:-6m}
K6_AI_PRE_ALLOCATED_VUS=${4:-50}
K6_AI_MAX_VUS=${5:-200}

RESOURCE_FILENAME=k6-ai-chat-test.yaml
TAG_PREFIX="ai-chat-baseline"
TAG_NAME="$TAG_PREFIX-$(date +%s)"
NEW_RESOURCE_FILENAME="${RESOURCE_FILENAME%.yaml}-temp.yaml"

echo "RESOURCE_FILENAME=$RESOURCE_FILENAME"
echo "TAG_NAME=$TAG_NAME"
echo "K6_AI_CHAT_URL=$K6_AI_CHAT_URL"
echo "K6_AI_RATE=$K6_AI_RATE"
echo "K6_AI_DURATION=$K6_AI_DURATION"
echo "K6_AI_PRE_ALLOCATED_VUS=$K6_AI_PRE_ALLOCATED_VUS"
echo "K6_AI_MAX_VUS=$K6_AI_MAX_VUS"

kubectl delete -n k6 --ignore-not-found=true --wait=true -f "$NEW_RESOURCE_FILENAME" || true

yq eval-all ".spec.script.configMap.file = \"k6_ai_chat_baseline.js\" |
  .spec.arguments = \"--tag testid=$TAG_NAME --tag benchmark=aigw-phase1 --tag scenario=ai-chat-baseline\" |
  (.spec.runner.env[] | select(.name == \"K6_AI_CHAT_URL\").value) |= \"$K6_AI_CHAT_URL\" |
  (.spec.runner.env[] | select(.name == \"K6_AI_RATE\").value) |= \"$K6_AI_RATE\" |
  (.spec.runner.env[] | select(.name == \"K6_AI_DURATION\").value) |= \"$K6_AI_DURATION\" |
  (.spec.runner.env[] | select(.name == \"K6_AI_PRE_ALLOCATED_VUS\").value) |= \"$K6_AI_PRE_ALLOCATED_VUS\" |
  (.spec.runner.env[] | select(.name == \"K6_AI_MAX_VUS\").value) |= \"$K6_AI_MAX_VUS\"" "$RESOURCE_FILENAME" > "$NEW_RESOURCE_FILENAME"

kubectl apply -n k6 -f "$NEW_RESOURCE_FILENAME"

echo "Started AI chat baseline run. Watch it with:"
echo "  kubectl get pods -n k6"
echo "  kubectl logs -n k6 -f job/$(yq '.metadata.name' "$NEW_RESOURCE_FILENAME")-initializer"
