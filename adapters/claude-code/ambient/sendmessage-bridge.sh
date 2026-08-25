#!/bin/bash
# adapters/claude-code/ambient/sendmessage-bridge.sh -- PostToolUse hook body
# (tool name: SendMessage) for the AMBIENT LANE.
#
# DESIGN NOTE -- OUTBOUND ONLY, BY CONSTRUCTION. This bridge mirrors OUTBOUND
# peer traffic only: it fires on THIS session's SendMessage tool calls. It
# cannot see messages this session RECEIVES (delivery is not a tool call on
# the receiver). Both sides of a conversation appear on the machine-ops board
# exactly when BOTH sessions run this hook -- which is what the installer
# wires machine-wide. Do not mistake a one-sided transcript for a one-sided
# conversation; check whether the peer has the hook installed.
#
# WHAT IT DOES: reads the PostToolUse hook JSON from stdin (tool_input carries
# to/summary/message), and posts one row into the standing run `machine-ops`.
#
# TO-RESOLUTION (issue #41): the SendMessage `to` value is free text a human
# or agent typed -- not guaranteed to spell a real seat. This bridge tries to
# resolve it to one, in order:
#   1. EXACT SEAT MATCH: `to` is itself a key of swarm_arm.seat_identities(runid)
#      (every enrolled seat that declared identity -- session-start.sh always
#      declares one, so this covers the normal roster).
#   2. AGENT_ID PREFIX MATCH: `to` is a prefix of an enrolled participant's
#      agent_id (the roster's on-disk filename). seat_identities has no
#      agent_id -> seat mapping (lib/swarm_arm.py is a different slice's write
#      set), so this reads swarm_arm's documented participant-file layout
#      directly, once, as a fallback only; seat_identities stays the single
#      source of truth for the exact-seat path above.
#
#      AMBIGUITY: a prefix is NOT assumed unique. Every matching agent_id's
#      seat is collected; resolution only succeeds when they all agree on
#      ONE seat. More than one distinct seat is ambiguous and is treated as
#      UNRESOLVED (below), never as "pick the first sorted match" -- on a
#      live board dozens of participants can share a prefix (e.g. every
#      inbox-cockpit-XXXX session), and silently narrowing a would-be
#      fan-out to one arbitrary one of them is worse than not resolving at
#      all. This is the same doctrine this repo already states for a
#      colliding SEAT claimed by more than one agent -- see
#      lib/swarm_arm.py:183-198 (seat_identities' docstring and the
#      seat_collisions() detector it names): make the collision VISIBLE
#      instead of picking a winner nobody chose.
#   RESOLVED -> posts UNICAST: to=<seat> (never topic= at the same time --
#     post()'s own guard forbids both; topic "@<seat>" is its side effect).
#       kind comment; seat this session's seat; to the resolved seat
#       text "-> <to>: <summary, else first 200 chars of message>"
#   UNRESOLVED (no match OR ambiguous) -> today's free-text fan-out row,
#     UNCHANGED, plus one stderr line -- "unresolved target <to>" (no match)
#     or "unresolved target <to>: ambiguous prefix match" (ambiguous).
#     Telemetry only -- `to` is already written into the row text below, so
#     stderr carries no more privacy risk than the mailbox does.
#       kind comment; topic ops; seat this session's seat
#       text "-> <to>: <summary, else first 200 chars of message>"
#
# PRIVACY: only the summary field (or a 200-char truncation of the message) is
# posted -- and therefore only that reaches the Discord mirror. Full message
# bodies stay wherever SendMessage put them; this bridge never echoes message
# contents to stderr, stdout, or the log.
#
# SKIP CONDITIONS (exit 0, no row, silent):
#   * COMMS_AMBIENT_OPTOUT is non-empty -- checked FIRST, before even the
#     state dir is created. Test harnesses and the mutation gate export this.
#   * payload not parseable, or tool_name is not SendMessage (self-filter for
#     installs whose hook schema cannot match on tool name)
#   * no session id in payload or $CLAUDE_SESSION_ID (a PID-fallback
#     enrollment cannot be rederived here)
#   * session not enrolled in machine-ops (bystander sessions stay silent --
#     the same property every other run has)
# This hook NEVER blocks the tool call: every path exits 0. Real errors go to
# $COMMS_STATE_DIR/ambient.log with no payload content.
#
# ISOLATION KNOBS (tests set these; production uses the defaults):
#   COMMS_STATE_DIR      roster state + ambient.log (default ~/.comms/state)
#   COMMS_ROOT           mailbox root (default /tmp)
#   COMMS_AMBIENT_OPTOUT non-empty -> exit 0 before any write. NOTE: no
#     throwaway-cwd guard here (unlike session-start.sh) -- an already
#     enrolled session may legitimately cd into a throwaway dir mid-session,
#     and the enroll gate upstream (is_participant) already keeps a bystander
#     session silent, so a second cwd check here would be redundant.
#
# COMPLETENESS MARKER: the FINAL line of this file is
#   # hook-eof-marker v1 do-not-remove
# It is LOAD-BEARING, not a comment to tidy away: the dispatch shim
# (~/.claude/state/bin/hook-shim.sh) validates a hook file against mid-write
# tears by checking that exact final line before dispatch. Removing it makes
# the shim treat this file as torn and skip it.

set -uo pipefail

# ---- opt-out: honored BEFORE any write, even before STATE_DIR is touched --
if [ -n "${COMMS_AMBIENT_OPTOUT:-}" ]; then
  exit 0
fi

STATE_DIR="${COMMS_STATE_DIR:-$HOME/.comms/state}"

