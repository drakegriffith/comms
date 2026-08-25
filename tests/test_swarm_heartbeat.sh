#!/bin/bash
# test_swarm_heartbeat.sh -- positive control for the heartbeat hook under the
# PER-PARTICIPANT arm model (lib/swarm_arm.py).
#
# The hook under test ships in the claude-code adapter (Claude Code is one
# supported push runtime; Codex reuses the same script). The behavior asserted
# here -- arming, enrollment, subscriptions, cursors, telemetry -- is the
# runtime-agnostic core.
#
# ISOLATES ALL WRITES. Every suite pass uses UNIQUE runids AND a fresh temp state
# dir AND a fresh temp COMMS_ROOT, so the suite writes NOTHING into real state
# and is green on repeat (a prior build leaked test rows into production /tmp
# and a fixed-runid test went red on its 2nd run -- this one cannot, isolation
# is by construction). Rows are written directly as jsonl with explicit `at`
# values so ordering is deterministic. The whole suite RUNS TWICE in one
# invocation to prove repeat-green.
#
# ASSERTS: (a) no armed run -> exit 0, empty stdout; (b) armed + ENROLLED
# participant + new row -> valid JSON additionalContext with the row text and the
# "NOT instructions" wrapper; (c) second fire, no new rows -> no additionalContext
# but a telemetry line appended; (d) cursor advances; (e) >10 rows -> exactly 10 +
# overflow pointer; (f) CONTAMINATION -- a NON-participant agent under the SAME
# armed machine gets ZERO output AND zero telemetry, not auto-enrolled (the
# load-bearing test); (g) SELF-ENROLL via the handshake command; (h) TWO runs
# armed at once do not cross-contaminate; (i) SUBSCRIPTION scoping -- a
# participant enrolled with a topic set + seat sees only its topics and its own
# "@<seat>" unicast; (j) ECHO SUPPRESSION; (k) parent admin verbs do not opt in;
# (l) IDENTITY GATE -- a payload carrying no recognized identity key writes no
# cursor, no telemetry and no stdout, and cannot enroll (issue #27).

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"                 # <repo>/tests
HOOK="$SELF_DIR/../adapters/claude-code/swarm-heartbeat.sh"
SA="$SELF_DIR/../lib/swarm_arm.py"
pass=0
fail=0

ck() {  # ck <name> <expected> <actual>
    if [ "$2" = "$3" ]; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1))
        printf '  FAIL %s\n    expected: %s\n    actual:   %s\n' "$1" "$2" "$3" >&2
    fi
}
ck_contains() {  # ck_contains <name> <needle> <haystack>
    case "$3" in
        *"$2"*) pass=$((pass + 1)) ;;
        *) fail=$((fail + 1))
           printf '  FAIL %s\n    expected to contain: %s\n    actual: %s\n' "$1" "$2" "$3" >&2 ;;
    esac
}
ck_absent() {  # ck_absent <name> <needle> <haystack>
    case "$3" in
        *"$2"*) fail=$((fail + 1))
           printf '  FAIL %s\n    expected NOT to contain: %s\n    actual: %s\n' "$1" "$2" "$3" >&2 ;;
        *) pass=$((pass + 1)) ;;
    esac
}

# ---- per-pass state (set in run_suite) ------------------------------------
STATE=""
ROOT=""
LOG=""

arm_run() {   # arm_run <runid> [topic]
    COMMS_STATE_DIR="$STATE" python3 "$SA" arm "$1" ${2:+--topic "$2"} >/dev/null
}
enroll_agent() {  # enroll_agent <runid> <agent_id> [topics] [seat]
    COMMS_STATE_DIR="$STATE" python3 "$SA" enroll "$1" --agent-id "$2" \
        ${3:+--topics "$3"} ${4:+--seat "$4"} >/dev/null
}
is_part() {   # is_part <runid> <agent_id> ; exit 0 if participant
    COMMS_STATE_DIR="$STATE" python3 "$SA" is-participant "$1" "$2"
}

