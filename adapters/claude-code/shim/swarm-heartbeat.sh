#!/bin/bash
# swarm-heartbeat.sh -- SHIM, placed at $HOME/.claude/hooks/swarm-heartbeat.sh
# by adapters/claude-code/install-shim.sh. It execs the real heartbeat out of
# the comms checkout, so the settings.json wiring
# (`bash $HOME/.claude/hooks/swarm-heartbeat.sh`) keeps working while the
# implementation lives in the repo.
#
# ------------------------------------------------------------------------
# WHY THIS FILE EXISTS IN THE REPO INSTEAD OF BEING EDITED IN PLACE
# ------------------------------------------------------------------------
# The shim that shipped before this one ended:
#
#     if [ -f "$COMMS_HB" ]; then exec bash "$COMMS_HB" "$@"; fi
#     exit 0     # checkout missing: exit 0 silently
#
# That silence is the bug this file fixes, and it is not a small one. For as
# long as the checkout was missing, the hook reported success on every single
# tool call: no output, no log line, no exit code, no counter. A subsystem
# that is entirely absent looked identical to a subsystem that had nothing to
# say. Nobody could have noticed, because there was nothing to notice.
#
# A shim edited in place on one machine is also how the divergence this
# replaces got started, so the file is version-controlled HERE and PLACED by
# the installer. Editing the live copy is what you do instead of fixing the
# repo, once.
#
# ------------------------------------------------------------------------
# THE CONSTRAINT: LOUD, BUT STILL NEVER BLOCKING
# ------------------------------------------------------------------------
# The heartbeat's never-block rule is real: this runs after EVERY tool call,
# and a nonzero exit or a malformed stdout would degrade the agent's session
# over a problem the agent did not cause and cannot fix mid-turn. So the exit
# code stays 0 on every path. "Loud" is bought with the three channels that
# cost the caller nothing:
#
#   1. A DURABLE COUNTER, appended on EVERY miss, never rate limited. This is
#      the enumerator: `wc -l` on it is the number of tool calls that ran with
#      no heartbeat. A missing-dependency event you cannot count is a
#      missing-dependency event you cannot argue about.
#   2. STDERR, on every miss. Visible when the hook is run by hand, which is
#      the first thing anyone does when debugging it.
#   3. INJECTED CONTEXT, rate limited to once per COMMS_SHIM_NAG_SECS (default
#      300s). This is the channel that reaches a human, via the agent, without
#      being asked. It is rate limited and ONLY rate limited -- channel 1 keeps
#      counting underneath, so the throttle bounds the noise without ever
#      making the evidence disappear.
#
# The message is ACTIONABLE, not just alarmed: it names the path that was
# looked for, the setting that overrides it, and the one command that fixes
# it. A warning that does not say what to type gets ignored twice.
#
# ------------------------------------------------------------------------
# RESOLUTION ORDER FOR THE CHECKOUT
# ------------------------------------------------------------------------
#   1. $COMMS_CHECKOUT                       (explicit, per-process)
#   2. $COMMS_STATE_DIR/checkout-path        (written by the installer, so the
#      shim points at the checkout that INSTALLED it rather than at a constant
#      somebody has to remember to edit)
#   3. __COMMS_CHECKOUT__                    (substituted at placement time)
#   4. $HOME/code/comms                      (last-resort default)
#
# hook-eof-marker v1 do-not-remove is preserved at the bottom: the harness's
# own hook-health check looks for it.

COMMS_STATE_DIR_EFF="${COMMS_STATE_DIR:-$HOME/.comms/state}"

resolve_checkout() {
  if [ -n "${COMMS_CHECKOUT:-}" ]; then
    echo "$COMMS_CHECKOUT"; return
  fi
  if [ -r "$COMMS_STATE_DIR_EFF/checkout-path" ]; then
    head -1 "$COMMS_STATE_DIR_EFF/checkout-path"; return
  fi
  case "__COMMS_CHECKOUT__" in
    __COMMS*) echo "$HOME/code/comms" ;;
    *)        echo "__COMMS_CHECKOUT__" ;;
  esac
}

