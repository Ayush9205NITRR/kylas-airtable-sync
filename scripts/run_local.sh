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
# Usage — no setup needed, the first run builds .venv and installs deps itself:
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

# Build the venv on first run so there is no separate setup step. macOS ships
# python3 but no `python`/`pip` on PATH, which is why `pip install -r ...`
# fails there with "command not found" — `python3 -m pip` always works.
if [ ! -x ".venv/bin/python3" ]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found. On a Mac install it with: brew install python3"
    exit 1
  fi
  echo "First run: creating .venv and installing dependencies (~1 min)..."
  python3 -m venv .venv || { echo "ERROR: could not create .venv"; exit 1; }
  .venv/bin/python3 -m pip install --quiet --upgrade pip
  .venv/bin/python3 -m pip install --quiet -r requirements.txt \
    || { echo "ERROR: dependency install failed"; exit 1; }
  echo "Setup done."
fi
# Address the venv interpreter directly rather than relying on `activate`, so
# this also works from cron, where PATH is minimal and no shell profile is read.
PY=".venv/bin/python3"

if [ ! -f .env ]; then
  echo "ERROR: .env missing. Create it in $(pwd) with these four lines"
  echo "       (same values as the GitHub Actions secrets of the same name):"
  echo ""
  echo "         KYLAS_API_KEY=..."
  echo "         AIRTABLE_PAT=..."
  echo "         AIRTABLE_BASE_ID=..."
  echo "         AIRTABLE_COMPANY_BASE_ID=..."
  echo ""
  echo "       .env is gitignored, so it never gets committed."
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
