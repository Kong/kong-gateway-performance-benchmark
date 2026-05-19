#!/bin/bash

set -euo pipefail

DURATION_SECONDS=${1:-60}
INTERVAL_SECONDS=${2:-5}

POD=$(kubectl get pod -n kong -l app.kubernetes.io/instance=kong,app.kubernetes.io/name=kong -o jsonpath='{.items[0].metadata.name}')

echo "timestamp,worker_count,total_rss_kb,max_worker_rss_kb,min_worker_rss_kb"

END_TIME=$(( $(date +%s) + DURATION_SECONDS ))
while [[ $(date +%s) -lt $END_TIME ]]; do
  OUTPUT=$(kubectl exec -n kong "$POD" -c proxy -- sh -lc "ps -eo pid,rss,comm,args | grep 'nginx: worker process' | grep -v grep" || true)

  if [[ -z "$OUTPUT" ]]; then
    echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ"),0,0,0,0"
    sleep "$INTERVAL_SECONDS"
    continue
  fi

  SUMMARY=$(printf '%s\n' "$OUTPUT" | awk '
    BEGIN { count=0; total=0; max=0; min=0 }
    {
      rss=$2
      total+=rss
      count+=1
      if (count == 1 || rss > max) max=rss
      if (count == 1 || rss < min) min=rss
    }
    END { printf "%d,%d,%d,%d", count, total, max, min }
  ')

  echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ"),$SUMMARY"
  sleep "$INTERVAL_SECONDS"
done
