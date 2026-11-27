#!/bin/bash
# =============================================================================
# run_release_baseline_non_dedicated.sh
#
# Wrapper around run_release_baseline.sh that uses non-dedicated/shared-host
# regression gates by default.
#
# Usage:
#   VERSION=ai-2.0.0-rc.4 ./run_release_baseline_non_dedicated.sh
#   VERSION=... REPEATS=6 DURATION=3m ./run_release_baseline_non_dedicated.sh
#
# Optional override:
#   GATES_FILE=/path/to/custom-gates.yaml ./run_release_baseline_non_dedicated.sh
#
# Matrix selection / ordering env vars are passed straight through to
# run_release_baseline.sh (see its header and release_matrix.yaml):
#   TIER=tier1|tier2  ORDER=interleaved|shuffle|sequential  SEED=N  DRY_RUN=true
#   e.g. DRY_RUN=true VERSION=x REPEATS=6 ./run_release_baseline_non_dedicated.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

GATES_FILE="${GATES_FILE:-$SCRIPT_DIR/release_gates.non_dedicated.yaml}"
if [ ! -f "$GATES_FILE" ]; then
  echo "ERROR: gates file not found: $GATES_FILE" >&2
  exit 2
fi

echo "Using non-dedicated gates: $GATES_FILE"
RELEASE_GATES_PATH="$GATES_FILE" bash "$SCRIPT_DIR/run_release_baseline.sh"
