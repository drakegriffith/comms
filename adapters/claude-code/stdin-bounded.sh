#!/bin/bash
# stdin-bounded.sh -- VENDORED COPY. Origin: the source harness's
# hooks/lib/stdin-bounded.sh (claude-harness working tree). Vendored beside
# swarm-heartbeat.sh so the heartbeat adapter is self-contained: resolution is
# sibling-first with an inline fallback, never a path outside this repo.
#
# Reads a hook payload from stdin under a time bound.
#
# SOURCE THIS, DO NOT EXECUTE IT:
#     . "<this repo>/adapters/claude-code/stdin-bounded.sh"
#     read_stdin_bounded
#     payload="$HOOK_STDIN"      # what arrived
#     # $HOOK_STDIN_STATUS is ok | terminal | line-timeout | total-timeout
#
# WHY THIS EXISTS
#   A behavioural probe (2026-08-17) held a pipe open, wrote nothing, and
#   measured which wired hooks never came back: 13 of 30 wired commands never
#   returned. Every one of them reads stdin with an unbounded construct --
#   `$(cat)`, `read -r -d ""`, `json.load(sys.stdin)`. A hook that hangs is
#   indistinguishable from a slow machine, which is why they all survived.
#
#   The host usually bounds the damage with its own per-hook timeout, but a
#   killed hook contributes no output: this beat's injection is DROPPED. A
#   bounded read keeps the hook's contribution alive.
#
# THE FOUR TRAPS, EVERY ONE MEASURED RATHER THAN REASONED ABOUT
#
#   1. NO PRE-READ GUARD CAN WORK. `[ -t 0 ]` is false for a pipe, and
#      `[ -e /dev/fd/0 ]` is TRUE for a live pipe (verified: rc 0 against a fifo
#      with a silent writer). Nothing inspectable before the read separates "a
#      writer exists and will send" from "a writer exists and never will". Only
#      waiting distinguishes them.
#
#   2. bash 3.2 RETURNS 1 FROM `read -t` ON TIMEOUT, NOT >128. GNU documents
#      >128; macOS ships 3.2.57 and returns 1, which is also the code for a clean
#      EOF. A status check written from the manual can never fire, so every cut
#      read logs as a complete one. Elapsed time is the observable that actually
#      separates them, so we test both.
#
#   3. A BARE `while read` LOOP CAPTURES ZERO BYTES FROM A REAL PAYLOAD.
#      Hook JSON arrives with NO trailing newline, so the final (usually only)
#      line leaves `read` with rc 1 and the data still in $line -- and a loop
#      that only appends inside the success branch discards it. Measured: 2MB in,
#      0 bytes captured. The `else` branch that appends the partial read is not a
#      refinement, it is the difference between working and silently empty.
#
#   4. ACCUMULATING INTO A SHELL VARIABLE IS O(n^2). Measured on a 5,000-line
#      1MB payload: 50s appending to a string, 0.45s appending to a temp file.
#      The string version would blow its own budget and report total-timeout on a
#      payload that arrived intact and instantly.
#
# LIMITATIONS
#   Bash only. Line-oriented: a NUL byte in the payload is dropped. Hook
#   payloads are JSON, which encodes NUL as backslash-u-0000, so no NUL reaches
#   us -- but a caller feeding this arbitrary binary would lose bytes.
#
# SELF-TEST
#   bash <this file> --selftest      0 pass / 1 fail

