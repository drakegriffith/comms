#!/bin/bash
# test_heartbeat_shim.sh -- the loud-shim suite.
#
# The single behavior under test: WHEN THE CHECKOUT IS MISSING, THE SHIM SAYS
# SO, AND STILL EXITS 0. The old shim satisfied half of that (exit 0) and the
# regression is invisible from the exit code alone, so every assertion here is
# about the EVIDENCE the miss path leaves behind, not about the status.
#
# ISOLATES ALL WRITES: COMMS_HOOKS_DIR and COMMS_STATE_DIR point at fresh
# mktemp dirs for every case, so this suite never reads or writes the real
# $HOME/.claude/hooks or $HOME/.comms/state, and is green on repeat.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd -P)"
REPO="$(cd "$SELF_DIR/.." && pwd -P)"
SHIM_SRC="$REPO/adapters/claude-code/shim/swarm-heartbeat.sh"
INSTALLER="$REPO/adapters/claude-code/install-shim.sh"

pass=0
fail=0
ck() {  # ck <name> <expected> <actual>
  if [ "$2" = "$3" ]; then
    pass=$((pass + 1)); echo "  ok   $1"
  else
    fail=$((fail + 1)); echo "  FAIL $1: expected [$2] got [$3]"
  fi
}
ckcontains() {  # ckcontains <name> <needle> <haystack>
  if printf '%s' "$3" | grep -Fq -- "$2"; then
    pass=$((pass + 1)); echo "  ok   $1"
  else
    fail=$((fail + 1)); echo "  FAIL $1: [$2] not found in output"
  fi
}

echo "== a. hit path: a present checkout is exec'd, and the shim adds nothing =="
T="$(mktemp -d -t shimtest)"
FAKE="$T/checkout"
mkdir -p "$FAKE/adapters/claude-code"
cat > "$FAKE/adapters/claude-code/swarm-heartbeat.sh" <<'EOF'
#!/bin/bash
echo "REAL-HEARTBEAT-RAN"
exit 0
EOF
OUT="$(COMMS_CHECKOUT="$FAKE" COMMS_STATE_DIR="$T/state" bash "$SHIM_SRC" 2>"$T/err")"
ck   "a1 exec'd the real heartbeat"       "REAL-HEARTBEAT-RAN" "$OUT"
ck   "a2 hit path writes no miss log"     "0" "$(ls "$T/state" 2>/dev/null | wc -l | tr -d ' ')"
ck   "a3 hit path is silent on stderr"    "0" "$(wc -c < "$T/err" | tr -d ' ')"
rm -rf "$T"

echo "== b. miss path: loud on all three channels, exit code still 0 =="
T="$(mktemp -d -t shimtest)"
STATE="$T/state"
OUT="$(COMMS_CHECKOUT="$T/no-such-checkout" COMMS_STATE_DIR="$STATE" \
       COMMS_SHIM_NAG_SECS=0 bash "$SHIM_SRC" 2>"$T/err")"
RC=$?
# The never-block rule. This is the one assertion the OLD shim also passed,
# and it must keep passing: making the failure visible must not make it fatal.
ck "b1 exit code is 0 (never blocks the tool call)" "0" "$RC"
ck "b2 channel 1: a miss log line was appended" "1" \
   "$(wc -l < "$STATE/heartbeat-shim-missing.log" 2>/dev/null | tr -d ' ')"
ckcontains "b3 channel 1 names the path it looked for" "$T/no-such-checkout" \
   "$(cat "$STATE/heartbeat-shim-missing.log")"
ckcontains "b4 channel 2: stderr carries the notice" "comms heartbeat DISABLED" \
   "$(cat "$T/err")"
ckcontains "b5 channel 3: additionalContext emitted" "additionalContext" "$OUT"
# ACTIONABLE, not merely alarmed.
ckcontains "b6 the message names the fix command" "install.sh" "$OUT"
ckcontains "b7 the message names the override knob" "COMMS_CHECKOUT" "$OUT"
ckcontains "b8 injected text is marked as data, not instructions" "NOT an instruction" "$OUT"
# Well-formed JSON or the runtime discards it, and we are silent again.
if printf '%s' "$OUT" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null; then
  ck "b9 stdout is valid JSON" "yes" "yes"
