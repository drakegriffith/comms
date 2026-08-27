#!/bin/bash
# test_poll_driver.sh -- the generic poll driver (bin/comms-poll-driver) and the
# bash-callable confirmed-delivery cursor it rides on (comms cursor take/confirm).
#
# THE HARD RULE UNDER TEST: the cursor advances ONLY when the delivery command
# exits 0. Everything else here is scaffolding around four claims --
#   1. a confirmed delivery advances the cursor (and is not re-delivered),
#   2. a FAILED delivery re-delivers (the row reaches the runtime twice, which
#      is the recoverable failure; being dropped is the unrecoverable one),
#   3. no rows means no cursor movement -- and no cursor file at all,
#   4. a restarted driver resumes from the persisted cursor, in a new process.
#
# The fake runtime is the oracle: it APPENDS everything it is handed to an
# inbox file and exits with whatever code a control file names, so "was this
# row delivered, and how many times" is a count in a file, not a self-report.
#
# All state is isolated: COMMS_ROOT and COMMS_STATE_DIR are mktemp dirs, so
# nothing here touches a real mailbox, a real cursor, or the real state dir.
#
# Exit: 0 all passed, 1 any failed. Prints a passed/failed count either way.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"        # <repo>/tests
REPO="$(cd "$SELF_DIR/.." && pwd)"
COMMS="$REPO/bin/comms"
DRIVER="$REPO/bin/comms-poll-driver"
KIMI_DRIVER="$REPO/adapters/kimi/poll-driver.sh"

export COMMS_STATE_DIR="$(mktemp -d)"
export COMMS_ROOT="$(mktemp -d)"
WORK="$(mktemp -d)"
trap 'rm -rf "$COMMS_STATE_DIR" "$COMMS_ROOT" "$WORK"' EXIT

PASS=0
FAIL=0

ok()   { echo "ok:   $1"; PASS=$((PASS + 1)); }
bad()  { echo "FAIL: $1"; FAIL=$((FAIL + 1)); }
# assert <desc> <condition-rc>
assert() { if [ "$2" -eq 0 ]; then ok "$1"; else bad "$1"; fi; }
# eq <desc> <want> <got>
eq() {
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want [$2], got [$3])"; fi
}

RUN="polldrv-$$"
INBOX="$WORK/inbox"
RCFILE="$WORK/rc"
: > "$INBOX"
echo 0 > "$RCFILE"

# ---- the fake runtime ------------------------------------------------------
# Takes the rows on stdin, records them, then fails or succeeds on command.
cat > "$WORK/fake-runtime" <<'EOF'
#!/bin/bash
{ echo "--- delivery ---"; cat; } >> "$INBOX_PATH"
exit "$(cat "$RC_PATH" 2>/dev/null || echo 0)"
EOF
# Same, but the rows arrive as an ARGUMENT (the {} form) instead of on stdin.
cat > "$WORK/fake-runtime-arg" <<'EOF'
#!/bin/bash
{ echo "--- delivery ---"; printf '%s\n' "$1"; echo "cwd=$PWD"; } >> "$INBOX_PATH"
exit "$(cat "$RC_PATH" 2>/dev/null || echo 0)"
EOF
chmod +x "$WORK/fake-runtime" "$WORK/fake-runtime-arg"
export INBOX_PATH="$INBOX" RC_PATH="$RCFILE"

CURSOR="$COMMS_STATE_DIR/poll-driver/$RUN/beta.all.json"
LOG="$COMMS_STATE_DIR/poll-driver/$RUN/beta.all.log"

# grep -c prints 0 and EXITS 1 on no match, so these must not use `|| echo 0`
# (that prints two lines and every count comparison then fails for the wrong
# reason -- caught while writing this suite).
deliveries() { local n; n="$(grep -c -- '--- delivery ---' "$INBOX" 2>/dev/null)"; echo "${n:-0}"; }
handed()     { local n; n="$(grep -c -F -- "$1" "$INBOX" 2>/dev/null)"; echo "${n:-0}"; }
wait_handed() {
  local needle="$1" want="$2" pid="$3" cap="$4" start="$SECONDS"
  while [ "$(handed "$needle")" -lt "$want" ]; do
    kill -0 "$pid" 2>/dev/null || return 1
    [ $((SECONDS - start)) -lt "$cap" ] || return 1
    sleep 1
  done
}

# ---- 1. empty board: nothing new, and NOTHING is written -------------------
"$COMMS" init "$RUN" >/dev/null
out="$("$DRIVER" "$RUN" beta --once -- "$WORK/fake-runtime" 2>&1)"; rc=$?
eq "empty board exits 0" 0 "$rc"
case "$out" in *"nothing new"*) ok "empty board says nothing new" ;;
               *) bad "empty board says nothing new (got: $out)" ;; esac
