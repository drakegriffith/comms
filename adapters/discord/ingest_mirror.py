#!/usr/bin/env python3
"""adapters/discord/ingest_mirror.py -- tail the swarm-heartbeat telemetry
log and post a "read" event to the convo Discord channel each time the
heartbeat hook actually delivers new mailbox rows into an enrolled agent's
context.

WHY A SEPARATE MODULE (not folded into mirror.py's row-mirroring path):
mirror.py mirrors MAILBOX rows (agent posts -> mailbox). This module mirrors
HEARTBEAT TELEMETRY rows (mailbox -> agent's context) -- a different source
file (a machine-local log, not the run mailbox), a different event shape
(counts, not a text row), and a different verb ("heard", not "posted").
Keeping it a separate module keeps mirror.py's already-large surface from
growing a second, unrelated tailer; the two share render/transport helpers
(post_content, build_author, machine_label, the convo lane's state dir and
missing-secret handling) via a plain `import mirror`.

LOG FORMAT (read from adapters/claude-code/swarm-heartbeat.sh's
append_telemetry(), verified against that file's source on 2026-08-24 -- not
guessed): one JSON object per line in
$COMMS_STATE_DIR/swarm-heartbeat.log --

    {"at": <iso8601>, "agent_id": <str>, "runid": <str>, "topic": <str>,
     "rows_inspected": <int>, "delta_emitted": <int>, "short_circuit": <bool>}

"delta_emitted" is the count of NEW mailbox rows the hook actually injected
into that agent's context on that beat (0 when nothing was new, or a
short-circuit skipped the read entirely). Only delta_emitted > 0 lines are
delivery events; every other line is heartbeat's own "silence is not
evidence" bookkeeping and is skipped here without posting anything.

WHAT THE LOG DOES NOT CARRY: which sibling seat(s) posted the delivered
rows -- only a count. Fabricating that would be a guess wearing a number, so
this module instead RE-DERIVES it by replaying the exact selection
swarm-heartbeat.sh's process_run() makes for that (runid, agent_id): the
per-agent topic/seat filter (swarm_arm.participant_sub), the one mailbox
parser (swarm_mailbox.read_siblings, which already excludes the reader's own
seat file), sorted by "at", capped at CAP (must track process_run's CAP).
The lower bound of the replay is THIS module's OWN persisted "last
attributed row timestamp" per (runid, agent_id) -- never swarm-heartbeat's
live swarm-cursor files, which may already be far ahead of the log line
being processed by the time this tailer runs; reading them would race.

The reconstructed row count is checked against delta_emitted as a positive
control (an enumerator that is never checked is a guess wearing a number):
a mismatch is named on stderr and the reconstruction is used as-is -- never
silently trusted, never fabricated to force agreement.

CURSOR: a plain byte offset into the heartbeat log, one file
($COMMS_STATE_DIR/discord-mirror-convo/heartbeat-ingest.cursor), written via
tmp + os.replace with a PID-suffixed tmp name -- same safety shape as
mirror.py's row cursor. A SEPARATE small file in the same directory
(heartbeat-ingest.attrib.json) holds the per-(runid, agent_id) replay
watermark described above; it is bookkeeping for reconstruction, not part of
the byte-offset contract. If the log has rotated or been truncated (offset
greater than the file's current size), the byte offset resets to 0 -- the
attrib watermarks are untouched by a reset (they are keyed on mailbox row
"at" values, which do not change if the log rotates).

AGGREGATION: one Discord message per delivery event (per BEAT), never per
mailbox row -- a beat that injected 4 rows is one "read 4 row(s)" post.

LAUNCHD SAFETY: same semantics as mirror.py's follow()/follow_all() -- a
missing convo-lane webhook secret does not exit; it warns once and backs off
60s (mirror.MISSING_SECRET_RETRY_SECONDS) instead of crash-looping under a
launchd KeepAlive job. A per-pass exception is caught, named on one stderr
line, and swallowed so it never kills the loop.

WIRE-UP: `mirror.py --follow-all --lane convo` runs this module's tail_once
once per pass, in the same process (see mirror.py's follow_all docstring,
INGEST WIRE-UP) -- no second launchd job. This module also has its own
--once/--follow CLI for a standalone run or direct testing.

CLI:
  ingest_mirror.py --once
  ingest_mirror.py --follow [--interval N]
Exit: 0 delivered (or nothing new) | 1 some post(s) failed | 2 missing
      convo-lane webhook secret (--once only).
"""

