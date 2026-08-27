#!/bin/bash
# test_comms_cli.sh -- smoke test for the comms dispatcher (bin/comms).
#
# Proves, per subcommand, that the wrapper reaches the RIGHT module and
# PRESERVES that module's exit code. All state is isolated: COMMS_STATE_DIR
# (read by swarm_arm.py and, through it, swarm_claims.py) and COMMS_ROOT
# (read by swarm_mailbox.py) both point at mktemp dirs, so nothing here touches
# real arm state, real mailboxes, or the real heartbeat's cursors.
#
# The one subcommand NOT checked here is `cursor` (the confirmed-delivery pair
# for shell drivers): it is routed through this same dispatcher and exercised
# end to end, receipts and all, in tests/test_poll_driver.sh, which is where its
# semantics live. Adding a duplicate stub here would pin the route twice and the
# meaning nowhere.
#
# Exit: 0 all passed, 1 any failed. Prints a passed/failed count either way.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"        # <repo>/tests
COMMS="$SELF_DIR/../bin/comms"

export COMMS_STATE_DIR="$(mktemp -d)"
export COMMS_ROOT="$(mktemp -d)"
trap 'rm -rf "$COMMS_STATE_DIR" "$COMMS_ROOT"' EXIT

PASS=0
FAIL=0

# check <desc> <expected_rc> <actual_rc> [output] [required_substring]
check() {
  local desc="$1" want="$2" got="$3" out="${4-}" need="${5-}"
  if [ "$got" -ne "$want" ]; then
    echo "FAIL: $desc (rc=$got, wanted $want)"
    FAIL=$((FAIL + 1))
    return
  fi
  if [ -n "$need" ] && ! printf '%s' "$out" | grep -qF -- "$need"; then
    echo "FAIL: $desc (rc ok, output missing: $need)"
    FAIL=$((FAIL + 1))
    return
  fi
  echo "ok:   $desc"
  PASS=$((PASS + 1))
}

RUN="commstest-$$"

# ---- wrapper's own contract ------------------------------------------------
out="$("$COMMS" no-such-subcommand 2>&1)"; rc=$?
check "unknown subcommand exits 2 with usage" 2 "$rc" "$out" "usage: comms"
out="$("$COMMS" 2>&1)"; rc=$?
check "no arguments exits 2 with usage" 2 "$rc" "$out" "usage: comms"

# ---- mailbox routing (swarm_mailbox.py honors COMMS_ROOT) ------------------
out="$("$COMMS" init "$RUN" 2>&1)"; rc=$?
check "init reaches mailbox, honors COMMS_ROOT" 0 "$rc" "$out" "$COMMS_ROOT/comms-$RUN"
out="$("$COMMS" post "$RUN" alpha bogus-kind "x" 2>&1)"; rc=$?
check "post invalid kind preserves mailbox exit 1" 1 "$rc" "$out" "invalid kind"
out="$("$COMMS" post "$RUN" alpha banana "x" 2>&1)"; rc=$?
check "post unknown kind banana still fails loud" 1 "$rc" "$out" "invalid kind"
out="$("$COMMS" post "$RUN" alpha comment "mid-run comment" --topic t0 2>&1)"; rc=$?
check "post kind comment exits 0" 0 "$rc" "$out" "mid-run comment"
out="$("$COMMS" post "$RUN" alpha reply "mid-run reply" --topic t0 2>&1)"; rc=$?
check "post kind reply exits 0" 0 "$rc" "$out" "mid-run reply"
out="$("$COMMS" post "$RUN" alpha status "progress note" --topic t0 2>&1)"; rc=$?
check "post kind status exits 0" 0 "$rc" "$out" "progress note"
out="$("$COMMS" post "$RUN" alpha finding "hello-topic" --topic t1 2>&1)"; rc=$?
check "post valid finding exits 0" 0 "$rc" "$out" "hello-topic"
out="$("$COMMS" read "$RUN" beta --topic t1 2>&1)"; rc=$?
check "read from sibling seat sees the row" 0 "$rc" "$out" "hello-topic"
out="$("$COMMS" subscribe "$RUN" beta t1 2>&1)"; rc=$?
check "subscribe exits 0, echoes topic set" 0 "$rc" "$out" '"t1"'
"$COMMS" post "$RUN" alpha finding "off-slice" --topic other >/dev/null 2>&1
out="$("$COMMS" subs "$RUN" beta 2>&1)"; rc=$?
if [ $rc -eq 0 ] && printf '%s' "$out" | grep -qF "hello-topic" \
   && ! printf '%s' "$out" | grep -qF "off-slice"; then
  echo "ok:   subs returns subscribed slice only"; PASS=$((PASS + 1))
