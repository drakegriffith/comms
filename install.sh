#!/bin/bash
# install.sh -- THE canonical installer for the comms stack.
#
# The rule this implements: anyone who installs should end up with the
# functionality the author has. Before this existed the repo had four adapter
# installers and no entrypoint, so "install comms" was a thing you had to
# already know how to do -- which is the definition of a stack only its author
# can run. One command now:
#
#     bash install.sh                # install / re-run safely (idempotent)
#     bash install.sh --check        # dry run; writes NOTHING, anywhere
#     bash install.sh --repair       # re-assert every piece of a partial install
#     bash install.sh --uninstall    # remove wiring + CLI; KEEPS your mailbox data
#     bash install.sh --uninstall --purge-state   # also delete the state dir
#
# WHAT IT INSTALLS (the parity set, asserted by tests/test_install_parity.sh)
#   1. state dirs        $COMMS_STATE_DIR (default ~/.comms/state) + swarm-arm/
#   2. the CLI           a symlink at ~/.local/bin/comms into THIS checkout
#   3. claude-code push  PostToolUse swarm-heartbeat hook in settings.json,
#                        plus the comms-say skill
#   4. verification      the suites, run for real, codes reported
#
# IT ORCHESTRATES, IT DOES NOT REIMPLEMENT. Step 3 is delegated to
# adapters/claude-code/install.sh. adapters/probe/INSTALLER-CHECKLIST.md makes
# the per-adapter installer the unit of this repo, and a root script that
# open-coded the same wiring would be a second place for it to drift -- the
# exact defect this repo was extracted to end. This file adds only what no
# adapter owns: the machine-level pieces (state, CLI) and one entrypoint.
#
# WHAT IT DELIBERATELY DOES NOT INSTALL (opt-ins; each is printed at the end
# with its exact command, because a silent opt-in is a surprise):
#   codex hooks, gemini, the Discord mirror, the ambient machine-ops lane,
#   launchd jobs, remote.
#
# EVERY PATH IS RELATIVE TO THIS CHECKOUT. The only paths that may name ~/.claude
# or ~/.codex are the runtime-owned config files, because those files belong to
# the runtimes, not to us. Both are overridable for testing:
#   COMMS_SETTINGS=<path>   claude-code settings.json  (default ~/.claude/settings.json)
#   COMMS_STATE_DIR=<path>  state root                 (default ~/.comms/state)
#   COMMS_BIN_DIR=<path>    CLI symlink dir            (default ~/.local/bin)
#
# Exit: 0 installed + verified | 1 failed | 2 installed but NOT verified.
# EXIT 2 IS NOT A PASS. It means verification never ran, which says nothing at
# all about whether the install works.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
EDITOR_PY="$REPO_ROOT/adapters/claude-code/settings_edit.py"
HEARTBEAT="$REPO_ROOT/adapters/claude-code/swarm-heartbeat.sh"

SETTINGS="${COMMS_SETTINGS:-$HOME/.claude/settings.json}"
STATE_DIR="${COMMS_STATE_DIR:-$HOME/.comms/state}"
BIN_DIR="${COMMS_BIN_DIR:-$HOME/.local/bin}"

MODE=install
CHECK=0
PURGE=0
for arg in "$@"; do
  case "$arg" in
    --check)        CHECK=1 ;;
    --repair)       MODE=repair ;;
    --uninstall)    MODE=uninstall ;;
    --purge-state)  PURGE=1 ;;
    -h|--help)      sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "install: unknown argument: $arg (try --help)" >&2; exit 1 ;;
  esac
done

say()  { echo "==> $*"; }
fail() { echo "install: FAILED: $*" >&2; exit 1; }

# ---- prerequisites, checked against THIS checkout --------------------------
command -v python3 >/dev/null 2>&1 || fail "python3 not found on PATH"
for f in \
    bin/comms \
    lib/swarm_mailbox.py \
    lib/swarm_arm.py \
    lib/swarm_claims.py \
    adapters/claude-code/swarm-heartbeat.sh \
    adapters/claude-code/stdin-bounded.sh \
    adapters/claude-code/settings_edit.py \
    adapters/claude-code/install.sh \
    tests/test_swarm_heartbeat.sh \
    tests/test_comms_cli.sh; do
  [ -e "$REPO_ROOT/$f" ] || fail "missing $REPO_ROOT/$f -- incomplete checkout?"
