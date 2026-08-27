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
# cursor, no telemetry and no stdout, and cannot enroll (issue #27); (p) FOR
# YOU FLAG -- a row on this agent's own unicast topic renders with a
# "[FOR YOU from <seat>]" prefix, a bystander row does not (issue #41);
# (q) THREAD SUFFIX -- a row carrying `thread` renders with " (thread <key>)",
# a row without one does not; (r) the two compose on one row.

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

# A payload from a tool call INSIDE a subagent: agent_id is the subagent's
# task id, session_id is the PARENT session (inherited-enrollment input).
child_payload() {  # child_payload <agent_id> <parent_session_id> [command]
    printf '{"hook_event_name":"PostToolUse","tool_name":"Bash","session_id":"%s","agent_id":"%s","tool_input":{"command":"%s"}}' \
        "$2" "$1" "${3:-ls}"
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
post_row() {  # post_row <runid> <seat> <kind> <text> <at> [topic] [thread]
    local dir="$ROOT/comms-$1"
    mkdir -p "$dir"
    local topic="${6:-default}"
    if [ -n "${7:-}" ]; then
        printf '{"seat":"%s","at":"%s","kind":"%s","text":"%s","topic":"%s","thread":"%s"}\n' \
            "$2" "$5" "$3" "$4" "$topic" "$7" >> "$dir/$2.jsonl"
    else
        printf '{"seat":"%s","at":"%s","kind":"%s","text":"%s","topic":"%s"}\n' \
            "$2" "$5" "$3" "$4" "$topic" >> "$dir/$2.jsonl"
    fi
}

# A PostToolUse payload for a FILE-TOUCHING tool (issue #42's doc-enrol leg).
# tool_input carries file_path, not command.
write_payload() {  # write_payload <agent_id> <tool_name> <file_path>
    printf '{"hook_event_name":"PostToolUse","tool_name":"%s","session_id":"sess-1","agent_id":"%s","tool_input":{"file_path":"%s"}}' \
        "$2" "$1" "$3"
}

command_payload() {  # command_payload <agent_id> <tool_name> <cwd> <command>
    python3 -c 'import json,sys
print(json.dumps({"hook_event_name":"PostToolUse", "tool_name":sys.argv[2],
 "session_id":"sess-1", "agent_id":sys.argv[1], "cwd":sys.argv[3],
 "tool_input":{"command":sys.argv[4]}}))' "$1" "$2" "$3" "$4"
}

# This agent's current subscription, comma-joined (empty => subscribe-all).
part_topics() {  # part_topics <runid> <agent_id>
    COMMS_STATE_DIR="$STATE" python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
import swarm_arm
print(",".join(swarm_arm.participant_sub(sys.argv[2], sys.argv[3])[0]), end="")' \
        "$SELF_DIR/../lib" "$1" "$2"
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

# One field of the LAST process_run telemetry row for a run. doc-enrol rows
# are skipped: they carry the enrolment, not the mailbox scan.
last_scan_field() {  # last_scan_field <runid> <field>
    python3 -c '
import json, sys
runid, field = sys.argv[2], sys.argv[3]
val = ""
try:
    fh = open(sys.argv[1])
except OSError:
    fh = []
for line in fh:
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if ev.get("runid") == runid and not str(ev.get("topic") or "").startswith("doc-enrol"):
        val = ev.get(field)
sys.stdout.write(json.dumps(val))' "$LOG" "$1" "$2"
}

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

    # -----------------------------------------------------------------------
    # (p) FOR-YOU FLAG (issue #41): a row riding this agent's own unicast
    #     topic "@<seat>" renders with a "[FOR YOU from <row seat>]" prefix.
    #     A bystander row on a plain topic in the same beat renders with the
    #     pre-#41 line format, unchanged.
    RP="hbtestp$$x$RANDOM"
    arm_run "$RP"
    enroll_agent "$RP" agentP "projP" seatP
    post_row "$RP" seatQ finding "DIRECT-TO-ME" "2026-07-01T00:00:01+00:00" "@seatP"
    post_row "$RP" seatQ finding "JUST-A-PROJP-ROW" "2026-07-01T00:00:02+00:00" "projP"
    run_hook agentP; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(p) unicast row carries the FOR YOU prefix" \
        "[FOR YOU from seatQ] [seatQ | finding | @seatP |" "$ctx"
    ck_contains "(p) FOR YOU row still carries its own text" "DIRECT-TO-ME" "$ctx"
    if printf '%s\n' "$ctx" | grep -qF -- '- [seatQ | finding | projP |'; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1)); echo "  FAIL (p) bystander row format changed: $ctx" >&2
    fi
    ck_absent "(p) bystander row is not tagged FOR YOU" \
        "FOR YOU from seatQ] [seatQ | finding | projP" "$ctx"

    # -----------------------------------------------------------------------
    # (q) THREAD SUFFIX (issue #41): a row carrying a `thread` field renders
    #     with a " (thread <key>)" suffix so a reply can target it; a row
    #     with no thread carries no suffix.
    RQ="hbtestq$$x$RANDOM"
    arm_run "$RQ"
    enroll_agent "$RQ" agentQ
    post_row "$RQ" seatR finding "ABOUT-A-DOC" "2026-07-02T00:00:01+00:00" "default" "doc:comms/README.md"
    post_row "$RQ" seatR finding "NO-THREAD-HERE" "2026-07-02T00:00:02+00:00"
    run_hook agentQ; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(q) threaded row carries the thread suffix" \
        "ABOUT-A-DOC (thread doc:comms/README.md)" "$ctx"
    ck_contains "(q) unthreaded row keeps the pre-#41 line format" \
        "- [seatR | finding | default | 2026-07-02T00:00:02+00:00] NO-THREAD-HERE" "$ctx"
    ck_absent "(q) unthreaded row carries no thread suffix" \
        "NO-THREAD-HERE (thread" "$ctx"

    # -----------------------------------------------------------------------
    # (r) FOR YOU + THREAD COMPOSE: a unicast reply-target row carries both
    #     markers at once.
    RR="hbtestr$$x$RANDOM"
    arm_run "$RR"
    enroll_agent "$RR" agentR "projR" seatR2
    post_row "$RR" seatS finding "REPLY-TARGET" "2026-07-03T00:00:01+00:00" "@seatR2" "doc:comms/lib/x.py"
    run_hook agentR; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(r) unicast + threaded row carries both markers" \
        "[FOR YOU from seatS] [seatS | finding | @seatR2 | 2026-07-03T00:00:01+00:00] REPLY-TARGET (thread doc:comms/lib/x.py)" \
        "$ctx"

    # -----------------------------------------------------------------------
    # (s) DOC-ENROL LEG (issue #42): a Write/Edit adds the edited file's thread
    #     key (swarm_mailbox.thread_key) to the acting agent's subscription, so
    #     sibling rows ABOUT that document reach the agent working on it.
    RS="hbtests$$x$RANDOM"
    arm_run "$RS"
    enroll_agent "$RS" agentS "projS" seatS
    enroll_agent "$RS" agentT "projS" seatT     # same run, does not touch the doc
    REPO="$(mktemp -d)" || exit 1
    REPONAME="$(basename "$REPO")"
    mkdir -p "$REPO/.git" "$REPO/sub"
    : > "$REPO/sub/a.py"; : > "$REPO/sub/b.py"; : > "$REPO/sub/c.py"

    run_hook_raw "$(write_payload agentS Write "$REPO/sub/a.py")"
    ck "(s) a Write beat exits 0" "0" "$HOOK_RC"
    ck_contains "(s) Write enrols the file's doc key" \
        "doc:$REPONAME/sub/a.py" "$(part_topics "$RS" agentS)"
    ck_contains "(s) the pre-existing subscription survives the merge" \
        "projS" "$(part_topics "$RS" agentS)"

    run_hook_raw "$(write_payload agentS Edit "$REPO/sub/b.py")"
    ck_contains "(s) Edit enrols the file's doc key too" \
        "doc:$REPONAME/sub/b.py" "$(part_topics "$RS" agentS)"

    #     A Bash beat carries no file_path and must enrol nothing.
    before_t="$(part_topics "$RS" agentT)"
    run_hook agentT "ls -la /tmp"
    ck "(s) a Bash beat enrols no doc topic" "$before_t" "$(part_topics "$RS" agentT)"

    #     TELEMETRY: an ADDED topic writes one extra line; a re-Write of the
    #     same file writes none (the add_topics no-op does not touch the file).
    lines_before="$(log_lines)"
    run_hook_raw "$(write_payload agentS Write "$REPO/sub/c.py")"
    added_delta=$(( $(log_lines) - lines_before ))
    ck_contains "(s) telemetry names the enrolled doc key" \
        "doc-enrol doc:$REPONAME/sub/c.py" "$(tail -4 "$LOG")"
    lines_before="$(log_lines)"
    run_hook_raw "$(write_payload agentS Write "$REPO/sub/c.py")"
    same_delta=$(( $(log_lines) - lines_before ))
    ck "(s) an added topic writes exactly one MORE telemetry line than a no-op" \
        "1" "$(( added_delta - same_delta ))"

    #     DELIVERY: a row whose `thread` is a subscribed doc key reaches the
    #     subscriber even though its `topic` is not subscribed. This is the
    #     filter extension the leg is worthless without.
    post_row "$RS" seatU finding "THREAD-ROW-FOR-S" "2026-08-01T00:00:01+00:00" \
        "otherproj" "doc:$REPONAME/sub/a.py"
    run_hook agentS; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(s) a subscribed doc key delivers a row by its thread field" \
        "THREAD-ROW-FOR-S" "$ctx"
    ck_contains "(s) the delivered row still renders its thread suffix" \
        "THREAD-ROW-FOR-S (thread doc:$REPONAME/sub/a.py)" "$ctx"
    run_hook agentT; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_absent "(s) a seat that never touched the doc does NOT get the row" \
        "THREAD-ROW-FOR-S" "$ctx"

    #     A row whose topic is unsubscribed AND whose thread is unsubscribed
    #     is still filtered out -- the extension widens by exactly one field.
    post_row "$RS" seatU finding "UNRELATED-THREAD-ROW" "2026-08-01T00:00:02+00:00" \
        "otherproj" "doc:$REPONAME/sub/zz.py"
    run_hook agentS; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_absent "(s) an unsubscribed thread is still filtered out" \
        "UNRELATED-THREAD-ROW" "$ctx"

    #     SUBSCRIBE-ALL IS NOT NARROWED. An agent enrolled with an empty topic
    #     set sees the whole board; adding a doc topic would flip it to a
    #     FILTERED subscription and silently hide every other row from it.
    enroll_agent "$RS" agentAll
    run_hook_raw "$(write_payload agentAll Write "$REPO/sub/a.py")"
    ck "(s) a subscribe-all agent is not narrowed by a doc topic" \
        "" "$(part_topics "$RS" agentAll)"
    #     Assert on THIS beat's own output: the doc-enrol leg runs before the
    #     row pass, so if it had narrowed agentAll the narrowing would show up
    #     in the very rows this beat emits.
    ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(s) the subscribe-all agent still receives the threaded row" \
        "THREAD-ROW-FOR-S" "$ctx"
    ck_contains "(s) the subscribe-all agent still receives every OTHER row" \
        "UNRELATED-THREAD-ROW" "$ctx"

    #     A Write OUTSIDE any repo keys to None and enrols nothing -- never a
    #     fabricated key.
    OUTSIDE="$(mktemp -d)" || exit 1
    : > "$OUTSIDE/loose.md"
    before_t="$(part_topics "$RS" agentT)"
    run_hook_raw "$(write_payload agentT Write "$OUTSIDE/loose.md")"
    ck "(s) a Write outside any repo enrols nothing" \
        "$before_t" "$(part_topics "$RS" agentT)"

    #     A NON-participant is not enrolled, and gains no topics, by writing a
    #     file inside a repo.
    run_hook_raw "$(write_payload docbystander Write "$REPO/sub/a.py")"
    ck "(s) a doc-writing bystander gets ZERO output" "" "$HOOK_OUT"
    if is_part "$RS" docbystander; then
        fail=$((fail + 1)); echo "  FAIL (s) a Write enrolled a bystander" >&2
    else
        pass=$((pass + 1))
    fi

    #     MultiEdit / NotebookEdit carry file_path too and take the same leg.
    run_hook_raw "$(write_payload agentS MultiEdit "$REPO/sub/m.py")"
    ck_contains "(s) MultiEdit enrols the doc key" \
        "doc:$REPONAME/sub/m.py" "$(part_topics "$RS" agentS)"
    run_hook_raw "$(write_payload agentS NotebookEdit "$REPO/sub/n.ipynb")"
    ck_contains "(s) NotebookEdit enrols the doc key" \
        "doc:$REPONAME/sub/n.ipynb" "$(part_topics "$RS" agentS)"

    #     AUTO-CLAIM (2026-08-26, Drake's option 2): the FIRST enrol of a doc key
    #     posts ONE claim row carrying that key, so two seats editing one file
    #     inside the alive window make a thread the board lane can render.
    #     kind=claim, never status: swarm_threads.alive() ignores status rows,
    #     so a status row could never make a thread alive.
    SEATFILE="$ROOT/comms-$RS/seatS.jsonl"
    claims_for() {  # claims_for <relpath> ; rows in seatS.jsonl carrying that key
        grep -c "\"thread\": \"doc:$REPONAME/$1\"" "$SEATFILE" 2>/dev/null | tr -d ' '
    }
    ck "(v) the first Write of a doc posts exactly one claim row" "1" "$(claims_for sub/a.py)"
    ck "(v) a re-Write of the same doc posts no second claim" "1" "$(claims_for sub/c.py)"
    ck "(v) MultiEdit's first enrol posts a claim too" "1" "$(claims_for sub/m.py)"
    claim_a="$(grep "\"thread\": \"doc:$REPONAME/sub/a.py\"" "$SEATFILE")"
    ck_contains "(v) the claim is kind=claim (status rows never count toward alive)" \
        '"kind": "claim"' "$claim_a"
    ck_contains "(v) the claim rides the repo board topic" \
        "\"topic\": \"board:$REPONAME\"" "$claim_a"
    ck_contains "(v) the claim text names the repo-relative path" \
        '"text": "editing sub/a.py"' "$claim_a"
    ck_contains "(v) the claim is posted as the participant's own seat" \
        '"seat": "seatS"' "$claim_a"

    #     No seat, no claim: a participant enrolled without --seat has no file
    #     to write, so the enrol still happens and nothing is posted.
    enroll_agent "$RS" agentNoSeat "projN"
    files_before="$(ls "$ROOT/comms-$RS" | wc -l | tr -d ' ')"
    run_hook_raw "$(write_payload agentNoSeat Write "$REPO/sub/a.py")"
    ck_contains "(v) a seatless participant still enrols the key" \
        "doc:$REPONAME/sub/a.py" "$(part_topics "$RS" agentNoSeat)"
    ck "(v) a seatless participant posts no claim (no new mailbox file)" \
        "$files_before" "$(ls "$ROOT/comms-$RS" | wc -l | tr -d ' ')"

    #     Subscribe-all is not narrowed (above) AND posts no claim: add_topics
    #     reports changed=False for it, and the claim rides on changed.
    enroll_agent "$RS" agentAllSeat "" seatAllS
    run_hook_raw "$(write_payload agentAllSeat Write "$REPO/sub/a.py")"
    if [ -e "$ROOT/comms-$RS/seatAllS.jsonl" ]; then
        fail=$((fail + 1)); echo "  FAIL (u) a subscribe-all seat posted a claim" >&2
    else
        pass=$((pass + 1))
    fi

    #     REPLY HINT (Drake's option 1, the companion line): a beat that delivers
    #     a threaded row also delivers ONE line naming the reply command, so the
    #     reply carries --thread and lands in the same thread. A beat with only
    #     unthreaded rows carries no hint.
    post_row "$RS" seatU finding "HINT-THREAD-ROW" "2026-08-01T00:00:03+00:00" \
        "otherproj" "doc:$REPONAME/sub/a.py"
    run_hook agentS; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(v) a threaded delivery carries the reply hint" \
        "comms post reply --to <seat> --thread <key>" "$ctx"
    ck_contains "(v) the reply hint names the run the beat came from" \
        "COMMS_RUN=$RS comms post reply" "$ctx"
    post_row "$RS" seatU comment "PLAIN-ROW" "2026-08-01T00:00:04+00:00" "projS"
    run_hook agentS; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(v) control: the plain row is delivered" "PLAIN-ROW" "$ctx"
    ck_absent "(v) an unthreaded delivery carries no reply hint" \
        "comms post reply" "$ctx"

    # -----------------------------------------------------------------------
    # (w) AGENT-AGNOSTIC DOC ENROL: Codex apply_patch paths and write-shaped
    #     Bash beats feed the same subscription + auto-claim path as Write/Edit.
    RW2="hbtestw2$$x$RANDOM"
    arm_run "$RW2"
    enroll_agent "$RW2" agentPatch "projW2" seatPatch
    enroll_agent "$RW2" agentBash "projW2" seatBash
    enroll_agent "$RW2" agentMismatch "projW2" seatMismatch
    enroll_agent "$RW2" agentOutside "projW2" seatOutside
    enroll_agent "$RW2" agentSlow "projW2" seatSlow
    enroll_agent "$RW2" agentRead "projW2" seatRead
    enroll_agent "$RW2" agentRedirect "projW2" seatRedirect
    enroll_agent "$RW2" agentHeredoc "projW2" seatHeredoc
    enroll_agent "$RW2" agentSeatless "projW2"
    REPO2="$(mktemp -d)" || exit 1
    REPONAME2="$(basename "$REPO2")"
    mkdir -p "$REPO2/sub"
    git -C "$REPO2" init -q
    : > "$REPO2/tracked.py"
    git -C "$REPO2" add tracked.py

    patch_add='*** Begin Patch
