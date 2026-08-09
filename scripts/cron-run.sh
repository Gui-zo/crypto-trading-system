#!/usr/bin/env bash
# Run a platform CLI command from cron.
#
#   scripts/cron-run.sh health-check
#   scripts/cron-run.sh dashboard
#
# Cron gives us a bare environment and no working directory, so this script:
#   * resolves the repo root from its own location and cd's there (the CLI reads
#     .env relative to the working directory);
#   * uses the project virtualenv's interpreter directly (no uv on cron's PATH —
#     and the system python is 3.11 with an incompatible pydantic);
#   * forces UTC, because funding settles on UTC boundaries and a host-local
#     daily window would silently straddle two funding days (ADR-0005);
#   * takes a per-command lock so a slow run never overlaps the next tick;
#   * appends timestamped output to data/logs/<command>.log (data/ is gitignored).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
export PLATFORM_RUN_SOURCE=CRON
export TZ=UTC

if [[ $# -lt 1 ]]; then
  echo "usage: $(basename "$0") <cli-command> [args...]" >&2
  exit 2
fi

COMMAND="$1"
PYTHON="$REPO_ROOT/.venv/bin/python"
LOG_DIR="$REPO_ROOT/data/logs"
LOG_FILE="$LOG_DIR/${COMMAND}.log"
LOCK_FILE="$LOG_DIR/${COMMAND}.lock"

mkdir -p "$LOG_DIR"

if [[ ! -x "$PYTHON" ]]; then
  echo "$(date -Is) FATAL: interpreter not found at $PYTHON (run 'uv sync')" >>"$LOG_FILE"
  exit 1
fi

status=0
{
  echo "$(date -Is) === start: $* ==="
  # -n: skip this tick entirely if the previous run is still going. Two
  # schedulers writing the same database both append evidence and double-count.
  if flock -n 9; then
    if "$PYTHON" apps/cli/main.py "$@" 2>&1; then
      echo "$(date -Is) === ok: $* ==="
    else
      status=$?
      echo "$(date -Is) === FAILED (exit $status): $* ==="
    fi
  else
    echo "$(date -Is) === skipped: previous run still in progress ==="
  fi
} >>"$LOG_FILE" 2>&1 9>"$LOCK_FILE"

# Propagate failure so cron (and any MAILTO) sees a non-zero exit.
exit "$status"
