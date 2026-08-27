#!/bin/bash
# test_push_probe.sh -- the push probe kit (adapters/probe/) under synthetic
# hook payloads.
#
# The kit decides whether a runtime gets a push adapter, so its own three
# outcomes are what must not drift:
#
#   injecting runtime   -> PUSH                (exit 0)
#   discarding runtime  -> NOT-PUSH            (exit 1) -- a real negative
#   hook never fired    -> COULD-NOT-DETERMINE (exit 2) -- neither pass nor fail
#
# Both known-answer cases from adapters/CONTRACT.md are replayed as fixtures:
# grok 0.2.106 (hooks fire, stdout discarded -> NOT-PUSH) and codex 0.148.0 /
# Claude Code (injection observed -> PUSH). The third case is the trap that was
# already sprung once: the first grok attempt answered NOTHING-APPEARED while
# the hook had never loaded, and reading that as a negative would have shipped a
# verdict backed by nothing.
#
# ISOLATES ALL WRITES. Every probe dir and every hook config is a fresh mktemp
# under a per-pass sandbox; the kit is never pointed at a real config, a real
# state dir, or a real mailbox. The suite RUNS TWICE in one invocation to prove
# repeat-green.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"                 # <repo>/tests
KIT="$SELF_DIR/../adapters/probe"
ARM="$KIT/arm-probe.sh"
HOOK="$KIT/push-probe-hook.sh"
VERDICT="$KIT/probe-verdict.sh"
# Canonical (no "..") form of HOOK, matching what arm-probe.sh's own SELF_DIR
# resolves to internally -- needed to compare against text the script prints.
HOOK_ABS="$(cd "$(dirname "$HOOK")" && pwd)/$(basename "$HOOK")"
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

SANDBOX=""

# ---- fixtures --------------------------------------------------------------
# A codex/Claude-shaped PostToolUse payload.
claude_payload() {
    printf '%s' '{"session_id":"s-abc","hook_event_name":"PostToolUse","tool_name":"Bash","tool_input":{"command":"echo hello"}}'
}
# The grok 0.2.106 payload, verbatim in the keys that matter (camelCase, grok's
# own tool name) -- see adapters/grok/README.md.
grok_payload() {
    printf '%s' '{"sessionId":"g-123","hookEventName":"post_tool_use","toolName":"run_terminal_command","toolInput":{"command":"echo hello"}}'
}

arm_probe() {  # arm_probe <dir> <config> [extra args...] -> ARM_OUT / ARM_RC
    local d="$1" c="$2"; shift 2
    ARM_OUT="$(bash "$ARM" --dir "$d" --config "$c" "$@" 2>&1)"
    ARM_RC=$?
}
fire_hook() {  # fire_hook <dir> <payload> -> HOOK_OUT / HOOK_RC
    local d="$1" p="$2"
    HOOK_OUT="$(printf '%s' "$p" | bash "$HOOK" "$d" 2>/dev/null)"
    HOOK_RC=$?
}
run_verdict() {  # run_verdict <args...> -> V_OUT / V_RC
    V_OUT="$(bash "$VERDICT" "$@" 2>&1)"
    V_RC=$?
}
passphrase_of() { tr -d '\r\n' < "$1/passphrase"; }