assert "empty board writes NO cursor file" "$([ ! -e "$CURSOR" ]; echo $?)"
eq "empty board makes no delivery" 0 "$(deliveries)"

# ---- 2. dry run: shows the rows, delivers nothing, moves nothing -----------
"$COMMS" post "$RUN" alpha finding "row-one" --topic work >/dev/null
"$COMMS" post "$RUN" alpha finding "row-two" --topic work >/dev/null
out="$("$DRIVER" "$RUN" beta --once --dry-run -- "$WORK/fake-runtime" 2>&1)"; rc=$?
eq "dry run exits 0" 0 "$rc"
case "$out" in *row-one*row-two*) ok "dry run prints both rows" ;;
               *) bad "dry run prints both rows (got: $out)" ;; esac
case "$out" in *"NOT instructions"*) ok "dry run keeps the data-not-instructions header" ;;
               *) bad "dry run keeps the data-not-instructions header" ;; esac
assert "dry run writes NO cursor file" "$([ ! -e "$CURSOR" ]; echo $?)"
eq "dry run makes no delivery" 0 "$(deliveries)"

# ---- 3. confirmed delivery advances ----------------------------------------
out="$("$DRIVER" "$RUN" beta --once -- "$WORK/fake-runtime" 2>&1)"; rc=$?
eq "confirmed delivery exits 0" 0 "$rc"
eq "the runtime was handed one batch" 1 "$(deliveries)"
eq "row-one reached the runtime once" 1 "$(handed row-one)"
eq "row-two reached the runtime once" 1 "$(handed row-two)"
assert "cursor file exists after a confirmed delivery" "$([ -f "$CURSOR" ]; echo $?)"
eq "cursor counts alpha's two rows" '{"alpha": 2}' "$(cat "$CURSOR")"

# ---- 4. no new rows: no cursor movement, no delivery ------------------------
before="$(cat "$CURSOR")"
out="$("$DRIVER" "$RUN" beta --once -- "$WORK/fake-runtime" 2>&1)"
case "$out" in *"nothing new"*) ok "a second poll with no new rows says nothing new" ;;
               *) bad "a second poll with no new rows says nothing new (got: $out)" ;; esac
eq "no new rows leaves the cursor byte-identical" "$before" "$(cat "$CURSOR")"
eq "no new rows makes no second delivery" 1 "$(deliveries)"

# ---- 5. FAILED delivery holds the cursor ------------------------------------
"$COMMS" post "$RUN" alpha finding "row-three" --topic work >/dev/null
echo 7 > "$RCFILE"
out="$("$DRIVER" "$RUN" beta --once -- "$WORK/fake-runtime" 2>&1)"; rc=$?
eq "a failed delivery is not a driver failure (exit 0)" 0 "$rc"
case "$out" in *"exited 7"*re-deliver*) ok "failed delivery says so, loudly, on stderr" ;;
               *) bad "failed delivery says so, loudly (got: $out)" ;; esac
eq "cursor did NOT advance past the failed delivery" "$before" "$(cat "$CURSOR")"
eq "row-three was attempted once" 1 "$(handed row-three)"

# ---- 6. ...and the held rows RE-DELIVER (the hard rule) --------------------
echo 0 > "$RCFILE"
"$DRIVER" "$RUN" beta --once -- "$WORK/fake-runtime" >/dev/null 2>&1
eq "row-three reached the runtime a SECOND time after the failure" 2 "$(handed row-three)"
eq "the two already-confirmed rows were NOT re-sent" 1 "$(handed row-one)"
eq "cursor advanced only after the delivery that worked" '{"alpha": 3}' "$(cat "$CURSOR")"

# ---- 7. restart resumes ------------------------------------------------------
# Every invocation above was already a separate process; this one adds a new
# row after the restart to prove the resume point is the CURSOR and not luck.
"$COMMS" post "$RUN" alpha finding "row-four" --topic work >/dev/null
"$DRIVER" "$RUN" beta --once -- "$WORK/fake-runtime" >/dev/null 2>&1
eq "a restarted driver delivers only the new row" 1 "$(handed row-four)"
eq "a restarted driver does not replay row-three" 2 "$(handed row-three)"
eq "cursor after the restart" '{"alpha": 4}' "$(cat "$CURSOR")"

# ---- 8. the CLI's own read cursor was never touched -------------------------
assert "driver leaves no CLI read cursor for this run" \
       "$([ ! -e "$COMMS_STATE_DIR/read-cursor/$RUN" ]; echo $?)"
out="$("$COMMS" read "$RUN" beta 2>&1)"
n="$(printf '%s\n' "$out" | grep -c row-)"
eq "a human's plain read still sees all four rows (--replay left them)" 4 "$n"

