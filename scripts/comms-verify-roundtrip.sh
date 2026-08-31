#!/bin/bash
# comms-verify-roundtrip.sh -- POSITIVE CONTROL for a comms install.
#
# It does not check that files exist. Files existing is what the broken
# duplicate install already had: it was complete, it passed its own suite, and
# it still delivered nothing anyone read. This script proves the only property
# that matters -- A MESSAGE POSTED THROUGH THIS INSTALL COMES BACK OUT OF IT --
# by posting a unique passphrase to a throwaway runid in a throwaway
# COMMS_ROOT and reading it back through the install's own CLI.
#
# ISOLATION IS BY CONSTRUCTION. COMMS_ROOT and COMMS_STATE_DIR both point at a
# fresh mktemp dir, and the runid carries this process's pid, so a pass writes
# nothing into the live board and re-running is green every time. Nothing here
# touches /tmp's real comms-* directories or ~/.comms/state.
#
# NEGATIVE CONTROL. Before trusting the pass, it asserts that a runid nobody
# posted to reads back EMPTY. Without that, a verifier that printed the
# passphrase from its own arguments would look identical to a working mailbox.
#
# usage: comms-verify-roundtrip.sh [--install <dir>] [--keep]
#        --install  the checkout to test (default: the repo this script is in)
#        --keep     leave the temp dir behind and print it
#
# exit 0 = round-trip proven | 1 = round-trip FAILED | 2 = could not run the
# test at all (NOT a pass).

set -uo pipefail

INSTALL=""
KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --install) INSTALL="${2:-}"; shift 2 ;;
    --keep)    KEEP=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 2 ;;
    *) echo "verify: unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$INSTALL" ]; then
  INSTALL="$(cd "$(dirname "$0")/.." && pwd -P)"
fi

fail()   { echo "verify: FAILED: $*" >&2; exit 1; }
cannot() { echo "verify: COULD NOT VERIFY (exit 2 is NOT a pass): $*" >&2; exit 2; }

# Locate the dispatcher in either install shape.
if   [ -x "$INSTALL/bin/comms" ];       then CLI="$INSTALL/bin/comms"
elif [ -x "$INSTALL/comms/bin/comms" ]; then CLI="$INSTALL/comms/bin/comms"
else cannot "no executable comms dispatcher under $INSTALL (looked at bin/comms and comms/bin/comms)"
fi

command -v python3 >/dev/null 2>&1 || cannot "python3 not on PATH"

TMP="$(mktemp -d -t comms-verify)" || cannot "mktemp failed"
cleanup() { [ "$KEEP" -eq 1 ] || rm -rf "$TMP"; }
trap cleanup EXIT

export COMMS_ROOT="$TMP/root"
export COMMS_STATE_DIR="$TMP/state"
# Clear the pre-extraction env names too: if either is set in this shell, it
# is not consulted (COMMS_* wins) but leaving them set invites a reader to
# believe the wrong root was under test.
unset CLAUDE_SWARM_ROOT SWARM_ARM_STATE_DIR
mkdir -p "$COMMS_ROOT" "$COMMS_STATE_DIR" || cannot "cannot create temp roots under $TMP"

RUNID="verify-roundtrip-$$-$(date +%s)"
PASSPHRASE="ROUNDTRIP-$$-$(date +%s)-$(od -An -N3 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')"
[ -n "$PASSPHRASE" ] || cannot "could not build a unique passphrase"

echo "verify: install     $INSTALL"
echo "verify: cli         $CLI"
echo "verify: COMMS_ROOT  $COMMS_ROOT   (throwaway)"
echo "verify: runid       $RUNID        (throwaway)"

# ---- negative control FIRST -----------------------------------------------
# An unrelated runid must read back nothing. If this prints rows, the reader
# is not scoped to the runid and every later assertion is meaningless.
NEG_RUNID="$RUNID-negative"
"$CLI" init "$NEG_RUNID" >/dev/null 2>&1
NEG_OUT="$("$CLI" read "$NEG_RUNID" verifier 2>/dev/null)"
if printf '%s' "$NEG_OUT" | grep -Fq "$PASSPHRASE"; then
  fail "NEGATIVE CONTROL failed: a runid nobody posted to returned the passphrase. The reader is not scoped and a pass here would prove nothing"
fi
echo "verify: negative control ok (empty runid reads back no passphrase)"

# ---- the round trip --------------------------------------------------------
if ! "$CLI" init "$RUNID" >/dev/null 2>&1; then
  cannot "'comms init $RUNID' failed under $CLI; the mailbox could not be created, which is a setup failure, not a delivery result"
fi

"$CLI" post "$RUNID" writer finding "$PASSPHRASE" --topic verify
POST_RC=$?
[ "$POST_RC" -eq 0 ] || fail "'comms post' exited $POST_RC"

READ_OUT="$("$CLI" read "$RUNID" reader 2>&1)"
READ_RC=$?
[ "$READ_RC" -eq 0 ] || fail "'comms read' exited $READ_RC; output was: $READ_OUT"

if ! printf '%s' "$READ_OUT" | grep -Fq "$PASSPHRASE"; then
  echo "verify: read output was:" >&2
  printf '%s\n' "$READ_OUT" >&2
  fail "the passphrase was posted but did not come back. The install accepts writes and produces no reads -- exactly the dead-end-mailbox failure this check exists to catch"
fi
echo "verify: round trip ok (passphrase posted by 'writer' was read by 'reader')"

# ---- prove the row is on disk under the throwaway root, not in a buffer ----
BOARD="$COMMS_ROOT/comms-$RUNID"
if [ ! -d "$BOARD" ]; then
  fail "the read succeeded but no board directory exists at $BOARD; the install is writing somewhere other than COMMS_ROOT, which is how a copy ends up talking to a mailbox nobody reads"
fi
ROWS="$(grep -rlF "$PASSPHRASE" "$BOARD" 2>/dev/null | wc -l | tr -d ' ')"
echo "verify: files_carrying_passphrase = $ROWS under $BOARD"
[ "$ROWS" -ge 1 ] || fail "no file under $BOARD carries the passphrase; the round trip did not go through the file mailbox"

echo "verify: PASS -- $INSTALL round-trips a message end to end"
[ "$KEEP" -eq 1 ] && echo "verify: temp kept at $TMP"
exit 0
