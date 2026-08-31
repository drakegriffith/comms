#!/bin/bash
# tests/test_install_parity.sh -- the PARITY suite for the canonical installer.
#
# Drake's rule is the spec: "anyone who installs should have the functionality
# that I have." This suite is the executable form of that sentence. It installs
# into a mktemp HOME -- never the real one -- and asserts the resulting tree and
# settings entries match the expected set.
#
# COUNTER BEFORE GATE: every assertion increments a counter at execution time.
# A run that inspected zero subjects fails loudly (see the tail of this file);
# a suite that silently ran nothing is not a pass. Exit 2 is not a pass either.
#
# Run:  bash tests/test_install_parity.sh
# Exit: 0 all asserts passed | 1 at least one failed / zero subjects inspected.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SELF_DIR/.." && pwd)"
# COMMS_INSTALLER retargets the suite at a different entrypoint. It exists so
# the SAME bar can be held up against the pre-parity installer
# (adapters/claude-code/install.sh) to show what a fresh machine got before.
INSTALLER="${COMMS_INSTALLER:-$REPO_ROOT/install.sh}"

PASS=0
FAIL=0

ok()   { PASS=$((PASS + 1)); echo "  ok   $*"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL $*"; }
ck()   { if [ "$1" = "$2" ]; then ok "$3 (=$1)"; else bad "$3: expected [$2] got [$1]"; fi; }
ckf()  { if [ -f "$1" ]; then ok "$2"; else bad "$2: no such file $1"; fi; }
ckd()  { if [ -d "$1" ]; then ok "$2"; else bad "$2: no such dir $1"; fi; }
cknf() { if [ -e "$1" ]; then bad "$2: $1 still exists"; else ok "$2"; fi; }

# A sandbox HOME per scenario. Nothing below ever names the real $HOME.
newhome() {
  SB="$(mktemp -d "${TMPDIR:-/tmp}/comms-parity.XXXXXX")"
  export SB
  mkdir -p "$SB/.claude"
}

# Run the installer with HOME redirected into the sandbox. env -i would drop
# PATH, so HOME is overridden explicitly and the rest of the environment is
# kept; COMMS_STATE_DIR is left unset on purpose so the default (~/.comms/state,
# i.e. the SANDBOX's) is what gets exercised.
#
# COMMS_SKIP_VERIFY=1: these scenarios are about the resulting TREE and the
# SETTINGS ENTRIES, which is what parity means here. Re-running the full test
# corpus on each of a dozen throwaway installs would cost minutes and prove
# nothing the corpus does not already prove on its own. The skip is not free:
# it makes the installer report rc=2 (could-not-verify), and scenario 11 does a
# real end-to-end install WITH verification to close that gap.
WIRED=2   # expected rc when verification was skipped -- 2, never 0
run_install() {
  HOME="$SB" \
  COMMS_STATE_DIR= COMMS_ROOT= COMMS_SKIP_VERIFY=1 \
  bash "$INSTALLER" "$@" 2>&1
}

# Read the PostToolUse commands out of the sandbox settings.json.
ptu_commands() {
  python3 - "$SB/.claude/settings.json" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for e in d.get("hooks", {}).get("PostToolUse", []):
    if isinstance(e, dict):
        for h in e.get("hooks", []) or []:
            if isinstance(h, dict):
                print(h.get("command", ""))
PY
}

jget() {  # jget <file> <python-expr over `d`>
  python3 - "$1" "$2" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(eval(sys.argv[2]))
PY
}

echo "== 0. the canonical entrypoint exists =="
# The whole premise: a fresh machine must have ONE documented command. If this
# fails, everything below is moot -- there is nothing to install with.
if [ -f "$INSTALLER" ]; then ok "install.sh exists at repo root"; else bad "install.sh missing at repo root ($INSTALLER)"; fi
if [ -x "$INSTALLER" ]; then ok "install.sh is executable"; else bad "install.sh not executable"; fi
if [ ! -f "$INSTALLER" ]; then
  echo
  echo "parity: $PASS passed, $FAIL failed (aborted: no installer to exercise)"
  exit 1
fi

echo "== 1. fresh install into an empty HOME =="
newhome
OUT="$(run_install)"; RC=$?
echo "$OUT" | sed 's/^/    | /'
ck "$RC" "$WIRED" "fresh install exit code"
ckd "$SB/.comms/state" "state dir created"
ckd "$SB/.comms/state/swarm-arm" "arm registry dir created"
ckf "$SB/.claude/settings.json" "settings.json created"

# The hook must point at THIS checkout by absolute path -- a wiring that names a
# path not on this machine is the exact failure the shim used to hide (silent
# exit 0 when the checkout is missing).
CMDS="$(ptu_commands)"
if echo "$CMDS" | grep -q "$REPO_ROOT/adapters/claude-code/swarm-heartbeat.sh"; then
  ok "PostToolUse hook wired to this checkout's heartbeat"
else
  bad "PostToolUse heartbeat wiring absent; got: $CMDS"
fi
ck "$(echo "$CMDS" | grep -c 'swarm-heartbeat.sh')" "1" "exactly one heartbeat entry"

# CLI reachable: the installer must leave a runnable comms on disk, not just
# print an alias suggestion a fresh user has to paste by hand.
CLI="$SB/.local/bin/comms"
if [ -x "$CLI" ] || [ -L "$CLI" ]; then ok "CLI installed at ~/.local/bin/comms"; else bad "CLI not installed at $CLI"; fi
HOME="$SB" "$CLI" arm parity-run --topic t >/dev/null 2>&1; ARMRC=$?
ck "$ARMRC" "0" "installed CLI actually runs (comms arm)"

# The claude-code adapter also ships the comms-say skill. A fresh install that
# wires the hook but drops the skill is not parity: the 1-1 terminal UX is part
# of what the author has.
ckf "$SB/.claude/skills/comms-say/SKILL.md" "comms-say skill installed"

echo "== 2. idempotent re-run over an existing install =="
BEFORE="$(cat "$SB/.claude/settings.json")"
OUT2="$(run_install)"; RC2=$?
ck "$RC2" "$WIRED" "re-run exit code"
AFTER="$(cat "$SB/.claude/settings.json")"
if [ "$BEFORE" = "$AFTER" ]; then ok "re-run left settings.json byte-identical"; else bad "re-run mutated settings.json"; fi
ck "$(ptu_commands | grep -c 'swarm-heartbeat.sh')" "1" "still exactly one heartbeat entry (no duplicate)"

echo "== 3. unrelated settings are never clobbered =="
newhome
cat > "$SB/.claude/settings.json" <<'JSON'
{
  "model": "opus",
  "permissions": {"allow": ["Bash(ls:*)"]},
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "bash /tmp/someone-elses-gate.sh"}]}
    ]
  }
}
JSON
run_install >/dev/null 2>&1; RC3=$?
ck "$RC3" "$WIRED" "install over a populated settings.json"
ck "$(jget "$SB/.claude/settings.json" 'd["model"]')" "opus" "unrelated top-level key preserved"
ck "$(jget "$SB/.claude/settings.json" 'd["permissions"]["allow"][0]')" "Bash(ls:*)" "unrelated permissions preserved"
ck "$(jget "$SB/.claude/settings.json" 'len(d["hooks"]["PreToolUse"])')" "1" "foreign PreToolUse hook preserved"
if [ -n "$(ls "$SB/.claude/"settings.json.comms-backup.* 2>/dev/null)" ]; then
  ok "a timestamped backup was written before the edit"