# ---- 9. delivery accounting -------------------------------------------------
assert "a delivery log exists" "$([ -f "$LOG" ]; echo $?)"
eq "log records the failed attempt" 1 "$(grep -c '"delivered": false' "$LOG")"
eq "log records the confirmed deliveries" 3 "$(grep -c '"delivered": true' "$LOG")"
case "$(head -1 "$LOG")" in *'"rc": 0'*'"rows": 2'*) ok "log line carries rows and rc" ;;
  *) bad "log line carries rows and rc (got: $(head -1 "$LOG"))" ;; esac

# ---- 10. the {} form, and --cwd --------------------------------------------
: > "$INBOX"
"$COMMS" post "$RUN" alpha finding "row-five" --topic work >/dev/null
"$DRIVER" "$RUN" beta --once --cwd "$WORK" -- "$WORK/fake-runtime-arg" '{}' >/dev/null 2>&1
eq "the {} form hands the rows as an argument" 1 "$(handed row-five)"
eq "--cwd runs the delivery command there" 1 "$(handed "cwd=$WORK")"

# ---- 11. one reader, one view: a topic view keeps its own cursor ------------
: > "$INBOX"
"$COMMS" post "$RUN" alpha finding "topic-a-row" --topic aaa >/dev/null
"$COMMS" post "$RUN" alpha finding "topic-b-row" --topic bbb >/dev/null
"$DRIVER" "$RUN" gamma --once --topic aaa -- "$WORK/fake-runtime" >/dev/null 2>&1
eq "a --topic driver delivers that topic's row" 1 "$(handed topic-a-row)"
eq "a --topic driver does not deliver another topic's row" 0 "$(handed topic-b-row)"
assert "the topic view has its own cursor file" \
       "$([ -f "$COMMS_STATE_DIR/poll-driver/$RUN/gamma.topic-aaa.json" ]; echo $?)"

# ---- 12. enroll-first invariant --------------------------------------------
out="$("$DRIVER" "$RUN" delta --once --enroll -- "$WORK/fake-runtime" 2>&1)"; rc=$?
eq "--enroll into an unarmed run fails loudly (exit 1)" 1 "$rc"
case "$out" in *"comms arm $RUN"*) ok "the refusal names the fix" ;;
               *) bad "the refusal names the fix (got: $out)" ;; esac
"$COMMS" arm "$RUN" >/dev/null 2>&1
out="$("$DRIVER" "$RUN" delta --once --enroll --topics "work,aaa" -- "$WORK/fake-runtime" 2>&1)"; rc=$?
eq "--enroll into an armed run proceeds" 0 "$rc"
out="$("$COMMS" status "$RUN" 2>&1)"
case "$out" in *delta*) ok "the seat is on the participant roster" ;;
               *) bad "the seat is on the participant roster (got: $out)" ;; esac
out="$("$COMMS" subs "$RUN" delta --replay 2>&1)"
case "$out" in *topic-a-row*) ok "--topics subscribed the seat's mailbox slice" ;;
               *) bad "--topics subscribed the seat's mailbox slice (got: $out)" ;; esac

# Widening a subscription changes the view. The new view starts at zero, so it
# replays the already-subscribed rows alongside the older rows newly brought
# into view; a third poll then has nothing left.
WIDE_RUN="driver-subs-widen-$$"
: > "$INBOX"
"$COMMS" init "$WIDE_RUN" >/dev/null
"$COMMS" subscribe "$WIDE_RUN" alpha proj >/dev/null
"$COMMS" post "$WIDE_RUN" gamma finding "LATER-1" --topic later >/dev/null
"$COMMS" post "$WIDE_RUN" gamma finding "LATER-2" --topic later >/dev/null
"$COMMS" post "$WIDE_RUN" gamma finding "PROJ-1" --topic proj >/dev/null
"$COMMS" post "$WIDE_RUN" gamma finding "PROJ-2" --topic proj >/dev/null
"$COMMS" post "$WIDE_RUN" gamma finding "PROJ-3" --topic proj >/dev/null
"$DRIVER" "$WIDE_RUN" alpha --subs --topics proj --once -- "$WORK/fake-runtime" >/dev/null 2>&1
eq "initial subscription poll delivers three project rows" 3 "$(handed PROJ-)"
"$DRIVER" "$WIDE_RUN" alpha --subs --topics proj,later --once -- "$WORK/fake-runtime" >/dev/null 2>&1
eq "widened subscription delivers both older later rows" 2 "$(handed LATER-)"
eq "widened subscription explicitly replays the three project rows" 6 "$(handed PROJ-)"
before="$(deliveries)"
out="$("$DRIVER" "$WIDE_RUN" alpha --subs --topics proj,later --once -- "$WORK/fake-runtime" 2>&1)"
case "$out" in *"nothing new"*) ok "third widened-subscription poll has nothing new" ;;
               *) bad "third widened-subscription poll has nothing new (got: $out)" ;; esac