*** Add File: sub/n.py
+hi
*** End Patch'
    run_hook_raw "$(command_payload agentPatch apply_patch "$REPO2" "$patch_add")"
    PATCHFILE="$ROOT/comms-$RW2/seatPatch.jsonl"
    patch_claims="$(grep -c "\"thread\": \"doc:$REPONAME2/sub/n.py\"" "$PATCHFILE" 2>/dev/null | tr -d ' ')"
    ck "(w-a) apply_patch Add File posts one claim" "1" "$patch_claims"
    patch_row="$(grep "\"thread\": \"doc:$REPONAME2/sub/n.py\"" "$PATCHFILE" 2>/dev/null)"
    ck_contains "(w-a) apply_patch claim text names the relative path" '"text": "editing sub/n.py"' "$patch_row"
    ck_contains "(w-a) apply_patch claim uses the repo board topic" "\"topic\": \"board:$REPONAME2\"" "$patch_row"

    patch_update='*** Begin Patch
*** Update File: sub/n.py
@@
-hi
+bye
*** End Patch'
    run_hook_raw "$(command_payload agentPatch apply_patch "$REPO2" "$patch_update")"
    ck "(w-b) apply_patch Update File does not duplicate a claim" "1" \
        "$(grep -c "\"thread\": \"doc:$REPONAME2/sub/n.py\"" "$PATCHFILE" 2>/dev/null | tr -d ' ')"

    patch_update_fresh='*** Begin Patch
