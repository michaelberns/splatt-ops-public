#!/usr/bin/env bash
# morning-sweep.sh: the unattended daily run of the skill harness sweep.
#
# What it does
#   Runs `tools/skill_harness.py sweep` from the repo this script lives in.
#   The sweep re-registers every skill, audits the hard-coded references in
#   them, validates those references against their live sources, and opens
#   a Todoist task for each broken one. Running it before the working day
#   means a broken reference shows up as a task in the morning instead of
#   as a failure in the middle of an ops run.
#
# Exit codes (passed through from the harness)
#   0 clean   1 harness missing or no python3   2 PocketBase down (buffered)
#   3 stale references found
#
# Install (cron, weekday mornings), using the path of the repo copy:
#   crontab -e
#   47 6 * * 1-5 /path/to/splatt-ops/skills/splatt-ss-agent/scripts/morning-sweep.sh
#
# Overrides
#   SKILL_HARNESS_SCRIPT   run a different harness file (used by the tests)
#   SPLATT_ENV_FILE        load a different .env instead of the repo's own
#   SKILL_HARNESS_LOG_DIR  where skill-harness-sweep.log goes (default ~/.claude)

set -euo pipefail

# skills/splatt-ss-agent/scripts/ is three folders below the repo root.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../.." && pwd)"

HARNESS="${SKILL_HARNESS_SCRIPT:-$REPO/tools/skill_harness.py}"
ENV_FILE="${SPLATT_ENV_FILE:-$REPO/.env}"
LOG_DIR="${SKILL_HARNESS_LOG_DIR:-${HOME}/.claude}"
LOG_FILE="${LOG_DIR}/skill-harness-sweep.log"

mkdir -p "$LOG_DIR"

{
  printf '\n============================================================\n'
  printf '[%s] morning sweep starting\n' "$(date '+%Y-%m-%d %H:%M:%S')"
  printf '============================================================\n'
} >> "$LOG_FILE"

if [[ ! -f "$HARNESS" ]]; then
  echo "ERROR: harness script not found at $HARNESS" >> "$LOG_FILE"
  exit 1
fi

# Load the .env if there is one. A missing file is not fatal: the harness
# still re-registers and audits, it just cannot reach Todoist or Telegram.
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
else
  echo "WARN: env file not found at $ENV_FILE, proceeding without Todoist and Telegram" >> "$LOG_FILE"
fi

# Cron's PATH is minimal, so fall back to the usual install locations.
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif [[ -x /usr/bin/python3 ]]; then
  PY=/usr/bin/python3
elif [[ -x /opt/homebrew/bin/python3 ]]; then
  PY=/opt/homebrew/bin/python3
else
  echo "ERROR: no python3 in PATH or known locations" >> "$LOG_FILE"
  exit 1
fi

cd "$(dirname "$HARNESS")"

# `|| RC=$?` keeps `set -e` from ending the script on a non zero exit, so
# the finish line below is written for exit 2 and 3 as well. Those are the
# runs somebody will open the log for.
RC=0
"$PY" "$HARNESS" sweep >> "$LOG_FILE" 2>&1 || RC=$?

printf '[%s] morning sweep finished, exit %d\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$RC" >> "$LOG_FILE"
exit $RC