# Per-line budget and total budget, both overridable by the caller BEFORE the
# call. Defaults sit well under typical host hook timeouts (5s) so a bounded
# read still leaves the hook time to do its work and emit a verdict.
#
# WHOLE SECONDS IN [1,60]. THE DOMAIN IS WRITTEN DOWN HERE BECAUSE SAYING
# NOTHING ABOUT IT IS WHAT ARMED THE BUG (2026-08-19). This block used to say
# only "overridable", which reads as "any number". Every macOS bash shebang
# resolves to 3.2.57, which rejects a fractional `read -t` outright. Measured
# against a 66-byte payload on a normal pipe with STDIN_LINE_SECONDS=0.5:
#     read: 0.5: invalid timeout specification
#     [: 0.5: integer expression expected
#     status=ok bytes=0        (default and =2 both give status=ok bytes=66)
# TWO breakages from one unvalidated value, and they compound: `read` failed
# instantly, then the elapsed-time test at trap 2 -- the ONLY thing on this
# shell that separates a cut read from a clean one -- could not evaluate either.
# So the read reported `ok` over zero bytes, byte-identical to a genuinely empty
# payload. A caller following the documentation got silent total data loss
# labelled `ok`.
#
# SO THE BUDGET IS VALIDATED AND A REJECTED ONE IS RECORDED RATHER THAN ABSORBED.
: "${STDIN_LINE_SECONDS:=2}"
: "${STDIN_TOTAL_SECONDS:=3}"

# THE BUDGET PREDICATE IS NOT DEFINED HERE. It is sourced from the vendored
# sibling stdin-budget.sh so this file and any other caller in this repo cannot
# drift apart. Resolution is SIBLING ONLY: this adapter must be self-contained,
# so there is no fallback to any tree outside the repo. If the sibling is
# unreadable we do not fail open (that would restore the exact defect: any
# budget accepted, payload lost, status `ok`) and we do not abort (a dead
# library would take the sourcing hook down). We fall back to an inline copy of
# the same rule AND raise a flag, so the status carries `no-budget-validator`
# and a reader can tell a degraded run from a clean one.
# Pure parameter expansion, NOT `dirname`: dirname is an external binary, and
# this resolution must survive an empty PATH (the origin file only passed its
# own empty-PATH assertion via a $HOME fallback that needed no fork; this
# vendored copy has no such fallback, so the fork has to go instead).
_sb_src="${BASH_SOURCE[0]:-$0}"
_sb_dir="${_sb_src%/*}"
[ "$_sb_dir" = "$_sb_src" ] && _sb_dir="."
if [ -z "${STDIN_BUDGET_CONTRACT:-}" ]; then
    STDIN_BUDGET_CONTRACT="$_sb_dir/stdin-budget.sh"
fi
STDIN_BUDGET_SOURCED=no
if [ -r "$STDIN_BUDGET_CONTRACT" ] && . "$STDIN_BUDGET_CONTRACT" 2>/dev/null && \
   command -v stdin_budget_usable >/dev/null 2>&1; then
    STDIN_BUDGET_SOURCED=yes
else
    stdin_budget_usable() {  # inline fallback; same rule, flagged as degraded
        case "${1:-}" in
            ''|*[!0-9]*) return 1 ;;
        esac
        [ "$1" -ge 1 ] && [ "$1" -le 60 ]
    }
fi

