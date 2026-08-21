#!/bin/bash
# adapters/claude-code/ambient/install.sh -- idempotent installer for the
# AMBIENT LANE on the Claude Code runtime.
#
# What it does:
#   (a) verifies python3 and the lib/ modules exist RELATIVE TO THIS CHECKOUT
#       (never a hardcoded home);
#   (b) wires TWO hooks into the target settings.json (default
#       ~/.claude/settings.json -- the one path in this repo that may name
#       ~/.claude, because settings.json is owned by the Claude Code runtime
#       itself; override with COMMS_SETTINGS=<path> for testing) via ONE
#       python json edit -- an already-present entry is detected and left
#       alone, unrelated settings are never clobbered, an unparseable file is
#       REFUSED (refusing beats clobbering):
#         * SessionStart -> session-start.sh   (no matcher: every session)
#         * PostToolUse  -> sendmessage-bridge.sh (matcher "SendMessage"; the
#           script ALSO self-filters on tool_name, so a schema that ignores
#           the matcher still behaves);
#   (c) prints (does NOT install) the launchd plist that keeps the Discord
#       mirror following the standing run `machine-ops`, mirroring the
#       adapters/discord/install.sh style -- running the mirror is the
#       operator's move, sequenced by hand.
#
# Post-install verification (suites) is deliberately NOT run here, unlike the
# sibling adapters/claude-code/install.sh: this installer is expected to run
# while other sessions are live, and the operator sequences verification --
# `python3 -m pytest tests -q` from the checkout is the command.
#
# Exit codes: 0 installed | 1 failed (including refusal on unparseable JSON).

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"        # <repo>/adapters/claude-code/ambient
REPO_ROOT="$(cd "$SELF_DIR/../../.." && pwd)"    # <repo>
SETTINGS="${COMMS_SETTINGS:-$HOME/.claude/settings.json}"

fail() { echo "install: FAILED: $*" >&2; exit 1; }

# ---- (a) prerequisites, relative to this checkout -------------------------
command -v python3 >/dev/null 2>&1 || fail "python3 not found on PATH"
for f in \
    lib/swarm_mailbox.py \
    lib/swarm_arm.py \
    adapters/claude-code/stdin-bounded.sh \
    adapters/claude-code/ambient/session-start.sh \
    adapters/claude-code/ambient/sendmessage-bridge.sh; do
  [ -e "$REPO_ROOT/$f" ] || fail "missing $REPO_ROOT/$f -- incomplete checkout?"
done

# ---- (b) wire both hooks, idempotently, in one edit -----------------------
# Hooks are wired by ABSOLUTE PATH to THIS checkout's scripts, so the wiring
# can never point at a file that does not exist on this machine.
export COMMS_SESSION_START_CMD="bash $SELF_DIR/session-start.sh"
export COMMS_BRIDGE_CMD="bash $SELF_DIR/sendmessage-bridge.sh"
export COMMS_SETTINGS_TARGET="$SETTINGS"
python3 - <<'PY' || fail "settings.json edit failed"
import json
import os
import sys

path = os.environ["COMMS_SETTINGS_TARGET"]
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
changed = False


def present(event, needle):
    return any(
        needle in (h.get("command") or "")
        for entry in hooks.get(event, [])
        if isinstance(entry, dict)
        for h in (entry.get("hooks") or [])
        if isinstance(h, dict)
    )


if present("SessionStart", "ambient/session-start.sh"):
    print("SessionStart wiring: already present in %s, left untouched" % path)
else:
    hooks.setdefault("SessionStart", []).append(
        {"hooks": [{"type": "command",
                    "command": os.environ["COMMS_SESSION_START_CMD"]}]}
    )
    changed = True
    print("SessionStart wiring: added ambient session-start entry to %s" % path)

if present("PostToolUse", "ambient/sendmessage-bridge.sh"):
    print("PostToolUse wiring: already present in %s, left untouched" % path)
else:
    hooks.setdefault("PostToolUse", []).append(
        {"matcher": "SendMessage",
         "hooks": [{"type": "command",
                    "command": os.environ["COMMS_BRIDGE_CMD"]}]}
    )
    changed = True
    print("PostToolUse wiring: added ambient sendmessage-bridge entry to %s" % path)

if changed:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".comms-tmp"
    with open(tmp, "w") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
PY

# ---- (c) print (do not install) the mirror plist --------------------------
MIRROR="$REPO_ROOT/adapters/discord/mirror.py"
cat <<EOF

ambient lane: hooks wired. The Discord side runs separately -- keep the
mirror following the standing run under launchd (NOT installed by this
script): save as ~/Library/LaunchAgents/com.comms.discord-mirror.machine-ops.plist,
then 'launchctl load' it:

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