import json
import os
import sys
import time

SELF_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SELF_DIR))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
sys.path.insert(0, SELF_DIR)
import swarm_arm  # noqa: E402
import swarm_mailbox  # noqa: E402
import mirror  # noqa: E402  (post_content, build_author, machine_label, lane state dir, secret handling)

LANE = "convo"

# Must match adapters/claude-code/swarm-heartbeat.sh's CAP -- the per-beat
# delivery truncation this module replays when reconstructing sender seats.
CAP = 10

# Placeholder passed to swarm_mailbox.read_siblings when an agent enrolled
# without declaring --seat (process_run's own-seat exclusion is then also a
# no-op, see _reconstruct_delta): a real seat name is required (empty raises
# ValueError), and this string is reserved the same way mirror.py reserves
# "discord-mirror" -- do not name a real seat this.
_NO_SEAT_PLACEHOLDER = "ingest-mirror-no-seat"

CURSOR_NAME = "heartbeat-ingest.cursor"      # byte offset into the log
ATTRIB_NAME = "heartbeat-ingest.attrib.json"  # per (runid, agent_id) replay watermark


def _state_dir():
    return os.environ.get("COMMS_STATE_DIR") or os.path.expanduser("~/.comms/state")


def _log_path():
    return os.path.join(_state_dir(), "swarm-heartbeat.log")


def _mirror_dir():
    # Reuses mirror.py's own convo-lane state dir resolver -- one place that
    # knows where the convo lane's state lives, never re-derived here.
    return mirror._mirror_dir(LANE)


def _cursor_path():
    return os.path.join(_mirror_dir(), CURSOR_NAME)


def _attrib_path():
    return os.path.join(_mirror_dir(), ATTRIB_NAME)


def _tmp_path(path):
    # PID-suffixed, same reasoning as mirror.py's _cursor_tmp_path: two
    # pollers racing on the same file still each get their own tmp name.
    return path + ".tmp." + str(os.getpid())


def _load_offset():
    try:
        with open(_cursor_path()) as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _save_offset(offset):
    os.makedirs(_mirror_dir(), exist_ok=True)
    path = _cursor_path()
    tmp = _tmp_path(path)
    with open(tmp, "w") as fh:
        fh.write(str(offset))
    os.replace(tmp, path)


def _load_attrib():
    try:
        with open(_attrib_path()) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_attrib(attrib):
    os.makedirs(_mirror_dir(), exist_ok=True)
    path = _attrib_path()
    tmp = _tmp_path(path)
    with open(tmp, "w") as fh:
        json.dump(attrib, fh)
    os.replace(tmp, path)


def read_new_events():
    """Return (events, new_offset): every whole JSON line appended to the
    heartbeat log since the last saved byte offset. If the log is shorter
    than the saved offset (rotated or truncated), resets to 0 and reads the
    whole current file -- never raises, never skips silently past a
    truncation. A malformed line is dropped, not fatal (mirrors
    swarm-heartbeat.sh's own tolerance of a partially-flushed line)."""
    path = _log_path()
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], 0
    offset = _load_offset()
    if offset > size:
        offset = 0
    events = []
    with open(path, "r") as fh:
        fh.seek(offset)
        chunk = fh.read()
        new_offset = fh.tell()
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events, new_offset


def _reconstruct_delta(runid, agent_id, attrib):
    """Replay swarm-heartbeat.sh's process_run() selection for this
    (runid, agent_id): topic/seat filter, own-seat exclusion (via
    swarm_mailbox.read_siblings, the one parser), sorted by `at`, bounded
    below by this module's own last-attributed watermark for this
    (runid, agent_id), capped at CAP. Returns (delta_rows, attrib_key)."""
    topics, seat = swarm_arm.participant_sub(runid, agent_id)
    subs = set(topics) if topics else None
    if subs is not None and seat:
        subs.add("@" + seat)
    # read_siblings(runid, seat) excludes <seat>.jsonl the same way
    # process_run's `r.get("seat") != seat` filter does when seat is known;
    # when seat is None (enrolled without --seat) neither excludes anything,
    # which matches process_run's `if seat:` guard.
    rows = swarm_mailbox.read_siblings(runid, seat or _NO_SEAT_PLACEHOLDER)
    if subs is not None:
        rows = [r for r in rows if (r.get("topic") or "default") in subs]
    key = runid + "\x00" + agent_id
    since = attrib.get(key, "")
    delta = [r for r in rows if (r.get("at") or "") > since]
    return delta[:CAP], key


