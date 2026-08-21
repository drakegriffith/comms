#!/bin/bash
# test_comms_cli.sh -- smoke test for the comms dispatcher (bin/comms).
#
# Proves, per subcommand, that the wrapper reaches the RIGHT module and
# PRESERVES that module's exit code. All state is isolated: COMMS_STATE_DIR
# (read by swarm_arm.py and, through it, swarm_claims.py) and COMMS_ROOT
# (read by swarm_mailbox.py) both point at mktemp dirs, so nothing here touches
# real arm state, real mailboxes, or the real heartbeat's cursors.
#
# Exit: 0 all passed, 1 any failed. Prints a passed/failed count either way.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"        # <repo>/tests
COMMS="$SELF_DIR/../bin/comms"

export COMMS_STATE_DIR="$(mktemp -d)"
export COMMS_ROOT="$(mktemp -d)"
trap 'rm -rf "$COMMS_STATE_DIR" "$COMMS_ROOT"' EXIT

PASS=0
FAIL=0

# check <desc> <expected_rc> <actual_rc> [output] [required_substring]
check() {
  local desc="$1" want="$2" got="$3" out="${4-}" need="${5-}"
  if [ "$got" -ne "$want" ]; then
    echo "FAIL: $desc (rc=$got, wanted $want)"
    FAIL=$((FAIL + 1))
    return
  fi
  if [ -n "$need" ] && ! printf '%s' "$out" | grep -qF "$need"; then
    echo "FAIL: $desc (rc ok, output missing: $need)"
    FAIL=$((FAIL + 1))
    return
  fi
  echo "ok:   $desc"
  PASS=$((PASS + 1))
}

RUN="commstest-$$"

# ---- wrapper's own contract ------------------------------------------------
out="$("$COMMS" no-such-subcommand 2>&1)"; rc=$?
check "unknown subcommand exits 2 with usage" 2 "$rc" "$out" "usage: comms"
out="$("$COMMS" 2>&1)"; rc=$?
check "no arguments exits 2 with usage" 2 "$rc" "$out" "usage: comms"

# ---- mailbox routing (swarm_mailbox.py honors COMMS_ROOT) ------------------
out="$("$COMMS" init "$RUN" 2>&1)"; rc=$?
check "init reaches mailbox, honors COMMS_ROOT" 0 "$rc" "$out" "$COMMS_ROOT/comms-$RUN"
out="$("$COMMS" post "$RUN" alpha bogus-kind "x" 2>&1)"; rc=$?
check "post invalid kind preserves mailbox exit 1" 1 "$rc" "$out" "invalid kind"
out="$("$COMMS" post "$RUN" alpha finding "hello-topic" --topic t1 2>&1)"; rc=$?
check "post valid finding exits 0" 0 "$rc" "$out" "hello-topic"
out="$("$COMMS" read "$RUN" beta --topic t1 2>&1)"; rc=$?
check "read from sibling seat sees the row" 0 "$rc" "$out" "hello-topic"
out="$("$COMMS" subscribe "$RUN" beta t1 2>&1)"; rc=$?
check "subscribe exits 0, echoes topic set" 0 "$rc" "$out" '"t1"'
"$COMMS" post "$RUN" alpha finding "off-slice" --topic other >/dev/null 2>&1
out="$("$COMMS" subs "$RUN" beta 2>&1)"; rc=$?
if [ $rc -eq 0 ] && printf '%s' "$out" | grep -qF "hello-topic" \
   && ! printf '%s' "$out" | grep -qF "off-slice"; then
  echo "ok:   subs returns subscribed slice only"; PASS=$((PASS + 1))
else
  echo "FAIL: subs returns subscribed slice only (rc=$rc)"; FAIL=$((FAIL + 1))
fi
out="$("$COMMS" read "$RUN" 2>&1)"; rc=$?
check "read with missing seat preserves mailbox exit 1" 1 "$rc" "$out" "read needs"

# ---- claims routing BEFORE arming (the not-armed edge) ---------------------
out="$("$COMMS" claim "$RUN" alpha /tmp/x 2>&1)"; rc=$?
check "claim on unarmed run preserves claims exit 3" 3 "$rc" "$out" "not-armed"
out="$("$COMMS" enroll "$RUN" 2>&1)"; rc=$?
check "enroll on unarmed run preserves arm exit 1" 1 "$rc" "$out" "not-armed"