else
  bad "no backup written before editing an existing settings.json"
fi

echo "== 4. a corrupt settings.json is REFUSED, never rewritten =="
newhome
printf '{"model": "opus", TRUNCATED' > "$SB/.claude/settings.json"
CORRUPT_BEFORE="$(cat "$SB/.claude/settings.json")"
OUT4="$(run_install)"; RC4=$?
if [ "$RC4" -ne 0 ]; then ok "refused with nonzero exit ($RC4)"; else bad "install returned 0 over corrupt settings.json"; fi
ck "$(cat "$SB/.claude/settings.json")" "$CORRUPT_BEFORE" "corrupt file left byte-for-byte untouched"
if echo "$OUT4" | grep -qi 'not valid json\|refus'; then ok "refusal message names the reason"; else bad "refusal message unclear: $OUT4"; fi

echo "== 5. a CONCURRENT write to settings.json is not clobbered =="
# The highest-risk line of code in the installer. A read-modify-write that
# snapshots the file, then writes it back, silently discards anything another
# process wrote in between -- and a lost hooks block breaks every Claude
# session on the machine. The installer must detect the change and refuse.
# COMMS_SETTINGS_RACE_HOOK is a TEST SEAM: a command the editor runs after it
# has snapshotted the file, simulating the concurrent writer deterministically.
newhome
cat > "$SB/.claude/settings.json" <<'JSON'
{"model": "opus", "hooks": {}}
JSON
RACER="$SB/racer.sh"
cat > "$RACER" <<JSON
#!/bin/bash
touch "$SB/racer-ran"
printf '%s' '{"model": "opus", "statusLine": {"type": "command", "command": "x"}, "hooks": {}}' > "$SB/.claude/settings.json"
JSON
chmod +x "$RACER"
OUT5="$(HOME="$SB" COMMS_SETTINGS_RACE_HOOK="bash $RACER" bash "$INSTALLER" 2>&1)"; RC5=$?
echo "$OUT5" | sed 's/^/    | /'
# Assert the seam actually fired. Without this, an installer that simply
# IGNORES the env var scores the same as one that honours it, and the scenario
# would prove nothing about a real race -- only that the var was unread.
ckf "$SB/racer-ran" "the race seam fired (installer honours the interposition point)"
if [ "$RC5" -ne 0 ]; then ok "refused the racy write with nonzero exit ($RC5)"; else bad "install returned 0 despite a concurrent write"; fi
if echo "$OUT5" | grep -qi 'concurrent\|changed'; then ok "message names the concurrency"; else bad "no concurrency message: $OUT5"; fi
# The concurrent writer's content must survive intact.
ck "$(jget "$SB/.claude/settings.json" 'd.get("statusLine",{}).get("command","MISSING")')" "x" "concurrent writer's change survived"

