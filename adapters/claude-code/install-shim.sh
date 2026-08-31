#!/bin/bash
# install-shim.sh -- place the loud heartbeat shim at the stable hook path.
#
# This is a SEPARATE, idempotent installer, not part of adapters/claude-code/
# install.sh. It is separate on purpose: the shim is only needed by setups
# whose settings.json points at $HOME/.claude/hooks/swarm-heartbeat.sh (the
# pre-extraction wiring). A fresh install wires the repo script by absolute
# path and needs no shim at all. Call this from install.sh when that
# entrypoint's owner wants it; until then it stands alone.
#
# What it places, and where:
#   shim/swarm-heartbeat.sh -> $COMMS_HOOKS_DIR/swarm-heartbeat.sh
#                              (default $HOME/.claude/hooks)
#   this checkout's path    -> $COMMS_STATE_DIR/checkout-path
#                              (default $HOME/.comms/state)
#
# The checkout-path file is the point of the exercise. Baking $HOME/code/comms
# into the shim is what made the last one fragile: move or rename the checkout
# and the shim silently stops resolving. Writing the path at install time means
# the shim always points at the checkout that installed it.
#
# IDEMPOTENT AND NON-CLOBBERING. An existing shim that is byte-identical is
# left alone. An existing file that is NOT this shim is BACKED UP, never
# overwritten in place, and the backup path is printed -- because the file
# sitting there may be the pre-extraction implementation, whose body is the
# only copy of behavior nobody has read yet.
#
# Overrides for testing (so a test never touches the real hooks dir):
#   COMMS_HOOKS_DIR   target directory for the shim
#   COMMS_STATE_DIR   target directory for checkout-path
#
# exit 0 installed (or already correct) | 1 failed

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd -P)"      # <repo>/adapters/claude-code
REPO_ROOT="$(cd "$SELF_DIR/../.." && pwd -P)"     # <repo>
SRC="$SELF_DIR/shim/swarm-heartbeat.sh"

HOOKS_DIR="${COMMS_HOOKS_DIR:-$HOME/.claude/hooks}"
STATE_DIR="${COMMS_STATE_DIR:-$HOME/.comms/state}"
DST="$HOOKS_DIR/swarm-heartbeat.sh"

fail() { echo "install-shim: FAILED: $*" >&2; exit 1; }

[ -f "$SRC" ] || fail "shim source missing at $SRC -- incomplete checkout?"
[ -f "$REPO_ROOT/adapters/claude-code/swarm-heartbeat.sh" ] || \
  fail "this checkout has no real heartbeat at adapters/claude-code/swarm-heartbeat.sh; installing a shim that points here would just relocate the missing dependency"

mkdir -p "$HOOKS_DIR" || fail "cannot create $HOOKS_DIR"
mkdir -p "$STATE_DIR" || fail "cannot create $STATE_DIR"

# Render: substitute the placement-time default so the shim still resolves even
# if the checkout-path file is later deleted.
RENDERED="$(mktemp -t comms-shim)" || fail "mktemp failed"
trap 'rm -f "$RENDERED"' EXIT
COMMS_SHIM_SRC="$SRC" COMMS_SHIM_ROOT="$REPO_ROOT" COMMS_SHIM_OUT="$RENDERED" python3 - <<'PY' || fail "render failed"
import os
src = os.environ["COMMS_SHIM_SRC"]
root = os.environ["COMMS_SHIM_ROOT"]
out = os.environ["COMMS_SHIM_OUT"]
body = open(src).read()
# Substitute only the resolution default, and only in the shell string; the
# comment block that names the token is left readable on purpose.
body = body.replace('*)        echo "__COMMS_CHECKOUT__" ;;',
                    '*)        echo "%s" ;;' % root)
body = body.replace('case "__COMMS_CHECKOUT__" in',
                    'case "%s" in' % root)
open(out, "w").write(body)
PY