# ---- arm routing (swarm_arm.py honors COMMS_STATE_DIR) ---------------------
out="$("$COMMS" arm "$RUN" --topic t1 2>&1)"; rc=$?
check "arm exits 0, honors COMMS_STATE_DIR" 0 "$rc" "$out" "$COMMS_STATE_DIR/swarm-arm/$RUN"
out="$("$COMMS" status "$RUN" 2>&1)"; rc=$?
check "status of armed run exits 0, reports armed" 0 "$rc" "$out" '"armed": true'
out="$("$COMMS" status 2>&1)"; rc=$?
check "bare status lists armed runs" 0 "$rc" "$out" "$RUN"
out="$("$COMMS" enroll "$RUN" --agent-id agent-a --topics t1 --seat alpha 2>&1)"; rc=$?
check "enroll with agent-id on armed run exits 0" 0 "$rc" "$out" "enrolled"
out="$("$COMMS" enroll "$RUN" 2>&1)"; rc=$?
check "marker-only enroll on armed run exits 0" 0 "$rc" "$out" "enrollment signalled"

# ---- claims routing on the armed run ---------------------------------------
out="$("$COMMS" claim "$RUN" alpha /tmp/x 2>&1)"; rc=$?
check "first claim wins, exits 0" 0 "$rc" "$out" "OK"
out="$("$COMMS" claim "$RUN" beta /tmp/x 2>&1)"; rc=$?
check "conflicting claim preserves exit 1, names holder" 1 "$rc" "$out" "HELD BY alpha"
out="$("$COMMS" who "$RUN" /tmp/x 2>&1)"; rc=$?
check "who names the holder" 0 "$rc" "$out" "alpha"
out="$("$COMMS" who 2>&1)"; rc=$?
check "who with no runid preserves claims usage exit 2" 2 "$rc" "$out" "usage: swarm_claims"
out="$("$COMMS" release "$RUN" beta /tmp/x 2>&1)"; rc=$?
check "release by non-owner refused, exit 1" 1 "$rc" "$out" "REFUSED"
out="$("$COMMS" release "$RUN" alpha /tmp/x 2>&1)"; rc=$?
check "release by owner exits 0" 0 "$rc" "$out" "RELEASED"
out="$("$COMMS" reap "$RUN" alpha 2>&1)"; rc=$?
check "reap of nothing preserves exit 1" 1 "$rc" "$out" "reaped 0"
"$COMMS" claim "$RUN" alpha /tmp/y >/dev/null 2>&1
out="$("$COMMS" reap "$RUN" alpha 2>&1)"; rc=$?
check "reap of a held claim exits 0" 0 "$rc" "$out" "/tmp/y"

# ---- disarm -----------------------------------------------------------------
out="$("$COMMS" disarm "$RUN" 2>&1)"; rc=$?
check "disarm exits 0" 0 "$rc"
out="$("$COMMS" status "$RUN" 2>&1)"; rc=$?
check "status after disarm reports unarmed" 0 "$rc" "$out" '"armed": false'

# ---- symlink invocation: PATH installs are symlinks, and dirname of the ----
# symlink points at the wrong tree unless the wrapper chases readlink first
# (defect caught live by the verifier seat, run comms-build-0821)
LINKDIR="$(mktemp -d)"
ln -s "$COMMS" "$LINKDIR/comms"
out="$("$LINKDIR/comms" status "$RUN" 2>&1)"; rc=$?
check "symlinked invocation still finds lib/" 0 "$rc" "$out" '"armed": false'
rm -rf "$LINKDIR"

# ---- isolation control: nothing leaked outside the temp dirs ---------------
if [ -e "$HOME/.comms/state/swarm-arm/$RUN" ] || [ -e "/tmp/comms-$RUN" ]; then
  echo "FAIL: state leaked outside the temp dirs"; FAIL=$((FAIL + 1))
else
  echo "ok:   no state outside COMMS_STATE_DIR / COMMS_ROOT"
  PASS=$((PASS + 1))
fi

echo "comms CLI smoke test: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