read_stdin_bounded() {
    HOOK_STDIN=""
    HOOK_STDIN_STATUS=ok

    local tf line rc started deadline line_budget total_budget budget_note

    # VALIDATED AT CALL TIME, NOT AT SOURCE TIME. The `:=` defaults above run once
    # when the file is sourced; a caller that exports a budget AFTER sourcing and
    # before calling would slip past a check placed up there.
    #
    # INTO LOCALS, NOT OVER THE CALLER'S VARIABLES. A sourced function that
    # rewrites the caller's environment is a side effect nobody reads about; the
    # record of what it asked for lives in the status, not in a silent overwrite
    # of its variable.
    budget_note=""
    line_budget="$STDIN_LINE_SECONDS"
    total_budget="$STDIN_TOTAL_SECONDS"
    [ "$STDIN_BUDGET_SOURCED" = yes ] || budget_note="no-budget-validator"
    stdin_budget_usable "$line_budget"  || { budget_note="${budget_note:+$budget_note+}bad-line-budget"; line_budget=2; }
    stdin_budget_usable "$total_budget" || { budget_note="${budget_note:+$budget_note+}bad-total-budget"; total_budget=3; }

    # An interactive terminal is not a payload. Return immediately rather than
    # sit for the full budget every time someone runs the hook by hand.
    if [ -t 0 ]; then
        HOOK_STDIN_STATUS=terminal
        [ -n "$budget_note" ] && HOOK_STDIN_STATUS="$HOOK_STDIN_STATUS+$budget_note"
        return 0
    fi

    tf="${TMPDIR:-/tmp}/stdinb.$$.$RANDOM"
    : > "$tf" 2>/dev/null || {
        HOOK_STDIN_STATUS=ok
        [ -n "$budget_note" ] && HOOK_STDIN_STATUS="$HOOK_STDIN_STATUS+$budget_note"
        return 0
    }

    deadline=$(( SECONDS + total_budget ))
    while :; do
        line=""
        started=$SECONDS
        if IFS= read -r -t "$line_budget" line; then
            printf '%s\n' "$line" >> "$tf"
        else
            rc=$?
            # TRAP 3. The last line of a hook payload has no trailing newline,
            # so it lands HERE with rc 1 and the bytes in $line. Dropping it
            # captures nothing at all on the common case.
            [ -n "$line" ] && printf '%s' "$line" >> "$tf"
            # TRAP 2. rc is 1 for BOTH timeout and clean EOF on bash 3.2, so rc
            # alone cannot tell them apart. If the read consumed its whole
            # per-line budget it was cut; if it returned promptly it was EOF.
            if [ "$rc" -gt 128 ] || [ $(( SECONDS - started )) -ge "$line_budget" ]; then
                HOOK_STDIN_STATUS=line-timeout
            fi
            break
        fi
        if [ "$SECONDS" -ge "$deadline" ]; then
            HOOK_STDIN_STATUS=total-timeout
            break
        fi
    done

    # TRAP 4. One read of a file, not n appends to a string.
    #
    # TRAP 5. This read must use NO EXTERNAL BINARY. `$(<file)` is a bash
    # builtin; `$(cat file)` is a fork that needs PATH. A guard has to work when
    # the environment is hostile, which is exactly when it matters. printf and
    # the >> redirect above are builtins too, so the whole read path is
    # PATH-free. `rm` is not, so its failure is tolerated: leaking a temp file
    # under an empty PATH is strictly better than a gate that stops guarding.
    HOOK_STDIN="$(<"$tf")"
    rm -f "$tf" 2>/dev/null || true

    # A REJECTED BUDGET RIDES ALONG WITH THE STATUS, IT DOES NOT REPLACE IT.
    # Replacing loses which shape of read happened; dropping it leaves the result
    # indistinguishable from one taken under a bound that was honoured. Appended,
    # so a reader matching on a substring still sees ok/line-timeout/total-timeout
    # and additionally learns the bound was not the one the caller asked for.
    [ -n "$budget_note" ] && HOOK_STDIN_STATUS="$HOOK_STDIN_STATUS+$budget_note"
    return 0
}

# ---------------------------------------------------------------------------
# Self-test. Every assertion below is a shape that actually broke something.
#
# THE GUARD ON THIS BLOCK IS LOAD-BEARING AND IS NOT ABOUT THE SELF-TEST.
# This file is SOURCED by hooks, and a sourced file sees the CALLER's positional
# parameters. Hooks may be wired as `bash foo.sh --hook`, so inside them $1 is
# "--hook", not anything of ours. Only look at arguments when this file was
# EXECUTED: `${BASH_SOURCE[0]}` equals `$0` only then; when sourced, $0 is the
# caller. Both --selftest spellings work and anything else is loud: a check that
# cannot be reached must not look like a check that passed.
if [ "${BASH_SOURCE[0]}" = "$0" ] && [ $# -gt 0 ]; then
    case "$1" in
        --selftest|--self-test) : ;;
        *) printf 'stdin-bounded.sh: unknown argument "%s" (expected --selftest)\n' "$1" >&2
           exit 2 ;;
    esac
