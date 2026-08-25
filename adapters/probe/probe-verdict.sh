#!/bin/bash
# adapters/probe/probe-verdict.sh -- read the push probe's evidence and print
# ONE verdict: PUSH, NOT-PUSH, or COULD-NOT-DETERMINE.
#
#   bash adapters/probe/probe-verdict.sh <probe-dir> [--answer-file F | --answer TEXT]
#                                        [--expect-event S] [--expect-tool S]
#
# THE ORDER IS THE POINT. This script reads the POSITIVE CONTROL first and, if
# the control fails, exits WITHOUT EVER OPENING the agent's answer. That is not
# ceremony: the first grok probe returned a clean NOTHING-APPEARED while the
# hook had never fired at all, and reading the answer first is how that gets
# written down as a measured negative. A probe that inspected zero subjects is
# not a negative result (adapters/CONTRACT.md).
#
#   | stdin copy | agent's answer | verdict            | exit |
#   | ---------- | -------------- | ------------------ | ---- |
#   | present    | passphrase     | PUSH               |  0   |
#   | present    | nothing        | NOT-PUSH           |  1   |
#   | missing    | not read       | COULD-NOT-DETERMINE|  2   |
#
# Exit 2 is NOT a pass and NOT a fail. It means the probe produced no evidence
# either way: fix the wiring, re-run, record nothing. Callers that treat any
# non-zero exit as "not push" have re-introduced the exact bug this kit exists
# to prevent -- branch on 2 separately.
#
# Exit codes: 0 PUSH | 1 NOT-PUSH (a real, recordable negative)
#           | 2 COULD-NOT-DETERMINE | 64 usage error.

set -uo pipefail

usage() { printf 'probe-verdict: %s\n' "$1" >&2; exit 64; }

DIR=""
ANSWER_FILE=""
ANSWER_TEXT=""
ANSWER_INLINE=0
EXPECT_EVENT=""
EXPECT_TOOL=""

while [ $# -gt 0 ]; do
    case "$1" in
        --answer-file)  [ $# -ge 2 ] || usage "--answer-file needs a path";  ANSWER_FILE="$2"; shift 2 ;;
        --answer)       [ $# -ge 2 ] || usage "--answer needs a value";      ANSWER_TEXT="$2"; ANSWER_INLINE=1; shift 2 ;;
        --expect-event) [ $# -ge 2 ] || usage "--expect-event needs a value"; EXPECT_EVENT="$2"; shift 2 ;;
        --expect-tool)  [ $# -ge 2 ] || usage "--expect-tool needs a value";  EXPECT_TOOL="$2"; shift 2 ;;
        -h|--help)      sed -n '2,30p' "$0"; exit 0 ;;
        -*)             usage "unknown argument: $1" ;;
        *)              [ -z "$DIR" ] || usage "one probe dir only (got: $DIR and $1)"; DIR="$1"; shift ;;
    esac
done

[ -n "$DIR" ] || usage "usage: probe-verdict.sh <probe-dir> [--answer-file F | --answer TEXT]"

STDIN_COPY="$DIR/stdin-copy.json"
HOOK_STDOUT="$DIR/hook-stdout.json"
PASSPHRASE_FILE="$DIR/passphrase"
[ -n "$ANSWER_FILE" ] || ANSWER_FILE="$DIR/agent-answer.txt"

evidence() {
    printf '\nevidence\n'
    printf '  probe dir      %s\n' "$DIR"
    printf '  stdin copy     %s%s\n' "$STDIN_COPY" "$([ -s "$STDIN_COPY" ] && echo '' || echo '   (MISSING)')"
    printf '  hook stdout    %s%s\n' "$HOOK_STDOUT" "$([ -s "$HOOK_STDOUT" ] && echo '' || echo '   (MISSING)')"
    printf '  fires          %s\n' "$DIR/fires.jsonl${FIRES:+  ($FIRES)}"
    if [ "$ANSWER_INLINE" -eq 1 ]; then
        printf '  agent answer   %s\n' "$ANSWER_STATE"
    else
        printf '  agent answer   %s%s\n' "$ANSWER_FILE" "$ANSWER_STATE"
    fi
}

