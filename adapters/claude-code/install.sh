#!/bin/bash
# adapters/claude-code/install.sh -- idempotent installer for the comms stack
# on the Claude Code runtime.
#
# What it does:
#   (a) verifies python3 and the lib/ modules exist RELATIVE TO THIS CHECKOUT
#       (never a hardcoded home);
#   (b) wires the PostToolUse swarm-heartbeat hook into the target
#       settings.json (default ~/.claude/settings.json -- the one path in this
#       repo that may name ~/.claude, because settings.json is owned by the
#       Claude Code runtime itself; override with COMMS_SETTINGS=<path> for
#       testing) via a python json edit -- an already-present entry is detected
#       and left alone, unrelated settings are never clobbered;
#   (c) prints the absolute path of bin/comms plus an alias suggestion
#       (PATH installation is deliberately out of scope);
#   (d) runs the existing test suites as post-install verification and
#       reports their real exit codes.
#
# Exit codes: 0 installed + verified | 1 failed | 2 could not verify.
# EXIT 2 IS NOT A PASS: it means verification never ran, which says nothing
# about whether the install works.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"       # <repo>/adapters/claude-code
REPO_ROOT="$(cd "$SELF_DIR/../.." && pwd)"      # <repo>
SETTINGS="${COMMS_SETTINGS:-$HOME/.claude/settings.json}"

fail()     { echo "install: FAILED: $*" >&2; exit 1; }
noverify() { echo "install: COULD NOT VERIFY (exit 2 is NOT a pass): $*" >&2; exit 2; }

# ---- (a) prerequisites, relative to this checkout -------------------------
command -v python3 >/dev/null 2>&1 || fail "python3 not found on PATH"
for f in \
    lib/swarm_mailbox.py \
    lib/swarm_arm.py \
    lib/swarm_claims.py \
    tests/test_swarm_heartbeat.sh \
    adapters/claude-code/swarm-heartbeat.sh \
    adapters/claude-code/stdin-bounded.sh \
    bin/comms; do
  [ -e "$REPO_ROOT/$f" ] || fail "missing $REPO_ROOT/$f -- incomplete checkout?"
done

# ---- (b) wire the PostToolUse heartbeat hook, idempotently ----------------
# The hook is wired by ABSOLUTE PATH to THIS checkout's adapter script, so the
# wiring can never point at a file that does not exist on this machine.
HOOK_CMD="bash $SELF_DIR/swarm-heartbeat.sh"

export COMMS_HOOK_CMD="$HOOK_CMD" COMMS_SETTINGS_TARGET="$SETTINGS"
python3 - <<'PY' || fail "settings.json edit failed"
import json
import os
import sys

path = os.environ["COMMS_SETTINGS_TARGET"]
cmd = os.environ["COMMS_HOOK_CMD"]
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
ptu = hooks.setdefault("PostToolUse", [])
present = any(
    "swarm-heartbeat.sh" in (h.get("command") or "")
    for entry in ptu
    if isinstance(entry, dict)
    for h in (entry.get("hooks") or [])
    if isinstance(h, dict)
)
if present:
    print("hook wiring: already present in %s, left untouched" % path)
else:
    ptu.append({"matcher": "*", "hooks": [{"type": "command", "command": cmd}]})
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".comms-tmp"
    with open(tmp, "w") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    print("hook wiring: added PostToolUse swarm-heartbeat entry to %s" % path)
PY

# ---- (c) CLI location (PATH install is out of scope) ----------------------
echo "comms CLI: $REPO_ROOT/bin/comms"
echo "suggested alias: alias comms='$REPO_ROOT/bin/comms'"

# ---- (d) post-install verification: run the existing suites ---------------
if ! python3 -m pytest --version >/dev/null 2>&1; then
  noverify "pytest not available (python3 -m pytest); install pytest and re-run"
fi

python3 -m pytest "$REPO_ROOT/tests" -q
PYTEST_RC=$?
bash "$REPO_ROOT/tests/test_swarm_heartbeat.sh"
HB_RC=$?
bash "$REPO_ROOT/tests/test_comms_cli.sh"
CLI_RC=$?
echo "verification: pytest rc=$PYTEST_RC, heartbeat suite rc=$HB_RC, CLI smoke rc=$CLI_RC"

if [ "$PYTEST_RC" -eq 0 ] && [ "$HB_RC" -eq 0 ] && [ "$CLI_RC" -eq 0 ]; then
  echo "install: OK (installed + verified)"
  exit 0
fi
fail "post-install verification failed (pytest rc=$PYTEST_RC, heartbeat rc=$HB_RC, CLI rc=$CLI_RC)"
