#!/bin/bash
# adapters/kimi/poll-driver.sh -- deliver new mailbox rows to a kimi session as
# resume turns.
#
# Kimi has NO hook surface, so push injection is impossible; this driver is the
# poll baseline made hands-free. It runs OUTSIDE the kimi session (a plain
# shell loop the operator or orchestrator starts) and delivers by resuming the
# session:  kimi -r <session> -p "<rows>" --output-format text
# run from the RECORDED cwd, because kimi sessions are directory-bound.
#
# WHAT IS LEFT HERE, AND WHY IT IS SO LITTLE (issues #29, #30). The loop this
# file used to contain -- read, format, invoke, remember what got through -- had
# nothing kimi-specific in it except the invocation, so it now lives once in
# `bin/comms-poll-driver` and this file is the kimi PARAMETERS: the resume
# command, the directory-bound cwd, and the cursor key. The confirmed-delivery
# rule is unchanged and is now enforced by the shared `comms cursor
# take/confirm` pair rather than by a private copy of it here: the cursor
# advances ONLY after a kimi invocation that exited 0, a failed invocation
# re-delivers next poll, and the read is a --replay so the CLI's own
# print-time cursor never competes with this one.
#
# usage: poll-driver.sh <runid> <seat> <kimi-session-id> <cwd>
#                       [--interval <seconds>] [--once]
#   --once   poll a single time and PRINT what would be delivered (no kimi
#            invocation, cursor not advanced). For testing.
#
# Exit: 0 (loop mode runs until killed; --once exits after one poll).

set -uo pipefail

SELF="$0"
while [ -L "$SELF" ]; do
  t="$(readlink "$SELF")"
  case "$t" in /*) SELF="$t" ;; *) SELF="$(dirname "$SELF")/$t" ;; esac
done
SELF_DIR="$(cd "$(dirname "$SELF")" && pwd -P)"      # <repo>/adapters/kimi
REPO_BIN="$(cd "$SELF_DIR/../.." && pwd)/bin"        # <repo>/bin
COMMS="$REPO_BIN/comms"
DRIVER="$REPO_BIN/comms-poll-driver"

usage() {
  echo "usage: poll-driver.sh <runid> <seat> <kimi-session-id> <cwd> [--interval <seconds>] [--once]" >&2
  exit 2
}

[ "$#" -ge 4 ] || usage
RUNID="$1"; SEAT="$2"; SESSION="$3"; CWD="$4"
shift 4
INTERVAL=15
ONCE=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --interval) [ "$#" -ge 2 ] || usage; INTERVAL="$2"; shift 2 ;;
    --once)     ONCE=1; shift ;;
    *)          usage ;;
  esac
done

[ -d "$CWD" ] || { echo "poll-driver: cwd does not exist: $CWD" >&2; exit 1; }

STATE_DIR="${COMMS_STATE_DIR:-$HOME/.comms/state}"
CURSOR_DIR="$STATE_DIR/kimi-cursor"
OLD_CURSOR="$CURSOR_DIR/$RUNID-$SEAT"        # pre-#30: a last-delivered `at`
CURSOR_FILE="$CURSOR_DIR/$RUNID-$SEAT.json"  # now: per-seat row counts

# ---- one-time cursor migration ---------------------------------------------
# The shared helper counts rows per seat; this driver used to store the `at` of
# the last row it delivered. Both mean "everything up to here is delivered", so
# the translation is exact: a seat's count is how many of its rows are at or
# before that timestamp -- the same `> cursor` test the old loop applied, just
# tallied instead of compared. Doing this beats letting the new cursor start at
# zero, which would re-deliver a live session's whole board once. If it cannot
# be done the driver says so and starts from zero: re-delivery is recoverable.
if [ -f "$OLD_CURSOR" ] && [ ! -f "$CURSOR_FILE" ]; then
  mkdir -p "$CURSOR_DIR"
  if "$COMMS" read "$RUNID" "$SEAT" --replay --subs | python3 -c '
import json, sys
last_at = sys.argv[1]
counts = {}
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    row = json.loads(line)
    if (row.get("at") or "") <= last_at:
        counts[row.get("seat", "?")] = counts.get(row.get("seat", "?"), 0) + 1
print(json.dumps(counts, sort_keys=True))
' "$(cat "$OLD_CURSOR")" > "$CURSOR_FILE.tmp.$$"; then
    mv "$CURSOR_FILE.tmp.$$" "$CURSOR_FILE"
    mv "$OLD_CURSOR" "$OLD_CURSOR.pre-counts"   # kept, not deleted: evidence
  else
    rm -f "$CURSOR_FILE.tmp.$$"
    echo "poll-driver: could not migrate the timestamp cursor at $OLD_CURSOR; starting from zero, expect one replay" >&2
  fi
fi

# ---- delegate --------------------------------------------------------------
# --once maps to --once --dry-run because this adapter's --once has always
# meant "show me what would go, invoke nothing"; the generic driver splits
# those two ideas, so a real single poll is `--once` alone there.
ARGS=("$RUNID" "$SEAT" --subs --cursor "$CURSOR_FILE" --cwd "$CWD" --interval "$INTERVAL")
[ "$ONCE" -eq 0 ] || ARGS=("${ARGS[@]}" --once --dry-run)

# NOTE: -p combines with neither -y nor --auto (see README.md). The rows are
# substituted for {} on the argv array, never through a shell.
exec "$DRIVER" "${ARGS[@]}" -- kimi -r "$SESSION" -p '{}' --output-format text