echo "== 6. uninstall removes the wiring and leaves foreign settings alone =="
newhome
cat > "$SB/.claude/settings.json" <<'JSON'
{
  "model": "opus",
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "bash /tmp/someone-elses-gate.sh"}]}
    ]
  }
}
JSON
run_install >/dev/null 2>&1
ck "$(ptu_commands | grep -c 'swarm-heartbeat.sh')" "1" "installed before uninstall"
OUT6="$(run_install --uninstall)"; RC6=$?
echo "$OUT6" | sed 's/^/    | /'
ck "$RC6" "0" "uninstall exit code"
ck "$(ptu_commands | grep -c 'swarm-heartbeat.sh')" "0" "heartbeat wiring removed"
ck "$(jget "$SB/.claude/settings.json" 'len(d["hooks"]["PreToolUse"])')" "1" "foreign hook survived uninstall"
ck "$(jget "$SB/.claude/settings.json" 'd["model"]')" "opus" "unrelated key survived uninstall"
cknf "$SB/.local/bin/comms" "CLI symlink removed"
cknf "$SB/.claude/skills/comms-say" "comms-say skill removed"
ckd "$SB/.comms/state" "state dir (user data) NOT deleted by uninstall"

echo "== 7. repair after a stale/partial install =="
# Drake's stated intent: delete a stale copy and reinstall. Repair must survive
# a half-removed install -- wiring present, CLI gone -- without duplicating.
newhome
run_install >/dev/null 2>&1
rm -f "$SB/.local/bin/comms"
OUT7="$(run_install --repair)"; RC7=$?
ck "$RC7" "$WIRED" "repair exit code"
if [ -x "$SB/.local/bin/comms" ] || [ -L "$SB/.local/bin/comms" ]; then ok "repair restored the CLI"; else bad "repair did not restore the CLI"; fi
ck "$(ptu_commands | grep -c 'swarm-heartbeat.sh')" "1" "repair did not duplicate the hook entry"

echo "== 8. --check writes nothing =="
newhome
OUT8="$(run_install --check)"; RC8=$?
ck "$RC8" "0" "--check exit code"
cknf "$SB/.claude/settings.json" "--check created no settings.json"
cknf "$SB/.comms" "--check created no state dir"

