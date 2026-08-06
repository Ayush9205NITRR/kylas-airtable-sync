#!/usr/bin/env bash
#
# Run the daily jobs locally, back-to-back — for when GitHub Actions is
# unavailable, or as the entry point for your own cron.
#
# Deliberately SEQUENTIAL, not parallel: every script shares the same Kylas
# API rate limit, and each process paces itself independently. Running them
# concurrently doubles the request rate, triggers far more 429 throttling and
# typically finishes SLOWER than running them one after another.
#
# Usage:
#   ./scripts/run_local.sh              # matrix + account status
#   ./scripts/run_local.sh matrix       # just the BD monthly matrix (~2 min)
#   ./scripts/run_local.sh status       # just the account health sweep (~10-25 min)
#
# On a Mac, wrap it so the machine cannot sleep mid-run:
#   caffeinate -i ./scripts/run_local.sh
#
# Logs land in logs/ with timestamps; each job's exit status is reported at
# the end, and one failing job does not stop the other.

set -uo pipefail
cd "$(dirname "$0")/.."

mkdir -p logs

# Prefer the venv interpreter so this works from cron, where PATH is minimal
# and an activated shell environment does not exist.
if   [ -x ".venv/bin/python3" ]; then PY=".venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then PY="python3"
else echo "ERROR: python3 not found. Run: python3 -m venv .venv && .venv/bin/python3 -m pip install -r requirements.txt"; exit 1
fi

if [ ! -f .env ]; then
  echo "ERROR: .env missing. It must define KYLAS_API_KEY, AIRTABLE_PAT,"
  echo "       AIRTABLE_BASE_ID and AIRTABLE_COMPANY_BASE_ID."
  exit 1
fi

WHICH="${1:-all}"
STAMP="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "=== run_local.sh  ${STAMP}  (mode: ${WHICH}, python: ${PY}) ==="

rc_matrix="skipped"
rc_status="skipped"

run_job () {           # run_job <label> <logfile> <script> [args...]
  local label="$1" log="$2"; shift 2
  echo ""
  echo "--- ${label}: starting $(date -u '+%H:%M:%SZ')  → logs/${log}"
  # tee so progress is visible live AND captured for later inspection
  "$PY" "$@" 2>&1 | tee -a "logs/${log}"
  return "${PIPESTATUS[0]}"      # exit status of python, not of tee
}

if [ "$WHICH" = "all" ] || [ "$WHICH" = "matrix" ]; then
  # Fast one first: it validates credentials in ~2 minutes, so a bad .env
  # fails here instead of 20 minutes into the long job.
  run_job "BD Monthly Matrix" "matrix.log" scripts/bd_monthly_matrix.py \
    && rc_matrix="OK" || rc_matrix="FAILED"
fi

if [ "$WHICH" = "all" ] || [ "$WHICH" = "status" ]; then
  run_job "Account Health sweep" "account_status.log" scripts/push_account_status.py --all \
    && rc_status="OK" || rc_status="FAILED"
fi

echo ""
echo "=== finished $(date -u '+%Y-%m-%dT%H:%M:%SZ') ==="
printf "  BD Monthly Matrix .... %s\n" "$rc_matrix"
printf "  Account Health sweep . %s\n" "$rc_status"
[ "$rc_matrix" = "FAILED" ] || [ "$rc_status" = "FAILED" ] && exit 1
exit 0