# A synthetic PostToolUse payload for a subagent. agent_id is the cursor +
# participation key; the command is what the enrollment handshake reads.
payload() {  # payload <agent_id> [command]
    printf '{"hook_event_name":"PostToolUse","tool_name":"Bash","session_id":"sess-1","agent_id":"%s","tool_input":{"command":"%s"}}' \
        "$1" "${2:-ls}"
}

# A GROK-shaped payload from a foreign runtime that scavenged a Claude-shaped
# hook config: camelCase sessionId, grok's own event/tool spellings, and NO
# agent_id anywhere. These are the key spellings from a real captured grok hook
# payload (PR #25's probe: hookEventName post_tool_use, toolName
# run_terminal_command, sessionId), not an invented shape.
foreign_payload() {  # foreign_payload [command]
    printf '{"hookEventName":"post_tool_use","toolName":"run_terminal_command","sessionId":"grok-sess-1","toolInput":{"command":"%s"}}' \
        "${1:-ls}"
}

# A Claude-shaped payload with the right keys and NO identity at all -- the
# other way a caller arrives unidentified.
anon_payload() {  # anon_payload [command]
    printf '{"hook_event_name":"PostToolUse","tool_name":"Bash","tool_input":{"command":"%s"}}' \
        "${1:-ls}"
}

# Payload with non-string agent_id (integer) -- type mismatch treated as no identity.
int_agent_id_payload() {  # int_agent_id_payload [command]
    printf '{"hook_event_name":"PostToolUse","tool_name":"Bash","agent_id":12345,"tool_input":{"command":"%s"}}' \
        "${1:-ls}"
}

# Payload with non-string agent_id (boolean) -- type mismatch treated as no identity.
bool_agent_id_payload() {  # bool_agent_id_payload [command]
    printf '{"hook_event_name":"PostToolUse","tool_name":"Bash","agent_id":true,"tool_input":{"command":"%s"}}' \
        "${1:-ls}"
}

# Payload with non-string agent_id (array) -- type mismatch treated as no identity.
list_agent_id_payload() {  # list_agent_id_payload [command]
    printf '{"hook_event_name":"PostToolUse","tool_name":"Bash","agent_id":["a","b"],"tool_input":{"command":"%s"}}' \
        "${1:-ls}"
}

# Append a row directly to a seat's jsonl (deterministic `at`).
post_row() {  # post_row <runid> <seat> <kind> <text> <at> [topic]
    local dir="$ROOT/comms-$1"
    mkdir -p "$dir"
    local topic="${6:-default}"
    printf '{"seat":"%s","at":"%s","kind":"%s","text":"%s","topic":"%s"}\n' \
        "$2" "$5" "$3" "$4" "$topic" >> "$dir/$2.jsonl"
}

HOOK_OUT=""
HOOK_RC=0
run_hook() {  # run_hook <agent_id> [command] ; sets globals HOOK_OUT + HOOK_RC (no
              # subshell around the call, so the globals survive).
    HOOK_OUT="$(payload "$1" "${2:-ls}" | COMMS_STATE_DIR="$STATE" COMMS_ROOT="$ROOT" \
        /bin/bash "$HOOK")"
    HOOK_RC=$?
}

# field of the emitted JSON: additionalContext (empty string if absent)
addl_ctx() {  # addl_ctx <json>
    printf '%s' "$1" | python3 -c 'import json,sys
raw=sys.stdin.read().strip()
if not raw:
    print("", end="")
else:
    d=json.loads(raw)
    print(d.get("hookSpecificOutput",{}).get("additionalContext",""), end="")'
}

run_hook_raw() {  # run_hook_raw <payload-json> ; same globals as run_hook
    HOOK_OUT="$(printf '%s' "$1" | COMMS_STATE_DIR="$STATE" COMMS_ROOT="$ROOT" \
        /bin/bash "$HOOK")"
    HOOK_RC=$?
}

log_lines() { wc -l < "$LOG" 2>/dev/null | tr -d ' '; }