eq "third widened-subscription poll delivers nothing" "$before" "$(deliveries)"

# A long-running driver must re-derive a mutable subscription view every poll.
# Every wait is capped and also fails immediately if the background driver dies.
LOOP_RUN="driver-subs-loop-$$"
: > "$INBOX"
"$COMMS" init "$LOOP_RUN" >/dev/null
"$COMMS" subscribe "$LOOP_RUN" alpha proj >/dev/null
"$COMMS" post "$LOOP_RUN" gamma finding "LATER-1" --topic later >/dev/null
"$COMMS" post "$LOOP_RUN" gamma finding "LATER-2" --topic later >/dev/null
"$COMMS" post "$LOOP_RUN" gamma finding "PROJ-1" --topic proj >/dev/null
"$COMMS" post "$LOOP_RUN" gamma finding "PROJ-2" --topic proj >/dev/null
"$COMMS" post "$LOOP_RUN" gamma finding "PROJ-3" --topic proj >/dev/null
"$DRIVER" "$LOOP_RUN" alpha --subs --topics proj --interval 1 -- "$WORK/fake-runtime" \
  >"$WORK/direct-loop.out" 2>"$WORK/direct-loop.err" &
LOOP_PID=$!
if wait_handed PROJ- 3 "$LOOP_PID" 8; then
  "$COMMS" subscribe "$LOOP_RUN" alpha proj later >/dev/null
  wait_handed LATER- 2 "$LOOP_PID" 8
  LOOP_WAIT_RC=$?
else
  LOOP_WAIT_RC=1
fi
kill "$LOOP_PID" 2>/dev/null || true
wait "$LOOP_PID" 2>/dev/null || true
eq "loop-mode widening delivers both older later rows" 0 "$LOOP_WAIT_RC"
eq "loop-mode widening announces one subscription view change" 1 \
  "$(grep -c 'subscription view changed .* -> .*; replaying the subscribed board' "$WORK/direct-loop.err" 2>/dev/null || true)"

# ---- 13. usage / argument errors --------------------------------------------
out="$("$DRIVER" "$RUN" beta --once -- 2>&1)"; rc=$?
eq "-- with no command is a usage error" 2 "$rc"
out="$("$DRIVER" "$RUN" beta --nonsense --once -- true 2>&1)"; rc=$?
eq "an unknown option is a usage error" 2 "$rc"
out="$("$DRIVER" "$RUN" beta --topic x --subs --once -- true 2>&1)"; rc=$?
eq "--topic with --subs is rejected before any read" 2 "$rc"
out="$("$DRIVER" "$RUN" beta --once --cwd /no/such/dir -- true 2>&1)"; rc=$?
eq "a missing --cwd fails at startup" 1 "$rc"

# ---- 14. the cursor CLI on its own ------------------------------------------
C2="$WORK/c2.json"
rows='{"seat": "alpha", "at": "1", "text": "a"}
{"seat": "alpha", "at": "2", "text": "b"}'
out="$(printf '%s\n' "$rows" | "$COMMS" cursor take "$C2")"; rc=$?
eq "cursor take exits 0" 0 "$rc"
eq "cursor take prints the receipt first" '{"alpha": 2}' "$(printf '%s\n' "$out" | head -1)"
eq "cursor take prints the fresh rows after it" 2 "$(printf '%s\n' "$out" | tail -n +2 | grep -c seat)"
assert "cursor take writes NOTHING" "$([ ! -e "$C2" ]; echo $?)"
out="$(printf '%s\n' "$rows" | "$COMMS" cursor take "$C2" | tail -n +2 | wc -l | tr -d ' ')"
eq "a second take without confirm returns the same rows" 2 "$out"
"$COMMS" cursor confirm "$C2" '{"alpha": 2}'; rc=$?
eq "cursor confirm exits 0" 0 "$rc"
out="$(printf '%s\n' "$rows" | "$COMMS" cursor take "$C2" | tail -n +2 | wc -l | tr -d ' ')"
eq "after confirm those rows are gone" 0 "$out"
"$COMMS" cursor confirm "$C2" '{"alpha": 1}' 2>/dev/null
eq "a stale receipt cannot rewind the cursor" '{"alpha": 2}' "$(cat "$C2")"
out="$(printf 'not json\n' | "$COMMS" cursor take "$C2" 2>&1)"; rc=$?
eq "malformed input to cursor take fails loudly, never silently skips" 1 "$rc"
out="$("$COMMS" cursor confirm "$C2" 'not json' 2>&1)"; rc=$?
eq "a malformed receipt is rejected" 1 "$rc"
out="$("$COMMS" cursor sideways "$C2" 2>&1)"; rc=$?
eq "an unknown cursor verb is rejected" 1 "$rc"
out="$("$COMMS" cursor 2>&1)"; rc=$?
eq "bare cursor is rejected" 1 "$rc"