def _distinct_seats(rows):
    seen = []
    for r in rows:
        s = r.get("seat", "?")
        if s not in seen:
            seen.append(s)
    return seen


def _receiving_author(runid, agent_id, machine, identities_cache):
    _, seat = swarm_arm.participant_sub(runid, agent_id)
    label = seat or ("agent " + agent_id[:8])
    if runid not in identities_cache:
        identities_cache[runid] = swarm_arm.seat_identities(runid)
    identity = identities_cache[runid].get(seat) if seat else None
    return mirror.build_author(label, identity, machine)


def process_events(events):
    """Turn raw telemetry lines into [(author, content), ...] posts, one per
    delivery event (delta_emitted > 0), aggregated PER BEAT -- never per
    row. Persists the replay watermark (see _reconstruct_delta) as it goes,
    then saves it once at the end."""
    posts = []
    attrib = _load_attrib()
    identities_cache = {}
    machine = mirror.machine_label()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        delta_emitted = ev.get("delta_emitted") or 0
        if not delta_emitted:
            continue
        runid = ev.get("runid")
        agent_id = ev.get("agent_id")
        if not runid or not agent_id:
            continue
        delta, key = _reconstruct_delta(runid, agent_id, attrib)
        if len(delta) != delta_emitted:
            sys.stderr.write(
                "ingest_mirror: reconstructed %d row(s) for run %r agent %r, "
                "telemetry said %d; using the reconstruction\n"
                % (len(delta), runid, agent_id, delta_emitted)
            )
        if delta:
            attrib[key] = delta[-1].get("at", attrib.get(key, ""))
        n = len(delta) if delta else delta_emitted
        seats = _distinct_seats(delta)
        seats_label = ", ".join(seats) if seats else "unknown sender(s)"
        author = _receiving_author(runid, agent_id, machine, identities_cache)
        content = "\U0001f441️ read %d row(s) from %s" % (n, seats_label)
        posts.append((author, content))
    _save_attrib(attrib)
    return posts


def tail_once(url):
    """One pass: read whatever is new in the heartbeat log, post one message
    per delivery event to the convo webhook, then advance the byte-offset
    cursor. Returns 0 all delivered (or nothing new) / 1 some post failed."""
    events, new_offset = read_new_events()
    if not events:
        return 0
    posts = process_events(events)
    rc = 0
    for author, content in posts:
        if not mirror.post_content(url, content, username=author):
            rc = 1
    _save_offset(new_offset)
    return rc


def _tail_once_logged(url):
    try:
        return tail_once(url)
    except Exception as exc:
        sys.stderr.write(
            "ingest_mirror: tail_once failed (%s); continuing\n"
            % exc.__class__.__name__
        )
        return 1


def follow(interval):
    """Poll forever. LAUNCHD SAFETY: identical shape to mirror.py's follow()
    -- a missing convo webhook secret warns once and backs off 60s instead
    of raising, so a launchd KeepAlive job waits quietly."""
    rc = 0
    while True:
        url = mirror._find_webhook_url(LANE)
        if url is None:
            mirror._warn_missing_secret(LANE)
            sleep_for = mirror.MISSING_SECRET_RETRY_SECONDS
        else:
            rc = max(rc, _tail_once_logged(url))
            sleep_for = interval
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            return rc


def main(argv):
    args = list(argv[1:])
    interval = float(os.environ.get("COMMS_MIRROR_INTERVAL", "5"))
    if "--interval" in args:
        i = args.index("--interval")
        try:
            interval = float(args[i + 1])
        except (IndexError, ValueError):
            sys.stderr.write("--interval needs a number\n")
            return 2
        del args[i : i + 2]
    if args == ["--once"]:
        url = mirror.resolve_webhook_url(LANE)  # exits 2 naming the drop-in if missing
        return tail_once(url)
    if args == ["--follow"]:
        try:
            return follow(interval)
        except KeyboardInterrupt:
            return 0
    sys.stderr.write(
        "usage: ingest_mirror.py --once\n"
        "       ingest_mirror.py --follow [--interval <seconds>]\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