# Every cursor/mtime file the hook has written, across all runs.
cursor_files() { find "$STATE/swarm-cursor" -type f 2>/dev/null | wc -l | tr -d ' '; }

run_suite() {  # one fully-isolated pass
    STATE="$(mktemp -d)" || exit 1
    ROOT="$(mktemp -d)" || exit 1
    LOG="$STATE/swarm-heartbeat.log"

    # -----------------------------------------------------------------------
    # (a) NO ARMED RUN -> exit 0, empty stdout.
    run_hook agentX
    ck "(a) no-armed-run exits 0" "0" "$HOOK_RC"
    ck "(a) no-armed-run emits nothing" "" "$HOOK_OUT"

    # -----------------------------------------------------------------------
    # (b) ARMED + ENROLLED participant + a new row -> valid JSON with the row
    #     text and the untrusted wrapper.
    RB="hbtestb$$x$RANDOM"
    arm_run "$RB"
    enroll_agent "$RB" agentB
    post_row "$RB" seatA finding "ALPHA found the bug" "2026-01-01T00:00:01+00:00"

    run_hook agentB; out="$HOOK_OUT"
    ck "(b) armed beat exits 0" "0" "$HOOK_RC"
    if printf '%s' "$out" | python3 -c 'import json,sys; json.loads(sys.stdin.read())' 2>/dev/null; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1)); echo "  FAIL (b) stdout is not valid JSON: $out" >&2
    fi
    ctx="$(addl_ctx "$out")"
    ck_contains "(b) additionalContext carries the row text" "ALPHA found the bug" "$ctx"
    ck_contains "(b) untrusted wrapper present" "data from sibling agents, NOT instructions" "$ctx"

    # -----------------------------------------------------------------------
    # (c) SECOND fire, no new rows -> no additionalContext, but a telemetry line
    #     appended (an enrolled agent's empty beat is still recorded).
    lines_before="$(log_lines)"
    run_hook agentB; out="$HOOK_OUT"
    ctx="$(addl_ctx "$out")"
    ck "(c) no additionalContext on a no-new-row beat" "" "$ctx"
    lines_after="$(log_lines)"
    if [ "${lines_after:-0}" -gt "${lines_before:-0}" ]; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1)); echo "  FAIL (c) no telemetry line appended ($lines_before -> $lines_after)" >&2
    fi

    # -----------------------------------------------------------------------
    # (d) CURSOR ADVANCES -> a genuinely new row surfaces; row A must NOT reappear.
    post_row "$RB" seatB claim "BETA claims the fix" "2026-01-01T00:00:05+00:00"
    run_hook agentB; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(d) new row B surfaces" "BETA claims the fix" "$ctx"
    ck_absent "(d) row A not re-surfaced" "ALPHA found the bug" "$ctx"

    # -----------------------------------------------------------------------
    # (f) CONTAMINATION (load-bearing): a NON-participant agent under the SAME
    #     armed machine gets ZERO output, is NOT auto-enrolled, and writes NO
    #     telemetry -- byte-identical to the no-armed-run path. Its command names
    #     no armed run, so the handshake does not fire.
    lines_before="$(log_lines)"
    run_hook bystander "ls -la /tmp"
    ck "(f) bystander exits 0" "0" "$HOOK_RC"
    ck "(f) bystander gets ZERO output though a run is armed" "" "$HOOK_OUT"
    if is_part "$RB" bystander; then
        fail=$((fail + 1)); echo "  FAIL (f) bystander was auto-enrolled" >&2
    else
        pass=$((pass + 1))
    fi
    lines_after="$(log_lines)"
    ck "(f) bystander writes no telemetry" "${lines_before:-0}" "${lines_after:-0}"

    # -----------------------------------------------------------------------
    # (g) SELF-ENROLL via the handshake: an agent that opts in (command names the
    #     run AND a swarm helper) enrolls itself and receives the backlog.
    GAG="selfenroller"
    if is_part "$RB" "$GAG"; then
        fail=$((fail + 1)); echo "  FAIL (g) agent was a participant before opting in" >&2
    else
        pass=$((pass + 1))
    fi
    run_hook "$GAG" "python3 lib/swarm_mailbox.py read $RB seatZ"
    if is_part "$RB" "$GAG"; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1)); echo "  FAIL (g) handshake did not enroll the agent" >&2
    fi
    ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(g) self-enrolled agent receives the backlog" "ALPHA found the bug" "$ctx"

    # -----------------------------------------------------------------------
    # (e) >10 ROWS -> exactly 10 emitted + an overflow pointer.
    RE="hbteste$$x$RANDOM"
    arm_run "$RE"
    enroll_agent "$RE" agentE
    for i in $(seq 1 12); do
        post_row "$RE" seatE finding "row number $i" "2026-02-01T00:00:$(printf '%02d' "$i")+00:00"
    done
    run_hook agentE; ctx="$(addl_ctx "$HOOK_OUT")"
    row_count=$(printf '%s\n' "$ctx" | grep -c '^- \[')
    ck "(e) exactly 10 rows surfaced" "10" "$row_count"
    ck_contains "(e) overflow pointer present" "2 more, read the full board" "$ctx"
    # The pointer must name --replay: the beat just advanced this agent's
    # cursor past those rows, and a plain `comms read` (which keeps a cursor of
    # its own since issue #33) would show the overflow reader nothing.
    ck_contains "(e) overflow pointer points at a command that still shows them" \
        "<seat> --replay" "$ctx"

    # -----------------------------------------------------------------------
    # (h) TWO RUNS armed at once do not cross-contaminate. agentH is enrolled in
    #     R1 ONLY; a row on R2 must never reach it.
    R1="hbtesth1$$x$RANDOM"
    R2="hbtesth2$$x$RANDOM"
    arm_run "$R1"
    arm_run "$R2"
    enroll_agent "$R1" agentH
    post_row "$R1" seatP finding "R1-ONLY-ROW" "2026-03-01T00:00:01+00:00"
    post_row "$R2" seatQ finding "R2-SECRET-ROW" "2026-03-01T00:00:02+00:00"
    run_hook agentH; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(h) participant sees its own run's row" "R1-ONLY-ROW" "$ctx"
    ck_absent "(h) other run's row does NOT cross over" "R2-SECRET-ROW" "$ctx"

    # -----------------------------------------------------------------------
    # (i) SUBSCRIPTION scoping: a participant enrolled with topic set
    #     {projA,broadcast} + seat seatMe sees those topics and its own
    #     "@seatMe" unicast, but not an unsubscribed project's rows nor another
    #     seat's unicast.
    RI="hbtesti$$x$RANDOM"
    arm_run "$RI"
    enroll_agent "$RI" agentI "projA,broadcast" seatMe
    post_row "$RI" wA  finding "row in projA"     "2026-04-01T00:00:01+00:00" "projA"
    post_row "$RI" wB  finding "row in projB"     "2026-04-01T00:00:02+00:00" "projB"
    post_row "$RI" hub finding "row in broadcast" "2026-04-01T00:00:03+00:00" "broadcast"
    post_row "$RI" snd finding "direct to me"     "2026-04-01T00:00:04+00:00" "@seatMe"
    post_row "$RI" snd finding "direct to other"  "2026-04-01T00:00:05+00:00" "@seatOther"
    run_hook agentI; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(i) subscribed projA surfaces"     "row in projA"     "$ctx"
    ck_contains "(i) subscribed broadcast surfaces" "row in broadcast" "$ctx"
    ck_contains "(i) own unicast surfaces"          "direct to me"     "$ctx"
    ck_absent   "(i) unsubscribed projB does not leak"  "row in projB"    "$ctx"
    ck_absent   "(i) another seat's unicast does not leak" "direct to other" "$ctx"

    # -----------------------------------------------------------------------
    # (j) ECHO SUPPRESSION (wave swarmw-0821a): a seat's own rows are never
    #     injected back at it; a sibling's row in the same topic still is.
    RJ="hbtestj$$x$RANDOM"
    arm_run "$RJ"
    enroll_agent "$RJ" agentJ "projJ" seatJ
    post_row "$RJ" seatJ finding "MY-OWN-ECHO-ROW"  "2026-05-01T00:00:01+00:00" "projJ"
    post_row "$RJ" seatK finding "SIBLING-REAL-ROW" "2026-05-01T00:00:02+00:00" "projJ"
    run_hook agentJ; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(j) sibling row still surfaces"    "SIBLING-REAL-ROW" "$ctx"
    ck_absent   "(j) own row is not echoed back"    "MY-OWN-ECHO-ROW"  "$ctx"

    # -----------------------------------------------------------------------
    # (k) PARENT VERBS DO NOT OPT IN (wave swarmw-0821a): arm/status commands
    #     naming the runid must not enroll; enroll and swarm_claims commands do.
    RK="hbtestk$$x$RANDOM"
    arm_run "$RK"
    run_hook agentKadmin "python3 lib/swarm_arm.py status $RK"
    if is_part "$RK" agentKadmin; then
        fail=$((fail + 1)); echo "  FAIL (k) status command enrolled the caller" >&2
    else
        pass=$((pass + 1))
    fi
    run_hook agentKadmin "python3 lib/swarm_arm.py arm $RK"
    if is_part "$RK" agentKadmin; then
        fail=$((fail + 1)); echo "  FAIL (k) arm command enrolled the caller" >&2
    else
        pass=$((pass + 1))
    fi
    run_hook agentKclaim "python3 lib/swarm_claims.py claim $RK me some/path"
    if is_part "$RK" agentKclaim; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1)); echo "  FAIL (k) claims command did not enroll" >&2
    fi
    run_hook agentKenroll "python3 lib/swarm_arm.py enroll $RK --topics projK --seat kk"
    if is_part "$RK" agentKenroll; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1)); echo "  FAIL (k) enroll command did not enroll" >&2
    fi

    # -----------------------------------------------------------------------
    # (l) IDENTITY GATE (issue #27): a caller the payload does not identify must
    #     advance NO cursor, ever. A runtime that scavenges Claude-shaped hook
    #     configs (grok scans ~/.claude/settings.json by default) executes this
    #     hook on every tool call and DISCARDS its stdout, so any cursor it
    #     advances marks rows delivered to a reader that never saw them.
    #
    #     The old code defaulted such a payload to the literal key "unknown".
    #     Nothing forbade "unknown" from being enrolled, and once it was, every
    #     unidentified caller on the machine shared that one cursor -- so this
    #     arms the run with "unknown" already a participant, which is the state
    #     the fallback made reachable, and asserts the foreign beat still does
    #     nothing.
    RL="hbtestl$$x$RANDOM"
    arm_run "$RL"
    enroll_agent "$RL" unknown
    post_row "$RL" seatL finding "L-ROW-NEVER-DELIVERED" "2026-06-01T00:00:01+00:00"
    lines_before="$(log_lines)"
    cursors_before="$(cursor_files)"
    run_hook_raw "$(foreign_payload 'ls -la')"
    ck "(l) foreign payload exits 0" "0" "$HOOK_RC"
    ck "(l) foreign payload emits ZERO stdout" "" "$HOOK_OUT"
    ck "(l) foreign payload writes NO cursor file" "${cursors_before:-0}" "$(cursor_files)"
    ck "(l) foreign payload writes NO telemetry" "${lines_before:-0}" "$(log_lines)"

    #     Same gate on the enrollment side: an unidentified payload whose command
    #     IS a valid opt-in must not enroll anything -- the handshake is never
    #     reached, so no shared key appears on the roster.
    RL2="hbtestl2$$x$RANDOM"
    arm_run "$RL2"
    lines_before="$(log_lines)"
    cursors_before="$(cursor_files)"
    run_hook_raw "$(anon_payload "python3 lib/swarm_mailbox.py read $RL2 seatZ")"
    ck "(l) anonymous opt-in exits 0" "0" "$HOOK_RC"
    ck "(l) anonymous opt-in emits ZERO stdout" "" "$HOOK_OUT"
    ck "(l) anonymous opt-in writes NO cursor file" "${cursors_before:-0}" "$(cursor_files)"
    ck "(l) anonymous opt-in writes NO telemetry" "${lines_before:-0}" "$(log_lines)"
    if is_part "$RL2" unknown; then
        fail=$((fail + 1)); echo "  FAIL (l) anonymous payload enrolled the shared 'unknown' key" >&2
    else
        pass=$((pass + 1))
    fi

    #     Positive control for this gate: an IDENTIFIED agent on the same armed
    #     run still enrolls and still receives the row, so (l) is not passing by
    #     the hook being dead.
    run_hook agentL "python3 lib/swarm_mailbox.py read $RL2 seatZ"
    if is_part "$RL2" agentL; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1)); echo "  FAIL (l) identified agent failed to enroll (gate is over-broad)" >&2
    fi
    post_row "$RL2" seatL2 finding "L2-ROW-DELIVERED" "2026-06-02T00:00:01+00:00"
    run_hook agentL; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(l) identified agent still receives rows" "L2-ROW-DELIVERED" "$ctx"

    #     Type guard for identity values: non-string agent_id is treated as no identity.
    #     Integer agent_id should exit 0 with no output and no cursor files.
    RL3="hbtestl3$$x$RANDOM"
    arm_run "$RL3"
    lines_before="$(log_lines)"
    cursors_before="$(cursor_files)"
    run_hook_raw "$(int_agent_id_payload "python3 lib/swarm_mailbox.py read $RL3 seatZ")"
    ck "(m) int agent_id exits 0" "0" "$HOOK_RC"
    ck "(m) int agent_id emits ZERO stdout" "" "$HOOK_OUT"
    ck "(m) int agent_id writes NO cursor file" "${cursors_before:-0}" "$(cursor_files)"
    ck "(m) int agent_id writes NO telemetry" "${lines_before:-0}" "$(log_lines)"

    #     Boolean agent_id should exit 0 with no output and no cursor files.
    RL4="hbtestl4$$x$RANDOM"
    arm_run "$RL4"
    lines_before="$(log_lines)"
    cursors_before="$(cursor_files)"
    run_hook_raw "$(bool_agent_id_payload "python3 lib/swarm_mailbox.py read $RL4 seatZ")"
    ck "(n) bool agent_id exits 0" "0" "$HOOK_RC"
    ck "(n) bool agent_id emits ZERO stdout" "" "$HOOK_OUT"
    ck "(n) bool agent_id writes NO cursor file" "${cursors_before:-0}" "$(cursor_files)"
    ck "(n) bool agent_id writes NO telemetry" "${lines_before:-0}" "$(log_lines)"

    #     Array agent_id should exit 0 with no output and no cursor files.
    RL5="hbtestl5$$x$RANDOM"
    arm_run "$RL5"
    lines_before="$(log_lines)"
    cursors_before="$(cursor_files)"
    run_hook_raw "$(list_agent_id_payload "python3 lib/swarm_mailbox.py read $RL5 seatZ")"
    ck "(o) array agent_id exits 0" "0" "$HOOK_RC"
    ck "(o) array agent_id emits ZERO stdout" "" "$HOOK_OUT"
    ck "(o) array agent_id writes NO cursor file" "${cursors_before:-0}" "$(cursor_files)"
    ck "(o) array agent_id writes NO telemetry" "${lines_before:-0}" "$(log_lines)"

    rm -rf "$STATE" "$ROOT"
}

# Run the whole suite TWICE in one invocation -> proves repeat-green.
for passno in 1 2; do
    run_suite
done

echo "swarm-heartbeat test: $pass passed, $fail failed"
[ "$fail" -eq 0 ] && exit 0 || exit 1