*** Update File: sub/u.py
@@
-old
+new
*** End Patch'
    run_hook_raw "$(command_payload agentPatch apply_patch "$REPO2" "$patch_update_fresh")"
    ck "(w-b2) apply_patch Update File enrols on its own" "1" \
        "$(grep -c "\"thread\": \"doc:$REPONAME2/sub/u.py\"" "$PATCHFILE" 2>/dev/null | tr -d ' ')"

    before_patch_topics="$(part_topics "$RW2" agentPatch)"
    patch_delete='*** Begin Patch
*** Delete File: sub/gone.py
*** End Patch'
    run_hook_raw "$(command_payload agentPatch apply_patch "$REPO2" "$patch_delete")"
    ck "(w-c) apply_patch Delete File enrols nothing" "$before_patch_topics" \
        "$(part_topics "$RW2" agentPatch)"

    GIT_REAL="$(command -v git)"
    SHIM="$(mktemp -d)" || exit 1
    GIT_CALLS="$SHIM/git.calls"
    cat > "$SHIM/git" <<EOF
#!/bin/sh
printf 'git %s\n' "\$*" >> "$GIT_CALLS"
exec "$GIT_REAL" "\$@"
EOF
    chmod +x "$SHIM/git"
    bash_command="cat <<'EOF' > sub/b.py
