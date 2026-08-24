#!/bin/bash
# adapters/remote/install.sh -- preflight + wiring instructions for the remote
# sync adapter. Deliberately writes NOTHING (trivially idempotent): it proves
# the path works and prints the commands, the same shape as
# adapters/discord/install.sh.
#
# WHY A PREFLIGHT AT ALL: this adapter's whole premise is one fact about
# ANOTHER machine -- that non-interactive ssh reaches it and finds a comms
# checkout there. That premise is cheap to measure and expensive to assume, so
# it is measured here rather than discovered at 6am by a poll loop.
#
# Checks (each asserts a POSITIVE result; none infers success from silence):
#   * python3 present
#   * sync.py present beside this script
#   * ssh -o BatchMode=yes reaches $COMMS_REMOTE_HOST without a prompt
#   * $COMMS_REMOTE_BIN exists on that host AND answers a real subcommand
#
# Exit: 0 ready | 2 could not reach the hub / remote CLI missing | 1 local break.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
SYNC="$SELF_DIR/sync.py"
HOST="${COMMS_REMOTE_HOST:-studio}"
# The default remote bin is READ FROM sync.py, never restated here: a default
# spelled in two files is a default that drifts, and this one is load-bearing
# (it is a tilde path on purpose -- see sync._ssh).
REMOTE_BIN="$(python3 -c "import sys; sys.path.insert(0, '$SELF_DIR'); import sync; print(sync.remote_bin())")" \
  || { echo "install: FAILED: could not import sync.py" >&2; exit 1; }
TIMEOUT="${COMMS_REMOTE_SSH_TIMEOUT:-10}"

command -v python3 >/dev/null || { echo "install: FAILED: python3 not found" >&2; exit 1; }
[ -f "$SYNC" ] || { echo "install: FAILED: missing $SYNC" >&2; exit 1; }
command -v ssh >/dev/null || { echo "install: FAILED: ssh not found" >&2; exit 1; }

if ! ssh -o BatchMode=yes -o ConnectTimeout="$TIMEOUT" "$HOST" true 2>/dev/null; then
  cat >&2 <<EOF
install: cannot reach hub "$HOST" non-interactively.

  Checked: ssh -o BatchMode=yes -o ConnectTimeout=$TIMEOUT $HOST true

  BatchMode=yes fails rather than prompting, on purpose: a poll loop that can
  block on a password prompt is a poll loop that silently stops.

  Fix one of:
    * wrong alias      -> set COMMS_REMOTE_HOST=<alias from ~/.ssh/config>
    * no key installed -> ssh-copy-id $HOST   (then re-run this script)
    * host asleep/off  -> wake it; this adapter needs the hub reachable ONCE
                          to verify, though it queues fine while it is not.
EOF
  exit 2
fi

# Assert the remote CLI ANSWERS, never merely that a path string exists: a
# file that is present but unrunnable (wrong perms, missing python3 over
# there) fails exactly the same way as a missing one, and only one of those
# is fixed by re-cloning.
remote_probe="$(ssh -o BatchMode=yes -o ConnectTimeout="$TIMEOUT" "$HOST" \
  "$REMOTE_BIN status" 2>&1)"
if [ $? -ne 0 ]; then
  cat >&2 <<EOF
install: reached "$HOST", but its comms CLI did not answer.

  Ran:  $REMOTE_BIN status
  Got:  $remote_probe

  Fix: set COMMS_REMOTE_BIN to the hub's checkout path (its \$HOME is very
  likely a different username than this machine's), or clone the comms repo
  there. NOTHING from this branch needs to be deployed to the hub -- the
  remote side is `comms post` and `comms read`, stable since PR #4.
EOF
  exit 2
fi

cat <<EOF
remote sync: ready.

  hub host       $HOST
  hub comms      $REMOTE_BIN   (answered: $remote_probe)
  this machine   $(python3 -c 'import os,socket;print(os.environ.get("COMMS_MACHINE_LABEL") or socket.gethostname().split(".")[0])')

Send one row to the hub (queues locally if the hub is offline):
  python3 $SYNC post <runid> <seat> <kind> <text> [--topic <name> | --to <seat>]

Bring the hub's new rows into this machine's mailbox:
  python3 $SYNC pull <runid>

Both, or poll:
  python3 $SYNC sync <runid>
  python3 $SYNC --follow <runid> --interval 30

Exit codes are the channel: 0 delivered/pulled, 1 queued (hub offline, row is
on local disk), 2 could-not-inspect. A pull prints inspected/mirrored/echo
every pass, so a sync that never reached the hub does not read like a quiet hub.

Under launchd, run --follow (it never exits nonzero on an unreachable hub, so
KeepAlive will not crash-loop it) -- same shape as the Discord mirror's job.

Recommended: export COMMS_MACHINE_LABEL to something short. It becomes part of
every seat name this machine exports, and a hostname like
"Christophers-MacBook-Pro-2" is a long thing to read on every row.
EOF
