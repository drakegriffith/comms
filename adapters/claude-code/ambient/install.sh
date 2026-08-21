#!/bin/bash
# adapters/claude-code/ambient/install.sh -- wire the AMBIENT LANE hooks into
# settings.json, routed through the dispatch shim. Shape follows the harness's
# shim-rewire.sh: exact-string entry map, --check dry-run, timestamped backup,
# staged parse before the real file is touched, atomic os.replace.
#
# HUMAN-RUN, ON PURPOSE: the permission classifier refuses agent edits to the
# settings.json hooks block, and that refusal is the AUTHORITY class working
# as designed, not an obstacle to route around. This script is the command the
# OPERATOR (Drake) runs by hand:
#
#     bash <checkout>/adapters/claude-code/ambient/install.sh          # apply
#     bash <checkout>/adapters/claude-code/ambient/install.sh --check  # dry-run
#
# What it wires (idempotent -- an already-present entry is detected by exact
# command string and left alone; unrelated settings are never clobbered; an
# unparseable settings file is REFUSED, because refusing beats clobbering):
#   * SessionStart -> session-start.sh      (no matcher: every session)
#   * PostToolUse  -> sendmessage-bridge.sh (matcher "SendMessage"; the script
#     ALSO self-filters on tool_name, so a schema ignoring the matcher still
#     behaves)
# BOTH commands route THROUGH the dispatch shim in OBSERVER mode --
#   bash $HOME/.claude/state/bin/hook-shim.sh observer <abs-path-to-hook>
# -- because neither hook may ever block a session or a tool call: a torn
# observer is skipped (exit 0) with a witness row, never failed closed. The
# shim validates each hook file's final-line completeness marker
# (# hook-eof-marker v1 do-not-remove) against mid-write tears; both hook
# scripts here carry it as their final line.
#
# Also PRINTS (does NOT install) the launchd plist that keeps the Discord
# mirror following the standing run `machine-ops`, mirroring the
# adapters/discord/install.sh style. Post-install verification (suites) is
# deliberately NOT run here, unlike the sibling adapters/claude-code/
# install.sh: this runs while other sessions are live; verify when convenient
# with `python3 -m pytest tests -q` from the checkout.
#
# Default target ~/.claude/settings.json -- the one path in this repo that may
# name ~/.claude, because settings.json is owned by the Claude Code runtime
# itself; override with COMMS_SETTINGS=<path> for testing.
#
# Exit codes: 0 wired (or --check) | 1 failed (incl. refusal on unparseable
# JSON) | 2 prerequisites missing (shim not installed, incomplete checkout).

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"        # <repo>/adapters/claude-code/ambient
REPO_ROOT="$(cd "$SELF_DIR/../../.." && pwd)"    # <repo>
SETTINGS="${COMMS_SETTINGS:-$HOME/.claude/settings.json}"
SHIM="$HOME/.claude/state/bin/hook-shim.sh"
CHECK=0; [ "${1:-}" = "--check" ] && CHECK=1

fail()   { echo "install: FAILED: $*" >&2; exit 1; }
prereq() { echo "install: PREREQUISITE MISSING: $*" >&2; exit 2; }

# ---- prerequisites, relative to this checkout -----------------------------
command -v python3 >/dev/null 2>&1 || prereq "python3 not found on PATH"
for f in \
    lib/swarm_mailbox.py \
    lib/swarm_arm.py \
    adapters/claude-code/stdin-bounded.sh \
    adapters/claude-code/ambient/session-start.sh \
    adapters/claude-code/ambient/sendmessage-bridge.sh; do
  [ -e "$REPO_ROOT/$f" ] || prereq "missing $REPO_ROOT/$f -- incomplete checkout?"
done
[ -x "$SHIM" ] || prereq "dispatch shim not installed at $SHIM -- run the harness's hook-shim-install.sh first"

# The shim skips a hook whose final line is not the completeness marker; a
# checkout that lost the markers would wire two permanently-skipped hooks.
MARKER='# hook-eof-marker v1 do-not-remove'
for f in session-start.sh sendmessage-bridge.sh; do
  [ "$(tail -n 1 "$SELF_DIR/$f")" = "$MARKER" ] \
    || prereq "$SELF_DIR/$f does not end with the completeness marker; the shim would skip it"
done

# ---- the edit: exact-string map, staged parse, backup, atomic replace -----
# Hook paths are ABSOLUTE to THIS checkout; the shim path is literal $HOME,
# expanded by the shell at hook run time (same convention as shim-rewire.sh).
export COMMS_SETTINGS_TARGET="$SETTINGS"
export COMMS_AMBIENT_DIR="$SELF_DIR"
CHECK="$CHECK" python3 - <<'PYEOF'
import json
import os
import shutil
import sys
import time