else
  echo "FAIL: subs returns subscribed slice only (rc=$rc)"; FAIL=$((FAIL + 1))
fi
out="$("$COMMS" read "$RUN" 2>&1)"; rc=$?
check "read with missing seat preserves mailbox exit 1" 1 "$rc" "$out" "read needs"

# ---- cursor-free NDJSON feed (S-W2 window interface) ----------------------
dir_checksum() {
  local dir="$1"
  if [ ! -d "$dir" ]; then printf 'MISSING'; return; fi
  (cd "$dir" && find . -type f -print0 | sort -z | xargs -0 shasum -a 256) \
    | shasum -a 256 | awk '{print $1}'
}
feed_capture() { # feed_capture <output-var> <rc-var> <args...>
  local outvar="$1" rcvar="$2" before_read before_swarm after_read after_swarm value code
  shift 2
  before_read="$(dir_checksum "$COMMS_STATE_DIR/read-cursor")"
  before_swarm="$(dir_checksum "$COMMS_STATE_DIR/swarm-cursor")"
  value="$("$COMMS" feed "$@" 2>&1)"; code=$?
  after_read="$(dir_checksum "$COMMS_STATE_DIR/read-cursor")"
  after_swarm="$(dir_checksum "$COMMS_STATE_DIR/swarm-cursor")"
  # The cursor guard only counts when the command did something: a feed that
  # exits non-zero and prints nothing also moves no cursor, so require rc 0 and
  # at least one emitted line unless the caller marked the case as expected-error
  # by passing FEED_EXPECT_ERROR=1.
  if [ "${FEED_EXPECT_ERROR:-0}" != "1" ] && { [ "$code" -ne 0 ] || [ -z "$value" ]; }; then
    echo "FAIL: feed cursor guard inspected a run that produced nothing (rc=$code)"
    FAIL=$((FAIL + 1))
  elif [ "$before_read" != "$after_read" ] || [ "$before_swarm" != "$after_swarm" ]; then
    echo "FAIL: feed leaves read/swarm cursor directories byte-identical"
    FAIL=$((FAIL + 1))
  else
    echo "ok:   feed leaves read/swarm cursor directories byte-identical"
    PASS=$((PASS + 1))
  fi
  printf -v "$outvar" '%s' "$value"
  printf -v "$rcvar" '%s' "$code"
}

FEED_RUN="commstest-feed-$$"
"$COMMS" init "$FEED_RUN" >/dev/null
"$COMMS" subscribe "$FEED_RUN" B topic-b >/dev/null
"$COMMS" post "$FEED_RUN" A finding "feed finding" --topic topic-b >/dev/null
"$COMMS" post "$FEED_RUN" A comment "threaded comment" --topic other --thread doc:repo/file.py >/dev/null
"$COMMS" post "$FEED_RUN" A reply "private reply" --to B >/dev/null
"$COMMS" post "$FEED_RUN" C status "feed status" --topic topic-b >/dev/null

feed_capture out rc "$FEED_RUN"
check "feed emits the four-row scratch run" 0 "$rc" "$(printf '%s\n' "$out" | wc -l | tr -d ' ')" "4"
schema_out="$(printf '%s\n' "$out" | python3 -c '
import json, sys
items = [json.loads(line) for line in sys.stdin if line.strip()]
assert [item["row"]["at"] for item in items] == sorted(item["row"]["at"] for item in items)
assert all(set(item) == {"run", "row", "render"} for item in items)
assert all(set(item["render"]) == {"author", "body", "title", "lane"} for item in items)
assert all(item["run"] == sys.argv[1] for item in items)
assert [item["row"]["text"] for item in items] == ["feed finding", "threaded comment", "private reply", "feed status"]
assert [item["render"]["lane"] for item in items] == ["board", "board", "convo", "status"]
print("schema-ok")
' "$FEED_RUN" 2>&1)"; rc=$?
check "feed schema preserves raw rows, fixed keys, order, and mailbox-vocabulary lanes" 0 "$rc" "$schema_out" "schema-ok"