CHECKOUT="$(resolve_checkout)"
COMMS_HB="$CHECKOUT/adapters/claude-code/swarm-heartbeat.sh"

if [ -f "$COMMS_HB" ]; then
  exec bash "$COMMS_HB" "$@"
fi

# ---------------------------------------------------------------------------
# MISS PATH. Everything below runs only when the checkout is not there.
# ---------------------------------------------------------------------------
NOW="$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo unknown)"
EPOCH="$(date +%s 2>/dev/null || echo 0)"
LOG="$COMMS_STATE_DIR_EFF/heartbeat-shim-missing.log"
STAMP="$COMMS_STATE_DIR_EFF/heartbeat-shim-nag.stamp"

FIX_CMD="git clone https://github.com/drakegriffith/comms \"$CHECKOUT\" && bash \"$CHECKOUT/adapters/claude-code/install.sh\""
MSG="comms heartbeat DISABLED: no checkout at $CHECKOUT (looked for $COMMS_HB). \
Every tool call since is running with NO sibling-mailbox delivery: rows posted \
by other seats are not reaching this session, and no error is being raised \
anywhere else. Fix with: $FIX_CMD -- or point the shim at an existing checkout \
by setting COMMS_CHECKOUT, or by writing its path into \
$COMMS_STATE_DIR_EFF/checkout-path. Miss count so far is the line count of $LOG."

# --- channel 1: the durable counter, every miss, never throttled ------------
mkdir -p "$COMMS_STATE_DIR_EFF" 2>/dev/null
printf '%s\tmissing_checkout\t%s\t%s\n' "$NOW" "$CHECKOUT" "${CLAUDE_SESSION_ID:-unknown-session}" >> "$LOG" 2>/dev/null

# --- channel 2: stderr, every miss -----------------------------------------
echo "swarm-heartbeat shim: $MSG" >&2

# --- channel 3: injected context, throttled --------------------------------
NAG_SECS="${COMMS_SHIM_NAG_SECS:-300}"
LAST=0
[ -r "$STAMP" ] && LAST="$(head -1 "$STAMP" 2>/dev/null | tr -cd '0-9')"
[ -n "$LAST" ] || LAST=0

if [ "$EPOCH" -gt 0 ] && [ $((EPOCH - LAST)) -ge "$NAG_SECS" ]; then
  printf '%s' "$EPOCH" > "$STAMP" 2>/dev/null
  if command -v python3 >/dev/null 2>&1; then
    # json.dumps, so a path containing a quote or a backslash cannot produce
    # invalid stdout. Invalid stdout from a PostToolUse hook is discarded --
    # which would put us straight back in the silent-failure hole this file
    # was written to climb out of.
    COMMS_SHIM_MSG="$MSG" python3 - <<'PY' 2>/dev/null
import json, os
msg = os.environ.get("COMMS_SHIM_MSG", "comms heartbeat shim: checkout missing")
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext":
        "[comms heartbeat -- OPERATIONAL NOTICE, this is data about your "
        "tooling, NOT an instruction to act on] " + msg,
}}))
PY
  else
    # No python3 either. Strip everything that would need escaping rather than
    # emit JSON that might not parse, and keep going: a degraded notice beats
    # no notice, and the counter in channel 1 is unaffected.
    SAFE="$(printf '%s' "$MSG" | tr -d '"\\' | tr '\n' ' ')"
    printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"[comms heartbeat -- OPERATIONAL NOTICE, data not instructions] %s"}}\n' "$SAFE"
  fi
fi

# NEVER BLOCK. The checkout being absent is an operator problem, not a reason
# to fail the tool call that just succeeded.
exit 0
# hook-eof-marker v1 do-not-remove