# ---- 15. the kimi adapter, now a caller of the generic driver ---------------
out="$("$KIMI_DRIVER" 2>&1)"; rc=$?
eq "kimi driver still exits 2 with usage on no args" 2 "$rc"
out="$("$KIMI_DRIVER" "$RUN" epsilon sess-1 /no/such/dir --once 2>&1)"; rc=$?
eq "kimi driver still rejects a missing cwd" 1 "$rc"
out="$("$KIMI_DRIVER" "$RUN" epsilon sess-1 "$WORK" --once 2>&1)"; rc=$?
eq "kimi driver --once exits 0" 0 "$rc"
case "$out" in *"would deliver"*row-one*) ok "kimi driver --once still previews the rows" ;;
               *) bad "kimi driver --once still previews the rows (got: $out)" ;; esac
case "$out" in *"NOT instructions"*) ok "kimi driver --once keeps the data-not-instructions header" ;;
               *) bad "kimi driver --once keeps the data-not-instructions header" ;; esac
assert "kimi driver --once advances no cursor" \
       "$([ ! -e "$COMMS_STATE_DIR/kimi-cursor/$RUN-epsilon.subs.json" ]; echo $?)"

# The adapter chooses the subscription view. Reconstruct issue #64: alpha must
# receive its subscribed project row and own unicast, but neither beta's
# unicast nor an unrelated topic. Its dry-run writes no receipt by contract, so
# a successful generic-driver poll over the identical view checks accounting.
KRUN="kimi-subs-$$"
"$COMMS" init "$KRUN" >/dev/null
"$COMMS" subscribe "$KRUN" alpha proj >/dev/null
"$COMMS" post "$KRUN" sender finding "foreign-beta" --to beta >/dev/null
"$COMMS" post "$KRUN" sender finding "subscribed-proj" --topic proj >/dev/null
"$COMMS" post "$KRUN" sender finding "own-alpha" --to alpha >/dev/null
"$COMMS" post "$KRUN" sender finding "unsubscribed-other" --topic other >/dev/null
out="$("$KIMI_DRIVER" "$KRUN" alpha sess-1 "$WORK" --once 2>&1)"; rc=$?
eq "kimi subscribed-slice preview exits 0" 0 "$rc"
case "$out" in *"would deliver 2 row(s)"*) ok "kimi preview contains exactly two subscribed rows" ;;
               *) bad "kimi preview contains exactly two subscribed rows (got: $out)" ;; esac
case "$out" in *subscribed-proj*own-alpha*|*own-alpha*subscribed-proj*)
  ok "kimi preview names the subscribed topic and own unicast" ;;
  *) bad "kimi preview names the subscribed topic and own unicast (got: $out)" ;; esac
case "$out" in *foreign-beta*|*unsubscribed-other*)
  bad "kimi preview excludes foreign unicast and unsubscribed topic (got: $out)" ;;
  *) ok "kimi preview excludes foreign unicast and unsubscribed topic" ;; esac

"$DRIVER" "$KRUN" alpha --subs --once -- "$WORK/fake-runtime" >/dev/null 2>&1
KLOG="$(find "$COMMS_STATE_DIR/poll-driver/$KRUN" -name 'alpha.subs-*.log' -print -quit)"
case "$(cat "$KLOG" 2>/dev/null)" in *'"view": "subs-'*) ok "receipt log records the digested subs view" ;;
  *) bad "receipt log records the digested subs view (got: $(cat "$KLOG" 2>/dev/null))" ;; esac

