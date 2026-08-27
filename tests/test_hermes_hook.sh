#!/bin/bash
# test_hermes_hook.sh -- adapters/hermes/hook.sh shim around the one heartbeat.
#
# Hermes's pre_llm_call shell hook receives a JSON payload on stdin once per turn
# and parses {"context": "..."} from stdout. This test drives the shim with a
# fake Hermes payload and checks it:
#   1. returns a context containing a peer row posted after enrollment,
#   2. returns {} with rc 0 when there are no new rows,
#   3. returns {} with rc 0 on malformed stdin (never block the host),
#   4. advances the heartbeat cursor at $COMMS_STATE_DIR/swarm-cursor/<runid>/<agent_id>,
#   5. reports a missing heartbeat on stderr while returning {} with rc 0,
#   6. keeps its subprocess timeout below Hermes's 60-second default.
#
# All state is isolated: COMMS_ROOT and COMMS_STATE_DIR are mktemp dirs.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SELF_DIR/.." && pwd)"
COMMS="$REPO/bin/comms"
HOOK="$REPO/adapters/hermes/hook.sh"

export COMMS_STATE_DIR="$(mktemp -d)"
export COMMS_ROOT="$(mktemp -d)"
trap 'rm -rf "$COMMS_STATE_DIR" "$COMMS_ROOT"' EXIT

PASS=0
FAIL=0

ok()   { echo "ok:   $1"; PASS=$((PASS + 1)); }
bad()  { echo "FAIL: $1"; FAIL=$((FAIL + 1)); }
assert() { if [ "$2" -eq 0 ]; then ok "$1"; else bad "$1"; fi; }
eq() {
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want [$2], got [$3])"; fi
}

RUN="hermes-$$"
SEAT="hermes-s1"
AGENT_ID="sess-hermes-$$-abc"
CWD="/tmp/hermes-test-cwd"
CURSOR="$COMMS_STATE_DIR/swarm-cursor/$RUN/$AGENT_ID"

# Common payload builder. Optional argument is an extra JSON fragment merged
# into the top-level object (used only for the malformed-stdin case). Uses the
# bash variables AGENT_ID and CWD for the session id and cwd fields.
hermes_payload() {
  local _extra="${1:-{}}"
  AGENT_ID="$AGENT_ID" CWD="$CWD" EXTRA="$_extra" python3 <<'PY'
import json, os
base = {
    "hook_event_name": "pre_llm_call",
    "tool_name": None,
    "tool_input": None,
    "session_id": os.environ["AGENT_ID"],
    "cwd": os.environ["CWD"],
    "extra": {
        "user_message": "do the thing",
        "conversation_history": [],
        "is_first_turn": False,
        "model": "anthropic/claude-sonnet-4.6",
        "platform": "cli"
    }
}
extra = os.environ.get("EXTRA", "{}")
try:
    extra_obj = json.loads(extra)
except json.JSONDecodeError:
    extra_obj = {}
base.update(extra_obj)
print(json.dumps(base))
PY
}

# ---- 1. context injection carries a peer row --------------------------------
"$COMMS" arm "$RUN" --topic work >/dev/null
# Enroll with the session id as agent_id, per the Hermes brief.
"$COMMS" enroll "$RUN" --agent-id "$AGENT_ID" --topics work --seat "$SEAT" >/dev/null
"$COMMS" post "$RUN" peer finding "hello from peer seat" --topic work >/dev/null

out="$(hermes_payload | "$HOOK" 2>/dev/null)"; rc=$?
eq "peer row exits 0" 0 "$rc"
case "$out" in
  '{"context":'*) ok "stdout is a single JSON object with context key" ;;
  *) bad "stdout missing context key (got: $out)" ;;
esac
case "$out" in
  *"hello from peer seat"*) ok "context contains the peer row text" ;;
  *) bad "context missing peer row text (got: $out)" ;;
esac
# Exactly one line of stdout.
nlines="$(printf '%s\n' "$out" | grep -c '^')"
eq "stdout is exactly one line" 1 "$nlines"
assert "heartbeat cursor advanced after delivery" "$([ -f "$CURSOR" ]; echo $?)"

# ---- 2. no new rows -> {} and rc 0 ------------------------------------------
before_cursor="$(cat "$CURSOR" 2>/dev/null || echo "")"
out="$(hermes_payload | "$HOOK" 2>/dev/null)"; rc=$?
eq "no new rows exits 0" 0 "$rc"
eq "no new rows stdout is {}" '{}' "$out"
after_cursor="$(cat "$CURSOR" 2>/dev/null || echo "")"
eq "no new rows leaves cursor unchanged" "$before_cursor" "$after_cursor"

# ---- 3. malformed stdin -> {} and rc 0 (never-block rule) -------------------
out="$(printf 'not-json{' | "$HOOK" 2>/dev/null)"; rc=$?
eq "malformed stdin exits 0" 0 "$rc"
eq "malformed stdin stdout is {}" '{}' "$out"

# ---- 4. cursor path is the heartbeat's own cursor file ----------------------
assert "cursor file exists at swarm-cursor/<runid>/<agent_id>" "$([ -f "$CURSOR" ]; echo $?)"
case "$(cat "$CURSOR" 2>/dev/null || echo "")" in
  2026-*) ok "cursor holds an ISO timestamp" ;;
  *) bad "cursor does not hold a timestamp (got: $(cat "$CURSOR" 2>/dev/null))" ;;
esac

# ---- 5. missing heartbeat is loud but never blocks --------------------------
MISSING_REPO="$(mktemp -d)"
mkdir -p "$MISSING_REPO/adapters/hermes"
cp "$HOOK" "$MISSING_REPO/adapters/hermes/hook.sh"
chmod +x "$MISSING_REPO/adapters/hermes/hook.sh"
missing_err="$MISSING_REPO/stderr"
out="$(hermes_payload | "$MISSING_REPO/adapters/hermes/hook.sh" 2>"$missing_err")"; rc=$?
eq "missing heartbeat exits 0" 0 "$rc"
eq "missing heartbeat stdout is {}" '{}' "$out"
assert "missing heartbeat reports one stderr line" "$(grep -qx 'hermes hook: heartbeat file missing' "$missing_err"; echo $?)"
eq "missing heartbeat stderr has exactly one line" 1 "$(wc -l < "$missing_err" | tr -d ' ')"
rm -rf "$MISSING_REPO"

# ---- 6. shim timeout stays below Hermes's 60-second default -----------------
# A grep assertion is deliberate: this is a source-level collision invariant.
timeout_seconds="$(sed -n 's/^[[:space:]]*timeout=\([0-9][0-9]*\),$/\1/p' "$HOOK")"
case "$timeout_seconds" in
  ''|*[!0-9]*) bad "subprocess timeout constant is a readable integer" ;;
  *)
    ok "subprocess timeout constant is a readable integer"
    assert "subprocess timeout is below Hermes default 60 seconds" "$([ "$timeout_seconds" -lt 60 ]; echo $?)"
    ;;
esac

echo
if [ "$FAIL" -eq 0 ]; then
    echo "passed: $PASS"
    exit 0
else
    echo "failed: $FAIL, passed: $PASS"
    exit 1
fi
