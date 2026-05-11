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

echo "[upstream restart summary]"
kubectl get pods -n upstream -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.containerStatuses[*]}{.name}{" restart="}{.restartCount}{" lastReason="}{.lastState.terminated.reason}{"\n"}{end}{end}'
echo

echo "[k6 restart summary]"
kubectl get pods -n k6 -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.containerStatuses[*]}{.name}{" restart="}{.restartCount}{" lastReason="}{.lastState.terminated.reason}{"\n"}{end}{end}'
echo