# The kimi override carries the same digest. Establish the first cursor through
# the generic driver, widen, then prove the adapter's dry preview sees the two
# older rows and the intentional full-view replay. Confirm that view and the
# adapter's third preview is empty.
KWIDE_RUN="kimi-subs-widen-$$"
"$COMMS" init "$KWIDE_RUN" >/dev/null
"$COMMS" subscribe "$KWIDE_RUN" alpha proj >/dev/null
"$COMMS" post "$KWIDE_RUN" gamma finding "K-LATER-1" --topic later >/dev/null
"$COMMS" post "$KWIDE_RUN" gamma finding "K-LATER-2" --topic later >/dev/null
"$COMMS" post "$KWIDE_RUN" gamma finding "K-PROJ-1" --topic proj >/dev/null
"$COMMS" post "$KWIDE_RUN" gamma finding "K-PROJ-2" --topic proj >/dev/null
"$COMMS" post "$KWIDE_RUN" gamma finding "K-PROJ-3" --topic proj >/dev/null
KDIGEST="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import swarm_mailbox; print(swarm_mailbox.subscription_digest(sys.argv[2], sys.argv[3]))' "$REPO/lib" "$KWIDE_RUN" alpha)"
KCURSOR="$COMMS_STATE_DIR/poll-driver/$KWIDE_RUN/alpha.subs-$KDIGEST.json"
"$DRIVER" "$KWIDE_RUN" alpha --subs --once -- /usr/bin/true >/dev/null 2>&1
"$COMMS" subscribe "$KWIDE_RUN" alpha proj later >/dev/null
out="$("$KIMI_DRIVER" "$KWIDE_RUN" alpha sess-1 "$WORK" --once 2>&1)"; rc=$?
eq "kimi widened-subscription preview exits 0" 0 "$rc"
case "$out" in *K-LATER-1*K-LATER-2*|*K-LATER-2*K-LATER-1*)
  ok "kimi widened-subscription preview names both older later rows" ;;
  *) bad "kimi widened-subscription preview names both older later rows (got: $out)" ;; esac
case "$out" in *"would deliver 5 row(s)"*K-PROJ-1*K-PROJ-2*K-PROJ-3*)
  ok "kimi widened-subscription preview explicitly replays the project rows" ;;
  *) bad "kimi widened-subscription preview explicitly replays the project rows (got: $out)" ;; esac
KDIGEST="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import swarm_mailbox; print(swarm_mailbox.subscription_digest(sys.argv[2], sys.argv[3]))' "$REPO/lib" "$KWIDE_RUN" alpha)"
KCURSOR="$COMMS_STATE_DIR/poll-driver/$KWIDE_RUN/alpha.subs-$KDIGEST.json"
"$DRIVER" "$KWIDE_RUN" alpha --subs --once -- /usr/bin/true >/dev/null 2>&1
out="$("$KIMI_DRIVER" "$KWIDE_RUN" alpha sess-1 "$WORK" --once 2>&1)"
case "$out" in *"nothing new"*) ok "kimi third widened-subscription preview has nothing new" ;;
               *) bad "kimi third widened-subscription preview has nothing new (got: $out)" ;; esac

# The adapter's real loop follows the same widening contract. A fake kimi is
# the oracle and records the prompt argument it received.
KLOOP_RUN="kimi-subs-loop-$$"
KIMI_BIN="$WORK/kimi-bin"
mkdir -p "$KIMI_BIN"
cat > "$KIMI_BIN/kimi" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$INBOX_PATH"
exit 0
EOF
chmod +x "$KIMI_BIN/kimi"
: > "$INBOX"
"$COMMS" init "$KLOOP_RUN" >/dev/null
"$COMMS" subscribe "$KLOOP_RUN" alpha proj >/dev/null
"$COMMS" post "$KLOOP_RUN" gamma finding "KLOOP-LATER-1" --topic later >/dev/null
"$COMMS" post "$KLOOP_RUN" gamma finding "KLOOP-LATER-2" --topic later >/dev/null
"$COMMS" post "$KLOOP_RUN" gamma finding "KLOOP-PROJ-1" --topic proj >/dev/null
"$COMMS" post "$KLOOP_RUN" gamma finding "KLOOP-PROJ-2" --topic proj >/dev/null
"$COMMS" post "$KLOOP_RUN" gamma finding "KLOOP-PROJ-3" --topic proj >/dev/null
PATH="$KIMI_BIN:$PATH" "$KIMI_DRIVER" "$KLOOP_RUN" alpha sess-loop "$WORK" --interval 1 \
  >"$WORK/kimi-loop.out" 2>"$WORK/kimi-loop.err" &
KLOOP_PID=$!
if wait_handed KLOOP-PROJ- 3 "$KLOOP_PID" 8; then
  "$COMMS" subscribe "$KLOOP_RUN" alpha proj later >/dev/null
  wait_handed KLOOP-LATER- 2 "$KLOOP_PID" 8
  KLOOP_WAIT_RC=$?
else
  KLOOP_WAIT_RC=1
fi
kill "$KLOOP_PID" 2>/dev/null || true
wait "$KLOOP_PID" 2>/dev/null || true
eq "kimi loop-mode widening delivers both older later rows" 0 "$KLOOP_WAIT_RC"
eq "kimi loop-mode widening announces one subscription view change" 1 \
  "$(grep -c 'subscription view changed .* -> .*; replaying the subscribed board' "$WORK/kimi-loop.err" 2>/dev/null || true)"