run_suite() {
    SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/comms-probe-test.XXXXXX")"
    local D C

    # ---- (a) hook mechanics: stdin copy, envelope, fire count -------------
    D="$SANDBOX/a"; C="$SANDBOX/a-hooks.json"
    arm_probe "$D" "$C"
    ck "(a) arm exits 0" "0" "$ARM_RC"
    ck "(a) arm creates the probe dir" "yes" "$([ -d "$D" ] && echo yes)"
    ck "(a) arm writes a passphrase" "yes" "$([ -s "$D/passphrase" ] && echo yes)"
    ck "(a) arm writes an isolated state dir" "yes" "$([ -d "$D/state" ] && echo yes)"
    ck "(a) arm writes NO stdin copy (nothing fired yet)" "" "$([ -e "$D/stdin-copy.json" ] && echo present)"
    PP="$(passphrase_of "$D")"
    ck_contains "(a) passphrase is probe-tagged" "COMMS-PROBE-" "$PP"

    fire_hook "$D" "$(claude_payload)"
    ck "(a) hook exits 0" "0" "$HOOK_RC"
    ck_contains "(a) hook stdout is an injection envelope" '"hookSpecificOutput"' "$HOOK_OUT"
    ck_contains "(a) hook stdout names additionalContext" '"additionalContext"' "$HOOK_OUT"
    ck_contains "(a) hook stdout carries the passphrase" "$PP" "$HOOK_OUT"
    ck_contains "(a) hook stdout is valid JSON" "ok" \
        "$(printf '%s' "$HOOK_OUT" | python3 -c 'import json,sys; json.load(sys.stdin); print("ok")' 2>&1)"
    ck "(a) stdin copy is byte-identical to the payload" "$(claude_payload)" "$(cat "$D/stdin-copy.json")"
    ck "(a) hook stdout is saved as evidence" "$HOOK_OUT" "$(cat "$D/hook-stdout.json")"
    ck "(a) fire count is 1" "1" "$(cat "$D/fire-count")"
    fire_hook "$D" "$(claude_payload)"
    ck "(a) fire count is 2 after a second beat" "2" "$(cat "$D/fire-count")"
    ck "(a) fires.jsonl keeps one line per fire" "2" "$(wc -l < "$D/fires.jsonl" | tr -d ' ')"

    #     A hook with no probe dir writes nothing and still exits 0 -- a probe
    #     that breaks the host's tool call changes what it is measuring.
    HOOK_OUT="$(printf '%s' "$(claude_payload)" | env -u COMMS_PROBE_DIR bash "$HOOK" 2>/dev/null)"
    ck "(a) hook with no dir exits 0" "0" "$?"
    ck "(a) hook with no dir emits nothing" "" "$HOOK_OUT"

    # ---- (b) known-answer PUSH: codex 0.148.0 / Claude Code ---------------
    D="$SANDBOX/b"; C="$SANDBOX/b-hooks.json"
    arm_probe "$D" "$C"
    PP="$(passphrase_of "$D")"
    fire_hook "$D" "$(claude_payload)"
    printf 'I saw an extra context block. It said: MAILBOX ROW ... passphrase %s .\n' "$PP" > "$D/agent-answer.txt"
    run_verdict "$D"
    ck "(b) injecting runtime -> exit 0" "0" "$V_RC"
    ck_contains "(b) verdict says PUSH" "PUSH" "$V_OUT"
    ck_contains "(b) verdict states the control PASSED" "positive control PASSED" "$V_OUT"
    ck_absent "(b) verdict is not could-not-determine" "COULD-NOT-DETERMINE" "$V_OUT"
    ck_contains "(b) verdict prints the stdin copy path" "$D/stdin-copy.json" "$V_OUT"
    ck_contains "(b) verdict prints the hook stdout path" "$D/hook-stdout.json" "$V_OUT"
    ck_contains "(b) verdict prints the answer path" "$D/agent-answer.txt" "$V_OUT"
    ck_contains "(b) verdict counts the fires" "1 fire(s)" "$V_OUT"
    #     Same evidence, answer supplied inline instead of from a file.
    run_verdict "$D" --answer "the passphrase was $PP"
    ck "(b) inline answer -> exit 0" "0" "$V_RC"

    #     INSTRUMENT CONTROL for the ordering test in (d). An unreadable answer
    #     file has to produce a VISIBLE error when the script actually opens it,
    #     or "no error appeared" in (d) would prove nothing -- silence is not
    #     evidence. Path b is the case that definitely reads the file.
    chmod 000 "$D/agent-answer.txt"
    run_verdict "$D"
    ck_contains "(b) chmod 000 IS detectable when the answer is read" \
        "Permission denied" "$V_OUT"
    chmod 644 "$D/agent-answer.txt"

    # ---- (c) known-answer NOT-PUSH: grok 0.2.106 --------------------------
    D="$SANDBOX/c"; C="$SANDBOX/c-hooks.json"
    arm_probe "$D" "$C"
    fire_hook "$D" "$(grok_payload)"
    printf 'NOTHING-APPEARED\n' > "$D/agent-answer.txt"
    run_verdict "$D"
    ck "(c) discarding runtime -> exit 1" "1" "$V_RC"
    ck_contains "(c) verdict says NOT-PUSH" "NOT-PUSH" "$V_OUT"
    ck_contains "(c) verdict states the control PASSED" "positive control PASSED" "$V_OUT"
    ck_contains "(c) verdict calls it a REAL negative" "REAL negative" "$V_OUT"
    ck_absent "(c) verdict is not could-not-determine" "COULD-NOT-DETERMINE" "$V_OUT"
    #     The control holds up when the expected event and tool are asserted,
    #     not merely assumed -- grok spells them post_tool_use / run_terminal_command.
    run_verdict "$D" --expect-event post_tool_use --expect-tool run_terminal_command
    ck "(c) asserted event+tool still NOT-PUSH" "1" "$V_RC"
    ck_contains "(c) asserted event+tool says NOT-PUSH" "NOT-PUSH" "$V_OUT"

    # ---- (d) the trap: hook never fired -----------------------------------
    #     Armed, never fired, and an answer of NOTHING-APPEARED sitting there.
    #     Read answer-first this is a clean negative; it is not a result at all.
    D="$SANDBOX/d"; C="$SANDBOX/d-hooks.json"
    arm_probe "$D" "$C"
    printf 'NOTHING-APPEARED\n' > "$D/agent-answer.txt"
    run_verdict "$D"
    ck "(d) hook never fired -> exit 2" "2" "$V_RC"
    ck_contains "(d) verdict is COULD-NOT-DETERMINE" "COULD-NOT-DETERMINE" "$V_OUT"
    ck_contains "(d) verdict names the missing control" "the hook never fired" "$V_OUT"
    ck_contains "(d) verdict says the answer was NOT read" "answer was NOT read" "$V_OUT"
    ck_absent "(d) verdict never says NOT-PUSH" "NOT-PUSH" "$V_OUT"
    ck_absent "(d) verdict never quotes the unread answer" "NOTHING-APPEARED" "$V_OUT"
    ck_contains "(d) verdict flags the missing stdin copy" "(MISSING)" "$V_OUT"
    ck_contains "(d) evidence carries the NOT READ marker" \
        "(NOT READ -- positive control failed)" "$V_OUT"

    #     THE ORDERING ITSELF, tested rather than claimed. Make the answer file
    #     UNREADABLE and re-run: the control still fails first, so the script
    #     must never open the file. Any edit that hoists the answer read above
    #     the control block turns that open into a visible "Permission denied"
    #     -- while the output still asserts the answer was NOT read, which is
    #     the lie this pair of assertions exists to catch. (b) proves the
    #     instrument can see an open at all.
    chmod 000 "$D/agent-answer.txt"
    run_verdict "$D"
    ck "(d) unreadable answer -> still exit 2" "2" "$V_RC"
    ck_absent "(d) the answer file was never opened" "Permission denied" "$V_OUT"
    ck_contains "(d) unreadable answer keeps the NOT READ marker" \
        "(NOT READ -- positive control failed)" "$V_OUT"
    chmod 644 "$D/agent-answer.txt"

    # ---- (e) other ways the control fails, all exit 2 ----------------------
    #     Fired, but no envelope was emitted (probe dir had no passphrase).
    D="$SANDBOX/e1"; C="$SANDBOX/e1-hooks.json"
    arm_probe "$D" "$C"
    rm -f "$D/passphrase"
    fire_hook "$D" "$(claude_payload)"
    ck "(e1) unarmed hook emits no envelope" "" "$HOOK_OUT"
    ck "(e1) unarmed hook still writes the stdin copy" "yes" "$([ -s "$D/stdin-copy.json" ] && echo yes)"
    run_verdict "$D" --answer "NOTHING-APPEARED"
    ck "(e1) no passphrase -> exit 2" "2" "$V_RC"
    ck_contains "(e1) verdict is COULD-NOT-DETERMINE" "COULD-NOT-DETERMINE" "$V_OUT"
    ck_contains "(e1) evidence carries the NOT READ marker" \
        "(NOT READ -- positive control failed)" "$V_OUT"
    ck_absent "(e1) verdict never quotes the unread answer" "NOTHING-APPEARED" "$V_OUT"

    #     Fired on the wrong event: the payload never names what we expected.
    D="$SANDBOX/e2"; C="$SANDBOX/e2-hooks.json"
    arm_probe "$D" "$C"
    fire_hook "$D" "$(grok_payload)"
    run_verdict "$D" --answer "NOTHING-APPEARED" --expect-event session_start
    ck "(e2) wrong event -> exit 2" "2" "$V_RC"
    ck_contains "(e2) verdict names the wrong event" "wrong event wired" "$V_OUT"
    ck_contains "(e2) wrong event keeps the NOT READ marker" \
        "(NOT READ -- positive control failed)" "$V_OUT"
    run_verdict "$D" --answer "NOTHING-APPEARED" --expect-tool Bash
    ck "(e2) wrong tool -> exit 2" "2" "$V_RC"
    ck_contains "(e2) verdict names the expected tool" "expected tool" "$V_OUT"
    ck_contains "(e2) wrong tool keeps the NOT READ marker" \
        "(NOT READ -- positive control failed)" "$V_OUT"

    #     Control fine, but nobody captured an answer -- zero subjects again.
    D="$SANDBOX/e3"; C="$SANDBOX/e3-hooks.json"
    arm_probe "$D" "$C"
    fire_hook "$D" "$(claude_payload)"
    run_verdict "$D"
    ck "(e3) no answer captured -> exit 2" "2" "$V_RC"
    ck_contains "(e3) verdict says the control passed but nothing was captured" \
        "no agent answer was captured" "$V_OUT"
    #     The ONE exit-2 shape where the control HELD. Claiming "the answer was
    #     NOT read" here would be the same class of error the kit exists to stop
    #     -- a sentence about evidence that does not match the evidence.
    ck_absent "(e3) control-passed exit 2 does NOT claim the control failed" \
        "(NOT READ -- positive control failed)" "$V_OUT"
    ck_contains "(e3) evidence says the answer was NOT CAPTURED" "(NOT CAPTURED)" "$V_OUT"
    ck_contains "(e3) reason says the control held" "The control held" "$V_OUT"
    printf '' > "$D/agent-answer.txt"
    run_verdict "$D"
    ck "(e3) empty answer file -> exit 2" "2" "$V_RC"
    ck_contains "(e3) empty answer is marked EMPTY, not NOT READ" "(EMPTY)" "$V_OUT"
    ck_absent "(e3) empty answer does NOT claim the control failed" \
        "(NOT READ -- positive control failed)" "$V_OUT"

    #     Stale evidence from an earlier arm: the emitted envelope carries a
    #     passphrase that is not this run's.
    D="$SANDBOX/e4"; C="$SANDBOX/e4-hooks.json"
    arm_probe "$D" "$C" --passphrase "COMMS-PROBE-OLD-0001"
    fire_hook "$D" "$(claude_payload)"
    printf '%s\n' "COMMS-PROBE-NEW-0002" > "$D/passphrase"
    run_verdict "$D" --answer "COMMS-PROBE-NEW-0002"
    ck "(e4) stale envelope -> exit 2" "2" "$V_RC"
    ck_contains "(e4) verdict names the stale evidence" "stale evidence" "$V_OUT"
    ck_contains "(e4) evidence carries the NOT READ marker" \
        "(NOT READ -- positive control failed)" "$V_OUT"

    #     No probe dir at all.
    run_verdict "$SANDBOX/never-armed" --answer "NOTHING-APPEARED"
    ck "(e5) missing probe dir -> exit 2" "2" "$V_RC"
    ck_contains "(e5) verdict says nothing was armed" "nothing was ever armed" "$V_OUT"
    ck_contains "(e5) evidence carries the NOT READ marker" \
        "(NOT READ -- positive control failed)" "$V_OUT"

    #     Usage errors are their own code, so 2 keeps meaning could-not-inspect.
    run_verdict
    ck "(e6) no arguments -> exit 64" "64" "$V_RC"
    run_verdict "$SANDBOX/a" --bogus
    ck "(e6) unknown flag -> exit 64" "64" "$V_RC"
    ARM_OUT="$(bash "$ARM" --format sideways 2>&1)"; ARM_RC=$?
    ck "(e6) bad --format -> exit 64" "64" "$ARM_RC"

    # ---- (f) arm wiring: idempotent, non-clobbering, isolated --------------
    D="$SANDBOX/f"; C="$SANDBOX/f-hooks.json"
    cat > "$C" <<'JSON'
{
  "PostToolUse": [
    {"matcher": "*", "hooks": [{"type": "command", "command": "bash /somewhere/unrelated.sh"}]}
  ],
  "SessionStart": [
    {"matcher": "*", "hooks": [{"type": "command", "command": "bash /somewhere/other.sh"}]}
  ]
}
JSON
    arm_probe "$D" "$C"
    ck "(f) arm into an existing config exits 0" "0" "$ARM_RC"
    ck "(f) unrelated PostToolUse hook survives" "1" \
        "$(grep -c 'unrelated.sh' "$C")"
    ck "(f) unrelated event survives" "1" "$(grep -c 'other.sh' "$C")"
    ck "(f) probe entry added once" "1" "$(grep -c 'push-probe-hook.sh' "$C")"
    arm_probe "$D" "$C"
    ck "(f) re-arm does not duplicate the entry" "1" "$(grep -c 'push-probe-hook.sh' "$C")"
    #     Re-arming into a NEW dir repoints the entry: an entry still writing to
    #     the old dir is how stale evidence gets read as fresh.
    arm_probe "$SANDBOX/f2" "$C"
    ck "(f) re-arm to a new dir still has one entry" "1" "$(grep -c 'push-probe-hook.sh' "$C")"
    #     Compare on the RESOLVED path, anchored at the closing quote: arm-probe
    #     records the physical dir (on macOS $TMPDIR reaches it via a symlink),
    #     and .../f is a prefix of .../f2.
    NEWDIR="$(cd "$SANDBOX/f2" && pwd)"; OLDDIR="$(cd "$D" && pwd)"
    ck "(f) re-arm repoints at the new dir" "1" "$(grep -c "$NEWDIR\"" "$C")"
    ck "(f) old dir no longer referenced" "0" "$(grep -c "$OLDDIR\"" "$C")"

    #     settings.json-shaped config: events live under a "hooks" key.
    C="$SANDBOX/f-settings.json"
    printf '%s\n' '{"permissions":{"allow":[]},"hooks":{"PostToolUse":[]}}' > "$C"
    arm_probe "$SANDBOX/f3" "$C"
    ck "(f) wrapped config exits 0" "0" "$ARM_RC"
    ck "(f) wrapped config keeps unrelated keys" "1" "$(grep -c 'permissions' "$C")"
    ck "(f) wrapped config nests under hooks" "ok" \
        "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("ok" if "push-probe-hook.sh" in d["hooks"]["PostToolUse"][0]["hooks"][0]["command"] else d)' "$C")"
    #     --format flat overrides the guess.
    C="$SANDBOX/f-flat.json"
    arm_probe "$SANDBOX/f4" "$C" --format flat --event Stop
    ck "(f) flat format writes the event at the top level" "ok" \
        "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("ok" if "Stop" in d and "hooks" not in d else d)' "$C")"

    #     An unparseable config is refused, not rewritten.
    C="$SANDBOX/f-broken.json"
    printf '%s' '{not json at all' > "$C"
    arm_probe "$SANDBOX/f5" "$C"
    ck "(f) unparseable config -> exit 1" "1" "$ARM_RC"
    ck "(f) unparseable config left byte-identical" '{not json at all' "$(cat "$C")"
    ck_contains "(f) arm says it refused" "refusing to edit" "$ARM_OUT"

    #     With no --config the probe is fully isolated: it writes a hooks.json
    #     inside its own dir and reaches no runtime.
    D="$SANDBOX/f6"
    ARM_OUT="$(bash "$ARM" --dir "$D" 2>&1)"; ARM_RC=$?
    ck "(f) arm with no --config exits 0" "0" "$ARM_RC"
    ck "(f) arm with no --config writes inside the probe dir" "yes" \
        "$([ -s "$D/hooks.json" ] && echo yes)"

    #     Distinct arms mint distinct passphrases, or one run's evidence would
    #     validate another's.
    bash "$ARM" --dir "$SANDBOX/g1" >/dev/null 2>&1
    bash "$ARM" --dir "$SANDBOX/g2" >/dev/null 2>&1
    ck "(f) two arms mint different passphrases" "differ" \
        "$([ "$(passphrase_of "$SANDBOX/g1")" != "$(passphrase_of "$SANDBOX/g2")" ] && echo differ)"

    # ---- (g) contract invariants ------------------------------------------
    #     The kit is an adapter-side tool: the core never learns it exists, and
    #     the kit never learns a runtime's name.
    ck "(g) bin/ and lib/ never mention the probe kit" "0" \
        "$(grep -rl 'push-probe-hook\|probe-verdict\|arm-probe' "$SELF_DIR/../bin" "$SELF_DIR/../lib" 2>/dev/null | wc -l | tr -d ' ')"
    #     Runtime names may appear in a comment or in printed advice (the PUSH
    #     verdict has to name the one heartbeat to reuse). They may never appear
    #     in control flow: the moment a kit script asks WHICH runtime it is, the
    #     kit stops being runtime-agnostic.
    ck "(g) no runtime name in kit control flow" "0" \
        "$(grep -in 'claude\|codex\|grok\|kimi\|gemini\|qwen\|copilot\|cursor' \
            "$ARM" "$HOOK" "$VERDICT" \
            | grep -v '^[^:]*:[0-9]*: *#' \
            | grep -vi 'printf\|echo\|sed -n' | wc -l | tr -d ' ')"
    #     The scripts write only into the probe dir handed to them.
    ck "(g) kit scripts never reference HOME" "0" \
        "$(grep -c 'HOME' "$ARM" "$HOOK" "$VERDICT" | awk -F: '{s+=$2} END {print s}')"

    # ---- (h) --format none: hand-wiring instead of a config ----------------
    #     A runtime whose hook config is not the {"EVENT": [...]} /
    #     {"hooks": {"EVENT": [...]}} JSON shape gets NO config write and a
    #     plain-text hand-wiring file instead, so a person can paste it into
    #     Cline's, Gemini's, Crush's, or Hermes's own config by hand.
    D="$SANDBOX/h"
    ARM_OUT="$(bash "$ARM" --dir "$D" --format none 2>&1)"; ARM_RC=$?
    ck "(h) format none exits 0" "0" "$ARM_RC"
    ck "(h) format none writes a passphrase" "yes" "$([ -s "$D/passphrase" ] && echo yes)"
    ck "(h) format none writes armed-at" "yes" "$([ -s "$D/armed-at" ] && echo yes)"
    ck "(h) format none writes event" "yes" "$([ -s "$D/event" ] && echo yes)"
    ck "(h) format none writes hand-wiring.txt" "yes" "$([ -s "$D/hand-wiring.txt" ] && echo yes)"
    ck "(h) format none writes NO hooks.json" "" "$([ -e "$D/hooks.json" ] && echo present)"
    ck "(h) format none writes NO settings.json" "" "$([ -e "$D/settings.json" ] && echo present)"
    ck "(h) format none creates no other json file in the probe dir" "0" \
        "$(find "$D" -maxdepth 1 -name '*.json' | wc -l | tr -d ' ')"
    D_ABS="$(cd "$D" && pwd)"
    HW="$D/hand-wiring.txt"
    PP="$(passphrase_of "$D")"
    ck_contains "(h) hand-wiring names the exact hook command" \
        "bash $HOOK_ABS $D_ABS" "$(cat "$HW")"
    ck_contains "(h) hand-wiring names the event" "PostToolUse" "$(cat "$HW")"
    ck_contains "(h) hand-wiring carries the envelope's event key" \
        '"hookEventName":"PostToolUse"' "$(cat "$HW")"
    ck_contains "(h) hand-wiring carries the envelope's context key" \
        '"additionalContext"' "$(cat "$HW")"
    ck_contains "(h) hand-wiring carries this run's passphrase" "$PP" "$(cat "$HW")"
    ck_contains "(h) arm-probe still prints the hand-wiring path" \
        "$D_ABS/hand-wiring.txt" "$ARM_OUT"
    ck_contains "(h) arm-probe still prints the verdict command" \
        "probe-verdict.sh $D_ABS" "$ARM_OUT"

    #     A custom --event flows through to the hand-wiring text too.
    D2="$SANDBOX/h2"
    ARM_OUT="$(bash "$ARM" --dir "$D2" --format none --event Stop 2>&1)"; ARM_RC=$?
    ck "(h) format none with --event exits 0" "0" "$ARM_RC"
    ck_contains "(h) hand-wiring reflects the custom event" \
        '"hookEventName":"Stop"' "$(cat "$D2/hand-wiring.txt")"

    #     A --config passed alongside --format none is still not written to --
    #     none means no config write, full stop.
    D3="$SANDBOX/h3"; C3="$SANDBOX/h3-hooks.json"
    ARM_OUT="$(bash "$ARM" --dir "$D3" --config "$C3" --format none 2>&1)"; ARM_RC=$?
    ck "(h) format none with --config still exits 0" "0" "$ARM_RC"
    ck "(h) format none with --config still writes nothing to it" "" \
        "$([ -e "$C3" ] && echo present)"

    rm -rf "$SANDBOX"
}

# Run the whole suite TWICE in one invocation -> proves repeat-green.
for passno in 1 2; do
    run_suite
done

echo "push-probe test: $pass passed, $fail failed"
[ "$fail" -eq 0 ] && exit 0 || exit 1