echo "== 9. THE PARITY TRAP: a shim-routed wiring is detected, never duplicated =="
# The live machine does not register the heartbeat by its bare path. It routes
# it through the harness's dispatch shim:
#   bash $HOME/.claude/state/bin/hook-shim.sh gate $HOME/.claude/hooks/swarm-heartbeat.sh
# That is a WORKING wiring. An installer whose presence check is an EXACT
# string match does not recognise it and appends a SECOND heartbeat entry.
# The damage is not a cosmetic duplicate: both beats advance the ONE delivery
# cursor keyed on (runid, agent_id), so one beat consumes rows the other never
# emitted. That is SILENT MESSAGE LOSS -- the failure this whole stack exists
# to prevent. Presence must therefore be judged on the script NAME.
newhome
cat > "$SB/.claude/settings.json" <<'JSON'
{
  "model": "opus",
  "hooks": {
    "PostToolUse": [
      {"matcher": "*", "hooks": [{"type": "command", "command": "bash $HOME/.claude/state/bin/hook-shim.sh gate $HOME/.claude/hooks/swarm-heartbeat.sh"}]}
    ]
  }
}
JSON
OUT9T="$(run_install)"; RC9T=$?
ck "$RC9T" "$WIRED" "install over a shim-routed wiring"
ck "$(ptu_commands | grep -c 'swarm-heartbeat.sh')" "1" "STILL exactly one heartbeat entry (no second beat on the cursor)"
# Assert on the FILE, not on the wording of the installer's chatter: the
# surviving command is the fact that matters, and pinning a message string
# would make this suite fail on a rephrase rather than on a regression.
if ptu_commands | grep -q 'hook-shim.sh gate'; then ok "the shim-routed command survived untouched in the file"; else bad "the shim-routed command was altered or removed"; fi
if echo "$OUT9T" | grep -qi 'already wired\|already present\|left untouched'; then ok "installer reported the existing wiring"; else bad "installer did not report the existing wiring"; fi

echo "== 10. no pytest and no uv: WIRED but honestly UNVERIFIED (rc 2, not 0) =="
# Two OPPOSITE failure modes, and this scenario pins both shut.
#   * Reporting success when verification never ran. Exit 2 is not a pass.
#   * "Fixing" the 2 by deleting the verification step, which does not perform
#     the check, it just stops asking.
# Under a stock PATH there is neither pytest nor uv, so the only honest result
# is: the wiring lands, and the installer says it could not verify, with rc 2.
newhome
OUT10="$(HOME="$SB" PATH="/usr/bin:/bin:/usr/sbin:/sbin" bash "$INSTALLER" 2>&1)"; RC10=$?
ck "$RC10" "2" "rc is 2 (could-not-verify), not 0 and not 1"
ck "$(ptu_commands | grep -c 'swarm-heartbeat.sh')" "1" "the wiring still landed"
if echo "$OUT10" | grep -qi 'not a pass\|could not verify'; then
  ok "output states plainly that exit 2 is not a pass"
else
  bad "output does not flag could-not-verify: $OUT10"
fi

echo "== 11b. SYMLINKED config targets are written THROUGH, not severed =="
# os.replace(tmp, link) unlinks the symlink and leaves a plain file behind. On
# a dotfile-managed machine the config paths these installers write are often
# symlinks into a managed repo -- on THIS machine ~/.codex/AGENTS.md is a
# symlink into a separate harness checkout -- so an installer that ignores this
# silently severs the link and orphans the real file. Drake's stated plan is to
# delete a stale install and re-run these, which is exactly the path that would
# trigger it.
newhome
mkdir -p "$SB/real" "$SB/.codex" "$SB/.claude"
echo '{"model": "opus"}' > "$SB/real/settings.json"
printf 'PRE-EXISTING HARNESS CONTENT\n' > "$SB/real/AGENTS.md"
ln -sfn "$SB/real/settings.json" "$SB/.claude/settings.json"
ln -sfn "$SB/real/AGENTS.md" "$SB/.codex/AGENTS.md"

