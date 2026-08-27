#!/bin/bash
# Translate Gemini AfterTool tool names into the vocabulary consumed by the
# one shared heartbeat, then replace this process with that heartbeat.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
HEARTBEAT="$(cd "$SELF_DIR/../claude-code" && pwd)/swarm-heartbeat.sh"

# Keep this inverse of Gemini CLI's TOOL_NAME_MAPPING in the adapter. Core must
# remain runtime-agnostic. hook_event_name passes through because the heartbeat
# does not branch on it; Gemini also ignores the heartbeat's output event name.
rewritten="$(python3 -c '
import json, sys
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
json.dump(payload, sys.stdout, separators=(",", ":"))
' 2>/dev/null)" || exit 0

exec /bin/bash "$HEARTBEAT" <<<"$rewritten"