bash write
EOF"
    (cd "$REPO2" && /bin/bash -c "$bash_command")
    HOOK_OUT="$(command_payload agentBash Bash "$REPO2" "$bash_command" | \
        PATH="$SHIM:$PATH" COMMS_STATE_DIR="$STATE" COMMS_ROOT="$ROOT" /bin/bash "$HOOK")"
    HOOK_RC=$?
    BASHFILE="$ROOT/comms-$RW2/seatBash.jsonl"
    ck "(w-d) Bash heredoc beat exits 0" "0" "$HOOK_RC"
    ck "(w-d) Bash heredoc posts one claim" "1" \
        "$(grep -c "\"thread\": \"doc:$REPONAME2/sub/b.py\"" "$BASHFILE" 2>/dev/null | tr -d ' ')"

    write_git_calls="$(wc -l < "$GIT_CALLS" | tr -d ' ')"
    : > "$GIT_CALLS"
    HOOK_OUT="$(command_payload agentBash Bash "$REPO2" 'cat sub/b.py' | \
        PATH="$SHIM:$PATH" COMMS_STATE_DIR="$STATE" COMMS_ROOT="$ROOT" /bin/bash "$HOOK")"
    HOOK_RC=$?
    ck "(w-e) read-shaped Bash spawns no git process" "0" \
        "$(wc -l < "$GIT_CALLS" | tr -d ' ')"
    printf 'git-spawn-proof read=%s write=%s\n' \
        "$(wc -l < "$GIT_CALLS" | tr -d ' ')" "$write_git_calls"

    : > "$GIT_CALLS"
    HOOK_OUT="$(command_payload agentRead Bash "$REPO2" 'grep -n "def" sub/b.py 2>/dev/null' | \
        PATH="$SHIM:$PATH" COMMS_STATE_DIR="$STATE" COMMS_ROOT="$ROOT" /bin/bash "$HOOK")"
    HOOK_RC=$?
    ck "(w-j) fd redirect on a read-shaped Bash command posts no claim" "0" \
        "$(grep "\"thread\": \"doc:$REPONAME2/sub/b.py\"" "$ROOT/comms-$RW2/seatRead.jsonl" 2>/dev/null | wc -l | tr -d ' ')"
    ck "(w-j) fd redirect on a read-shaped Bash command spawns no git" "0" \
        "$(wc -l < "$GIT_CALLS" | tr -d ' ')"

    : > "$GIT_CALLS"
    HOOK_OUT="$(command_payload agentRedirect Bash "$REPO2" 'printf x > sub/b.py' | \
        PATH="$SHIM:$PATH" COMMS_STATE_DIR="$STATE" COMMS_ROOT="$ROOT" /bin/bash "$HOOK")"
    HOOK_RC=$?
    ck "(w-j) output redirect remains write-shaped" "1" \
        "$(grep -c "\"thread\": \"doc:$REPONAME2/sub/b.py\"" "$ROOT/comms-$RW2/seatRedirect.jsonl" 2>/dev/null | tr -d ' ')"
    HOOK_OUT="$(command_payload agentHeredoc Bash "$REPO2" 'cat <<EOF > sub/b.py' | \
        PATH="$SHIM:$PATH" COMMS_STATE_DIR="$STATE" COMMS_ROOT="$ROOT" /bin/bash "$HOOK")"
    HOOK_RC=$?
    ck "(w-j) heredoc plus output redirect remains write-shaped" "1" \
        "$(grep -c "\"thread\": \"doc:$REPONAME2/sub/b.py\"" "$ROOT/comms-$RW2/seatHeredoc.jsonl" 2>/dev/null | tr -d ' ')"

    before_mismatch="$(part_topics "$RW2" agentMismatch)"
    run_hook_raw "$(command_payload agentMismatch Bash "$REPO2" 'printf x > unrelated.py')"
    ck "(w-f) basename gate rejects an unrelated dirty path" "$before_mismatch" \
        "$(part_topics "$RW2" agentMismatch)"

    OUTSIDE2="$(mktemp -d)" || exit 1
    OUTSIDE_ERR="$STATE/outside-repo.err"
    HOOK_OUT="$(command_payload agentOutside Bash "$OUTSIDE2" 'printf x > loose.py' | \
        COMMS_STATE_DIR="$STATE" COMMS_ROOT="$ROOT" /bin/bash "$HOOK" 2>"$OUTSIDE_ERR")"
    HOOK_RC=$?
    ck "(w-g) write-shaped Bash outside a repo exits 0" "0" "$HOOK_RC"
    ck "(w-g) write-shaped Bash outside a repo enrols nothing" "projW2" \
        "$(part_topics "$RW2" agentOutside)"
    ck "(w-g) write-shaped Bash outside a repo is silent" "" "$(cat "$OUTSIDE_ERR")"

    SLOW_SHIM="$(mktemp -d)" || exit 1
    cat > "$SLOW_SHIM/git" <<'EOF'