path = os.environ["COMMS_SETTINGS_TARGET"]
amb = os.environ["COMMS_AMBIENT_DIR"]
check = os.environ.get("CHECK") == "1"
SHIM = 'bash $HOME/.claude/state/bin/hook-shim.sh'

# Exact-string map of the entries to add: event -> (entry, exact command).
# Presence is judged on the EXACT command string; a substring cousin (same
# script, different checkout path) is reported, never touched.
ENTRIES = {
    "SessionStart": {
        "hooks": [{"type": "command",
                   "command": f"{SHIM} observer {amb}/session-start.sh"}],
    },
    "PostToolUse": {
        "matcher": "SendMessage",
        "hooks": [{"type": "command",
                   "command": f"{SHIM} observer {amb}/sendmessage-bridge.sh"}],
    },
}
NAMES = {"SessionStart": "ambient/session-start.sh",
         "PostToolUse": "ambient/sendmessage-bridge.sh"}

try:
    with open(path) as fh:
        settings = json.load(fh)
except FileNotFoundError:
    settings = {}
except json.JSONDecodeError as exc:
    # Refusing beats clobbering: a rewrite of an unparseable settings file
    # would silently destroy whatever the broken bytes were.
    sys.stderr.write("refusing to edit %s: not valid JSON (%s)\n" % (path, exc))
    sys.exit(1)

hooks = settings.setdefault("hooks", {})


def commands(event):
    return [
        h.get("command") or ""
        for entry in hooks.get(event, [])
        if isinstance(entry, dict)
        for h in (entry.get("hooks") or [])
        if isinstance(h, dict)
    ]


changed = False
for event, entry in ENTRIES.items():
    want = entry["hooks"][0]["command"]
    have = commands(event)
    if want in have:
        print("%s wiring: already present (exact match), left untouched" % event)
        continue
    cousins = [c for c in have if NAMES[event] in c]
    for c in cousins:
        print("%s NOTE: a different %s entry exists and is left untouched: %s"
              % (event, NAMES[event], c))
    verb = "would add" if check else "adding"
    print("%s wiring: %s: %s" % (event, verb, want))
    if not check:
        hooks.setdefault(event, []).append(entry)
        changed = True

if check:
    print("--check: nothing written")
    sys.exit(0)
if not changed:
    print("nothing to do")
    sys.exit(0)

d = os.path.dirname(path)
if d:
    os.makedirs(d, exist_ok=True)
if os.path.exists(path):
    bak = path + ".pre-ambient." + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, bak)
    print("backup at %s" % bak)
tmp = path + ".tmp-ambient"
with open(tmp, "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
with open(tmp) as fh:
    json.load(fh)               # parse the staged file before it goes live
os.replace(tmp, path)           # atomic; a crash never leaves a half-write
print("written atomically to %s" % path)
PYEOF
rc=$?
[ $rc -ne 0 ] && exit $rc
[ "$CHECK" = "1" ] && exit 0

# ---- print (do not install) the mirror plist ------------------------------
MIRROR="$REPO_ROOT/adapters/discord/mirror.py"
cat <<EOF

ambient lane: hooks wired through the dispatch shim (observer mode). The
Discord side runs separately -- keep the mirror following the standing run
under launchd (NOT installed by this script): save as
~/Library/LaunchAgents/com.comms.discord-mirror.machine-ops.plist, then
'launchctl load' it:

  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0"><dict>
    <key>Label</key><string>com.comms.discord-mirror.machine-ops</string>
    <key>ProgramArguments</key><array>
      <string>/usr/bin/python3</string>
      <string>$MIRROR</string>
      <string>--follow</string><string>machine-ops</string>
    </array>
    <key>EnvironmentVariables</key><dict>
      <key>COMMS_MACHINE_LABEL</key><string>$(hostname -s)</string>
    </dict>
    <key>KeepAlive</key><true/>
  </dict></plist>

Env knobs: COMMS_MACHINE_LABEL (prefix; default hostname -s),
COMMS_ROOT (mailbox root), COMMS_STATE_DIR (arm/roster/ambient.log),
COMMS_SECRETS_FILE (default ~/.secrets/comms.env), COMMS_MIRROR_INTERVAL.
Verify when convenient: python3 -m pytest $REPO_ROOT/tests -q
EOF