fi
if [ "${BASH_SOURCE[0]}" = "$0" ] && [ $# -gt 0 ]; then
    pass=0; fail=0; bctl=NOT-RUN
    ck() {  # ck <name> <expected> <actual>
        if [ "$2" = "$3" ]; then pass=$((pass+1))
        else fail=$((fail+1)); printf '  FAIL %s\n    expected: %s\n    actual:   %s\n' "$1" "$2" "$3" >&2
        fi
    }

    # ABSOLUTE, always. BASH_SOURCE is whatever path the caller typed, so a
    # relative one silently fails to source from a helper with a different cwd
    # -- and a helper that failed to source reports empty, which reads exactly
    # like a hook that received nothing.
    SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
    tmp="$(mktemp -d)" || exit 1
    # The mutants below are COPIES of this file run from $tmp, where no sibling
    # stdin-budget.sh exists. The origin file solved this with a $HOME fallback;
    # this vendored copy is repo-contained, so the contract file is copied
    # beside the mutants instead. A control that cannot run is not a control
    # that passed.
    cp "$(dirname "$SELF")/stdin-budget.sh" "$tmp/" 2>/dev/null || true

    # A helper that sources us and prints what it got. It must be a SEPARATE
    # process with its own stdin: a backgrounded subshell in a non-interactive
    # shell gets /dev/null for stdin, so a rig that redirects its own call
    # rather than the subject under test measures nothing and reports a pass.
    cat > "$tmp/sub.sh" <<SUB
#!/bin/bash
. "$SELF"
read_stdin_bounded
printf '%s|%s' "\$HOOK_STDIN_STATUS" "\${#HOOK_STDIN}"
SUB

    # 1. THE COMMON CASE: JSON with no trailing newline must arrive whole.
    #    This is trap 3. A loop without the partial-append branch scores 0 here.
    payload='{"hook_event_name":"PreToolUse","tool_name":"Bash"}'
    printf '%s' "$payload" > "$tmp/p1"
    ck "payload with no trailing newline arrives intact" \
       "ok|${#payload}" "$(/bin/bash "$tmp/sub.sh" < "$tmp/p1")"

    # 2. BYTE EXACTNESS across a CR and a blank line, which a naive line loop
    #    normalises away.
    cat > "$tmp/sub2.sh" <<SUB
#!/bin/bash
. "$SELF"
read_stdin_bounded
printf '%s' "\$HOOK_STDIN" > "$tmp/got"
SUB
    printf '{"a":"x\ry"}\n\n{"b":2}' > "$tmp/p2"
    /bin/bash "$tmp/sub2.sh" < "$tmp/p2"
    if cmp -s "$tmp/p2" "$tmp/got"; then pass=$((pass+1))
    else fail=$((fail+1)); echo "  FAIL multi-line payload is not byte-exact" >&2; fi

    # 3. THE DEFECT ITSELF: a live pipe whose writer never writes. Unbounded,
    #    this never returns. The writer must be a real child process holding the
    #    fifo open -- that is the only shape no pre-read guard can detect.
    fifo="$tmp/fifo"
    mkfifo "$fifo" 2>/dev/null
    sleep 20 > "$fifo" &
    writer=$!
    t0=$SECONDS
    out="$(/bin/bash "$tmp/sub.sh" < "$fifo")"
    elapsed=$(( SECONDS - t0 ))
    kill "$writer" 2>/dev/null
    ck "a silent writer is reported as a cut read, not a clean one" "line-timeout|0" "$out"
    if [ "$elapsed" -lt 10 ]; then pass=$((pass+1))
    else fail=$((fail+1)); echo "  FAIL silent writer took ${elapsed}s -- not bounded" >&2; fi

    # 4. AN INTERACTIVE TERMINAL IS NOT A PAYLOAD, and must not cost the budget.
    ck "a closed stdin returns promptly as ok" "ok|0" \
       "$(/bin/bash "$tmp/sub.sh" < /dev/null)"

    # 5. A LARGE MULTI-LINE PAYLOAD MUST NOT BLOW ITS OWN BUDGET. This is trap 4:
    #    the string-append version takes 50s here and reports total-timeout on a
    #    payload that arrived instantly and intact.
    python3 - "$tmp/big" <<'PY' 2>/dev/null || printf 'x%.0s' $(seq 1 100) > "$tmp/big"
import json, sys
open(sys.argv[1], "w").write("\n".join(json.dumps({"i": i, "pad": "x"*200}) for i in range(5000)))
PY
    want=$(wc -c < "$tmp/big" | tr -d ' ')
    t0=$SECONDS
    got="$(/bin/bash "$tmp/sub.sh" < "$tmp/big")"
    elapsed=$(( SECONDS - t0 ))
    ck "a 5000-line payload arrives whole and on time" "ok|$want" "$got"
    if [ "$elapsed" -le "$STDIN_TOTAL_SECONDS" ]; then pass=$((pass+1))
    else fail=$((fail+1)); echo "  FAIL large payload took ${elapsed}s (budget ${STDIN_TOTAL_SECONDS}s)" >&2; fi

    # 6. POSITIVE CONTROL. A copy of this file with the partial-append branch
    #    removed -- trap 3, the single most likely way to write this wrong --
    #    MUST fail assertion 1. If the mutant passes, these assertions are not
    #    reading what they claim to and the whole suite is decoration.
    sed 's/^            \[ -n "\$line" \] && printf/            false \&\& printf/' "$SELF" > "$tmp/mutant.sh"
    if ! grep -q 'false && printf' "$tmp/mutant.sh"; then
        fail=$((fail+1)); ctl=BROKEN
        echo "  FAIL positive control: the mutation did not apply, so it proves nothing" >&2
    else
        cat > "$tmp/msub.sh" <<SUB
#!/bin/bash
. "$tmp/mutant.sh"
read_stdin_bounded
printf '%s|%s' "\$HOOK_STDIN_STATUS" "\${#HOOK_STDIN}"
SUB
        mout="$(/bin/bash "$tmp/msub.sh" < "$tmp/p1")"
        if [ "$mout" = "ok|0" ]; then ctl=HOLDS; pass=$((pass+1))
        else ctl=BROKEN; fail=$((fail+1))
             echo "  FAIL positive control: mutant captured '$mout', expected 'ok|0'" >&2; fi
    fi

    # 6b. NO EXTERNAL BINARY ON THE READ PATH. A gate must still work when the
    #     environment is hostile, because that is when it matters.
    cat > "$tmp/pathsub.sh" <<SUB
#!/bin/bash
PATH="" . "$SELF"
PATH="" read_stdin_bounded
printf '%s|%s' "\$HOOK_STDIN_STATUS" "\${#HOOK_STDIN}"
SUB
    ck "the read path uses no external binary (empty PATH)" \
       "ok|${#payload}" "$(PATH="" /bin/bash "$tmp/pathsub.sh" < "$tmp/p1")"

    # -----------------------------------------------------------------------
    # 6c. A BUDGET THIS SHELL CANNOT USE MUST NOT SILENTLY DELETE THE PAYLOAD.
    #     bash 3.2 rejects `read -t 0.5` outright, so an unvalidated fractional
    #     budget produced status=ok over 0 bytes on a 66-byte payload -- the
    #     data gone AND the evidence of loss gone, from following this file's
    #     own documentation.
    #
    #     THESE ASSERT ON THE CAPTURED BYTES AND THE RECORDED STATUS, NEVER ON
    #     AN EXIT CODE. read_stdin_bounded returns 0 in every arm by design
    #     (its callers own their own exit contracts), so an exit-code assertion
    #     here could not fail in either direction and would prove nothing.
    #
    #     BOTH DIRECTIONS. Refusing a bad budget is only half the fix;
    #     truncating real input is a worse bug than the hang, so the payload
    #     must still arrive whole under the fallback bound.

    # DIRECTION A: a VALID non-default budget is honoured and stamps nothing.
    #     Without this arm the suite could pass by stamping every read.
    ck "a valid budget is honoured and adds no note" "ok|${#payload}" \
       "$(STDIN_LINE_SECONDS=1 STDIN_TOTAL_SECONDS=2 /bin/bash "$tmp/sub.sh" < "$tmp/p1")"

    # DIRECTION B: a FRACTIONAL budget still captures the whole payload, and says
    #     so. `ok+bad-line-budget`: the primary word still describes the read, the
    #     suffix says the configuration was refused.
    ck "a fractional budget does not eat the payload" "ok+bad-line-budget|${#payload}" \
       "$(STDIN_LINE_SECONDS=0.5 /bin/bash "$tmp/sub.sh" < "$tmp/p1")"

    # A budget large enough to be a hang with a number attached is rejected too,
    # because the upper bound is part of the liveness guarantee, not politeness.
    ck "an out-of-range budget is rejected and recorded" "ok+bad-total-budget|${#payload}" \
       "$(STDIN_TOTAL_SECONDS=100000 /bin/bash "$tmp/sub.sh" < "$tmp/p1")"

    # Non-numeric, zero, negative, fractional and out-of-range all fell in the
    # same hole; each reached `read -t` unchallenged before this fix.
    #
    # A NULL BUDGET IS DELIBERATELY ABSENT FROM THIS LIST AND THAT IS NOT AN
    # OVERSIGHT. `: "${STDIN_LINE_SECONDS:=2}"` above fires on unset OR null, so
    # an empty value is already the default by the time the predicate could see
    # it. Asserting `ok+bad-line-budget` for it would be asserting a lie.
    for badv in banana 0 -1 2.0 61 '1 2'; do
        ck "budget '$badv' is refused, payload intact" "ok+bad-line-budget|${#payload}" \
           "$(STDIN_LINE_SECONDS="$badv" /bin/bash "$tmp/sub.sh" < "$tmp/p1")"
    done

    # DIRECTION C: a GENUINELY BLOCKED WRITER, with a bad budget on top. Liveness
    #     has to survive both at once: the fallback bound must be a REAL bound,
    #     and the result must say both that the read was cut and that the budget
    #     was not honoured. ELAPSED TIME IS ASSERTED, because "it returned" is the
    #     claim and an outside kill would otherwise look like a clean return.
    fifo2="$tmp/badbudget.fifo"
    if mkfifo "$fifo2" 2>/dev/null; then
        sleep 20 > "$fifo2" &
        writer2=$!
        bb_t0=$SECONDS
        bb_out="$(STDIN_LINE_SECONDS=0.5 /bin/bash "$tmp/sub.sh" < "$fifo2")"
        bb_el=$(( SECONDS - bb_t0 ))
        kill "$writer2" 2>/dev/null
        ck "silent writer + bad budget says both things" "line-timeout+bad-line-budget|0" "$bb_out"
        if [ "$bb_el" -lt 10 ]; then pass=$((pass+1))
        else fail=$((fail+1)); echo "  FAIL silent writer + bad budget took ${bb_el}s -- the fallback bound is not a bound" >&2; fi
    else
        fail=$((fail+1)); echo "  FAIL could not mkfifo; the blocked-writer arm inspected 0 subjects" >&2
    fi

    # DIRECTION D: A LEGITIMATELY EMPTY PAYLOAD MUST NOT READ AS A LOSS.
    #     Before the fix, "empty because the budget was unusable" and "empty
    #     because the producer sent nothing" were the same two words, `ok|0`.
    #     They must now be distinguishable, so the assertion is on the PAIR,
    #     not on either row alone.
    empty_clean="$(/bin/bash "$tmp/sub.sh" < /dev/null)"
    empty_badb="$(STDIN_LINE_SECONDS=0.5 /bin/bash "$tmp/sub.sh" < /dev/null)"
    ck "a genuinely empty payload is still plain ok" "ok|0" "$empty_clean"
    ck "an empty payload under a bad budget is not the same row" "ok+bad-line-budget|0" "$empty_badb"
    if [ "$empty_clean" != "$empty_badb" ]; then pass=$((pass+1))
    else fail=$((fail+1)); echo "  FAIL empty-and-fine is indistinguishable from empty-under-a-bad-budget" >&2; fi

    # 6d. POSITIVE CONTROL FOR THE FIX ITSELF, NOT FOR THE CAUSE. Ablate the
    #     validation in a COPY OF THE SHIPPING FILE and DIRECTION B must go red
    #     with the original defect. Mutating the shipping file by rewrite rather
    #     than hand-writing a "broken version" matters: a hand-written one
    #     measures transcription, not the cut. If removing the fix changes
    #     nothing, the branch is shadowed and every assertion above is
    #     decoration.
    sed 's|^    stdin_budget_usable "\$line_budget"|    true "$line_budget"|' "$SELF" > "$tmp/nobudget.sh"
    if ! grep -q '^    true "\$line_budget"' "$tmp/nobudget.sh"; then
        fail=$((fail+1)); bctl=BROKEN
        echo "  FAIL budget positive control: the mutation did not apply, so it proves nothing" >&2
    else
        cat > "$tmp/nbsub.sh" <<SUB
#!/bin/bash
. "$tmp/nobudget.sh"
read_stdin_bounded
printf '%s|%s' "\$HOOK_STDIN_STATUS" "\${#HOOK_STDIN}"
SUB
        nb="$(STDIN_LINE_SECONDS=0.5 /bin/bash "$tmp/nbsub.sh" < "$tmp/p1" 2>/dev/null)"
        if [ "$nb" = "ok|0" ]; then bctl=HOLDS; pass=$((pass+1))
        else bctl=BROKEN; fail=$((fail+1))
             echo "  FAIL budget positive control: unvalidated build reported '$nb', expected 'ok|0' (the defect)" >&2; fi
    fi

    # 7. THE SOURCED-WITH-FOREIGN-ARGS SHAPE. A wired gate invoked as
    #    `bash gate.sh --hook` sources us, and inside us $1 is "--hook".
    #    Sourcing must complete silently and define the function; it must NOT
    #    interpret the caller's flags or exit.
    cat > "$tmp/argsub.sh" <<SUB
#!/bin/bash
. "$SELF"
printf '%s|%s' "\$(type -t read_stdin_bounded)" "\$1"
SUB
    ck "sourcing with the caller's own flags is inert" "function|--hook" \
       "$(/bin/bash "$tmp/argsub.sh" --hook < /dev/null)"

    # 8. BOTH SPELLINGS RUN, AND ANYTHING ELSE IS LOUD. The failure this replaces
    #    was exit 0 with empty output on a typo, which reads as a clean pass.
    #
    #    THE NESTING GUARD IS NOT OPTIONAL. An assertion about ARGUMENT DISPATCH
    #    must test dispatch, not re-run the thing doing the dispatching --
    #    a recursive full-suite invocation is unbounded fork recursion.
    #    The nested run still REPORTS its own failures rather than exiting 0
    #    flat.
    if [ -n "${STDIN_BOUNDED_NESTED:-}" ]; then
        rm -rf "$tmp"
        echo "stdin-bounded selftest: $pass passed, $fail failed (nested; dispatch confirmed)"
        [ "$fail" -eq 0 ] && exit 0 || exit 1
    fi
    alt_out="$(STDIN_BOUNDED_NESTED=1 /bin/bash "$SELF" --self-test 2>&1)"
    case "$alt_out" in
        *"stdin-bounded selftest:"*) pass=$((pass+1)) ;;
        *) fail=$((fail+1)); echo "  FAIL --self-test spelling did not dispatch: '$alt_out'" >&2 ;;
    esac
    junk_out="$(/bin/bash "$SELF" --bogus 2>&1)"; junk_rc=$?
    if [ "$junk_rc" -ne 0 ] && [ -n "$junk_out" ]; then pass=$((pass+1))
    else fail=$((fail+1)); echo "  FAIL an unknown flag exited $junk_rc saying '$junk_out'" >&2; fi

    # 9. WHOLENESS, WHICH `bash -n` CANNOT SEE. A single NUL byte truncates what
    #    bash parses -- content after it silently vanishes -- while `bash -n`
    #    still exits 0. "bash -n passed" is not evidence a file is whole.
    if LC_ALL=C tr -d '\000' < "$SELF" | cmp -s - "$SELF"; then pass=$((pass+1))
    else fail=$((fail+1)); echo "  FAIL this file contains a NUL byte; bash silently drops what follows it" >&2; fi

    rm -rf "$tmp"
    # BOTH controls are printed. A control whose verdict nobody sees is the same
    # shape of defect as a status field nobody reads.
    echo "stdin-bounded selftest: $pass passed, $fail failed, partial-append control $ctl, budget control $bctl"
    [ "$fail" -eq 0 ] && exit 0 || exit 1
fi
