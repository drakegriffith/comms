#!/bin/bash
# adapters/hermes/hook.sh -- Hermes pre_llm_call shell-hook shim.
#
# Hermes's pre_llm_call hook receives a JSON payload on stdin once per turn and
# expects {"context": "..."} on stdout to inject context into the user message.
# This shim translates that payload into the shape the one heartbeat expects,
# execs adapters/claude-code/swarm-heartbeat.sh, and translates its stdout back
# into Hermes's {"context": ...} envelope.
#
# DESIGN
#   * ONE heartbeat, never a copy: the identity gate, arm gate, subscription
#     filter, and cursor rules live in swarm-heartbeat.sh. A second heartbeat
#     would be a second place for them to drift.
#   * The shim is a pure translator: it maps Hermes's session_id to the
#     heartbeat's agent_id/session_id, sets hook_event_name to PostToolUse and
#     tool_name to Bash with an empty command so the heartbeat's enrol/claim legs
#     stay inert, passes cwd through, and rewrites the stdout envelope.
#   * NEVER BLOCK: malformed stdin, a missing heartbeat, or any runtime error
#     produces {} and exits 0. A shell hook that aborts the agent turn is a bug.
#
# The seat must enroll explicitly before rows arrive:
#   bin/comms enroll <run> --agent-id <session id> --seat <name>

set -uo pipefail

# Locate this repo from the script's resolved path, matching swarm-heartbeat.sh.
HB_SELF="${BASH_SOURCE[0]:-$0}"
while [ -L "$HB_SELF" ]; do
  _t="$(readlink "$HB_SELF")"
  case "$_t" in
    /*) HB_SELF="$_t" ;;
    *)  HB_SELF="$(dirname "$HB_SELF")/$_t" ;;
  esac
done
HB_SELF_DIR="$(cd "$(dirname "$HB_SELF")" && pwd -P)"   # <repo>/adapters/hermes
HB_REPO_ROOT="$(cd "$HB_SELF_DIR/../.." && pwd)"        # <repo>
HEARTBEAT="$HB_REPO_ROOT/adapters/claude-code/swarm-heartbeat.sh"

# Read stdin in bash (heredoc python cannot read stdin), then hand it to python
# via an env var -- the same pattern swarm-heartbeat.sh uses for HB_PAYLOAD.
input_data=""
if [ -t 0 ]; then
    # No piped stdin: nothing to translate.
    :
else
    input_data="$(cat)"
fi
export HERMES_PAYLOAD="$input_data"
export HERMES_HEARTBEAT="$HEARTBEAT"

exec python3 <<'PY'
import json
import os
import subprocess
import sys

HEARTBEAT = os.environ.get("HERMES_HEARTBEAT", "")
STDIN_DATA = os.environ.get("HERMES_PAYLOAD", "")

def emit_empty():
    sys.stdout.write("{}")
    sys.stdout.flush()
    sys.exit(0)

stdin_data = STDIN_DATA
if not stdin_data or not stdin_data.strip():
    emit_empty()

try:
    payload = json.loads(stdin_data)
except json.JSONDecodeError:
    emit_empty()

if not isinstance(payload, dict):
    emit_empty()

session_id = payload.get("session_id")
if not isinstance(session_id, str) or not session_id:
    # No stable identity -> no cursor. Fail open.
    emit_empty()

cwd = payload.get("cwd")
if not isinstance(cwd, str):
    cwd = ""

# Build the heartbeat payload. PostToolUse + Bash(empty) keeps enrol/claim inert.
heartbeat_payload = {
    "agent_id": session_id,
    "session_id": session_id,
    "hook_event_name": "PostToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": ""},
    "cwd": cwd,
}

# Run the one heartbeat, capturing stdout. Any failure is a silent no-op.
heartbeat_stdout = ""
try:
    if not HEARTBEAT or not os.path.isfile(HEARTBEAT):
        emit_empty()
    proc = subprocess.run(
        ["bash", HEARTBEAT],
        input=json.dumps(heartbeat_payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    heartbeat_stdout = proc.stdout or ""
except Exception:
    emit_empty()

# Parse the heartbeat envelope and emit Hermes's shape.
additional = ""
if heartbeat_stdout.strip():
    try:
        parsed = json.loads(heartbeat_stdout)
        if isinstance(parsed, dict):
            hook_output = parsed.get("hookSpecificOutput", {})
            if isinstance(hook_output, dict):
                additional = hook_output.get("additionalContext", "")
    except (json.JSONDecodeError, TypeError):
        additional = ""

if isinstance(additional, str) and additional:
    sys.stdout.write(json.dumps({"context": additional}))
else:
    sys.stdout.write("{}")
sys.stdout.flush()
sys.exit(0)
PY
