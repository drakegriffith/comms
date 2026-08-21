#!/bin/bash
# adapters/codex/install.sh -- idempotently wire the comms heartbeat into the
# Codex CLI's hooks.json.
#
# Codex 0.148.0 runs Claude-shaped hooks.json PostToolUse hooks with a
# byte-compatible payload and injects hookSpecificOutput.additionalContext
# (proven 2026-08-21; see README.md beside this file). So Codex does not need
# its own heartbeat: this installer points Codex at the SAME script the
# claude-code adapter ships. One implementation, two runtimes.
#
# Idempotent: an entry whose command mentions swarm-heartbeat.sh is detected
# and left alone; other entries are never clobbered; the file is created if
# absent. Override the target with COMMS_CODEX_HOOKS=<path> for testing.
#
# Exit codes: 0 wired (or already wired) | 1 failed.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"           # <repo>/adapters/codex
HEARTBEAT="$(cd "$SELF_DIR/../claude-code" && pwd)/swarm-heartbeat.sh"
HOOKS_FILE="${COMMS_CODEX_HOOKS:-$HOME/.codex/hooks.json}"

[ -e "$HEARTBEAT" ] || { echo "install: FAILED: missing $HEARTBEAT" >&2; exit 1; }

export COMMS_HOOK_CMD="bash $HEARTBEAT" COMMS_HOOKS_TARGET="$HOOKS_FILE"
python3 - <<'PY' || { echo "install: FAILED: hooks.json edit failed" >&2; exit 1; }
import json
import os
import sys

path = os.environ["COMMS_HOOKS_TARGET"]
cmd = os.environ["COMMS_HOOK_CMD"]
try:
    with open(path) as fh:
        data = json.load(fh)
except FileNotFoundError:
    data = {}
except json.JSONDecodeError as exc:
    # Refusing beats clobbering: a rewrite of an unparseable file would
    # silently destroy whatever the broken bytes were.
    sys.stderr.write("refusing to edit %s: not valid JSON (%s)\n" % (path, exc))
    sys.exit(1)

# hooks.json shape: tolerate both a top-level event map ({"PostToolUse": [...]})
# and a settings-style wrapper ({"hooks": {"PostToolUse": [...]}}). An existing
# "hooks" dict is honoured; a fresh file gets the top-level event map, matching
# the file's own name.
container = data["hooks"] if isinstance(data.get("hooks"), dict) else data
ptu = container.setdefault("PostToolUse", [])
if not isinstance(ptu, list):
    sys.stderr.write("refusing to edit %s: PostToolUse is not a list\n" % path)
    sys.exit(1)

present = any(
    "swarm-heartbeat.sh" in (h.get("command") or "")
    for entry in ptu
    if isinstance(entry, dict)
    for h in (entry.get("hooks") or [])
    if isinstance(h, dict)
)
if present:
    print("codex hook wiring: already present in %s, left untouched" % path)
else:
    ptu.append({"matcher": "*", "hooks": [{"type": "command", "command": cmd}]})
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".comms-tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    print("codex hook wiring: added PostToolUse swarm-heartbeat entry to %s" % path)
PY

echo "note: headless codex runs need --dangerously-bypass-hook-trust (hook trust"
echo "is hash-pinned and untrusted hooks are skipped SILENTLY -- see README.md)."