#!/bin/sh
exec sleep 5
EOF
    chmod +x "$SLOW_SHIM/git"
    post_row "$RW2" seatOther finding "SLOW-BEAT-DELIVERS" \
        "2026-08-03T00:00:01+00:00" "projW2"
    started="$(date +%s)"
    HOOK_OUT="$(command_payload agentSlow Bash "$REPO2" 'printf x > slow.py' | \
        PATH="$SLOW_SHIM:$PATH" COMMS_STATE_DIR="$STATE" COMMS_ROOT="$ROOT" /bin/bash "$HOOK" 2>/dev/null)"
    HOOK_RC=$?
    elapsed=$(( $(date +%s) - started ))
    ck "(w-h) timed-out git beat exits 0" "0" "$HOOK_RC"
    if [ "$elapsed" -lt 4 ]; then pass=$((pass + 1)); else
        fail=$((fail + 1)); echo "  FAIL (w-h) git timeout took ${elapsed}s" >&2; fi
    ck_contains "(w-h) timed-out git still delivers ordinary rows" \
        "SLOW-BEAT-DELIVERS" "$(addl_ctx "$HOOK_OUT")"

    run_hook_raw "$(command_payload agentSeatless apply_patch "$REPO2" "$patch_add")"
    ck_contains "(w-i) seatless apply_patch participant enrols" \
        "doc:$REPONAME2/sub/n.py" "$(part_topics "$RW2" agentSeatless)"
    if [ -e "$ROOT/comms-$RW2/agentSeatless.jsonl" ]; then
        fail=$((fail + 1)); echo "  FAIL (w-i) seatless apply_patch posted a claim" >&2
    else
        pass=$((pass + 1))
    fi

    rm -rf "$REPO2" "$OUTSIDE2" "$SHIM" "$SLOW_SHIM"

    # -----------------------------------------------------------------------
    # (x) DELIVERY ORDER: rows specifically for this seat precede ordinary
    #     subscribed rows and do not consume the ordinary-row CAP. Status rows
    #     older than the thread alive window are consumed without delivery.
    RX="hbtestx$$x$RANDOM"
    arm_run "$RX"
    enroll_agent "$RX" agentX "projX,doc:repo/watched.py" seatX
    for n in $(seq -w 1 25); do
        post_row "$RX" seatPeer finding "X1-TOPIC-$n" \
            "2026-08-27T01:05:$n+00:00" "projX"
    done
    post_row "$RX" seatPeer finding "X1-UNICAST-LAST" \
        "2026-08-27T01:06:00+00:00" "@seatX"
    run_hook agentX; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(x-1) unicast behind the topic backlog is delivered" \
        "[FOR YOU from seatPeer] [seatPeer | finding | @seatX | 2026-08-27T01:06:00+00:00] X1-UNICAST-LAST" "$ctx"
    ck "(x-1) ordinary topic rows remain capped at ten" "10" \
        "$(printf '%s' "$ctx" | grep -c 'X1-TOPIC-' | tr -d ' ')"
    ck_contains "(x-1) overflow counts only held ordinary rows" \
        "15 more, read the full board" "$ctx"

    RX2="hbtestx2$$x$RANDOM"
    arm_run "$RX2"
    enroll_agent "$RX2" agentX2 "projX" seatX2
    for n in $(seq -w 1 12); do
        post_row "$RX2" seatPeer finding "X2-UNICAST-$n" \
            "2026-08-27T02:00:$n+00:00" "@seatX2"
        post_row "$RX2" seatPeer finding "X2-TOPIC-$n" \
            "2026-08-27T02:01:$n+00:00" "projX"
    done
    run_hook agentX2; ctx="$(addl_ctx "$HOOK_OUT")"
    ck "(x-2) all twelve unicasts are emitted" "12" \
        "$(printf '%s' "$ctx" | grep -c 'X2-UNICAST-' | tr -d ' ')"
    ck "(x-2) ten ordinary rows are emitted" "10" \
        "$(printf '%s' "$ctx" | grep -c 'X2-TOPIC-' | tr -d ' ')"
    ck_contains "(x-2) overflow holds only two ordinary rows" \
        "2 more, read the full board" "$ctx"

    RX3="hbtestx3$$x$RANDOM"
    arm_run "$RX3"
    enroll_agent "$RX3" agentX3 "projX,doc:repo/watched.py" seatX3
    for n in $(seq -w 1 20); do
        post_row "$RX3" seatPeer finding "X3-TOPIC-$n" \
            "2026-08-27T03:00:$n+00:00" "projX"
    done
    post_row "$RX3" seatPeer finding "X3-THREAD-LAST" \
        "2026-08-27T03:01:00+00:00" "other" "doc:repo/watched.py"
    run_hook agentX3; ctx="$(addl_ctx "$HOOK_OUT")"
    first_x3="$(printf '%s\n' "$ctx" | grep 'X3-' | head -1)"
    ck_contains "(x-3) subscribed-thread row is emitted first" "X3-THREAD-LAST" "$first_x3"
    if grep -q 'swarm_mailbox.row_reaches' "$HOOK" \
        && ! grep -q 'if (r.get("topic") or "default") in subs or' "$HOOK"; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1))
        echo "  FAIL (x-3) one-implementation guard: heartbeat uses the shared subscription predicate only" >&2
    fi

    old_at="$(python3 -c 'import datetime; print((datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(hours=2)).isoformat())')"
    fresh_at="$(python3 -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat())')"
    RX4="hbtestx4$$x$RANDOM"
    arm_run "$RX4"
    enroll_agent "$RX4" agentX4 "projX" seatX4
    for n in $(seq -w 1 30); do
        post_row "$RX4" seatPeer status "X4-STALE-$n" "$old_at" "projX"
    done
    post_row "$RX4" seatPeer finding "X4-FRESH-FINDING" "$fresh_at" "projX"
    run_hook agentX4; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(x-4) fresh finding survives stale status backlog" "X4-FRESH-FINDING" "$ctx"
    ck_absent "(x-4) stale status rows are skipped" "X4-STALE-" "$ctx"
    ck_absent "(x-4) skipped status rows do not create overflow" \
        "more, read the full board" "$ctx"
    run_hook agentX4
    ck "(x-4) second beat is empty after stale rows are consumed" "" \
        "$(addl_ctx "$HOOK_OUT")"

    RX5="hbtestx5$$x$RANDOM"
    arm_run "$RX5"
    enroll_agent "$RX5" agentX5 "projX" seatX5
    young_at="$(python3 -c 'import datetime; print((datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(minutes=5)).isoformat())')"
    post_row "$RX5" seatPeer status "X5-YOUNG-STATUS" "$young_at" "projX"
    run_hook agentX5
    ck_contains "(x-5) five-minute-old status is delivered" "X5-YOUNG-STATUS" \
        "$(addl_ctx "$HOOK_OUT")"

    RX6="hbtestx6$$x$RANDOM"
    arm_run "$RX6"
    enroll_agent "$RX6" agentX6 "projX" seatX6
    post_row "$RX6" seatPeer status "X6-BAD-DATE" "not-a-date" "projX"
    run_hook agentX6
    ck_contains "(x-6) unparseable status date is delivered" "X6-BAD-DATE" \
        "$(addl_ctx "$HOOK_OUT")"

    RX7="hbtestx7$$x$RANDOM"
    arm_run "$RX7"
    enroll_agent "$RX7" agentX7 "projX" seatX7
    two_min_at="$(python3 -c 'import datetime; print((datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(minutes=2)).isoformat())')"
    post_row "$RX7" seatPeer status "X7-OVERRIDE-STALE" "$two_min_at" "projX"
    HOOK_OUT="$(payload agentX7 | COMMS_THREAD_ALIVE_SECONDS=60 \
        COMMS_STATE_DIR="$STATE" COMMS_ROOT="$ROOT" /bin/bash "$HOOK")"
    ck_absent "(x-7) sixty-second window skips two-minute status" \
        "X7-OVERRIDE-STALE" "$(addl_ctx "$HOOK_OUT")"

    # Byte-identity control against the base commit under equivalent isolated
    # state: ordinary rows exercise neither priority nor stale-status behavior.
    BASE_TREE="$STATE/base-tree"
    mkdir -p "$BASE_TREE/adapters/claude-code" "$BASE_TREE/lib"
    git show 18e1b24:adapters/claude-code/swarm-heartbeat.sh > \
        "$BASE_TREE/adapters/claude-code/swarm-heartbeat.sh"
    git show 18e1b24:adapters/claude-code/stdin-bounded.sh > \
        "$BASE_TREE/adapters/claude-code/stdin-bounded.sh"
    git show 18e1b24:lib/swarm_arm.py > "$BASE_TREE/lib/swarm_arm.py"
    chmod +x "$BASE_TREE/adapters/claude-code/swarm-heartbeat.sh"
    CUR_STATE="$STATE/control-current"; BASE_STATE="$STATE/control-base"
    CUR_ROOT="$ROOT/control-current"; BASE_ROOT="$ROOT/control-base"
    COMMS_STATE_DIR="$CUR_STATE" python3 "$SA" arm control >/dev/null
    COMMS_STATE_DIR="$CUR_STATE" python3 "$SA" enroll control --agent-id control-agent \
        --topics projX --seat control-seat >/dev/null
    COMMS_STATE_DIR="$BASE_STATE" python3 "$BASE_TREE/lib/swarm_arm.py" arm control >/dev/null
    COMMS_STATE_DIR="$BASE_STATE" python3 "$BASE_TREE/lib/swarm_arm.py" enroll control \
        --agent-id control-agent --topics projX --seat control-seat >/dev/null
    for control_root in "$CUR_ROOT" "$BASE_ROOT"; do
        ROOT="$control_root"
        post_row control seatPeer finding CONTROL-1 "2026-08-27T04:00:01+00:00" projX
        post_row control seatPeer finding CONTROL-2 "2026-08-27T04:00:02+00:00" projX
        post_row control seatPeer finding CONTROL-3 "2026-08-27T04:00:03+00:00" projX
    done
    ROOT="$CUR_ROOT"
    current_control="$(payload control-agent | COMMS_STATE_DIR="$CUR_STATE" \
        COMMS_ROOT="$CUR_ROOT" /bin/bash "$HOOK")"
    base_control="$(payload control-agent | COMMS_STATE_DIR="$BASE_STATE" \
        COMMS_ROOT="$BASE_ROOT" /bin/bash "$BASE_TREE/adapters/claude-code/swarm-heartbeat.sh")"
    ck "(x-9) ordinary-row beat is byte-identical to base commit" \
        "$base_control" "$current_control"
    ROOT="${CUR_ROOT%/control-current}"

    RX10="hbtestx10$$x$RANDOM"
    arm_run "$RX10"
    enroll_agent "$RX10" agentX10 "projX" seatX10
    for n in $(seq -w 1 25); do
        post_row "$RX10" seatPeer finding "X10-TOPIC-$n" \
            "2026-08-27T05:00:$n+00:00" "projX"
    done
    post_row "$RX10" seatPeer finding "X10-UNICAST-LAST" \
        "2026-08-27T05:01:00+00:00" "@seatX10"
    run_hook agentX10; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(x-10) beat 1 delivers FOR YOU row" "X10-UNICAST-LAST" "$ctx"
    ck "(x-10) beat 1 delivers first ten ordinary rows" "10" \
        "$(printf '%s' "$ctx" | grep -c 'X10-TOPIC-' | tr -d ' ')"
    ck_contains "(x-10) beat 1 reports fifteen held rows" \
        "15 more, read the full board" "$ctx"
    FORWARDED="$STATE/swarm-cursor/$RX10/agentX10.forwarded"
    ck "(x-10) forwarded set holds the unicast key after beat 1" \
        "2026-08-27T05:01:00+00:00	seatPeer" "$(cat "$FORWARDED" 2>/dev/null)"
    run_hook agentX10; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_absent "(x-10) beat 2 does not repeat FOR YOU row" "X10-UNICAST-LAST" "$ctx"
    ck "(x-10) beat 2 delivers next ten ordinary rows" "10" \
        "$(printf '%s' "$ctx" | grep -c 'X10-TOPIC-' | tr -d ' ')"
    ck_contains "(x-10) beat 2 reports five held rows" \
        "5 more, read the full board" "$ctx"
    run_hook agentX10; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_absent "(x-10) beat 3 does not repeat FOR YOU row" "X10-UNICAST-LAST" "$ctx"
    ck "(x-10) beat 3 delivers final five ordinary rows" "5" \
        "$(printf '%s' "$ctx" | grep -c 'X10-TOPIC-' | tr -d ' ')"
    ck_absent "(x-10) beat 3 has no overflow hint" \
        "more, read the full board" "$ctx"
    run_hook agentX10
    ck "(x-10) beat 4 is empty" "" "$(addl_ctx "$HOOK_OUT")"
    ck "(x-10) forwarded set is written empty after beat 3" "0" \
        "$(wc -c < "$FORWARDED" | tr -d ' ')"

    RX11="hbtestx11$$x$RANDOM"
    arm_run "$RX11"
    enroll_agent "$RX11" agentX11 "projX" seatX11
    X11_TEXT='X11 sentence one names @name, includes "double quotes", a | pipe, a <tag>, and a \ backslash. Sentence two preserves every byte while making this message deliberately long. Sentence three keeps going so truncation or reconstruction is visible. Sentence four repeats the contract in plain text: priority delivery must retain the complete finding. Sentence five adds enough material to cross the requested threshold without relying on rendering width. Sentence six says that punctuation, spacing, and symbols all belong to the payload. Sentence seven makes this exact string longer than six hundred characters. Sentence eight continues with stable prose for a byte-for-byte equality assertion. Sentence nine closes the message after another deliberately verbose clause whose only job is to make accidental shortening immediately observable in the emitted row.'
    python3 - "$ROOT/comms-$RX11/seatPeer.jsonl" "$X11_TEXT" <<'PY'