else
  ck "b9 stdout is valid JSON" "yes" "no"
fi
rm -rf "$T"

echo "== c. the counter keeps counting while the nag is throttled =="
# The throttle bounds the NOISE; it must not bound the EVIDENCE. If both were
# throttled, a long outage would look like a short one.
T="$(mktemp -d -t shimtest)"
STATE="$T/state"
N1="$(COMMS_CHECKOUT="$T/nope" COMMS_STATE_DIR="$STATE" COMMS_SHIM_NAG_SECS=99999 bash "$SHIM_SRC" 2>/dev/null)"
N2="$(COMMS_CHECKOUT="$T/nope" COMMS_STATE_DIR="$STATE" COMMS_SHIM_NAG_SECS=99999 bash "$SHIM_SRC" 2>/dev/null)"
N3="$(COMMS_CHECKOUT="$T/nope" COMMS_STATE_DIR="$STATE" COMMS_SHIM_NAG_SECS=99999 bash "$SHIM_SRC" 2>/dev/null)"
ck "c1 three beats, three log lines (counter unthrottled)" "3" \
   "$(wc -l < "$STATE/heartbeat-shim-missing.log" | tr -d ' ')"
ckcontains "c2 the first beat still injected" "additionalContext" "$N1"
ck "c3 the second beat is throttled to no stdout" "" "$N2"
ck "c4 the third beat is throttled to no stdout" "" "$N3"
rm -rf "$T"

echo "== d. resolution order: checkout-path file beats the baked default =="
T="$(mktemp -d -t shimtest)"
FAKE="$T/elsewhere"
mkdir -p "$FAKE/adapters/claude-code" "$T/state"
cat > "$FAKE/adapters/claude-code/swarm-heartbeat.sh" <<'EOF'
#!/bin/bash
echo "FROM-RELOCATED-CHECKOUT"
EOF
printf '%s\n' "$FAKE" > "$T/state/checkout-path"
OUT="$(COMMS_STATE_DIR="$T/state" bash "$SHIM_SRC" 2>/dev/null)"
ck "d1 checkout-path file is honoured" "FROM-RELOCATED-CHECKOUT" "$OUT"
# And the env override beats the file, so a debugging session can redirect it
# without editing state.
OUT2="$(COMMS_CHECKOUT="$T/gone" COMMS_STATE_DIR="$T/state" COMMS_SHIM_NAG_SECS=0 bash "$SHIM_SRC" 2>/dev/null)"
ckcontains "d2 COMMS_CHECKOUT overrides the file" "$T/gone" "$OUT2"
rm -rf "$T"

echo "== e. installer: idempotent, non-clobbering, self-verifying =="
T="$(mktemp -d -t shimtest)"
HOOKS="$T/hooks"; STATE="$T/state"
OUT="$(COMMS_HOOKS_DIR="$HOOKS" COMMS_STATE_DIR="$STATE" bash "$INSTALLER" 2>&1)"; RC=$?
ck "e1 first install exits 0" "0" "$RC"
ck "e2 the shim landed" "yes" "$([ -f "$HOOKS/swarm-heartbeat.sh" ] && echo yes || echo no)"
ck "e3 checkout-path recorded" "$REPO" "$(cat "$STATE/checkout-path" 2>/dev/null)"
ckcontains "e4 installer ran its own miss-path probe" "miss path verified LOUD" "$OUT"
OUT2="$(COMMS_HOOKS_DIR="$HOOKS" COMMS_STATE_DIR="$STATE" bash "$INSTALLER" 2>&1)"
ckcontains "e5 second install is a no-op" "already current" "$OUT2"
# A foreign file at the target is backed up, never silently replaced: it may be
# the only copy of a pre-extraction implementation.
echo "#!/bin/bash" > "$HOOKS/swarm-heartbeat.sh"
echo "echo SOMEBODY-ELSES-HOOK" >> "$HOOKS/swarm-heartbeat.sh"
OUT3="$(COMMS_HOOKS_DIR="$HOOKS" COMMS_STATE_DIR="$STATE" bash "$INSTALLER" 2>&1)"
ckcontains "e6 a foreign hook is backed up" "backed up to" "$OUT3"
BAKS="$(ls "$HOOKS"/swarm-heartbeat.sh.pre-comms-shim.* 2>/dev/null | wc -l | tr -d ' ')"
ck "e7 the backup file exists" "1" "$BAKS"
ckcontains "e8 the backup holds the foreign body" "SOMEBODY-ELSES-HOOK" \
   "$(cat "$HOOKS"/swarm-heartbeat.sh.pre-comms-shim.* 2>/dev/null)"
