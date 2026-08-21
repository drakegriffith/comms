#!/bin/bash
# adapters/claude-code/ambient/sendmessage-bridge.sh -- PostToolUse hook body
# (tool name: SendMessage) for the AMBIENT LANE.
#
# DESIGN NOTE -- OUTBOUND ONLY, BY CONSTRUCTION. This bridge mirrors OUTBOUND
# peer traffic only: it fires on THIS session's SendMessage tool calls. It
# cannot see messages this session RECEIVES (delivery is not a tool call on
# the receiver). Both sides of a conversation appear on the machine-ops board
# exactly when BOTH sessions run this hook -- which is what the installer
# wires machine-wide. Do not mistake a one-sided transcript for a one-sided
# conversation; check whether the peer has the hook installed.
#
# WHAT IT DOES: reads the PostToolUse hook JSON from stdin (tool_input carries
# to/summary/message), and posts one row into the standing run `machine-ops`:
#   kind  comment
#   topic ops
#   seat  this session's enrolled seat (from the machine-ops roster)
#   text  "-> <to>: <summary, else first 200 chars of message>"
#
# PRIVACY: only the summary field (or a 200-char truncation of the message) is
# posted -- and therefore only that reaches the Discord mirror. Full message
# bodies stay wherever SendMessage put them; this bridge never echoes message
# contents to stderr, stdout, or the log.
#
# SKIP CONDITIONS (exit 0, no row, silent):
#   * payload not parseable, or tool_name is not SendMessage (self-filter for
#     installs whose hook schema cannot match on tool name)
#   * no session id in payload or $CLAUDE_SESSION_ID (a PID-fallback
#     enrollment cannot be rederived here)
#   * session not enrolled in machine-ops (bystander sessions stay silent --
#     the same property every other run has)
# This hook NEVER blocks the tool call: every path exits 0. Real errors go to
# $COMMS_STATE_DIR/ambient.log with no payload content.
#
# ISOLATION KNOBS (tests set these; production uses the defaults):
#   COMMS_STATE_DIR  roster state + ambient.log (default ~/.comms/state)
#   COMMS_ROOT       mailbox root (default /tmp)

set -uo pipefail

STATE_DIR="${COMMS_STATE_DIR:-$HOME/.comms/state}"

# ---- locate this repo from THIS script's resolved path --------------------
AMB_SELF="${BASH_SOURCE[0]:-$0}"
while [ -L "$AMB_SELF" ]; do
  _t="$(readlink "$AMB_SELF")"
  case "$_t" in
    /*) AMB_SELF="$_t" ;;
    *)  AMB_SELF="$(dirname "$AMB_SELF")/$_t" ;;
  esac
done
AMB_SELF_DIR="$(cd "$(dirname "$AMB_SELF")" && pwd -P)"  # <repo>/adapters/claude-code/ambient
AMB_REPO_ROOT="$(cd "$AMB_SELF_DIR/../../.." && pwd)"    # <repo>

# ---- bounded stdin read (vendored helper, one dir up) ---------------------
_amb_lib="$AMB_SELF_DIR/../stdin-bounded.sh"
if [ -r "$_amb_lib" ]; then
  . "$_amb_lib"
fi
if type read_stdin_bounded >/dev/null 2>&1; then
  read_stdin_bounded
  input="$HOOK_STDIN"
else
  input=""
fi

export AMB_PAYLOAD="$input"
export AMB_STATE_DIR="$STATE_DIR"
export AMB_SWARM_LIB="$AMB_REPO_ROOT/lib"

python3 <<'PY' || true
import datetime
import json
import os
import sys

state_dir = os.environ.get("AMB_STATE_DIR") or os.path.expanduser("~/.comms/state")

def log(msg):
    # Generic errors only -- NEVER payload content (privacy contract above).
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "ambient.log"), "a") as fh:
            at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            fh.write("%s sendmessage-bridge: %s\n" % (at, msg))
    except OSError:
        pass

try:
    sys.path.insert(0, os.environ.get("AMB_SWARM_LIB") or "")
    import swarm_arm
    import swarm_mailbox

    RUNID = "machine-ops"
    TOPIC = "ops"
    TEXT_CAP = 200

    try:
        payload = json.loads(os.environ.get("AMB_PAYLOAD", "") or "{}")
    except json.JSONDecodeError:
        sys.exit(0)  # unparseable payload: skip, never block the tool call
    if not isinstance(payload, dict):
        sys.exit(0)

    # Self-filter: correct even when the hook schema could not match on tool
    # name and this runs on every PostToolUse.
    if payload.get("tool_name") != "SendMessage":
        sys.exit(0)

    agent_id = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
    if not agent_id:
        sys.exit(0)  # PID-fallback enrollments are unreachable from here
    if not swarm_arm.is_participant(RUNID, agent_id, state_dir=state_dir):
        sys.exit(0)  # not enrolled: bystander stays silent

    _topics, seat = swarm_arm.participant_sub(RUNID, agent_id, state_dir=state_dir)
    if not seat:
        sys.exit(0)  # seatless enrollment: no own file to write

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        sys.exit(0)
    to = str(tool_input.get("to") or "?")
    summary = tool_input.get("summary")
    if summary:
        body = str(summary)
    else:
        body = str(tool_input.get("message") or "")[:TEXT_CAP]
    if not body:
        sys.exit(0)  # nothing worth a row

    text = "-> %s: %s" % (to, body)
    swarm_mailbox.post(RUNID, seat, "comment", text, topic=TOPIC)
except SystemExit:
    raise
except Exception as exc:
    log("error: %s" % exc.__class__.__name__)  # class only, no content
sys.exit(0)
PY

exit 0
