#!/bin/bash
# tests/test_gemini_hook.sh -- Gemini AfterTool translation into the one heartbeat.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SELF_DIR/.." && pwd)"
HOOK="$REPO/adapters/gemini/hook.sh"
SA="$REPO/lib/swarm_arm.py"

export COMMS_STATE_DIR="$(mktemp -d)"
export COMMS_ROOT="$(mktemp -d)"
WORK="$(mktemp -d)"
trap 'rm -rf "$COMMS_STATE_DIR" "$COMMS_ROOT" "$WORK"' EXIT

PASS=0
FAIL=0
ok() { echo "ok:   $1"; PASS=$((PASS + 1)); }
bad() { echo "FAIL: $1"; FAIL=$((FAIL + 1)); }
eq() {
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want [$2], got [$3])"; fi
}
contains() {
  case "$3" in *"$2"*) ok "$1" ;; *) bad "$1 (missing [$2])" ;; esac
}

arm_enroll() {
  COMMS_STATE_DIR="$COMMS_STATE_DIR" python3 "$SA" arm "$1" >/dev/null
  COMMS_STATE_DIR="$COMMS_STATE_DIR" python3 "$SA" enroll "$1" \
    --agent-id "$2" --seat gemini >/dev/null
}

payload() {
  python3 -c 'import json,sys
print(json.dumps({"session_id":sys.argv[1], "cwd":sys.argv[2],
 "hook_event_name":"AfterTool", "tool_name":sys.argv[3],
 "tool_input":json.loads(sys.argv[4])}))' "$1" "$2" "$3" "$4"
}

post_row() {
  mkdir -p "$COMMS_ROOT/comms-$1"
  printf '{"seat":"peer","at":"2026-08-26T00:00:00+00:00","kind":"finding","text":"%s","topic":"default"}\n' \
    "$2" >> "$COMMS_ROOT/comms-$1/peer.jsonl"
}

# (a) Gemini shell name maps to Bash and session_id supplies identity.
RUN_A="gemini-a-$$"
arm_enroll "$RUN_A" gem-session-a
post_row "$RUN_A" "GEMINI ROW ARRIVED"
out="$(payload gem-session-a "$WORK" run_shell_command '{"command":"ls"}' | bash "$HOOK")"; rc=$?
eq "AfterTool delivery exits 0" 0 "$rc"
contains "AfterTool delivery emits the peer row" "GEMINI ROW ARRIVED" "$out"
contains "AfterTool delivery keeps the heartbeat envelope" '"additionalContext"' "$out"

# (b) Gemini write_file takes the same doc-enrol path as native Write.
GIT_REPO="$WORK/repo"
mkdir -p "$GIT_REPO/sub"
git -C "$GIT_REPO" init -q
touch "$GIT_REPO/sub/a.py"

doc_claim() { # doc_claim <runtime tool name>
  local tool="$1" run="gemini-doc-$RANDOM" state="$WORK/state-$RANDOM" root="$WORK/root-$RANDOM"
  mkdir -p "$state" "$root"
  COMMS_STATE_DIR="$state" python3 "$SA" arm "$run" >/dev/null
  COMMS_STATE_DIR="$state" python3 "$SA" enroll "$run" --agent-id doc-session \
    --topics baseline --seat gemini >/dev/null
  payload doc-session "$GIT_REPO" "$tool" "{\"file_path\":\"$GIT_REPO/sub/a.py\"}" |
    COMMS_STATE_DIR="$state" COMMS_ROOT="$root" bash "$HOOK" >/dev/null
  python3 -c 'import json,glob,sys
rows=[]
for path in glob.glob(sys.argv[1]+"/comms-"+sys.argv[2]+"/*.jsonl"):
  for line in open(path):
    row=json.loads(line); row.pop("at",None); rows.append(row)
print(json.dumps(rows,sort_keys=True,separators=(",",":")))' "$root" "$run"
}
gemini_claim="$(doc_claim write_file)"
native_claim="$(doc_claim Write)"
eq "write_file doc-enrol matches Write apart from timestamps" "$native_claim" "$gemini_claim"
contains "write_file posts the document claim" 'editing sub/a.py' "$gemini_claim"

# (c) Unknown tool names pass through and the heartbeat still delivers rows.
RUN_C="gemini-c-$$"
arm_enroll "$RUN_C" gem-session-c
post_row "$RUN_C" "UNKNOWN TOOL ROW"
out="$(payload gem-session-c "$WORK" future_gemini_tool '{}' | bash "$HOOK")"; rc=$?
eq "unknown tool delivery exits 0" 0 "$rc"
contains "unknown tool still delivers" "UNKNOWN TOOL ROW" "$out"

# (d) Malformed stdin is never blocking and emits no stdout.
out="$(printf '%s' '{broken json' | bash "$HOOK" 2>/dev/null)"; rc=$?
eq "malformed stdin exits 0" 0 "$rc"
eq "malformed stdin emits no stdout" "" "$out"

echo "gemini hook test: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