import json
import os
import sys
path, text = sys.argv[1:]
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "a") as fh:
    fh.write(json.dumps({"seat": "seatPeer", "at": "2026-08-27T06:00:00+00:00",
                         "kind": "finding", "text": text, "topic": "@seatX11"}) + "\n")
PY
    run_hook agentX11; ctx="$(addl_ctx "$HOOK_OUT")"
    first_x11="$(printf '%s\n' "$ctx" | sed -n '2p')"
    expected_x11="- [FOR YOU from seatPeer] [seatPeer | finding | @seatX11 | 2026-08-27T06:00:00+00:00] $X11_TEXT"
    ck "(x-11) full priority text is verbatim on the first row" \
        "$expected_x11" "$first_x11"

    RX12="hbtestx12$$x$RANDOM"
    arm_run "$RX12"
    enroll_agent "$RX12" agentX12 "projX" seatX12
    post_row "$RX12" seatPeer finding "X12-FRESH" \
        "2026-08-27T07:00:00+00:00" "projX"
    # A checkout whose lib/ predates swarm_threads.py: a WHOLE TREE, not an env
    # override, so the production hook keeps resolving lib/ from its own dir.
    SHIM_TREE="$STATE/shim-tree"
    mkdir -p "$SHIM_TREE/adapters/claude-code" "$SHIM_TREE/lib"
    cp "$HOOK" "$SHIM_TREE/adapters/claude-code/swarm-heartbeat.sh"
    cp "$SELF_DIR/../adapters/claude-code/stdin-bounded.sh" \
        "$SHIM_TREE/adapters/claude-code/stdin-bounded.sh"
    cp "$SA" "$SHIM_TREE/lib/swarm_arm.py"
    cp "$SELF_DIR/../lib/swarm_mailbox.py" "$SHIM_TREE/lib/swarm_mailbox.py"
    chmod +x "$SHIM_TREE/adapters/claude-code/swarm-heartbeat.sh"
    X12_ERR="$STATE/x12.err"
    HOOK_OUT="$(payload agentX12 | COMMS_STATE_DIR="$STATE" COMMS_ROOT="$ROOT" \
        /bin/bash "$SHIM_TREE/adapters/claude-code/swarm-heartbeat.sh" 2>"$X12_ERR")"
    ck_contains "(x-12) missing swarm_threads still delivers a fresh row" \
        "X12-FRESH" "$(addl_ctx "$HOOK_OUT")"
    ck "(x-12) missing swarm_threads prints exactly one stderr line" \
        "1" "$(wc -l < "$X12_ERR" | tr -d ' ')"

    # A checkout missing the required mailbox implementation must decline the
    # delivery loudly while preserving the hook's never-block exit behavior.
    NO_MAIL_TREE="$STATE/no-mail-tree"
    mkdir -p "$NO_MAIL_TREE/adapters/claude-code" "$NO_MAIL_TREE/lib"
    cp "$HOOK" "$NO_MAIL_TREE/adapters/claude-code/swarm-heartbeat.sh"
    cp "$SELF_DIR/../adapters/claude-code/stdin-bounded.sh" \
        "$NO_MAIL_TREE/adapters/claude-code/stdin-bounded.sh"
    cp "$SA" "$NO_MAIL_TREE/lib/swarm_arm.py"
    chmod +x "$NO_MAIL_TREE/adapters/claude-code/swarm-heartbeat.sh"
    NO_MAIL_ERR="$STATE/no-mail.err"
    NO_MAIL_OUT="$(payload agentX12 | COMMS_STATE_DIR="$STATE" COMMS_ROOT="$ROOT" \
        /bin/bash "$NO_MAIL_TREE/adapters/claude-code/swarm-heartbeat.sh" 2>"$NO_MAIL_ERR")"
    ck "(x-12) missing swarm_mailbox makes no delivery" "" "$NO_MAIL_OUT"
    ck "(x-12) missing swarm_mailbox prints exactly one stderr line" \
        "1" "$(wc -l < "$NO_MAIL_ERR" | tr -d ' ')"
    ck_contains "(x-12) missing swarm_mailbox names the loud non-delivery" \
        "swarm-heartbeat: swarm_mailbox unavailable:" "$(cat "$NO_MAIL_ERR")"

    RX13="hbtestx13$$x$RANDOM"
    arm_run "$RX13"
    enroll_agent "$RX13" agentX13A "projX" seatX13A
    enroll_agent "$RX13" agentX13B "projX" seatX13B
    for n in $(seq -w 1 11); do
        post_row "$RX13" seatPeer finding "X13-TOPIC-$n" \
            "2026-08-27T08:00:$n+00:00" "projX"
    done
    post_row "$RX13" seatPeer finding "X13-FOR-A" \
        "2026-08-27T08:01:00+00:00" "@seatX13A"
    post_row "$RX13" seatPeer finding "X13-FOR-B" \
        "2026-08-27T08:01:01+00:00" "@seatX13B"
    run_hook agentX13A
    ck_contains "(x-13) seat A receives its FOR YOU row" \
        "X13-FOR-A" "$(addl_ctx "$HOOK_OUT")"
    # Seed B's own forwarded file with A's key by hand: a forwarded set keyed
    # on (at, seat) must not suppress B's row, which shares the poster but not
    # the timestamp. An implementation keyed on the poster alone fails here.
    mkdir -p "$STATE/swarm-cursor/$RX13"
    printf '2026-08-27T08:01:00+00:00\tseatPeer\n' \
        > "$STATE/swarm-cursor/$RX13/agentX13B.forwarded"
    run_hook agentX13B
    ck_contains "(x-13) seat A forwarded state does not suppress seat B" \
        "X13-FOR-B" "$(addl_ctx "$HOOK_OUT")"

    #     A THREAD_KEY THAT RAISES must not break the beat. An embedded NUL in
    #     file_path makes os.path.realpath raise ValueError -- the leg is
    #     wrapped, so the beat still emits its rows and exits 0.
    post_row "$RS" seatU finding "BEAT-SURVIVES" "2026-08-02T00:00:01+00:00" "projS"
    ERRF="$STATE/doc-enrol.err"
    HOOK_OUT="$(printf '%s' "$(write_payload agentT Write '/tmp/nul\u0000path.md')" \
        | COMMS_STATE_DIR="$STATE" COMMS_ROOT="$ROOT" /bin/bash "$HOOK" 2>"$ERRF")"
    HOOK_RC=$?
    ck "(s) a raising thread_key still exits 0" "0" "$HOOK_RC"
    ck "(s) a raising thread_key writes exactly one stderr line" \
        "1" "$(wc -l < "$ERRF" | tr -d ' ')"
    ck_contains "(s) the stderr line names the leg" "swarm-heartbeat: doc-enrol" \
        "$(cat "$ERRF")"
    ck_contains "(s) the beat's own rows are still delivered" "BEAT-SURVIVES" \
        "$(addl_ctx "$HOOK_OUT")"

    rm -rf "$REPO" "$OUTSIDE"

    # -----------------------------------------------------------------------
    # (t) AN ENROL INVALIDATES THE MTIME SHORT-CIRCUIT (issue #42 review).
    #     The short-circuit skips the mailbox scan when no .jsonl is newer than
    #     the last scan. That treats the mailbox as the only input to "is there
    #     anything for me" -- but the SUBSCRIPTION is the other half of that
    #     query, and growing it can make an ALREADY-PRESENT row match. A beat
    #     that enrols a doc key must therefore rescan even though no file moved.
    RV="hbtestv$$x$RANDOM"
    arm_run "$RV"
    enroll_agent "$RV" agentV "projV" seatV
    REPOV="$(mktemp -d)" || exit 1
    REPOVNAME="$(basename "$REPOV")"
    mkdir -p "$REPOV/.git"
    : > "$REPOV/v.py"
    post_row "$RV" seatW finding "V-SEED-ROW" "2026-09-01T00:00:01+00:00" "projV"
    run_hook agentV                       # delivers the seed, records the mtime
    run_hook agentV                       # nothing new -> short-circuits
    ck "(t) a quiet beat short-circuits the scan" "true" "$(last_scan_field "$RV" short_circuit)"
    ck "(t) a short-circuited beat inspects zero rows" "0" "$(last_scan_field "$RV" rows_inspected)"

    #     Same mailbox, not one byte changed -- but this beat enrols a doc key.
    run_hook_raw "$(write_payload agentV Write "$REPOV/v.py")"
    ck "(t) an enrol beat does NOT short-circuit" "false" "$(last_scan_field "$RV" short_circuit)"
    if [ "$(last_scan_field "$RV" rows_inspected)" -gt 0 ]; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1)); echo "  FAIL (t) enrol beat inspected no rows" >&2
    fi

    #     The bypass is for THIS beat only: the next quiet beat short-circuits
    #     again, so the speed path is not permanently disabled.
    run_hook agentV
    ck "(t) the beat after an enrol short-circuits again" "true" \
        "$(last_scan_field "$RV" short_circuit)"

    # -----------------------------------------------------------------------
    # (u) FORWARD-ONLY DELIVERY -- the convener's v1 ruling, pinned here.
    #     Subscribing to a doc does NOT replay rows about that doc that are
    #     already behind this seat's cursor. The seat's cursor is one position
    #     over the whole board, so replaying them would mean rewinding it and
    #     re-delivering every other row too. Replay design is issue #57.
    RW="hbtestw$$x$RANDOM"
    arm_run "$RW"
    enroll_agent "$RW" agentW "projW" seatW2
    REPOW="$(mktemp -d)" || exit 1
    REPOWNAME="$(basename "$REPOW")"
    mkdir -p "$REPOW/.git"
    : > "$REPOW/w.py"

    #     A threaded row nobody is subscribed to yet, then a plain subscribed
    #     row with a LATER `at` that drags the cursor past it.
    post_row "$RW" seatX finding "ROW-BEFORE-ENROL" "2026-09-02T00:00:01+00:00" \
        "otherproj" "doc:$REPOWNAME/w.py"
    post_row "$RW" seatX finding "CURSOR-MOVER" "2026-09-02T00:00:09+00:00" "projW"
    run_hook agentW; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(u) the cursor-moving row is delivered" "CURSOR-MOVER" "$ctx"
    ck_absent "(u) the pre-enrol threaded row is not delivered yet" \
        "ROW-BEFORE-ENROL" "$ctx"

    #     Now enrol the doc key. The pre-enrol row NOW matches the filter, and
    #     the scan DOES run (no short-circuit, see (t)) -- so it is inspected
    #     and deliberately not delivered, because it sits behind the cursor.
    run_hook_raw "$(write_payload agentW Write "$REPOW/w.py")"
    ck_contains "(u) the enrol landed" "doc:$REPOWNAME/w.py" "$(part_topics "$RW" agentW)"
    ck_absent "(u) FORWARD-ONLY: a row already behind the cursor is NOT replayed" \
        "ROW-BEFORE-ENROL" "$(addl_ctx "$HOOK_OUT")"

    #     A row about the same doc posted AFTER the enrol is delivered.
    post_row "$RW" seatX finding "ROW-AFTER-ENROL" "2026-09-02T00:00:20+00:00" \
        "otherproj" "doc:$REPOWNAME/w.py"
    run_hook agentW; ctx="$(addl_ctx "$HOOK_OUT")"
    ck_contains "(u) FORWARD-ONLY: a row posted after the enrol IS delivered" \
        "ROW-AFTER-ENROL" "$ctx"
    ck_absent "(u) FORWARD-ONLY: the pre-enrol row stays undelivered forever" \
        "ROW-BEFORE-ENROL" "$ctx"

    rm -rf "$REPOV" "$REPOW"

    # -----------------------------------------------------------------------
    # (v) INHERITED ENROLLMENT (subagents, issue #78): a child payload under an
    #     ENROLLED parent auto-enrolls with a derived seat, a NOW cursor (no
    #     backlog replay), and one arrival row; a child of an UNENROLLED parent
    #     stays a bystander (contamination property survives the feature).
    RV2="hbtestv$$x$RANDOM"
    arm_run "$RV2"
    enroll_agent "$RV2" parentP ops parseat
    post_row "$RV2" seatX finding "ANCIENT-BROADCAST" "2026-01-01T00:00:01+00:00" ops

    #     Child of an enrolled parent: first beat enrolls + posts arrival,
    #     and the pre-birth broadcast is NOT replayed (cursor starts at now).
    run_hook_raw "$(child_payload childtask9 parentP ls)"
    if is_part "$RV2" childtask9; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1)); echo "  FAIL (v) child of enrolled parent did not enroll" >&2
    fi
    seat_got="$(COMMS_STATE_DIR="$STATE" python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