feed_capture out rc "$FEED_RUN" --audience everyone
everyone_body="$(printf '%s\n' "$out" | python3 -c '
import json, sys
print(next(item["render"]["body"] for item in map(json.loads, sys.stdin) if item["row"]["text"] == "feed finding"))
' 2>&1)"; body_rc=$?
check "feed everyone audience uses shared plain-language table" 0 "$body_rc" "$everyone_body" "✅ Found something: feed finding"
FEED_EXPECT_ERROR=1 feed_capture out rc "$FEED_RUN" --audience operators
if [ "$rc" -eq 2 ] && printf '%s' "$out" | grep -qF engineer \
   && printf '%s' "$out" | grep -qF everyone; then
  echo "ok:   feed rejects unknown audience and names both legal values"; PASS=$((PASS + 1))
else
  echo "FAIL: feed rejects unknown audience and names both legal values (rc=$rc)"; FAIL=$((FAIL + 1))
fi

feed_capture out rc "$FEED_RUN" --seat B
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -qF "feed finding" \
   && printf '%s' "$out" | grep -qF "private reply" \
   && printf '%s' "$out" | grep -qF "feed status" \
   && ! printf '%s' "$out" | grep -qF "threaded comment"; then
  echo "ok:   feed --seat is exactly read_for's subscribed view"; PASS=$((PASS + 1))
else
  echo "FAIL: feed --seat is exactly read_for's subscribed view (rc=$rc)"; FAIL=$((FAIL + 1))
fi
since="$(printf '%s\n' "$out" | python3 -c 'import json,sys; print(json.loads(next(sys.stdin))["row"]["at"])')"
feed_capture out rc "$FEED_RUN" --since "$since"
if [ "$rc" -eq 0 ] && ! printf '%s' "$out" | grep -qF "feed finding" \
   && printf '%s' "$out" | grep -qF "threaded comment"; then
  echo "ok:   feed --since is strict"; PASS=$((PASS + 1))
else
  echo "FAIL: feed --since is strict (rc=$rc)"; FAIL=$((FAIL + 1))
fi

FEED_EXPECT_ERROR=1 feed_capture out rc "missing-feed-$$"
check "feed missing run exits 2 naming the run" 2 "$rc" "$out" "missing-feed-$$"

follow_out="$(mktemp)"
before_read="$(dir_checksum "$COMMS_STATE_DIR/read-cursor")"
before_swarm="$(dir_checksum "$COMMS_STATE_DIR/swarm-cursor")"
COMMS_FEED_INTERVAL=0.05 "$COMMS" feed "$FEED_RUN" --follow >"$follow_out" 2>&1 &
follow_pid=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do grep -qF "feed status" "$follow_out" && break; sleep 0.05; done
"$COMMS" post "$FEED_RUN" D finding "posted after follow start" --topic topic-b >/dev/null
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do grep -qF "posted after follow start" "$follow_out" && break; sleep 0.05; done
kill "$follow_pid" 2>/dev/null || true
wait "$follow_pid" 2>/dev/null || true
after_read="$(dir_checksum "$COMMS_STATE_DIR/read-cursor")"
after_swarm="$(dir_checksum "$COMMS_STATE_DIR/swarm-cursor")"
if grep -qF "posted after follow start" "$follow_out"; then
  echo "ok:   feed --follow prints a row posted after start"; PASS=$((PASS + 1))
else
  echo "FAIL: feed --follow prints a row posted after start"; FAIL=$((FAIL + 1))
fi
if [ "$before_read" = "$after_read" ] && [ "$before_swarm" = "$after_swarm" ]; then
  echo "ok:   feed --follow leaves read/swarm cursor directories byte-identical"; PASS=$((PASS + 1))
