#!/bin/bash

set -euo pipefail

timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo "Infrastructure snapshot: $timestamp"
echo "========================================"
echo

echo "[kong pods]"
kubectl top pod -n kong || true
echo

echo "[upstream pods]"
kubectl top pod -n upstream || true
echo

echo "[k6 pods]"
kubectl top pod -n k6 || true
echo

echo "[node metrics]"
kubectl top node || true
echo

echo "[kong restart summary]"
kubectl get pods -n kong -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.containerStatuses[*]}{.name}{" restart="}{.restartCount}{" lastReason="}{.lastState.terminated.reason}{"\n"}{end}{end}'
echo

echo "[kong worker rss snapshot]"
POD=$(kubectl get pod -n kong -l app.kubernetes.io/instance=kong,app.kubernetes.io/name=kong -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [[ -n "${POD:-}" ]]; then
  kubectl exec -n kong "$POD" -c proxy -- sh -lc "ps -eo pid,rss,comm,args | grep 'nginx: worker process' | grep -v grep" || true
else
  echo "kong pod not found"
fi
echo

echo "[upstream restart summary]"
kubectl get pods -n upstream -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.containerStatuses[*]}{.name}{" restart="}{.restartCount}{" lastReason="}{.lastState.terminated.reason}{"\n"}{end}{end}'
echo

echo "[k6 restart summary]"
kubectl get pods -n k6 -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.containerStatuses[*]}{.name}{" restart="}{.restartCount}{" lastReason="}{.lastState.terminated.reason}{"\n"}{end}{end}'
echo