# ---------------------------------------------------------------------------
# GATE VALIDITY -- assert BEFORE placing, never after.
#
# The live registration for this hook is GATE mode: hook-shim.sh validates the
# target and, if the target does not parse or does not end with the marker, the
# gate fails closed -- which on a `"matcher": "*"` PostToolUse entry means every
# tool call on the machine gets refused. That failure would be caused by this
# installer, arrive on a machine already running sessions, and look like
# something else entirely. So the two conditions hook-shim checks are checked
# HERE, on the rendered bytes, before anything is written to the hooks dir.
# ---------------------------------------------------------------------------
MARKER='# hook-eof-marker v1 do-not-remove'
bash -n "$RENDERED" 2>/dev/null || \
  fail "the rendered shim does not parse under 'bash -n'. Placing it would turn the live gate into a machine-wide tool-call refusal"
[ "$(tail -n 1 "$RENDERED")" = "$MARKER" ] || \
  fail "the rendered shim's last line is not exactly the hook-eof-marker. hook-shim.sh treats that as an invalid hook and the gate fails closed. Last line was: $(tail -n 1 "$RENDERED")"
# The same two conditions on the file the shim will hand control to: a shim
# that execs an unparseable heartbeat has only moved the failure one hop.
bash -n "$REPO_ROOT/adapters/claude-code/swarm-heartbeat.sh" 2>/dev/null || \
  fail "this checkout's adapters/claude-code/swarm-heartbeat.sh does not parse; the shim would exec a broken gate"
[ "$(tail -n 1 "$REPO_ROOT/adapters/claude-code/swarm-heartbeat.sh")" = "$MARKER" ] || \
  fail "this checkout's adapters/claude-code/swarm-heartbeat.sh does not end with the hook-eof-marker"
echo "install-shim: gate validity ok (bash -n parses, eof-marker is the last line, both hops)"

if [ -f "$DST" ] && cmp -s "$RENDERED" "$DST"; then
  echo "install-shim: already current at $DST"
else
  if [ -e "$DST" ]; then
    BAK="$DST.pre-comms-shim.$(date -u '+%Y%m%dT%H%M%SZ')"
    cp -p "$DST" "$BAK" || fail "could not back up the existing $DST"
    echo "install-shim: existing hook backed up to $BAK"
  fi
  cp "$RENDERED" "$DST" || fail "could not write $DST"
  chmod 755 "$DST"      || fail "could not chmod $DST"
  echo "install-shim: placed $DST"
fi

printf '%s\n' "$REPO_ROOT" > "$STATE_DIR/checkout-path" || fail "could not write $STATE_DIR/checkout-path"
echo "install-shim: checkout-path -> $STATE_DIR/checkout-path ($REPO_ROOT)"

# Positive control on the placement itself. Run the placed shim with a
# deliberately absent checkout and assert it produced the miss evidence. A
# placement that is never exercised is a placement nobody knows works, and the
# whole point of this file is that the miss path is not silent.
PROBE_STATE="$(mktemp -d -t comms-shim-probe)" || fail "mktemp -d failed"
OUT="$(COMMS_CHECKOUT="$PROBE_STATE/definitely-not-a-checkout" \
       COMMS_STATE_DIR="$PROBE_STATE" \
       COMMS_SHIM_NAG_SECS=0 \
       bash "$DST" 2>"$PROBE_STATE/err")"
if [ ! -s "$PROBE_STATE/heartbeat-shim-missing.log" ]; then
  rm -rf "$PROBE_STATE"
  fail "the placed shim wrote NO miss log when its checkout was absent -- the silent-failure regression is back"
fi
if [ -z "$OUT" ]; then
  rm -rf "$PROBE_STATE"
  fail "the placed shim emitted no additionalContext on a miss with the nag throttle disabled"
fi
if [ ! -s "$PROBE_STATE/err" ]; then
  rm -rf "$PROBE_STATE"
  fail "the placed shim wrote nothing to stderr on a miss"
fi
rm -rf "$PROBE_STATE"
echo "install-shim: miss path verified LOUD (log line + stderr + additionalContext, exit 0)"
exit 0