import swarm_arm
print(swarm_arm.participant_sub(sys.argv[2], sys.argv[3])[1] or "", end="")' \
        "$SELF_DIR/../lib" "$RV2" childtask9)"
    ck "(v) child seat is parent seat + -sub-<4>" "parseat-sub-chil" "$seat_got"
    ck "(v) child inherits the parent subscription" "ops" "$(part_topics "$RV2" childtask9)"
    ck_absent "(v) pre-birth broadcast is not replayed to the child" \
        "ANCIENT-BROADCAST" "$(addl_ctx "$HOOK_OUT")"
    arrivals="$(grep -c "subagent started under parseat" "$ROOT/comms-$RV2/parseat-sub-chil.jsonl" 2>/dev/null || echo 0)"
    ck "(v) exactly one arrival row posted" "1" "$arrivals"

    #     Second beat: no re-enroll, no second arrival row.
    run_hook_raw "$(child_payload childtask9 parentP ls)"
    arrivals2="$(grep -c "subagent started under parseat" "$ROOT/comms-$RV2/parseat-sub-chil.jsonl" 2>/dev/null || echo 0)"
    ck "(v) idempotent: still one arrival row after a second beat" "1" "$arrivals2"

    #     A row addressed to the child AFTER its birth delivers, FOR YOU flagged.
    post_row "$RV2" seatX comment "direct to the child" "2126-01-01T00:00:01+00:00" "@parseat-sub-chil"
    run_hook_raw "$(child_payload childtask9 parentP ls)"
    ck_contains "(v) post-birth unicast row delivers to the child" \
        "[FOR YOU from seatX]" "$(addl_ctx "$HOOK_OUT")"

    #     Child of an UNENROLLED parent: bystander -- no enrollment, no output.
    run_hook_raw "$(child_payload childtask8 strangerP ls)"
    if is_part "$RV2" childtask8; then
        fail=$((fail + 1)); echo "  FAIL (v) child of unenrolled parent was auto-enrolled" >&2
    else
        pass=$((pass + 1))
    fi
    ck "(v) bystander child emits nothing" "" "$HOOK_OUT"

    rm -rf "$STATE" "$ROOT"
}

# Run the whole suite TWICE in one invocation -> proves repeat-green.
for passno in 1 2; do
    run_suite
done

echo "swarm-heartbeat test: $pass passed, $fail failed"
[ "$fail" -eq 0 ] && exit 0 || exit 1
