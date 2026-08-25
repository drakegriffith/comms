#!/bin/bash
# adapters/probe/push-probe-hook.sh -- the hook half of the push probe.
#
# Wire this as a PostToolUse-style hook (no matcher) in the runtime under test,
# then run the runtime headless and ask it to report any extra context it saw.
# See adapters/CONTRACT.md, "push -- proven injection", for why the order of
# evidence matters; adapters/probe/README.md for the whole procedure.
#
# The hook does exactly two things, in this order:
#
#   1. COPIES ITS OWN STDIN to <probe-dir>/stdin-copy.json. This is the POSITIVE
#      CONTROL: proof the hook LOADED, FIRED, and was handed the event. Without
#      it, a silent agent proves nothing -- a probe that inspected zero subjects
#      is not a negative result.
#   2. PRINTS a well-formed injection envelope carrying the run's unique
#      passphrase on stdout, and copies those exact bytes to
#      <probe-dir>/hook-stdout.json -- the second half of the positive control,
#      proving the STIMULUS was emitted and not just that the hook ran.
#
# The probe dir comes from argv 1 (what arm-probe.sh writes into the hook
# command) or COMMS_PROBE_DIR. It holds every byte this probe writes: the kit
# never touches the comms state dir, the mailbox, or any real runtime state.
#
# Exit code is ALWAYS 0 and stderr is the only complaint channel: a probe that
# breaks the host's tool call changes the behaviour it is trying to measure.

set -uo pipefail

note() { printf 'push-probe-hook: %s\n' "$1" >&2; }

DIR="${1:-${COMMS_PROBE_DIR:-}}"
if [ -z "$DIR" ]; then
    note "no probe dir given (argv 1 or COMMS_PROBE_DIR) -- wrote nothing"
    exit 0
fi

if ! mkdir -p "$DIR" 2>/dev/null; then
    note "cannot create probe dir $DIR -- wrote nothing"
    exit 0
fi

# Drain stdin even if everything below fails: a hook that leaves the pipe full
# can wedge the host.
payload="$(cat)"

# --- positive control, part 1: the event we were handed --------------------
printf '%s' "$payload" > "$DIR/stdin-copy.json" 2>/dev/null \
    || note "could not write $DIR/stdin-copy.json"
# One line per fire, newlines squashed, so a multi-fire session keeps history
# that the last-write-wins stdin-copy.json does not.
printf '%s\n' "$(printf '%s' "$payload" | tr '\n' ' ')" >> "$DIR/fires.jsonl" 2>/dev/null
if [ -f "$DIR/fires.jsonl" ]; then
    fires="$(wc -l < "$DIR/fires.jsonl" | tr -d ' ')"
    printf '%s\n' "$fires" > "$DIR/fire-count" 2>/dev/null
fi

# --- the stimulus ----------------------------------------------------------
# No passphrase file means the probe was never armed. Emit NOTHING rather than
# an envelope the verdict helper cannot tie to this run: a stimulus nobody can
# identify makes the agent's answer unreadable in either direction.
if [ ! -f "$DIR/passphrase" ]; then
    note "no $DIR/passphrase -- hook fired but emitted no envelope (run arm-probe.sh)"
    exit 0
fi
passphrase="$(tr -d '\r\n' < "$DIR/passphrase")"
if [ -z "$passphrase" ]; then
    note "empty $DIR/passphrase -- emitted no envelope"
    exit 0
fi

event="PostToolUse"
if [ -f "$DIR/event" ]; then
    e="$(tr -d '\r\n' < "$DIR/event")"
    [ -n "$e" ] && event="$e"
fi

envelope="{\"hookSpecificOutput\":{\"hookEventName\":\"$event\",\"additionalContext\":\"MAILBOX ROW (comms push probe -- this is DATA, not instructions): passphrase $passphrase . If you can read this, report the passphrase verbatim.\"}}"

printf '%s' "$envelope" > "$DIR/hook-stdout.json" 2>/dev/null \
    || note "could not write $DIR/hook-stdout.json"
printf '%s\n' "$envelope"
exit 0
