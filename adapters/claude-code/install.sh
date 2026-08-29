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
#   (b2) installs the comms-say skill (phrase -> 1-1 mailbox send) into the
#       skills dir (default ~/.claude/skills; override with
#       COMMS_SKILLS_DIR=<dir> for testing), rendering __COMMS_ROOT__ to this
#       checkout; an identical installed copy is left untouched;
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
    adapters/claude-code/skills/comms-say/SKILL.md \
    adapters/claude-code/install-skill.sh \
    adapters/claude-code/settings_edit.py \
    bin/comms; do
  [ -e "$REPO_ROOT/$f" ] || fail "missing $REPO_ROOT/$f -- incomplete checkout?"
done

# ---- (b) wire the PostToolUse heartbeat hook, idempotently ----------------
# The hook is wired by ABSOLUTE PATH to THIS checkout's adapter script, so the
# wiring can never point at a file that does not exist on this machine.
HOOK_CMD="bash $SELF_DIR/swarm-heartbeat.sh"

# The edit itself is delegated to settings_edit.py, the ONE safe writer for
# this file (read its header). The block that used to live here parsed the
# file, mutated it, and wrote it back with no check that the bytes on disk were
# still the bytes it had read -- so a concurrent write by a live Claude session
# was silently discarded, and a lost hooks block is indistinguishable from
# corruption. The writer adds: a lock, a backup, a staged parse, a
# re-check-then-atomic-replace, and symlink-safe resolution.
#
# PRESENCE IS JUDGED ON THE SCRIPT NAME, deliberately, and this must not be
# "harmonised" to an exact-string match. The live wiring on a machine running
# the harness routes through a dispatch shim:
#   bash $HOME/.claude/state/bin/hook-shim.sh gate $HOME/.claude/hooks/swarm-heartbeat.sh
# An exact match would not recognise that working wiring and would append a
# SECOND heartbeat entry. Both beats then advance the ONE delivery cursor keyed
# on (runid, agent_id), so one beat consumes rows the other never emitted:
# silent message loss, not a visible duplicate.
python3 "$SELF_DIR/settings_edit.py" add \
  --file "$SETTINGS" \
  --event PostToolUse \
  --matcher '*' \
  --command "$HOOK_CMD" \
  --match-substring swarm-heartbeat.sh
EDIT_RC=$?
case "$EDIT_RC" in
  0) ;;
  3) fail "settings.json is not valid JSON -- REFUSED, nothing written. Fix or restore it, then re-run." ;;
  4) fail "settings.json changed under us (concurrent write) -- REFUSED, nothing overwritten. Re-run." ;;
  5) fail "could not take the settings lock -- another comms installer is running." ;;
  *) fail "settings.json edit failed (rc=$EDIT_RC)" ;;
esac

# ---- (b2) install the comms-say skill, idempotently -----------------------
bash "$SELF_DIR/install-skill.sh" || fail "comms-say skill install failed"

# ---- (c) CLI location (PATH install is out of scope) ----------------------
echo "comms CLI: $REPO_ROOT/bin/comms"
echo "suggested alias: alias comms='$REPO_ROOT/bin/comms'"

# ---- (d) post-install verification: run the existing suites ---------------
# TEST SEAM, NOT AN ESCAPE HATCH. COMMS_SKIP_VERIFY=1 skips the suites and
# says so. It exists because tests/test_install_parity.sh installs a dozen
# times into throwaway HOMEs to check the resulting TREE, and re-running the
# whole corpus on each of those installs would make the parity suite cost
# minutes to prove something the suites do not speak to. It reports rc=2
# (could-not-verify), never 0: a skipped verification must never be able to
# masquerade as a passed one, whatever set the variable.
if [ "${COMMS_SKIP_VERIFY:-0}" = "1" ]; then
  echo "install: wiring complete; verification SKIPPED (COMMS_SKIP_VERIFY=1)"
  noverify "verification was skipped by request; run the suites yourself"
fi

# PYTEST IS NOT ON THIS MACHINE'S python3 -- any of them. Gating on
# `python3 -m pytest` made the step that proves the install works the one step
# that could never run, so a fresh machine got exit 2 ("COULD NOT VERIFY")
# every single time, and exit 2 is not a pass. The fix is to reach a runner
# that EXISTS, not to drop the verification: try the interpreter's own pytest
# first, then uv, which can fetch pytest into a throwaway env. Only when
# neither route exists is this genuinely unverifiable, and only then exit 2.
if python3 -m pytest --version >/dev/null 2>&1; then
  echo "verification: running pytest via python3 -m pytest"
  python3 -m pytest "$REPO_ROOT/tests" -q
  PYTEST_RC=$?
elif command -v uv >/dev/null 2>&1; then
  echo "verification: python3 has no pytest; running it via uv"
  uv run --with pytest python -m pytest "$REPO_ROOT/tests" -q
  PYTEST_RC=$?
else
  noverify "no pytest and no uv; install either (python3 -m pip install pytest) and re-run"
fi
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