FIRES=""
ANSWER_STATE="   (NOT READ -- positive control failed)"

undetermined() {  # undetermined <why>
    printf 'COULD-NOT-DETERMINE\n'
    printf '  reason: %s\n' "$1"
    printf '  The agent answer was NOT read. This is not a negative result:\n'
    printf '  fix the wiring, re-run the probe, and record nothing.\n'
    evidence
    exit 2
}

# ---- positive control, read FIRST -----------------------------------------
[ -d "$DIR" ] || undetermined "no probe dir at $DIR -- nothing was ever armed"

[ -s "$PASSPHRASE_FILE" ] \
    || undetermined "no passphrase at $PASSPHRASE_FILE -- run arm-probe.sh first"
PASSPHRASE="$(tr -d '\r\n' < "$PASSPHRASE_FILE")"

[ -s "$STDIN_COPY" ] \
    || undetermined "the hook never fired: no stdin copy at $STDIN_COPY"

[ -s "$DIR/fires.jsonl" ] && FIRES="$(wc -l < "$DIR/fires.jsonl" | tr -d ' ') fire(s)"

stdin_bytes="$(cat "$STDIN_COPY")"
if [ -n "$EXPECT_EVENT" ]; then
    case "$stdin_bytes" in
        *"$EXPECT_EVENT"*) ;;
        *) undetermined "the hook fired but its payload never names the expected event ($EXPECT_EVENT) -- wrong event wired" ;;
    esac
fi
if [ -n "$EXPECT_TOOL" ]; then
    case "$stdin_bytes" in
        *"$EXPECT_TOOL"*) ;;
        *) undetermined "the hook fired but its payload never names the expected tool ($EXPECT_TOOL) -- the agent may not have run the command you asked for" ;;
    esac
fi

[ -s "$HOOK_STDOUT" ] \
    || undetermined "the hook fired but emitted no envelope (no $HOOK_STDOUT) -- nothing was there to inject"
case "$(cat "$HOOK_STDOUT")" in
    *"$PASSPHRASE"*) ;;
    *) undetermined "the emitted envelope does not carry this run's passphrase -- stale evidence from an earlier arm; re-arm and re-run" ;;
esac

# ---- only now: the agent's answer -----------------------------------------
if [ "$ANSWER_INLINE" -eq 1 ]; then
    answer="$ANSWER_TEXT"
    ANSWER_STATE="(inline, ${#answer} bytes)"
    [ -n "$answer" ] || undetermined "the agent answer given with --answer is empty -- nothing was captured to interpret"
else
    [ -f "$ANSWER_FILE" ] \
        || undetermined "positive control PASSED but no agent answer was captured at $ANSWER_FILE -- run the runtime and tee its answer there"
    answer="$(cat "$ANSWER_FILE")"
    ANSWER_STATE=""
    [ -n "$answer" ] || undetermined "positive control PASSED but $ANSWER_FILE is empty -- nothing was captured to interpret"
fi

printf 'positive control PASSED: the hook fired, was handed the event, and emitted the envelope.\n'
case "$answer" in
    *"$PASSPHRASE"*)
        printf 'PUSH\n'
        printf '  The passphrase %s came back in the agent answer: the runtime\n' "$PASSPHRASE"
        printf '  injects hookSpecificOutput.additionalContext. Declare push, wire\n'
        printf '  adapters/claude-code/swarm-heartbeat.sh, and record the runtime\n'
        printf '  version and date beside the verdict.\n'
        evidence
        exit 0
        ;;
    *)
        printf 'NOT-PUSH\n'
        printf '  The hook ran and emitted %s, and the agent never saw it.\n' "$PASSPHRASE"
        printf '  This is a REAL negative -- record it with the runtime version and\n'
        printf '  date. The runtime has a hook surface but discards hook stdout, so\n'
        printf '  its delivery path is whatever it passed earlier (poll, usually).\n'
        evidence
        exit 1
        ;;
esac
