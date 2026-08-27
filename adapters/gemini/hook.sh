#!/bin/bash
# Translate Gemini AfterTool tool names into the vocabulary consumed by the
# one shared heartbeat. COMMS_GEMINI_DUMP is a test-only observation seam for
# the translated payload; normal installs leave it unset.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
HEARTBEAT="$(cd "$SELF_DIR/../claude-code" && pwd)/swarm-heartbeat.sh"

# Keep this inverse of Gemini CLI's TOOL_NAME_MAPPING in the adapter. Core must
# remain runtime-agnostic. hook_event_name passes through because the heartbeat
# does not branch on it; Gemini also ignores the heartbeat's output event name.
rewritten="$(python3 -c '
import json, sys
import os
try:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ValueError("payload is not an object")
except (ValueError, TypeError):
    raise SystemExit(2)
tool_map = {
    "run_shell_command": "Bash",
    "write_file": "Write",
    "replace": "Edit",
    "read_file": "Read",
}
name = payload.get("tool_name")
if name in tool_map:
    payload["tool_name"] = tool_map[name]
dump = os.environ.get("COMMS_GEMINI_DUMP")
if dump:
    with open(dump, "w") as fh:
        json.dump(payload, fh, separators=(",", ":"))
json.dump(payload, sys.stdout, separators=(",", ":"))
' 2>/dev/null)" || exit 0

LOG_DIR="${COMMS_STATE_DIR:-${TMPDIR:-/tmp}/comms-state}"
if mkdir -p "$LOG_DIR" 2>/dev/null; then
  /bin/bash "$HEARTBEAT" <<<"$rewritten" 2>>"$LOG_DIR/gemini-hook.log" || true
else
  /bin/bash "$HEARTBEAT" <<<"$rewritten" 2>/dev/null || true
fi
exit 0