else
  echo "FAIL: feed --follow leaves read/swarm cursor directories byte-identical"; FAIL=$((FAIL + 1))
fi
rm -f "$follow_out"

# ---- read cursor (issue #33) ------------------------------------------------
# The defect: two consecutive reads for the same (runid, seat) replayed rows the
# first read already returned, while adapters/pi/README.md promised they would
# not. Fresh reader seats (gamma, delta) so the reads above cannot muddy these.
# By now alpha has posted: 3 rows on t0, "hello-topic" on t1, "off-slice" on
# other.
out="$("$COMMS" read "$RUN" gamma 2>&1)"; rc=$?
check "first read of a fresh seat sees the board" 0 "$rc" "$out" "hello-topic"
out="$("$COMMS" read "$RUN" gamma 2>&1)"; rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then
  echo "ok:   second read of the same seat returns nothing new"; PASS=$((PASS + 1))
else
  echo "FAIL: second read of the same seat returns nothing new (rc=$rc, out=$out)"
  FAIL=$((FAIL + 1))
fi
if [ -f "$COMMS_STATE_DIR/read-cursor/$RUN/gamma.all.json" ]; then
  echo "ok:   read cursor lives under COMMS_STATE_DIR"; PASS=$((PASS + 1))
else
  echo "FAIL: read cursor lives under COMMS_STATE_DIR"; FAIL=$((FAIL + 1))
fi
"$COMMS" post "$RUN" alpha finding "after-cursor" --topic t1 >/dev/null 2>&1
out="$("$COMMS" read "$RUN" gamma 2>&1)"; rc=$?
if [ $rc -eq 0 ] && printf '%s' "$out" | grep -qF "after-cursor" \
   && ! printf '%s' "$out" | grep -qF "hello-topic"; then
  echo "ok:   a row posted after the cursor is the only thing the next read shows"
  PASS=$((PASS + 1))
else
  echo "FAIL: a row posted after the cursor is the only thing the next read shows (rc=$rc)"
  FAIL=$((FAIL + 1))
fi
out="$("$COMMS" read "$RUN" gamma --replay 2>&1)"; rc=$?
check "--replay prints the whole board again" 0 "$rc" "$out" "hello-topic"
out="$("$COMMS" read "$RUN" gamma 2>&1)"; rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then
  echo "ok:   --replay leaves the cursor where it was"; PASS=$((PASS + 1))
else
  echo "FAIL: --replay leaves the cursor where it was (rc=$rc, out=$out)"
  FAIL=$((FAIL + 1))
fi

# Topic-filter interaction: a filtered read owns its OWN cursor, so it can
# never mark a row on another topic delivered (that row would then be
# unreachable from any later read -- silent loss).
out="$("$COMMS" read "$RUN" delta --topic t1 2>&1)"; rc=$?
if [ $rc -eq 0 ] && printf '%s' "$out" | grep -qF "hello-topic" \
   && ! printf '%s' "$out" | grep -qF "off-slice"; then
  echo "ok:   --topic read returns that topic only"; PASS=$((PASS + 1))
else
  echo "FAIL: --topic read returns that topic only (rc=$rc)"; FAIL=$((FAIL + 1))
fi
out="$("$COMMS" read "$RUN" delta --topic t1 2>&1)"; rc=$?
if [ $rc -eq 0 ] && [ -z "$out" ]; then
  echo "ok:   second --topic read returns nothing new"; PASS=$((PASS + 1))
else
  echo "FAIL: second --topic read returns nothing new (rc=$rc, out=$out)"
  FAIL=$((FAIL + 1))
fi
out="$("$COMMS" read "$RUN" delta 2>&1)"; rc=$?
if [ $rc -eq 0 ] && printf '%s' "$out" | grep -qF "off-slice" \
   && printf '%s' "$out" | grep -qF "progress note"; then
  echo "ok:   a --topic read never consumes another topic's rows"; PASS=$((PASS + 1))
else
  echo "FAIL: a --topic read never consumes another topic's rows (rc=$rc)"
  FAIL=$((FAIL + 1))
fi

