#!/bin/bash
# test_retire_preflight.sh -- suite for scripts/comms-retire-preflight.sh.
#
# WHAT IS ACTUALLY UNDER TEST. Not "does it print a verdict" -- it is the
# REFUSALS. A preflight checker that green-lights everything is
# indistinguishable from no preflight checker at all, and the way this class of
# tool fails is by returning a confident 0 from a scan that inspected nothing.
# So the cases below are, in order: the scan that found nothing must NOT pass;
# the copy that holds unique files must NOT pass; the referrer that would break
# must NOT pass; and only then, the genuinely-safe copy must pass.
#
# ISOLATION. Every case runs with HOME pointed at a fresh mktemp dir. The
# script's default referrer roots and install search roots are all $HOME-
# relative, and its live-state probe asks each install's own swarm_arm for a
# default that expands ~ -- so a faked HOME moves the entire world under test
# into the temp dir. This suite therefore reads nothing from and writes nothing
# to the real ~/.claude, ~/.comms or ~/code, and is green on repeat.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd -P)"
REPO="$(cd "$SELF_DIR/.." && pwd -P)"
PF="$REPO/scripts/comms-retire-preflight.sh"

pass=0
fail=0
ck() {
  if [ "$2" = "$3" ]; then pass=$((pass + 1)); echo "  ok   $1"
  else fail=$((fail + 1)); echo "  FAIL $1: expected [$2] got [$3]"; fi
}
ckcontains() {
  if printf '%s' "$3" | grep -Fq -- "$2"; then pass=$((pass + 1)); echo "  ok   $1"
  else
    fail=$((fail + 1)); echo "  FAIL $1: [$2] not in output"
    # Print the subject on failure. A red line that does not show what it read
    # sends the next person to re-run the whole suite by hand to see it.
    printf '%s\n' "$3" | sed 's|^|        > |'
  fi
}