done

# ===========================================================================
# UNINSTALL
# ===========================================================================
if [ "$MODE" = uninstall ]; then
  say "removing the claude-code hook wiring from $SETTINGS"
  # Matched on the script NAME, so a wiring installed under any spelling --
  # direct, or routed through a dispatch shim -- is found and removed.
  if [ -e "$SETTINGS" ]; then
    ARGS=(remove --file "$SETTINGS" --contains swarm-heartbeat.sh)
    [ "$CHECK" = 1 ] && ARGS+=(--check)
    python3 "$EDITOR_PY" "${ARGS[@]}" || fail "could not edit $SETTINGS"
  else
    echo "    $SETTINGS does not exist; nothing to unwire"
  fi

  say "removing the comms-say skill"
  SKILL_DIR="${COMMS_SKILLS_DIR:-$HOME/.claude/skills}/comms-say"
  if [ -d "$SKILL_DIR" ]; then
    [ "$CHECK" = 1 ] && echo "    would remove $SKILL_DIR" || { rm -rf "$SKILL_DIR"; echo "    removed $SKILL_DIR"; }
  else
    echo "    no skill at $SKILL_DIR"
  fi

  say "removing the CLI symlink"
  if [ -L "$BIN_DIR/comms" ]; then
    [ "$CHECK" = 1 ] && echo "    would remove $BIN_DIR/comms" || { rm -f "$BIN_DIR/comms"; echo "    removed $BIN_DIR/comms"; }
  elif [ -e "$BIN_DIR/comms" ]; then
    # Not ours to delete: a real file there was put there by someone else.
    echo "    NOTE: $BIN_DIR/comms is a regular file, not our symlink -- left alone"
  else
    echo "    no CLI symlink at $BIN_DIR/comms"
  fi

  if [ "$PURGE" = 1 ]; then
    say "purging state dir $STATE_DIR (--purge-state)"
    [ "$CHECK" = 1 ] && echo "    would delete $STATE_DIR" || rm -rf "$STATE_DIR"
  else
    say "KEEPING $STATE_DIR"
    echo "    Mailbox rows, rosters and cursors are DATA, not install artifacts."
    echo "    Delete them deliberately with --purge-state, never as a side effect."
  fi
  echo
  echo "uninstall: OK"
  exit 0
fi

# ===========================================================================
# INSTALL / REPAIR  (repair is install; every step is written to be re-assertable)
# ===========================================================================
[ "$MODE" = repair ] && say "repair: re-asserting every piece of the install"

# ---- 1. state dirs --------------------------------------------------------
say "state dirs under $STATE_DIR"
if [ "$CHECK" = 1 ]; then
  echo "    would create $STATE_DIR and $STATE_DIR/swarm-arm"
else
  mkdir -p "$STATE_DIR/swarm-arm" || fail "cannot create $STATE_DIR/swarm-arm"
  echo "    ok $STATE_DIR/swarm-arm"
fi

# ---- 2. CLI reachable -----------------------------------------------------
# A symlink, not a copy: a copy goes stale the moment the checkout is updated,
# and a stale CLI against a current lib/ is the drift this repo exists to stop.
# bin/comms chases symlinks to find its own checkout, so this resolves correctly.
say "CLI at $BIN_DIR/comms -> $REPO_ROOT/bin/comms"
if [ "$CHECK" = 1 ]; then
  echo "    would symlink $BIN_DIR/comms"
elif [ -e "$BIN_DIR/comms" ] && [ ! -L "$BIN_DIR/comms" ]; then
  echo "    REFUSING: $BIN_DIR/comms exists and is not a symlink."
  echo "    Something else owns that name. Remove it yourself, then re-run;"
  echo "    this installer does not delete files it did not create."
else
  mkdir -p "$BIN_DIR" || fail "cannot create $BIN_DIR"
  ln -sfn "$REPO_ROOT/bin/comms" "$BIN_DIR/comms" || fail "cannot symlink into $BIN_DIR"
  echo "    ok"
  case ":${PATH}:" in
    *":$BIN_DIR:"*) : ;;
    *) echo "    NOTE: $BIN_DIR is not on your PATH. Either add it, or invoke"
       echo "          the CLI by absolute path: $REPO_ROOT/bin/comms" ;;
  esac