# ---- post short form / env defaults (issue #41) ----------------------------
# COMMS_RUN / COMMS_SEAT let `comms post <kind> <text> --to ...` work without
# spelling out <runid> <seat>. Isolated to its own runid/seat so it cannot be
# confused with the explicit-form rows already posted on $RUN above.
out="$("$COMMS" 2>&1)"
check "usage text documents --thread" 2 "$?" "$out" "--thread"
RS="commstest-short-$$"
out="$(COMMS_RUN="$RS" COMMS_SEAT=shortseat "$COMMS" post reply "short-form reply" --to othr 2>&1)"; rc=$?
check "post short form (2 positionals) exits 0, env defaults fill runid/seat" 0 "$rc" "$out" "short-form reply"
out="$(cat "$COMMS_ROOT/comms-$RS/shortseat.jsonl" 2>&1)"
check "short form wrote to COMMS_RUN's mailbox under COMMS_SEAT's own file" 0 0 "$out" '"seat": "shortseat"'
check "short form row carries --to" 0 0 "$out" '"to": "othr"'
out="$(COMMS_RUN="$RS" COMMS_SEAT=shortseat "$COMMS" post reply "threaded reply" --to othr --thread doc:x 2>&1)"; rc=$?
check "post short form with --thread exits 0" 0 "$rc" "$out" "threaded reply"
check "short form row carries --thread's key" 0 0 "$(cat "$COMMS_ROOT/comms-$RS/shortseat.jsonl")" '"thread": "doc:x"'
out="$(env -u CLAUDE_SESSION_ID COMMS_RUN="$RS" "$COMMS" post reply "no seat available" --to othr 2>&1)"; rc=$?
check "post short form with no COMMS_SEAT and no session id fails loud" 1 "$rc" "$out" "COMMS_SEAT"
out="$("$COMMS" post "$RUN" alpha reply "explicit form unaffected" --topic t0 2>&1)"; rc=$?
check "post explicit 4-positional form still works unchanged" 0 "$rc" "$out" "explicit form unaffected"

# ---- threads metric routing (swarm_threads.py; issue #43) ------------------
# One smoke check: a FRESH, empty COMMS_ROOT (not the shared one above, which
# already has a --thread row from the post-short-form block) has zero
# threaded rows anywhere, which is the metric's own positive control (a
# metric that inspected nothing is never a quiet, healthy exit 0) -- the
# fixture-driven cases (alive/seats/--run/--json) already live in
# tests/test_swarm_threads.py, so this only proves the WRAPPER reaches that
# module and preserves its exit code, not the predicate's own logic (same
# division of labor as every other case here).
EMPTY_ROOT="$(mktemp -d)"
out="$(COMMS_ROOT="$EMPTY_ROOT" "$COMMS" threads 2>&1)"; rc=$?
check "threads on an empty mailbox preserves the positive control, exit 2" \
  2 "$rc" "$out" "inspected nothing"
rm -rf "$EMPTY_ROOT"

# ---- claims routing BEFORE arming (the not-armed edge) ---------------------
out="$("$COMMS" claim "$RUN" alpha /tmp/x 2>&1)"; rc=$?
check "claim on unarmed run preserves claims exit 3" 3 "$rc" "$out" "not-armed"
out="$("$COMMS" enroll "$RUN" 2>&1)"; rc=$?
check "enroll on unarmed run preserves arm exit 1" 1 "$rc" "$out" "not-armed"

# ---- arm routing (swarm_arm.py honors COMMS_STATE_DIR) ---------------------
out="$("$COMMS" arm "$RUN" --topic t1 2>&1)"; rc=$?
check "arm exits 0, honors COMMS_STATE_DIR" 0 "$rc" "$out" "$COMMS_STATE_DIR/swarm-arm/$RUN"
out="$("$COMMS" status "$RUN" 2>&1)"; rc=$?
check "status of armed run exits 0, reports armed" 0 "$rc" "$out" '"armed": true'
out="$("$COMMS" status 2>&1)"; rc=$?
check "bare status lists armed runs" 0 "$rc" "$out" "$RUN"
out="$("$COMMS" enroll "$RUN" --agent-id agent-a --topics t1 --seat alpha 2>&1)"; rc=$?
check "enroll with agent-id on armed run exits 0" 0 "$rc" "$out" "enrolled"
out="$("$COMMS" enroll "$RUN" 2>&1)"; rc=$?
check "marker-only enroll on armed run exits 0" 0 "$rc" "$out" "enrollment signalled"
out="$("$COMMS" enroll "$RUN" --agent-id agent-k --seat kimi1 \
      --model "Kimi K3" --project agent-os --area hooks/ 2>&1)"; rc=$?