# The placed shim resolves this checkout with no env help at all -- the render
# step baked the path in, so deleting checkout-path does not break it.
rm -f "$STATE/checkout-path"
OUT4="$(COMMS_STATE_DIR="$T/emptystate" bash "$HOOKS/swarm-heartbeat.sh" 2>/dev/null <<< '{}')"
ck "e9 placed shim still resolves without checkout-path" "" \
   "$(printf '%s' "$OUT4" | grep -c 'comms heartbeat DISABLED' | tr -d ' ' | sed 's/^0$//')"
rm -rf "$T"

echo "== f. gate validity: the two conditions hook-shim.sh enforces =="
# The live registration runs this file in GATE mode behind hook-shim.sh, whose
# _validate requires the last line to be exactly the marker and requires the
# file to parse. Break either and the gate fails closed on a "*" matcher, which
# refuses every tool call on the machine. These are therefore not style checks.
MARKER='# hook-eof-marker v1 do-not-remove'
ck "f1 shim source parses under bash -n" "0" \
   "$(bash -n "$SHIM_SRC" 2>/dev/null; echo $?)"
ck "f2 shim source's last line is exactly the marker" "$MARKER" \
   "$(tail -n 1 "$SHIM_SRC")"
ck "f3 the heartbeat it execs parses too" "0" \
   "$(bash -n "$REPO/adapters/claude-code/swarm-heartbeat.sh" 2>/dev/null; echo $?)"
ck "f4 the heartbeat it execs ends with the marker" "$MARKER" \
   "$(tail -n 1 "$REPO/adapters/claude-code/swarm-heartbeat.sh")"
# And the PLACED file, after rendering, must still satisfy both -- the render
# step is exactly where a stray trailing line would get introduced.
T="$(mktemp -d -t shimtest)"
COMMS_HOOKS_DIR="$T/hooks" COMMS_STATE_DIR="$T/state" bash "$INSTALLER" >/dev/null 2>&1
ck "f5 the PLACED shim parses" "0" \
   "$(bash -n "$T/hooks/swarm-heartbeat.sh" 2>/dev/null; echo $?)"
ck "f6 the PLACED shim ends with the marker" "$MARKER" \
   "$(tail -n 1 "$T/hooks/swarm-heartbeat.sh")"
# The installer must REFUSE rather than place a file that would fail the gate.
BAD="$T/badrepo/adapters/claude-code"
mkdir -p "$BAD/shim"
sed '$d' "$SHIM_SRC" > "$BAD/shim/swarm-heartbeat.sh"   # drop the marker line
cp "$INSTALLER" "$BAD/install-shim.sh"
cp "$REPO/adapters/claude-code/swarm-heartbeat.sh" "$BAD/swarm-heartbeat.sh"
OUTBAD="$(COMMS_HOOKS_DIR="$T/hooks2" COMMS_STATE_DIR="$T/state2" bash "$BAD/install-shim.sh" 2>&1)"
BADRC=$?
ck "f7 a marker-less shim source is refused, not placed" "1" "$BADRC"
ckcontains "f8 the refusal names the reason" "hook-eof-marker" "$OUTBAD"
ck "f9 nothing was written to the target dir" "no" \
   "$([ -f "$T/hooks2/swarm-heartbeat.sh" ] && echo yes || echo no)"
rm -rf "$T"

echo
echo "test_heartbeat_shim: pass=$pass fail=$fail"
# A suite that asserted nothing is a suite that failed.
if [ "$pass" -eq 0 ]; then
  echo "test_heartbeat_shim: ZERO assertions ran -- that is a failure, not a pass" >&2
  exit 1
fi
[ "$fail" -eq 0 ] || exit 1
exit 0
