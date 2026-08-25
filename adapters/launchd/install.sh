#!/bin/bash
# adapters/launchd/install.sh -- fill in and install the nightly thread-compile
# launchd job (com.comms.thread-compile.plist), same idempotent-check shape as
# adapters/discord/install.sh: existence checks first, exact-match presence
# check before touching anything, exit 2 names a missing prerequisite rather
# than half-installing.
#
# WHAT THIS SCRIPT DOES:
#   1. resolves python3 and this checkout's absolute path to
#      scripts/comms_compile_threads.py
#   2. fills the plist TEMPLATE's four __PLACEHOLDER__ tokens (launchd cannot
#      expand $HOME or resolve a relative path itself -- see the template's
#      own header comment) and writes the result to
#      ~/Library/LaunchAgents/com.comms.thread-compile.plist
#   3. creates ~/Library/Logs/comms/ (StandardOutPath/StandardErrorPath's
#      parent -- launchd does not create it for you, and a job whose log
#      directory is missing fails silently on its first scheduled run)
#   4. prints (does NOT run) the launchctl commands that load it -- see
#      LAUNCHCTL BELOW.
#
# IDEMPOTENT: re-running compares the WOULD-BE file to what's already there
# byte-for-byte before writing (same "exact match, left untouched" shape as
# adapters/claude-code/ambient/install.sh's settings.json wiring) -- a
# checkout moved to a new path, or a python3 upgrade, produces different
# bytes and gets rewritten; an unchanged checkout leaves the installed plist
# (and any already-loaded job) untouched.
#
# LAUNCHCTL IS NEVER RUN BY THIS SCRIPT. The plist is copied into place and
# the two commands that would load/reload it are printed for a human to run:
# a `launchctl bootstrap`/`kickstart` invocation changes THIS machine's
# running launchd state outside the checkout entirely, which is exactly the
# class of side effect adapters/discord/install.sh and adapters/claude-code/
# ambient/install.sh already both refuse to take unattended (the latter prints
# its mirror-following plist rather than loading it, for the same reason).
# A human runs the printed command when ready.
#
# Exit: 0 plist written (or already up to date) | 1 broken | 2 prerequisite
# missing (python3 not found, or the compile script is not in this checkout).

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"          # <repo>/adapters/launchd
REPO_ROOT="$(cd "$SELF_DIR/../.." && pwd)"         # <repo>
TEMPLATE="$SELF_DIR/com.comms.thread-compile.plist"
SCRIPT="$REPO_ROOT/scripts/comms_compile_threads.py"
LABEL="com.comms.thread-compile"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/comms"

fail()   { echo "install: FAILED: $*" >&2; exit 1; }
prereq() { echo "install: PREREQUISITE MISSING: $*" >&2; exit 2; }

[ -f "$TEMPLATE" ] || prereq "missing $TEMPLATE"
[ -f "$SCRIPT" ] || prereq "missing $SCRIPT -- incomplete checkout?"
PYTHON3="$(command -v python3)" || prereq "python3 not found on PATH"

mkdir -p "$LOG_DIR" || fail "could not create $LOG_DIR"

STDOUT_LOG="$LOG_DIR/thread-compile.out.log"
STDERR_LOG="$LOG_DIR/thread-compile.err.log"

rendered="$(
  sed \
    -e "s#__PYTHON3__#$PYTHON3#g" \
    -e "s#__SCRIPT__#$SCRIPT#g" \
    -e "s#__STDOUT_LOG__#$STDOUT_LOG#g" \
    -e "s#__STDERR_LOG__#$STDERR_LOG#g" \
    "$TEMPLATE"
)" || fail "template substitution failed"

if [ -f "$DEST" ] && [ "$(cat "$DEST")" = "$rendered" ]; then
  echo "install: $DEST already up to date, left untouched"
else
  mkdir -p "$(dirname "$DEST")" || fail "could not create $(dirname "$DEST")"
  tmp="$DEST.tmp-$$"
  printf '%s\n' "$rendered" > "$tmp" || fail "could not write $tmp"
  mv -f "$tmp" "$DEST" || fail "could not install $DEST"
  echo "install: wrote $DEST"
fi

cat <<EOF

Plist written. This script does NOT call launchctl -- run these by hand
when ready (a fresh bootstrap fails harmlessly with "service already
loaded" if it's already running; kickstart forces an immediate first run
for testing without waiting for 18:00/01:00):

  launchctl bootstrap gui/\$(id -u) "$DEST"
  launchctl kickstart -k gui/\$(id -u)/$LABEL

Logs land in:
  $STDOUT_LOG
  $STDERR_LOG

Uninstall:
  launchctl bootout gui/\$(id -u)/$LABEL
  rm "$DEST"
EOF