# build_world <dir> -> a fake HOME containing:
#   $H/code/comms          an AUTHORITATIVE repo-shape install (a real clone of
#                          this checkout's files, so bin/comms really runs)
#   $H/.claude             a pre-extraction-shape install, git-tracked with a
#                          remote, holding comms/ (all mappable) and
#                          lib/swarm/ (mappable plus one unique file)
#   $H/.comms/state        the live state dir, with one armed run
build_world() {
  local H="$1"
  mkdir -p "$H/code" "$H/.comms/state/swarm-arm/machine-ops"

  # --- authoritative: a working copy of this checkout ---------------------
  mkdir -p "$H/code/comms/lib" "$H/code/comms/bin" "$H/code/comms/tests" \
           "$H/code/comms/adapters/claude-code"
  cp "$REPO"/lib/*.py            "$H/code/comms/lib/"
  cp "$REPO/bin/comms"           "$H/code/comms/bin/comms"
  chmod +x "$H/code/comms/bin/comms"
  cp "$REPO/README.md"           "$H/code/comms/README.md"
  cp "$REPO/tests/test_comms_cli.sh" "$H/code/comms/tests/"
  cp "$REPO/adapters/claude-code/install.sh" "$H/code/comms/adapters/claude-code/"

  # --- doomed: the pre-extraction shape ------------------------------------
  mkdir -p "$H/.claude/comms/bin" "$H/.claude/comms/tests" \
           "$H/.claude/lib/swarm" "$H/.claude/hooks"
  cp "$REPO/bin/comms" "$H/.claude/comms/bin/comms"
  chmod +x "$H/.claude/comms/bin/comms"
  echo "# old readme" > "$H/.claude/comms/README.md"
  echo "# old installer" > "$H/.claude/comms/install.sh"
  echo "# old smoke test" > "$H/.claude/comms/tests/test_comms_cli.sh"
  cp "$REPO"/lib/swarm_mailbox.py "$H/.claude/lib/swarm/"
  cp "$REPO"/lib/swarm_arm.py     "$H/.claude/lib/swarm/"
  cp "$REPO"/lib/swarm_claims.py  "$H/.claude/lib/swarm/"
  # The file with no successor anywhere. This is the whole point.
  echo "# seat allocation doctrine, harness-native, never extracted" \
    > "$H/.claude/lib/swarm/ROLES.md"

  # A settings.json with one registration, pointed at the SURVIVOR. Without a
  # config to read, the hook-registration scan has zero subjects and the whole
  # checker is obliged to answer "cannot determine" -- so every case here needs
  # one, and cases that want a hook hit overwrite it.
  cat > "$H/.claude/settings.json" <<'EOF'
{"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [{"type": "command",
  "command": "bash $HOME/code/comms/adapters/claude-code/swarm-heartbeat.sh"}]}]}}
EOF

  # Make ~/.claude a tracked checkout with a remote that contains HEAD, so the
  # recoverability check has something true to find.
  ( cd "$H/.claude" \
    && git init -q -b master . \
    && git config user.email t@t && git config user.name t \
    && git add -A && git commit -qm init \
    && git clone -q --bare . "$H/remote.git" \
    && git remote add origin "$H/remote.git" \
    && git fetch -q origin ) >/dev/null 2>&1
}

# Commit AND push. The checker refuses when no remote-tracking ref contains
# HEAD, which is correct behaviour and which a fixture that only commits will
# trip on every case -- masking whatever that case was actually testing.
commit_and_push() {  # commit_and_push <fakehome> <msg>
  ( cd "$1/.claude" \
    && git add -A && git commit -qm "$2" \
    && git push -q origin HEAD ) >/dev/null 2>&1
}

run_pf() {  # run_pf <fakehome> <args...>
  local H="$1"; shift
  local out rc
  out="$(HOME="$H" COMMS_CHECKOUT="" bash "$PF" "$@" 2>&1)"
  rc=$?
  # PF_TEST_DEBUG=1 dumps every run. The checker's whole value is the sentence
  # it prints, so a suite that can only show pass/fail is hard to debug against.
  [ "${PF_TEST_DEBUG:-0}" = "1" ] && printf '%s\n' "$out" | sed 's|^|    |' >&2
  printf '%s' "$out"
  return "$rc"
}

echo "== a. a scan that inspected nothing is exit 2, never exit 0 =="
T="$(mktemp -d -t pftest)"; H="$T/home"; mkdir -p "$H"
mkdir -p "$H/empty-dir"
OUT="$(run_pf "$H" --doomed "$H/empty-dir")"; RC=$?
ck "a1 rc is 2 (cannot determine), not 0" "2" "$RC"
ckcontains "a2 says exit 2 is not a pass" "NOT a pass" "$OUT"
ckcontains "a3 names the failed enumerator" "installs_found" "$OUT"
rm -rf "$T"

echo "== b. a lone install is not a duplicate =="
T="$(mktemp -d -t pftest)"; H="$T/home"; build_world "$H"
rm -rf "$H/code/comms"
OUT="$(run_pf "$H" --doomed "$H/.claude/comms")"; RC=$?
ck "b1 rc is 2 with no second copy" "2" "$RC"
ckcontains "b2 refuses to call a sole install a duplicate" "SOLE install" "$OUT"
rm -rf "$T"

echo "== c. THE LOAD-BEARING CASE: a file with no successor blocks deletion =="
# lib/swarm/ROLES.md exists only in the doomed tree. Reinstalling from the
# authoritative repo would not put it back, so 'delete then reinstall' is not
# a round trip, it is a deletion. The checker must say so.
T="$(mktemp -d -t pftest)"; H="$T/home"; build_world "$H"
OUT="$(run_pf "$H" --doomed "$H/.claude/lib/swarm" --authoritative "$H/code/comms")"; RC=$?
ck "c1 rc is 1 (refused)" "1" "$RC"
ckcontains "c2 verdict is REFUSED" "PREFLIGHT: REFUSED" "$OUT"
ckcontains "c3 names the orphan by path" "ROLES.md" "$OUT"
ckcontains "c4 says it is deleting content, not a duplicate" "deleting content" "$OUT"
# The counts have to be printed, not just the verdict: an unexplained refusal
# gets overridden by the next person in a hurry.
ckcontains "c5 prints subjects_inspected" "subjects_inspected" "$OUT"
ckcontains "c6 prints restored_by_reinstall" "restored_by_reinstall" "$OUT"
rm -rf "$T"

echo "== d. a referrer naming an orphan is HARD and blocks =="
T="$(mktemp -d -t pftest)"; H="$T/home"; build_world "$H"
# Exactly the real referrer shape: a doctrine file naming a RELATIVE path, with
# no absolute path anywhere in it for a naive grep to find.
printf 'Before any dispatch, read lib/swarm/ROLES.md first.\n' > "$H/.claude/AGENTS.md"
commit_and_push "$H" referrer
OUT="$(run_pf "$H" --doomed "$H/.claude/lib/swarm" --authoritative "$H/code/comms")"; RC=$?
ck "d1 rc is 1 (refused)" "1" "$RC"
ckcontains "d2 the relative-path referrer was found" "AGENTS.md" "$OUT"
ckcontains "d3 it is classified HARD" "HARD" "$OUT"
ckcontains "d4 hard_referrers is reported as a count" "hard_referrers = " "$OUT"
rm -rf "$T"

echo "== e. deleting the authority itself is refused =="
T="$(mktemp -d -t pftest)"; H="$T/home"; build_world "$H"
OUT="$(run_pf "$H" --doomed "$H/code/comms" --authoritative "$H/code/comms")"; RC=$?
ck "e1 rc is 1" "1" "$RC"
ckcontains "e2 says so plainly" "IS the authoritative install" "$OUT"
rm -rf "$T"

echo "== f. uncommitted content blocks: single-copy bytes are unrecoverable =="
T="$(mktemp -d -t pftest)"; H="$T/home"; build_world "$H"
echo "notes nobody committed" > "$H/.claude/comms/scratch.md"
OUT="$(run_pf "$H" --doomed "$H/.claude/comms" --authoritative "$H/code/comms")"; RC=$?
ck "f1 rc is 1" "1" "$RC"
ckcontains "f2 names the uncommitted-path refusal" "uncommitted or untracked" "$OUT"
rm -rf "$T"

echo "== h. a GATE-mode hook registration on the doomed path is the hard stop =="
# This is the refusal with the largest blast radius, and the one least visible
# by reading: the registration is a JSON string with a $HOME in it, so neither
# an absolute-path grep nor a human skim of the directory finds it. Deleting
# the target of a "*"-matcher gate hook does not break the mailbox; it starts
# refusing every tool call on the machine, in sessions already running.
T="$(mktemp -d -t pftest)"; H="$T/home"; build_world "$H"
cat > "$H/.claude/settings.json" <<'EOF'
{"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [{"type": "command",
  "command": "bash $HOME/.claude/state/bin/hook-shim.sh gate $HOME/.claude/comms/bin/comms"}]}]}}
EOF
commit_and_push "$H" settings
OUT="$(run_pf "$H" --doomed "$H/.claude/comms" --authoritative "$H/code/comms")"; RC=$?
ck "h1 rc is 1 (refused)" "1" "$RC"
ckcontains "h2 the \$HOME-spelled registration was resolved and found" "registrations_naming_doomed = 1" "$OUT"
ckcontains "h3 the mode is identified as gate" "mode=gate" "$OUT"
ckcontains "h4 the refusal states the blast radius" "every matching tool call on this machine is refused" "$OUT"
ckcontains "h5 the refusal states the required ORDER" "REWIRE the registration to the surviving path FIRST" "$OUT"
rm -rf "$T"

echo "== i. an observer-mode registration also blocks, at lower severity =="
T="$(mktemp -d -t pftest)"; H="$T/home"; build_world "$H"
cat > "$H/.claude/settings.json" <<'EOF'
{"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
  "command": "bash $HOME/.claude/state/bin/hook-shim.sh observer $HOME/.claude/comms/bin/comms"}]}]}}
EOF
commit_and_push "$H" settings
OUT="$(run_pf "$H" --doomed "$H/.claude/comms" --authoritative "$H/code/comms")"; RC=$?
ck "i1 rc is 1" "1" "$RC"
ckcontains "i2 mode is observer" "mode=observer" "$OUT"
ckcontains "i3 refusal wording is the non-gate one" "a feature that stops with no error" "$OUT"
rm -rf "$T"

echo "== j. an unreadable hook config is exit 2, never a quiet pass =="
T="$(mktemp -d -t pftest)"; H="$T/home"; build_world "$H"
rm -f "$H/.claude/settings.json"
commit_and_push "$H" nosettings
OUT="$(run_pf "$H" --doomed "$H/.claude/comms" --authoritative "$H/code/comms")"; RC=$?
ck "j1 rc is 2 when no hook config could be read" "2" "$RC"
ckcontains "j2 says the claim would rest on no evidence" "rests on no evidence" "$OUT"
rm -rf "$T"

echo "== g. GREEN: a true duplicate, everything mapped, clean tree =="
T="$(mktemp -d -t pftest)"; H="$T/home"; build_world "$H"
MAN="$T/manifest.txt"
OUT="$(run_pf "$H" --doomed "$H/.claude/comms" --authoritative "$H/code/comms" --manifest "$MAN")"; RC=$?
ck "g1 rc is 0" "0" "$RC"
ckcontains "g2 verdict is GREEN" "PREFLIGHT: GREEN" "$OUT"
ckcontains "g3 orphans = 0" "orphans            = 0" "$OUT"
ckcontains "g4 the round-trip positive control ran and passed" "round-trip         = pass" "$OUT"
ck "g5 a manifest was written" "yes" "$([ -s "$MAN" ] && echo yes || echo no)"
# Green must still be a MEASUREMENT, so the counts it rests on are printed.
ckcontains "g6 files_scanned is nonzero and shown" "files_scanned      = " "$OUT"
if printf '%s' "$OUT" | grep -Fq "files_scanned      = 0"; then
  fail=$((fail + 1)); echo "  FAIL g7 green rested on a zero-file scan"
else
  pass=$((pass + 1)); echo "  ok   g7 green did not rest on a zero-file scan"
fi
rm -rf "$T"

echo
echo "test_retire_preflight: pass=$pass fail=$fail"
if [ "$pass" -eq 0 ]; then
  echo "test_retire_preflight: ZERO assertions ran -- a failure, not a pass" >&2
  exit 1
fi
[ "$fail" -eq 0 ] || exit 1
exit 0