fi

# ---- 3. the claude-code integration (DELEGATED) ---------------------------
# THE HIGHEST-RISK WRITE ON THE MACHINE lives at the end of this call: a
# mangled settings.json breaks every Claude session, not just comms. The
# adapter installer performs it through settings_edit.py -- lock, snapshot,
# refuse-on-unparseable, backup, staged parse, re-check that the file has not
# changed under us, atomic replace, symlink-safe. See that file's header.
#
# --check maps onto the editor's own dry run rather than being faked here, so
# what --check exercises is the SAME code path that would run for real.
say "claude-code integration (PostToolUse hook + comms-say skill)"
if [ "$CHECK" = 1 ]; then
  python3 "$EDITOR_PY" add --file "$SETTINGS" --event PostToolUse --matcher '*' \
    --command "bash $HEARTBEAT" --match-substring swarm-heartbeat.sh --check
  HOOK_RC=$?
  echo "    (--check: the comms-say skill install is not exercised)"
else
  COMMS_SETTINGS="$SETTINGS" bash "$REPO_ROOT/adapters/claude-code/install.sh"
  HOOK_RC=$?
fi

case "$HOOK_RC" in
  0) ;;
  2) # The adapter could not VERIFY. The wiring itself did land; what did not
     # happen is the proof. Say exactly that, and carry the 2 outward -- a
     # could-not-verify reported as success is how an install that never
     # worked gets believed.
     echo
     echo "install: wiring landed, but VERIFICATION COULD NOT RUN (rc=2)."
     echo "         EXIT 2 IS NOT A PASS. See the adapter's message above."
     exit 2 ;;
  3) fail "settings.json is not valid JSON -- REFUSED, nothing was written. Fix or restore it, then re-run." ;;
  4) fail "settings.json changed while installing (a live Claude session wrote it) -- REFUSED, nothing was overwritten. Re-run." ;;
  5) fail "could not take the settings lock -- another installer is running." ;;
  *) fail "claude-code adapter install failed (rc=$HOOK_RC)" ;;
esac

if [ "$CHECK" = 1 ]; then
  echo
  echo "--check: nothing was written. Re-run without --check to install."
  exit 0
fi

# ---- opt-ins, printed with their exact commands ---------------------------
cat <<EOF

--------------------------------------------------------------------------
Installed. Optional adapters -- none of these were touched:

  codex push hook        bash $REPO_ROOT/adapters/codex/install.sh
                         HAZARD: also maintains a marker-fenced block in
                         ~/.codex/AGENTS.md. If that path is a SYMLINK on your
                         machine, check the adapter's symlink handling before
                         running it -- see "Known parity gaps" in README.md.
  gemini push hook       bash $REPO_ROOT/adapters/gemini/install.sh
  discord mirror         bash $REPO_ROOT/adapters/discord/install.sh
                         (preflight only; needs DISCORD_COMMS_WEBHOOK_URL in
                          ~/.secrets/comms.env, and it writes nothing itself)
  launchd nightly job    bash $REPO_ROOT/adapters/launchd/install.sh
                         (writes the plist; you run launchctl yourself)
  remote seats           bash $REPO_ROOT/adapters/remote/install.sh
  ambient machine-ops    bash $REPO_ROOT/adapters/claude-code/ambient/install.sh
                         REQUIRES a dispatch shim at
                         ~/.claude/state/bin/hook-shim.sh that this repo DOES
                         NOT SHIP. Without it that installer exits 2. See
                         "Known parity gaps" in README.md.
  kimi / pi / other      no install; poll with 'comms read' -- adapters/*/README.md

Quickstart:
  $REPO_ROOT/bin/comms arm myrun --topic proj
  $REPO_ROOT/bin/comms enroll myrun --seat alpha --topics proj
  $REPO_ROOT/bin/comms post myrun alpha finding "hello" --topic proj
--------------------------------------------------------------------------
EOF

# The adapter installer ran the suites and its exit code already carries their
# verdict (0 verified, 2 could-not-verify, 1 failed); all three were handled
# above. Reaching here means it returned 0.
echo "install: OK (installed + verified)"
exit 0
