#!/usr/bin/env bash
# Weekly knowledge-base refresh. Add to crontab:
#   0 3 * * 1  /Users/ahsan/Public/Ripah/Ripha_projects/Riphah_Voice_Agent/scripts/refresh.sh
#
# Fees and deadlines change; a stale KB quotes last year's numbers with this
# year's confidence. --refresh bypasses the HTML cache so pages are re-fetched.
set -euo pipefail

cd "$(dirname "$0")/.."
LOG_DIR="data/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/refresh-$(date +%Y-%m-%d).log"

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

{
  echo "=== refresh started $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  "$PY" -m kb.build --refresh
  echo "=== retrieval checks ==="
  "$PY" eval/run_eval.py --retrieval
  echo "=== refresh finished $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
} 2>&1 | tee "$LOG"

# Keep two months of logs.
find "$LOG_DIR" -name 'refresh-*.log' -mtime +60 -delete 2>/dev/null || true