run_install >/dev/null 2>&1
if [ -L "$SB/.claude/settings.json" ]; then ok "settings.json symlink SURVIVED the install"; else bad "settings.json symlink was severed"; fi
ck "$(jget "$SB/real/settings.json" 'len(d["hooks"]["PostToolUse"])')" "1" "the wiring landed in the symlink TARGET"

HOME="$SB" COMMS_CODEX_HOOKS="$SB/.codex/hooks.json" COMMS_CODEX_AGENTS="$SB/.codex/AGENTS.md" \
  bash "$REPO_ROOT/adapters/codex/install.sh" >/dev/null 2>&1
if [ -L "$SB/.codex/AGENTS.md" ]; then ok "AGENTS.md symlink SURVIVED the codex install"; else bad "AGENTS.md symlink was SEVERED by the codex install"; fi
if grep -q 'PRE-EXISTING HARNESS CONTENT' "$SB/real/AGENTS.md"; then ok "pre-existing harness content preserved"; else bad "pre-existing harness content destroyed"; fi
if grep -q 'comms:begin' "$SB/real/AGENTS.md"; then ok "the comms block landed in the symlink target"; else bad "comms block did not reach the target"; fi

echo "== 11. a REAL end-to-end install, verification actually run =="
# Every scenario above skips the suites to stay fast. This one does not: it is
# the scenario proving the success path can reach a working test runner and
# report a verdict it actually derived.
#
# It asserts the installer AGREES WITH THE CORPUS, rather than pinning rc=0.
# Pinning 0 would make this suite fail whenever any unrelated test in the repo
# is red -- it would be a second, worse copy of the corpus. The property that
# belongs to the INSTALLER is: it must not claim success when the suites fail,
# and must not claim failure when they pass. So derive the corpus verdict
# independently, then require the installer to match it.
# The corpus verdict is derived BEFORE the sandbox is created: some suites in
# it sweep temp state, and a sandbox that existed during the sweep can be
# collected out from under this scenario.
if command -v uv >/dev/null 2>&1; then
  RUNNER="uv run --with pytest python -m pytest"
elif python3 -m pytest --version >/dev/null 2>&1; then
  RUNNER="python3 -m pytest"
else
  RUNNER=""
fi

if [ -z "$RUNNER" ]; then
  bad "no test runner available (neither pytest nor uv) -- cannot prove the success path"
else
  $RUNNER "$REPO_ROOT/tests" -q >/tmp/comms-parity-corpus.$$ 2>&1
  CORPUS_RC=$?
  echo "    corpus verdict (derived independently): rc=$CORPUS_RC -- $(tail -1 /tmp/comms-parity-corpus.$$)"
  rm -f /tmp/comms-parity-corpus.$$

  newhome
  OUT11="$(HOME="$SB" bash "$INSTALLER" 2>&1)"; RC11=$?
  ck "$(ptu_commands | grep -c 'swarm-heartbeat.sh')" "1" "one heartbeat entry after a real install"
  if [ "$CORPUS_RC" -eq 0 ]; then
    ck "$RC11" "0" "corpus green => installer exits 0"
    if echo "$OUT11" | grep -qi 'installed + verified'; then ok "reports installed + verified"; else bad "no verified message"; fi
  else
    # The corpus is red for reasons that have nothing to do with installing.
    # The installer's duty here is to REPORT that, not to launder it into a 0.
    ck "$RC11" "1" "corpus red => installer exits 1 (does NOT launder failure into success)"
    if echo "$OUT11" | grep -qi 'verification failed'; then ok "names the verification failure"; else bad "does not name the failure"; fi
    if echo "$OUT11" | grep -qi 'installed + verified'; then bad "FALSELY claims verified while the corpus is red"; else ok "makes no false verified claim"; fi
  fi
fi

rm -rf "${TMPDIR:-/tmp}"/comms-parity.* 2>/dev/null

echo
SUBJECTS=$((PASS + FAIL))
echo "parity: $PASS passed, $FAIL failed, $SUBJECTS assertions executed"
if [ "$SUBJECTS" -eq 0 ]; then
  echo "parity: ZERO SUBJECTS INSPECTED -- that is a failure, not a pass" >&2
  exit 1
fi
[ "$FAIL" -eq 0 ] || exit 1
echo "parity: OK"
exit 0