# A whole-board counts cursor cannot be reused for the narrower subscription
# view: its per-poster count can silently skip that poster's first visible row.
KVIEW_RUN="kimi-view-key-$$"
"$COMMS" init "$KVIEW_RUN" >/dev/null
"$COMMS" subscribe "$KVIEW_RUN" alpha watched >/dev/null
"$COMMS" post "$KVIEW_RUN" gamma finding "unsubscribed-one" --topic other >/dev/null
"$COMMS" post "$KVIEW_RUN" gamma finding "unsubscribed-two" --topic other >/dev/null
"$COMMS" post "$KVIEW_RUN" gamma finding "unsubscribed-three" --topic other >/dev/null
mkdir -p "$COMMS_STATE_DIR/kimi-cursor"
printf '%s\n' '{"gamma": 3}' > "$COMMS_STATE_DIR/kimi-cursor/$KVIEW_RUN-alpha.json"
"$COMMS" post "$KVIEW_RUN" gamma finding "first-subscribed-row" --topic watched >/dev/null
out="$("$KIMI_DRIVER" "$KVIEW_RUN" alpha sess-1 "$WORK" --once 2>&1)"; rc=$?
eq "kimi view-key preview exits 0" 0 "$rc"
case "$out" in *first-subscribed-row*) ok "kimi view-key preview delivers the subscribed row" ;;
               *) bad "kimi view-key preview delivers the subscribed row (got: $out)" ;; esac

# The one-time migration off the old last-`at` timestamp cursor: a driver that
# had already delivered everything must not re-deliver the whole board.
mkdir -p "$COMMS_STATE_DIR/kimi-cursor"
LAST_AT="$("$COMMS" read "$RUN" zeta --replay | tail -1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["at"])')"
printf '%s' "$LAST_AT" > "$COMMS_STATE_DIR/kimi-cursor/$RUN-zeta"
out="$("$KIMI_DRIVER" "$RUN" zeta sess-1 "$WORK" --once 2>&1)"; rc=$?
eq "kimi cursor migration exits 0" 0 "$rc"
case "$out" in *"nothing new"*) ok "a migrated timestamp cursor does not replay the board" ;;
               *) bad "a migrated timestamp cursor does not replay the board (got: $out)" ;; esac
assert "migration leaves the old cursor file behind as evidence" \
       "$([ -f "$COMMS_STATE_DIR/kimi-cursor/$RUN-zeta.pre-counts" ]; echo $?)"
"$COMMS" post "$RUN" alpha finding "post-migration-row" --topic work >/dev/null
out="$("$KIMI_DRIVER" "$RUN" zeta sess-1 "$WORK" --once 2>&1)"
case "$out" in *post-migration-row*) ok "a migrated cursor still delivers what comes next" ;;
               *) bad "a migrated cursor still delivers what comes next (got: $out)" ;; esac

# Migration must count only the same subscription view the new cursor owns.
KMIG_RUN="kimi-migration-view-$$"
"$COMMS" init "$KMIG_RUN" >/dev/null
"$COMMS" subscribe "$KMIG_RUN" alpha watched >/dev/null
"$COMMS" post "$KMIG_RUN" gamma finding "migration-before-subscribed" --topic watched >/dev/null
"$COMMS" post "$KMIG_RUN" gamma finding "migration-before-unsubscribed" --topic other >/dev/null
MIG_AT="$("$COMMS" read "$KMIG_RUN" alpha --replay | tail -1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["at"])')"
printf '%s' "$MIG_AT" > "$COMMS_STATE_DIR/kimi-cursor/$KMIG_RUN-alpha"
"$COMMS" post "$KMIG_RUN" gamma finding "migration-after-subscribed" --topic watched >/dev/null
out="$("$KIMI_DRIVER" "$KMIG_RUN" alpha sess-1 "$WORK" --once 2>&1)"; rc=$?
eq "kimi subscription-view migration exits 0" 0 "$rc"
case "$out" in *"would deliver 1 row(s)"*) ok "kimi subscription-view migration delivers exactly one row" ;;
  *) bad "kimi subscription-view migration delivers exactly one row (got: $out)" ;; esac
case "$out" in *migration-after-subscribed*) ok "kimi migration delivers the post-timestamp subscribed row" ;;
               *) bad "kimi migration delivers the post-timestamp subscribed row (got: $out)" ;; esac
case "$out" in *migration-before-subscribed*|*migration-before-unsubscribed*)
  bad "kimi migration excludes pre-timestamp rows (got: $out)" ;;
  *) ok "kimi migration excludes pre-timestamp rows" ;; esac