check "enroll with identity metadata exits 0" 0 "$rc" "$out" "enrolled"
out="$(python3 -c "
import sys, json
sys.path.insert(0, '$SELF_DIR/../lib')
import swarm_arm
print(json.dumps(swarm_arm.seat_identities('$RUN'), sort_keys=True))
" 2>&1)"; rc=$?
check "identity metadata roundtrips through the roster" 0 "$rc" "$out" \
      '"kimi1": {"area": "hooks/", "model": "Kimi K3", "project": "agent-os"}'

# ---- claims routing on the armed run ---------------------------------------
out="$("$COMMS" claim "$RUN" alpha /tmp/x 2>&1)"; rc=$?
check "first claim wins, exits 0" 0 "$rc" "$out" "OK"
out="$("$COMMS" claim "$RUN" beta /tmp/x 2>&1)"; rc=$?
check "conflicting claim preserves exit 1, names holder" 1 "$rc" "$out" "HELD BY alpha"
out="$("$COMMS" who "$RUN" /tmp/x 2>&1)"; rc=$?
check "who names the holder" 0 "$rc" "$out" "alpha"
out="$("$COMMS" who 2>&1)"; rc=$?
check "who with no runid preserves claims usage exit 2" 2 "$rc" "$out" "usage: swarm_claims"
out="$("$COMMS" release "$RUN" beta /tmp/x 2>&1)"; rc=$?
check "release by non-owner refused, exit 1" 1 "$rc" "$out" "REFUSED"
out="$("$COMMS" release "$RUN" alpha /tmp/x 2>&1)"; rc=$?
check "release by owner exits 0" 0 "$rc" "$out" "RELEASED"
out="$("$COMMS" reap "$RUN" alpha 2>&1)"; rc=$?
check "reap of nothing preserves exit 1" 1 "$rc" "$out" "reaped 0"
"$COMMS" claim "$RUN" alpha /tmp/y >/dev/null 2>&1
out="$("$COMMS" reap "$RUN" alpha 2>&1)"; rc=$?
check "reap of a held claim exits 0" 0 "$rc" "$out" "/tmp/y"

# ---- disarm -----------------------------------------------------------------
out="$("$COMMS" disarm "$RUN" 2>&1)"; rc=$?
check "disarm exits 0" 0 "$rc"
out="$("$COMMS" status "$RUN" 2>&1)"; rc=$?
check "status after disarm reports unarmed" 0 "$rc" "$out" '"armed": false'

# ---- symlink invocation: PATH installs are symlinks, and dirname of the ----
# symlink points at the wrong tree unless the wrapper chases readlink first
# (defect caught live by the verifier seat, run comms-build-0821)
LINKDIR="$(mktemp -d)"
ln -s "$COMMS" "$LINKDIR/comms"
out="$("$LINKDIR/comms" status "$RUN" 2>&1)"; rc=$?
check "symlinked invocation still finds lib/" 0 "$rc" "$out" '"armed": false'
rm -rf "$LINKDIR"

# ---- isolation control: nothing leaked outside the temp dirs ---------------
if [ -e "$HOME/.comms/state/swarm-arm/$RUN" ] || [ -e "/tmp/comms-$RUN" ] \
   || [ -e "$HOME/.comms/state/read-cursor/$RUN" ]; then
  echo "FAIL: state leaked outside the temp dirs"; FAIL=$((FAIL + 1))
else
  echo "ok:   no state outside COMMS_STATE_DIR / COMMS_ROOT"
  PASS=$((PASS + 1))
fi

echo "comms CLI smoke test: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
