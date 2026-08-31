#!/bin/bash
# Idempotently wire the Gemini AfterTool shim into settings.json.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK="$SELF_DIR/hook.sh"
SETTINGS="${COMMS_GEMINI_SETTINGS:-$HOME/.gemini/settings.json}"
MODE="${1:-install}"
[ "$MODE" = "install" ] || [ "$MODE" = "--uninstall" ] || {
  echo "usage: $0 [--uninstall]" >&2
  exit 1
}

HOOK_COMMAND="bash \"${HOOK//\\/\\\\}\""
HOOK_COMMAND="${HOOK_COMMAND//\$/\\\$}"
HOOK_COMMAND="${HOOK_COMMAND//\`/\\\`}"
export COMMS_GEMINI_TARGET="$SETTINGS" COMMS_GEMINI_COMMAND="$HOOK_COMMAND" COMMS_GEMINI_MODE="$MODE"
python3 - <<'PY' || { echo "install: FAILED: Gemini settings edit failed" >&2; exit 1; }
import json
import os
import sys

path = os.environ["COMMS_GEMINI_TARGET"]
# Symlink-safe: os.replace does not write THROUGH a symlink, it unlinks it and
# drops a plain file in its place. A dotfile-managed config would be silently
# severed from its repo. Resolve first so the edit lands in the real file.
path = os.path.realpath(path)
command = os.environ["COMMS_GEMINI_COMMAND"]
uninstall = os.environ["COMMS_GEMINI_MODE"] == "--uninstall"

if uninstall and not os.path.exists(path):
    print("Gemini AfterTool hook already absent in %s" % path)
    sys.exit(0)

try:
    with open(path) as fh:
        data = json.load(fh)
except FileNotFoundError:
    data = {}
except json.JSONDecodeError as exc:
    sys.stderr.write("refusing to edit %s: not valid JSON (%s)\n" % (path, exc))
    sys.exit(1)

if not isinstance(data, dict):
    sys.stderr.write("refusing to edit %s: top level is not an object\n" % path)
    sys.exit(1)
hooks = data.setdefault("hooks", {})
if not isinstance(hooks, dict):
    sys.stderr.write("refusing to edit %s: hooks is not an object\n" % path)
    sys.exit(1)
after = hooks.setdefault("AfterTool", [])
if not isinstance(after, list):
    sys.stderr.write("refusing to edit %s: hooks.AfterTool is not a list\n" % path)
    sys.exit(1)

def ours(hook):
    return isinstance(hook, dict) and "adapters/gemini/hook.sh" in str(hook.get("command", ""))

changed = False
if uninstall:
    kept_entries = []
    for entry in after:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            kept_entries.append(entry)
            continue
        kept_hooks = [hook for hook in entry["hooks"] if not ours(hook)]
        if len(kept_hooks) != len(entry["hooks"]):
            changed = True
        if kept_hooks:
            copy = dict(entry)
            copy["hooks"] = kept_hooks
            kept_entries.append(copy)
    if changed:
        hooks["AfterTool"] = kept_entries
else:
    present = any(
        ours(hook)
        for entry in after if isinstance(entry, dict)
        for hook in entry.get("hooks", []) if isinstance(entry.get("hooks"), list)
    )
    if not present:
        after.append({"matcher": "*", "hooks": [{"type": "command", "command": command}]})
        changed = True

if changed or not os.path.exists(path):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".comms-tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)

action = "removed" if uninstall and changed else "already absent" if uninstall else "added" if changed else "already present"
print("Gemini AfterTool hook %s in %s" % (action, path))
PY