# ---- 15b. loud decline when lib/ cannot supply the digest; --cursor refused with --subs
# The heartbeat declines loudly when swarm_mailbox is missing (x-12); the driver
# must do the same instead of polling forever and reporting success.
NOMAIL_RUN="nomail-$$"
"$COMMS" init "$NOMAIL_RUN" >/dev/null
"$COMMS" subscribe "$NOMAIL_RUN" alpha proj >/dev/null
"$COMMS" post "$NOMAIL_RUN" gamma finding "NOMAIL-ROW" --topic proj >/dev/null
NOMAIL_TREE="$WORK/nomail-tree"
mkdir -p "$NOMAIL_TREE/bin" "$NOMAIL_TREE/lib"
cp "$COMMS" "$NOMAIL_TREE/bin/comms"
cp "$DRIVER" "$NOMAIL_TREE/bin/comms-poll-driver"
cp "$REPO/lib/swarm_arm.py" "$NOMAIL_TREE/lib/swarm_arm.py" 2>/dev/null || true
chmod +x "$NOMAIL_TREE/bin/comms" "$NOMAIL_TREE/bin/comms-poll-driver"
NOMAIL_ERR="$WORK/nomail.err"
NOMAIL_OUT="$("$NOMAIL_TREE/bin/comms-poll-driver" "$NOMAIL_RUN" alpha --subs --once -- /usr/bin/true 2>"$NOMAIL_ERR")"; rc=$?
eq "missing swarm_mailbox: driver exits 1, never 0" 1 "$rc"
eq "missing swarm_mailbox: exactly one driver-owned stderr line" 1 "$(grep -c "^comms-poll-driver: cannot derive the subscription view" "$NOMAIL_ERR")"
eq "missing swarm_mailbox: no raw traceback reaches stderr" 0 "$(grep -c "Traceback" "$NOMAIL_ERR")"
# Positive control: the real tree delivers the same row.
CTRL_OUT="$("$DRIVER" "$NOMAIL_RUN" alpha --subs --once --dry-run -- /usr/bin/true 2>&1)"
case "$CTRL_OUT" in *NOMAIL-ROW*) ok "missing swarm_mailbox control: the real tree previews the row" ;; *) bad "missing swarm_mailbox control: the real tree previews the row (got: $CTRL_OUT)" ;; esac
REFUSE_OUT="$("$DRIVER" "$NOMAIL_RUN" alpha --subs --cursor "$WORK/fixed.json" --once -- /usr/bin/true 2>&1)"; rc=$?
eq "--cursor with --subs is refused as a usage error" 2 "$rc"
case "$REFUSE_OUT" in *"--cursor cannot be combined with --subs"*) ok "--cursor with --subs names the reason" ;; *) bad "--cursor with --subs names the reason (got: $REFUSE_OUT)" ;; esac
assert "--cursor with --subs writes no cursor file" "$([ ! -e "$WORK/fixed.json" ]; echo $?)"

# The kimi adapter derives a digest at startup too: same loud decline, no traceback.
KNOMAIL_TREE="$WORK/knomail-tree"
mkdir -p "$KNOMAIL_TREE/bin" "$KNOMAIL_TREE/lib" "$KNOMAIL_TREE/adapters/kimi"
cp "$COMMS" "$KNOMAIL_TREE/bin/comms"; cp "$DRIVER" "$KNOMAIL_TREE/bin/comms-poll-driver"
cp "$KIMI_DRIVER" "$KNOMAIL_TREE/adapters/kimi/poll-driver.sh"
chmod +x "$KNOMAIL_TREE/bin/comms" "$KNOMAIL_TREE/bin/comms-poll-driver" "$KNOMAIL_TREE/adapters/kimi/poll-driver.sh"
KNOMAIL_ERR="$WORK/knomail.err"
"$KNOMAIL_TREE/adapters/kimi/poll-driver.sh" "$NOMAIL_RUN" alpha sess-x "$WORK" --once >/dev/null 2>"$KNOMAIL_ERR"; rc=$?
eq "kimi adapter missing swarm_mailbox: exits 1" 1 "$rc"
eq "kimi adapter missing swarm_mailbox: one adapter-owned stderr line" 1 "$(grep -c "^kimi poll-driver: cannot derive the subscription view" "$KNOMAIL_ERR")"
eq "kimi adapter missing swarm_mailbox: no raw traceback" 0 "$(grep -c "Traceback" "$KNOMAIL_ERR")"

# ---- 16. isolation control: nothing leaked outside the temp dirs -----------
if [ -e "$HOME/.comms/state/poll-driver/$RUN" ] || [ -e "/tmp/comms-$RUN" ] \
   || [ -e "$HOME/.comms/state/kimi-cursor/$RUN-zeta" ]; then
  bad "state leaked outside the temp dirs"
else
  ok "no state outside COMMS_STATE_DIR / COMMS_ROOT"
fi

echo "poll driver test: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