# ---- locate this repo from THIS script's resolved path --------------------
AMB_SELF="${BASH_SOURCE[0]:-$0}"
while [ -L "$AMB_SELF" ]; do
  _t="$(readlink "$AMB_SELF")"
  case "$_t" in
    /*) AMB_SELF="$_t" ;;
    *)  AMB_SELF="$(dirname "$AMB_SELF")/$_t" ;;
  esac
done
AMB_SELF_DIR="$(cd "$(dirname "$AMB_SELF")" && pwd -P)"  # <repo>/adapters/claude-code/ambient
AMB_REPO_ROOT="$(cd "$AMB_SELF_DIR/../../.." && pwd)"    # <repo>

# ---- bounded stdin read (vendored helper, one dir up) ---------------------
_amb_lib="$AMB_SELF_DIR/../stdin-bounded.sh"
if [ -r "$_amb_lib" ]; then
  . "$_amb_lib"
fi
if type read_stdin_bounded >/dev/null 2>&1; then
  read_stdin_bounded
  input="$HOOK_STDIN"
else
  input=""
fi

export AMB_PAYLOAD="$input"
export AMB_STATE_DIR="$STATE_DIR"
export AMB_SWARM_LIB="$AMB_REPO_ROOT/lib"

python3 <<'PY' || true
import datetime
import json
import os
import sys

state_dir = os.environ.get("AMB_STATE_DIR") or os.path.expanduser("~/.comms/state")

def log(msg):
    # Generic errors only -- NEVER payload content (privacy contract above).
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "ambient.log"), "a") as fh:
            at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            fh.write("%s sendmessage-bridge: %s\n" % (at, msg))
    except OSError:
        pass

try:
    sys.path.insert(0, os.environ.get("AMB_SWARM_LIB") or "")
    import swarm_arm
    import swarm_mailbox

    RUNID = "machine-ops"
    TOPIC = "ops"
    TEXT_CAP = 200

    try:
        payload = json.loads(os.environ.get("AMB_PAYLOAD", "") or "{}")
    except json.JSONDecodeError:
        sys.exit(0)  # unparseable payload: skip, never block the tool call
    if not isinstance(payload, dict):
        sys.exit(0)

    # Self-filter: correct even when the hook schema could not match on tool
    # name and this runs on every PostToolUse.
    if payload.get("tool_name") != "SendMessage":
        sys.exit(0)

    agent_id = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
    if not agent_id:
        sys.exit(0)  # PID-fallback enrollments are unreachable from here
    if not swarm_arm.is_participant(RUNID, agent_id, state_dir=state_dir):
        sys.exit(0)  # not enrolled: bystander stays silent

    _topics, seat = swarm_arm.participant_sub(RUNID, agent_id, state_dir=state_dir)
    if not seat:
        sys.exit(0)  # seatless enrollment: no own file to write

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        sys.exit(0)
    to = str(tool_input.get("to") or "?")
    summary = tool_input.get("summary")
    if summary:
        body = str(summary)
    else:
        body = str(tool_input.get("message") or "")[:TEXT_CAP]
    if not body:
        sys.exit(0)  # nothing worth a row

    def resolve_seat(candidate):
        """`candidate` -> (seat, None) on a clean resolution, (None, reason)
        otherwise ("no-match" or "ambiguous"). Exact seat match first
        (against seat_identities' keys); then agent_id prefix match (see the
        file header for why this reads the participant dir directly instead
        of calling into lib/swarm_arm.py).

        AMBIGUITY (issue #41 verifier finding on PR #52): a prefix is not
        assumed unique -- on a live board dozens of participants can share
        one prefix (e.g. every inbox-cockpit-XXXX session). The FIRST
        sorted-filename hit used to win silently, which narrowed what should
        have stayed a fan-out down to one arbitrary seat with no signal that
        anything was lost. This collects every matching agent_id's seat and
        only resolves when they all agree; more than one DISTINCT seat is
        reported as ambiguous and falls through to the unresolved path below,
        the same doctrine seat_identities' own docstring states for a
        colliding SEAT (lib/swarm_arm.py:183-198, seat_collisions()) --
        visibility over silent arbitrary choice.
        """
        roster = swarm_arm.seat_identities(RUNID, state_dir=state_dir)
        if candidate in roster:
            return (candidate, None)
        pdir = os.path.join(state_dir, "swarm-arm", RUNID, "participants")
        try:
            names = sorted(os.listdir(pdir))
        except OSError:
            return (None, "no-match")
        matched_seats = []
        for name in names:
            if not name.startswith(candidate):
                continue
            try:
                with open(os.path.join(pdir, name)) as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("seat"):
                if data["seat"] not in matched_seats:
                    matched_seats.append(data["seat"])
        if len(matched_seats) == 1:
            return (matched_seats[0], None)
        if len(matched_seats) > 1:
            return (None, "ambiguous")
        return (None, "no-match")

    text = "-> %s: %s" % (to, body)
    resolved, reason = resolve_seat(to)
    if resolved:
        swarm_mailbox.post(RUNID, seat, "comment", text, to=resolved)
    else:
        swarm_mailbox.post(RUNID, seat, "comment", text, topic=TOPIC)
        if reason == "ambiguous":
            sys.stderr.write("unresolved target %s: ambiguous prefix match\n" % to)
        else:
            sys.stderr.write("unresolved target %s\n" % to)
except SystemExit:
    raise
except Exception as exc:
    log("error: %s" % exc.__class__.__name__)  # class only, no content
sys.exit(0)
PY

exit 0
# hook-eof-marker v1 do-not-remove
