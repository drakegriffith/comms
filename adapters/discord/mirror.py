#!/usr/bin/env python3
"""discord mirror: tail one run's local comms mailbox and post each row as a
one-liner to a single Discord channel via webhook.

WHY: each machine runs its OWN local mailbox (no cross-machine file sync).
Discord is the merge point and the dashboard: every machine's mirror posts into
the same channel, so a human watching one channel sees the whole fleet's
conversation, prefixed [machine/seat] so provenance survives the merge.
(COMMS_AUDIENCE=everyone trades that machine prefix for readability on
purpose; the default keeps it. The mailbox row still carries the source.)

WHAT IS MIRRORED: every row in the run's mailbox, whatever its kind. The kind
vocabulary is deliberately NOT enforced here -- lib/swarm_mailbox.VALID_KINDS
is the write-side gate, and it is being extended on a parallel branch
(comment|reply|status). A mirror that hardcoded the vocabulary would silently
drop the new kinds; this one renders whatever the row carries.

RENDERING (human-readable, three visible verbs): a mailbox row renders as
TWO parts -- an author (Discord webhook `username`, one seat's identity per
POST) and a content string (an emoji-prefixed one-liner). See build_author,
build_content, format_row.

  author:  "<seat> · <model> on <project> (<machine>)", or, without
           enrollment identity (lib/swarm_arm --model/--project/--area),
           "<seat> (<machine>)". Sanitized against @everyone/@here and
           zero-width characters (see comms_render) -- identity is
           display-only prose and never gates anything.
  content: one leading emoji chosen by the row's shape (see comms_render and
           build_content's docstring) -- the "posted to mailbox" verb. The
           "agent born" verb (the ambient session-started status row) and
           the "heard from mailbox" verb (posted by adapters/discord/
           ingest_mirror.py from the heartbeat telemetry log, NOT this
           module's mailbox tail) round out the three visible verbs.

  audience: COMMS_AUDIENCE picks the vocabulary all of the above speak,
           "engineer" (default, everything described here) or "everyone"
           (plain sentences, no machine names or paths). See the audience
           section below; the mailbox rows never change.

Because a single POST's Discord `username` is one value, rows batched into
one message must share an author -- chunk_rows batches per (seat), never
mixing two seats into the same POST even when they would fit under the
content cap.

WHAT IS NOT MIRRORED: claims, arming, subscriptions, cursors -- machine-local
state stays machine-local. Command direction (Discord -> machine) is out of
scope; durable commands go through the GitHub board.

READ PATH: reuses swarm_mailbox.read_siblings (never a second parser), with
with_source=True so each row carries the identity of the file it came out of.
read_siblings excludes the named seat's own file, so the mirror reads as a
reserved observer seat ("discord-mirror") that never posts; it therefore sees
every real seat's rows. Do not name a real seat "discord-mirror" -- its rows
would be invisible to the mirror by construction.

PULLED ROWS ARE NOT MIRRORED (issue #20): the run dir also holds
`remote~<hub>.jsonl`, the file adapters/remote appends rows PULLED off another
machine to. Those rows are copies of hub rows the hub's own mirror already
posted to this same channel, so posting them here posts everything twice, once
per machine. They are dropped by the keep-predicate and still counted against
the cursor (count-but-skip, the same shape the lane filter uses). The
discriminator is the SOURCE FILE, never the seat string: a row this machine
PUSHED lands on the hub as a first-class `alpha~macbook.jsonl` whose only
mirror is that hub's, so "skip any seat with a ~" would silence exactly the
rows that most need posting.

CURSOR: per-run JSON file in $COMMS_STATE_DIR/discord-mirror/ mapping
"<seat>/<source file>#<inode>" -> count of that seat's rows already mirrored
FROM THAT FILE. Seat files are append-only with one writer, so a per-file row
count is a stable cursor: restarts never repost, new rows are exactly
rows[count:]. Keying on the seat ALONE was issue #23 -- one seat can own rows
in two files at once (its own and the pull mirror), and a pulled row with an
older `at` then shifts the merged sequence and re-posts a delivered row. The
inode makes the key an identity rather than a name, so a purged and re-created
seat file starts a fresh count instead of skipping its rows. A cursor file in
the old {seat: count} shape is read, honored, and migrated in place on the
next poll (swarm_mailbox.fresh_rows_by_seat). Written via tmp + os.replace so
a crash never leaves a half-written cursor.

CONCURRENCY (enforced, not advisory): every pass takes an exclusive
fcntl.flock on <runid>.lock in the lane's state dir, so exactly one poller
owns a (runid, lane) at a time. A second poller does NOT block or double-post:
it writes one stderr line and returns 0, and the next poll picks the rows up
(delayed, never dropped). The lock is per (runid, lane) and held for one pass
only -- unrelated runs never contend, --follow-all can walk the whole fleet
serially, and an ad-hoc --once can slot between a follower's polls.

FAILURE: a row that cannot be delivered after the retry budget is NEVER
dropped silently -- it is written to <runid>.skipped.jsonl in the state dir
(flushed and fsynced before the cursor moves past it, so a crash cannot keep
the newer cursor and lose the record) and shouted to stderr, then the cursor
advances past it (the skipped file is the durable record; re-posting forever
would wedge the mirror behind one bad batch). 429 honours Retry-After.

HOW MANY TIMES A ROW POSTS: exactly once across concurrent pollers (the lock,
see CONCURRENCY below), at least once across a crash. The cursor is saved
after the posts, so a crash between a delivered chunk and that save re-posts
the chunk next pass. Deliberate: the alternative direction loses rows
invisibly. See run_once.

SECRET: DISCORD_COMMS_WEBHOOK_URL from the environment, else parsed from
$COMMS_SECRETS_FILE (default ~/.secrets/comms.env). The URL is never printed,
logged, or echoed. Missing -> exit 2 naming the exact drop-in line.

LANES: --lane names which Discord channel a pass targets. The default lane
("all", used when --lane is omitted) is the original single-channel behavior,
byte-identical: format, secret var (DISCORD_COMMS_WEBHOOK_URL), state dir, and
row set are all unchanged. The "convo" lane mirrors agent-to-agent
conversation only -- a row is conversation iff its topic starts with "@"
(a unicast, see swarm_mailbox SELF_TOPIC_PREFIX) OR its kind is "comment" or
"reply" -- to a SEPARATE webhook (DISCORD_COMMS_CONVO_WEBHOOK_URL) and a
SEPARATE state dir (discord-mirror-convo/), so the two lanes never share a
cursor or a skipped-rows log. A row that does not match the convo lane's
filter is still counted against the cursor (the cursor is a per-seat row
count over ALL rows, filter or no) -- otherwise a run with a mix of chatter
and plain findings would re-scan its non-convo rows on every pass forever.
Lanes are otherwise identical: same batching, same retry, same skip-and-log
failure path, just parameterized by lane name -- with ONE exception, the
board lane, below.

BOARD LANE (issue #40): mirrors rows that name the DOCUMENT they are about
(a `thread` field, written by swarm_mailbox.post out of thread_key) into a
Discord FORUM channel, one thread per document, through
DISCORD_COMMS_FORUM_WEBHOOK_URL and the discord-mirror-board/ state dir. Two
things make it unlike every other lane:

  WHEN IT POSTS. A row is not posted on arrival. It is HELD until its
  document's conversation is alive -- lib/swarm_threads.alive: at least
  COMMS_THREAD_ALIVE_SEATS distinct non-status seats, and some consecutive
  pair from two different seats no more than COMMS_THREAD_ALIVE_SECONDS
  apart. Without that gate, every document a single agent so much as
  mentioned opens a thread, and a board of one-line threads is a board
  nobody reads. That gate is ONE-WAY: it decides whether to OPEN a thread,
  never whether to deliver into one that exists, and the thread map is the
  record of the transition (see _drain). Alive is judged per (run, lane) in
  v1 -- design note D2 -- so a document needs two seats within ONE run to
  open its thread; the map is fleet-wide, so once any run opens it, every
  run's rows land in it. A row with NO thread is count-but-skipped, exactly
  as the convo lane skips a non-conversation row: a forum webhook has no
  un-threaded destination, and the `all` lane already mirrors those rows.

  WHAT IT REMEMBERS. A second state file, <runid>.held.json, shape
  {thread_key: [rows]} in `at` order -- "what have I not yet posted", which
  the cursor structurally cannot answer ("what have I read"). It is written
  BEFORE the cursor advances on every pass, and that order is what makes
  advancing the cursor safe. See _board_pass for the whole per-pass order and
  why each step sits where it does, and adapters/discord/threads.py for the
  fleet-wide map that turns a thread key into a Discord thread id.

LAUNCHD SAFETY: --follow and --follow-all run under a launchd KeepAlive job,
which restarts a job that exits nonzero -- so a job that exits on every poll
of a not-yet-configured secret crash-loops forever instead of waiting quietly
for the human to drop the key in. --once is a one-shot invocation (a human or
a script checking the result), so it keeps the loud exit 2. --follow and
--follow-all instead catch the missing-secret condition, write ONE stderr
line (not the multi-line drop-in -- that would spam a log file once per
poll), and retry in 60s instead of the normal --interval, without raising.

CLI:
  mirror.py --once <runid> [--lane <name>]
  mirror.py --follow <runid> [--interval N] [--lane <name>]   # poll loop (default 5s)
  mirror.py --follow-all [--interval N] [--lane <name>]       # poll EVERY run under the mailbox root
Exit: 0 mirrored (or nothing new) | 1 some rows skipped after retries |
      2 usage or missing webhook secret (--once only).
"""

import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.request

SELF_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SELF_DIR))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
sys.path.insert(0, SELF_DIR)  # so `import threads` works however this is loaded
import comms_machine  # noqa: E402  (machine_label, re-exported below)
import comms_render  # noqa: E402  (runtime-agnostic audience vocabulary)
import swarm_arm  # noqa: E402  (one roster reader; see IDENTITY below)
import swarm_mailbox  # noqa: E402  (one parser; see READ PATH above)
import swarm_threads  # noqa: E402  (the alive predicate; shared with bin/comms)
import threads  # noqa: E402  (the thread map; see BOARD LANE below)

# Reserved observer seat name handed to read_siblings so the mirror sees every
# REAL seat's rows (read_siblings excludes only the named seat's own file).
OBSERVER_SEAT = "discord-mirror"

TEXT_CAP = 300  # chars of row text per line
# Discord hard-caps message content at 2000 chars; stay under with headroom.
CONTENT_CAP = 1900
MAX_RETRIES = 3  # additional attempts after the first
SECRET_VAR = "DISCORD_COMMS_WEBHOOK_URL"
DEFAULT_LANE = "all"

# The forum board's webhook var, resolved through the SAME env-or-secrets
# path as every other lane var (see _find_webhook_url_for_var). It IS a lane
# now -- the board lane, below. Slice 1 deliberately kept it out of
# LANE_SECRET_VARS because a forum channel needs thread_name / ?thread_id=
# and nothing could post that shape yet; this slice is what changes that.
# The LANE is named "board" (what it is to a human), not "forum" (Discord's
# channel type), so `--lane forum` remains an unknown lane.
FORUM_SECRET_VAR = "DISCORD_COMMS_FORUM_WEBHOOK_URL"

BOARD_LANE = "board"

# Lane name -> secret var. The default lane keeps the pre-lane var so an
# un-flagged invocation is byte-identical to before this feature existed.
LANE_SECRET_VARS = {
    DEFAULT_LANE: SECRET_VAR,
    "convo": "DISCORD_COMMS_CONVO_WEBHOOK_URL",
    BOARD_LANE: FORUM_SECRET_VAR,
}

# Lane name -> state subdir. Separate dirs so cursors and skipped-rows logs
# never mix between lanes (mixing would let one lane's cursor accidentally
# skip rows the other lane never delivered). The board lane's dir name is
# imported from threads.py rather than spelled again here: the thread map is
# that module's file, and two spellings would eventually put a lane's cursor
# and its thread map in two different directories.
LANE_STATE_DIRS = {
    DEFAULT_LANE: "discord-mirror",
    "convo": "discord-mirror-convo",
    BOARD_LANE: threads.STATE_DIRS[BOARD_LANE],
}

# ---- board lane knobs (issue #40's config table) --------------------------
#
# Defaults live in lib/swarm_threads (the predicate's own defaults); these
# names are the env overrides, read HERE and passed through, so the predicate
# stays pure and one lane's operational tuning never becomes a library's
# ambient state.
ALIVE_SECONDS_VAR = "COMMS_THREAD_ALIVE_SECONDS"
ALIVE_SEATS_VAR = "COMMS_THREAD_ALIVE_SEATS"
HOLD_MAX_VAR = "COMMS_THREAD_HOLD_MAX"

ALIVE_SECONDS_DEFAULT = swarm_threads.DEFAULT_WINDOW_S
ALIVE_SEATS_DEFAULT = swarm_threads.DEFAULT_MIN_SEATS

# How many rows one thread key may hold un-posted before the oldest are
# dropped (recorded in the skipped log, never silently). Bounds a key that
# never goes alive: without it, one seat monologuing into a document grows a
# state file without limit.
HOLD_MAX_DEFAULT = 500

# On EVERY board POST. A constant, not a knob: a mailbox row is prose an
# agent wrote, and prose containing @everyone must never ring a phone.
NO_MENTIONS = {"parse": []}

# Discord's cap on a thread name.
THREAD_NAME_CAP = 100


def _env_int(var, default):
    """This knob's value, or `default` if unset or unparseable. A typo in a
    launchd plist must degrade to the documented default, not take the lane
    down: the lane's job is delivering rows, and refusing to run because a
    tuning parameter was misspelled loses more than it protects."""
    raw = os.environ.get(var)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        sys.stderr.write(
            "discord mirror: %s=%r is not a number; using %d\n" % (var, raw, default)
        )
        return default

# LAUNCHD SAFETY: how long --follow / --follow-all wait before retrying a
# poll after a missing-secret condition, instead of crash-looping under a
# KeepAlive launchd job. See module docstring, LAUNCHD SAFETY.
MISSING_SECRET_RETRY_SECONDS = 60


def _state_dir():
    # Same default chain as lib/swarm_arm.py so every comms component keeps
    # its state under one root.
    return os.environ.get("COMMS_STATE_DIR") or os.path.expanduser("~/.comms/state")


def _mirror_dir(lane=DEFAULT_LANE):
    return os.path.join(_state_dir(), LANE_STATE_DIRS[lane])


def _safe(runid):
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(runid))


def _cursor_path(runid, lane=DEFAULT_LANE):
    return os.path.join(_mirror_dir(lane), _safe(runid) + ".cursor.json")


def _skipped_path(runid, lane=DEFAULT_LANE):
    return os.path.join(_mirror_dir(lane), _safe(runid) + ".skipped.jsonl")


def _held_path(runid, lane=DEFAULT_LANE):
    """The board lane's HELD ROWS file: {thread_key: [rows]}, rows in `at`
    order. See the module docstring's BOARD LANE section for why this is a
    second file and not a smarter cursor."""
    return os.path.join(_mirror_dir(lane), _safe(runid) + ".held.json")


# ---- one poller per (run, lane) -------------------------------------------
#
# WHAT THIS PREVENTS: two pollers on one (run, lane) both read the same
# cursor, both post, and BOTH advance it -- the result is double-posted rows
# in the channel, not a race that merely errors. The README used to forbid
# that in prose; prose is not a mechanism, and the two shapes that trip it
# (`--follow <runid>` twice, or `--follow <runid>` alongside a `--follow-all`
# covering the same lane) are one launchd plist typo apart.
#
# WHY NON-BLOCKING, AND WHY 0: a poller that blocked would queue passes
# behind each other and, under a launchd KeepAlive job, pile up processes
# waiting for a lock the first one holds forever. Skipping is free instead:
# the loser's rows are exactly what the winner is posting, and anything that
# arrives after the winner's read is picked up by the loser's NEXT poll --
# delayed, never dropped. Exit 0 for the same reason the missing-secret path
# does not crash-loop: contention is a normal condition, not a failure.
#
# WHY PER PASS AND NOT PER PROCESS: --follow-all walks every run in one pass,
# so a process-lifetime lock would have to be taken per run anyway, and a
# human's ad-hoc `--once` would be locked out of a machine running a follower
# for as long as it runs. (The board lane's fleet-wide thread map has its OWN
# lock, in adapters/discord/threads.py, for exactly that reason: a thread key
# spans runs, which this lock deliberately does not -- see issue #40, D3.)
def _lock_path(runid, lane=DEFAULT_LANE):
    return os.path.join(_mirror_dir(lane), _safe(runid) + ".lock")


def _acquire_pass_lock(runid, lane=DEFAULT_LANE):
    """Take this (runid, lane)'s exclusive flock, or return None if another
    poller holds it. Returns the open fd, which the caller must close (that
    releases the lock, including if the process dies)."""
    os.makedirs(_mirror_dir(lane), exist_ok=True)
    fd = os.open(_lock_path(runid, lane), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # EAGAIN/EWOULDBLOCK -- and ONLY that -- means another poller holds
        # it. Any other OSError (permissions, a stale handle, an unwritable
        # state dir) is a broken machine, and swallowing it as contention
        # would leave a mirror that quietly posts nothing forever. Let it
        # raise: --once is loud, and --follow's _run_once_logged names it on
        # one line and keeps polling.
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _is_pulled_row(row):
    """True if this row was read out of adapters/remote's pull mirror file --
    a copy of a hub row the hub's own mirror already posted to this channel.
    See module docstring, PULLED ROWS ARE NOT MIRRORED (issue #20)."""
    return swarm_mailbox.is_mirror_source(row)


def _lane_keep(lane):
    """This lane's keep-predicate: which FRESH rows get posted. Two filters,
    composed rather than merged, because they answer different questions --
    "is this row conversation" (a lane's taste) and "did some other machine's
    mirror already post this row" (a fleet-wide fact true in every lane).
    The convo lane additionally excludes threaded rows because the board lane
    owns their rendering. Rows any filter rejects are still counted against
    the cursor."""
    lane_filter = None
    if lane == "convo":
        lane_filter = _is_convo_lane_row
    elif lane == BOARD_LANE:
        lane_filter = _is_threaded_row

    def keep(row):
        if _is_pulled_row(row):
            return False
        return True if lane_filter is None else lane_filter(row)

    return keep


def _is_convo_lane_row(row):
    """The convo lane's filter: conversation (_is_convo_row) that carries no
    `thread`. A threaded conversational row is still conversation, but the
    board lane owns its rendering inside the document's forum thread, and one
    row must not appear in two lanes outside `all`."""
    return _is_convo_row(row) and not _is_threaded_row(row)


def _is_convo_row(row):
    """A row is agent-to-agent conversation iff it is a unicast (topic
    starts with swarm_mailbox's "@" prefix) or its kind is one of
    swarm_mailbox.CONVO_KINDS. Kind alone catches broadcast conversational
    traffic (e.g. the ambient sendmessage bridge posts kind=comment on a
    non-"@" topic); topic alone catches a direct message posted with ANY
    kind -- including finding/status/blocker/claim, by design: a message
    addressed to one seat is conversation regardless of what kind carries
    it. See adapters/discord/README.md, Lanes."""
    topic = str(row.get("topic", ""))
    return topic.startswith("@") or row.get("kind") in swarm_mailbox.CONVO_KINDS


def _is_threaded_row(row):
    """The board lane's filter: a row belongs to this lane iff it names the
    document it is about (`thread`, written by swarm_mailbox.post).

    DEVIATION from the #40 design note, which said rows without a thread take
    "today's non-thread path, unchanged". In this lane there is no such path
    to take: a forum webhook REJECTS a POST carrying neither thread_name nor
    ?thread_id=, so an un-threaded row has nowhere to go here. It is
    count-but-skipped exactly as the convo lane skips a non-conversation row
    -- and it is not lost: the `all` lane mirrors every row, threaded or not.
    """
    return bool(row.get(swarm_threads.THREAD_FIELD))


def _find_config_var(var):
    """Resolve VAR with NO side effects (no stderr, no exit): returns the
    value or None. Env var first, else parsed from the secrets file. This
    is the one env-or-secrets-file resolution path in this module -- every
    webhook var and the audience switch (AUDIENCE_VAR) resolve through it.
    The secrets file doubles as the install's only config file on purpose:
    a launchd follower inherits no shell, so an `export` in a terminal
    never reaches it, while a line in ~/.secrets/comms.env reaches every
    follower on the machine."""
    url = os.environ.get(var)
    if url:
        return url
    path = os.environ.get("COMMS_SECRETS_FILE") or os.path.expanduser(
        "~/.secrets/comms.env"
    )
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(var + "="):
                    val = line.split("=", 1)[1].strip().strip("'\"")
                    if val:
                        return val
    except OSError:
        pass
    return None


def _resolve_webhook_url_for_var(var):
    """Same as _find_webhook_url_for_var but exits 2 with the exact drop-in
    line for VAR when absent. Never prints the value."""
    url = _find_config_var(var)
    if url:
        return url
    sys.stderr.write(
        "discord mirror: no webhook configured.\n"
        "  1. open -e ~/.secrets/comms.env\n"
        "  2. add line: %s=<paste webhook URL from "
        "Discord channel settings>\n"
        "  3. chmod 600 ~/.secrets/comms.env\n" % var
    )
    sys.exit(2)


def _find_webhook_url(lane=DEFAULT_LANE):
    """Resolve this lane's webhook URL with NO side effects (no stderr, no
    exit): returns the URL or None. Factored out of resolve_webhook_url so
    a caller that must not exit or print on every poll -- the follow loops'
    launchd-safety check, see module docstring LAUNCHD SAFETY -- can test
    for presence quietly instead of triggering the multi-line drop-in
    message once per poll."""
    return _find_config_var(LANE_SECRET_VARS[lane])


def resolve_webhook_url(lane=DEFAULT_LANE):
    """Env var first, else parse the secrets file. Exits 2 with the exact
    drop-in line for THIS lane's var when absent. Never prints the value."""
    return _resolve_webhook_url_for_var(LANE_SECRET_VARS[lane])


def find_forum_webhook_url():
    """Resolve the forum board's webhook URL, same env-or-secrets-file path
    as every lane var, with no side effects.

    This var IS the board lane's secret now (LANE_SECRET_VARS["board"]), so
    `_find_webhook_url("board")` resolves the same value. The named helper
    stays because it says WHICH webhook without the caller knowing the lane
    vocabulary, and because callers outside this module already reach for
    it. There is still no lane named "forum": the lane is "board"."""
    return _find_config_var(FORUM_SECRET_VAR)


def resolve_forum_webhook_url():
    """Same as find_forum_webhook_url but exits 2 with the drop-in message
    when absent, mirroring resolve_webhook_url's contract."""
    return _resolve_webhook_url_for_var(FORUM_SECRET_VAR)


# machine_label lives in lib/comms_machine.py and is RE-EXPORTED here, not
# reimplemented: it moved out when adapters/remote/ began writing the label
# into seat names that cross the network (a sync adapter must not import a
# display adapter to learn its own machine's name). The name stays bound in
# this module's namespace because ingest_mirror.py, landings.py, and the test
# suite all reach it as mirror.machine_label().
machine_label = comms_machine.machine_label


# ---- author (Discord webhook `username`) ----------------------------------
# Zero-width chars a seat/model name could smuggle in to make a rendered
# ---- audience: who is reading the channel ----------------------------------
# One binary switch, read once per render, that picks the VOCABULARY every
# renderer in this adapter speaks (build_author, build_content, thread_title,
# build_read_content; ingest_mirror.py reuses them). The mailbox rows and the
# terminal output never change -- this is the window's language, not the
# agents'. The system cannot compute who is reading a Discord channel, which
# is the reason this is a parameter at all (design doctrine D7).
#
#   engineer  (default) the rendering every existing install already has
#   everyone  plain sentences: "Found something:", "Message to <seat>:",
#             no machine names, no full paths, no bare ids
#
# The vocabulary is CLOSED: an unlisted value raises here and exits 2 in
# main() naming both values. A silent fallback would leave a person who
# typed COMMS_AUDIENCE=simple wondering why nothing changed.
AUDIENCE_VAR = "COMMS_AUDIENCE"
AUDIENCE_ENGINEER = "engineer"
AUDIENCE_EVERYONE = "everyone"
AUDIENCES = (AUDIENCE_ENGINEER, AUDIENCE_EVERYONE)


# Set by pin_audience() once main() has validated the value; a follower then
# never re-reads the secrets file per row (two opens per row otherwise), and
# an edit to the file while it runs cannot raise mid-loop and stall every
# delivery behind a swallowed ValueError. Restart to pick up a change, which
# is what the README says. None = unpinned: library callers and tests read
# the environment on every call.
_PINNED_AUDIENCE = None


def audience():
    """The configured audience, one of AUDIENCES. in: the value pinned by
    pin_audience() if any, else COMMS_AUDIENCE from the environment, else
    the secrets file (see _find_config_var), else AUDIENCE_ENGINEER.
    errors: ValueError naming both legal values when the configured one is
    neither."""
    if _PINNED_AUDIENCE is not None:
        return _PINNED_AUDIENCE
    value = (_find_config_var(AUDIENCE_VAR) or AUDIENCE_ENGINEER).strip().lower()
    if value not in AUDIENCES:
        raise ValueError(
            "%s must be one of: %s (got %r)" % (AUDIENCE_VAR, ", ".join(AUDIENCES), value)
        )
    return value


def pin_audience(value=None):
    """Fix audience() for the rest of this process to `value` (default: the
    value resolved now). Called by main() after validation; None unpins."""
    global _PINNED_AUDIENCE
    _PINNED_AUDIENCE = None
    _PINNED_AUDIENCE = audience() if value is None else value
    return _PINNED_AUDIENCE


def _report_bad_audience(exc):
    sys.stderr.write(
        "discord mirror: %s\n"
        "  set it in ~/.secrets/comms.env (or the environment):\n"
        "  %s=%s   # plain-language rendering for non-engineers\n"
        "  %s=%s   # the default\n"
        % (exc, AUDIENCE_VAR, AUDIENCE_EVERYONE, AUDIENCE_VAR, AUDIENCE_ENGINEER)
    )


def resolve_audience():
    """audience(), but exits 2 with the drop-in lines when the value is not
    legal (the same contract as resolve_webhook_url). main() checks the
    value first thing and returns 2 itself, so a typo is caught by the
    human running --once, never discovered as a follower silently
    rendering the engineer vocabulary."""
    try:
        return pin_audience()
    except ValueError as exc:
        _report_bad_audience(exc)
        sys.exit(2)


AUTHOR_MAX_LEN = 80  # Discord webhook `username` limit (HTTP 400 above it)


def build_author(seat, identity, machine):
    """This message's Discord webhook `username` -- one seat's authorship per
    POST (see chunk_rows). With identity (swarm_arm seat_identities:
    {model, project, area}, declared keys only):

        <seat> · <model> on <project> (<machine>)

    Any subset of model/project renders (absent parts drop out); without
    identity at all:

        <seat> (<machine>)

    Sanitized against @everyone/@here and zero-width characters by the shared
    renderer.
    """
    identity = identity or {}
    selected_audience = audience()
    # Discord rejects a webhook `username` over AUTHOR_MAX_LEN with HTTP 400,
    # which the mirror does not retry, so the row would be skipped for good
    # (#59: 165 rows lost to 93-101 char lines). Shed the least identifying
    # segment first: project, then model, then hard-truncate the seat line.
    for use_model, use_project in ((True, True), (True, False), (False, False)):
        reduced = dict(identity)
        if not use_model:
            reduced.pop("model", None)
        if not use_project:
            reduced.pop("project", None)
        author = comms_render.build_author(seat, reduced, machine, selected_audience)
        if len(author) <= AUTHOR_MAX_LEN:
            return author
    return author[:AUTHOR_MAX_LEN]


def build_read_content(n, seats):
    """The "heard from mailbox" verb's content (posted by ingest_mirror.py
    from the heartbeat telemetry): `n` rows were injected into a seat's
    context, sent by `seats` (first-seen order, may be empty when the
    reconstruction found none). Lives here, beside the other verbs, so one
    audience switch covers all three."""
    return comms_render.build_read_content(n, seats, audience())


def build_content(row):
    """Return this row's transport-capped Discord message content."""
    adapted = dict(row)
    adapted["text"] = str(row.get("text", ""))[:TEXT_CAP]
    return comms_render.build_content(adapted, audience())


def format_row(row, machine, identity=None):
    """(author, content) for one row -- author is this row's seat's Discord
    webhook `username` (build_author), content is the emoji-prefixed message
    body (build_content). Split because Discord authorship rides the
    per-POST `username` field, not message text; see chunk_rows for how rows
    sharing an author are batched into one POST."""
    seat = row.get("seat", "?")
    return build_author(seat, identity, machine), build_content(row)


def _load_cursor(runid, lane=DEFAULT_LANE):
    try:
        with open(_cursor_path(runid, lane)) as fh:
            cur = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return cur if isinstance(cur, dict) else {}


def _cursor_tmp_path(runid, lane=DEFAULT_LANE):
    # PID-suffixed (S3, cheap half): two pollers racing on the SAME
    # (run, lane) still each get their own tmp file, so one process's
    # partial write is never clobbered or vanished-out-from-under the
    # other's os.replace (which raised FileNotFoundError before this).
    # This does not make concurrent pollers on one (run, lane) SAFE to run
    # -- see adapters/discord/README.md, Concurrency -- it only stops the
    # tmp-file collision; the two would still double-post to the channel.
    return _cursor_path(runid, lane) + ".tmp." + str(os.getpid())


def _atomic_write_json(path, data, tmp):
    """Write `data` to `path` DURABLY: temp file, flush, fsync, rename, then
    fsync the containing directory.

    ORDERING TWO PYTHON WRITES DOES NOT ORDER TWO DISK WRITES (PR #51 review,
    Codex 3). This lane's entire crash-safety argument is "the held file is
    durable before the cursor moves past its rows". Without the fsync, both
    files are still dirty page cache when os.replace returns, and a power
    loss is free to keep the newer cursor and lose the held file it depends
    on -- which loses rows permanently, the one failure this lane refuses.
    The rename itself is atomic either way; what the fsync buys is that the
    BYTES are on the platter before the next file's rename can be.

    The directory fsync (what makes the RENAME durable, not just the bytes)
    is best-effort: some filesystems refuse an fsync on a directory fd, and
    by then the rename has already happened, so refusing to fail here is the
    difference between a weaker guarantee and no write at all.
    """
    with open(tmp, "w") as fh:
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    try:
        dir_fd = os.open(os.path.dirname(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _save_cursor(runid, cursor, lane=DEFAULT_LANE):
    os.makedirs(_mirror_dir(lane), exist_ok=True)
    _atomic_write_json(
        _cursor_path(runid, lane), cursor, _cursor_tmp_path(runid, lane)
    )


def collect_new(runid, lane=DEFAULT_LANE):
    """Return (fresh_rows, new_cursor). Per-(seat, source file) row counts are
    the cursor: seat files are append-only single-writer, so row N of a seat
    IN ONE FILE is stable forever and 'new' is exactly indices >= that key's
    count. read_siblings sorts by `at` with a stable sort, so each file's own
    order is preserved inside the merged stream.

    SOURCE-KEYED, NOT SEAT-KEYED (issues #20, #23): the read asks for
    with_source=True, which stamps each row with the identity of the file it
    was parsed out of. That is what makes the count stable when one seat owns
    rows in two files at once, and what tells a pulled copy from a first-class
    row -- see the module docstring's CURSOR and PULLED ROWS sections.

    LANE FILTER: the cursor always advances over EVERY row (every row is
    counted regardless of lane), but only rows this lane's keep-predicate
    accepts are returned in `fresh` for posting -- pulled rows in every lane,
    plus non-conversation rows in the convo lane. A row the predicate rejects
    still counts against that lane's cursor -- see module docstring, LANES.

    The counting itself is swarm_mailbox.fresh_rows_by_seat (moved there when
    adapters/remote/sync.py needed the same arithmetic over another machine's
    rows -- one cursor rule, two adapters). This function keeps what is
    Discord-specific: which run to read, which lane's cursor, and which rows
    that lane wants."""
    rows = swarm_mailbox.read_siblings(runid, OBSERVER_SEAT, with_source=True)
    cursor = _load_cursor(runid, lane)
    return swarm_mailbox.fresh_rows_by_seat(rows, cursor, keep=_lane_keep(lane))


def chunk_rows(rows, machine, cap=CONTENT_CAP, identities=None):
    """Batch rows into as few Discord messages as fit under the content cap,
    NEVER mixing two seats into one message: Discord's webhook `username` is
    per-POST, so a batch that mixed seats could only speak with one seat's
    voice for the others' rows too. A seat change always starts a new chunk,
    even when the next row would still fit under `cap`.

    Returns a list of (author, content, rows_in_chunk) so a failed POST can
    name exactly which rows it skipped and which author it failed under.
    `identities` (optional) maps seat -> enrollment identity for build_author;
    omitted = identity-free authorship ("<seat> (<machine>)")."""
    identities = identities or {}
    chunks = []
    cur_author, cur_seat = None, None
    cur_lines, cur_rows, size = [], [], 0

    def flush():
        if cur_lines:
            chunks.append((cur_author, "\n".join(cur_lines), list(cur_rows)))

    for row in rows:
        seat = row.get("seat", "?")
        author, line = format_row(row, machine, identities.get(seat))
        new_seat = cur_seat is not None and seat != cur_seat
        over_cap = cur_lines and size + 1 + len(line) > cap
        if new_seat or over_cap:
            flush()
            cur_lines, cur_rows, size = [], [], 0
        cur_seat, cur_author = seat, author
        cur_lines.append(line)
        cur_rows.append(row)
        size += len(line) + (1 if size else 0)
    flush()
    return chunks


def post_content(url, content, username=None, allowed_mentions=None):
    """POST one message. Honours 429 Retry-After, caps retries. Returns True
    delivered / False gave up. Never raises for HTTP-level failure and never
    prints the URL.

    `username` (optional): per-message Discord webhook author override --
    the "post as this seat" mechanism (see build_author). Omitted -> the
    webhook's own configured name, exactly the pre-authorship behavior.

    `allowed_mentions` (optional): Discord's mention-suppression object. Sent
    only when given, so an omitted one leaves the payload byte-identical to
    the pre-board shape. The board lane passes NO_MENTIONS on every post."""
    payload = {"content": content}
    if username:
        payload["username"] = username
    if allowed_mentions is not None:
        payload["allowed_mentions"] = allowed_mentions
    body = json.dumps(payload).encode("utf-8")
    attempt = 0
    while True:
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "comms-discord-mirror",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < MAX_RETRIES:
                retry_after = exc.headers.get("Retry-After") or "1"
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = 1.0
                time.sleep(min(delay, 30))
                attempt += 1
                continue
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            sys.stderr.write(
                "discord mirror: webhook POST failed (HTTP %d) after %d attempt(s)%s\n"
                % (exc.code, attempt + 1, (": " + detail) if detail else "")
            )
            return False
        except (urllib.error.URLError, OSError) as exc:
            if attempt < MAX_RETRIES:
                time.sleep(1)
                attempt += 1
                continue
            sys.stderr.write(
                "discord mirror: webhook POST failed (%s) after %d attempt(s)\n"
                % (exc.__class__.__name__, attempt + 1)
            )
            return False


def _log_skipped(runid, rows, reason, lane=DEFAULT_LANE):
    """The never-drop-silently channel: skipped rows go to stderr AND a
    durable state-dir file, then the cursor may advance past them.

    Rows are recorded AS AUTHORED (swarm_mailbox.without_source): this file is
    the replay record, and the source tag is a read-time fact about this
    machine's disk, not part of the row its author wrote."""
    os.makedirs(_mirror_dir(lane), exist_ok=True)
    path = _skipped_path(runid, lane)
    with open(path, "a") as fh:
        for row in rows:
            fh.write(
                json.dumps({"reason": reason, "row": swarm_mailbox.without_source(row)})
                + "\n"
            )
        # ON THE PLATTER BEFORE THE CURSOR FORGETS THE ROW. This file is the
        # ONLY record of a row the caller is about to advance past, so the
        # write has to be durable before _save_cursor runs -- a crash in
        # between would otherwise persist the newer cursor and lose the
        # recovery record, which is a silent drop wearing a "never silently
        # lossy" docstring.
        fh.flush()
        os.fsync(fh.fileno())
    sys.stderr.write(
        "discord mirror: SKIPPED %d row(s) (%s); recorded in %s\n"
        % (len(rows), reason, path)
    )


# ---- the board lane: held rows, alive threads, the drain ------------------


def _load_held(runid, lane):
    """The held-rows file as {thread_key: [rows]}, or {} if it is absent,
    unreadable, corrupt, or not a dict.

    Corrupt reads as EMPTY AND LOUD (D6): the cursor is already past these
    rows, so the backlog is genuinely lost and no amount of retrying gets it
    back -- what is left is to say so on stderr rather than crash the lane or
    pretend. Same shape as _load_cursor's tolerance, for the same reason: a
    state file this process wrote is not an input worth dying over.
    """
    path = _held_path(runid, lane)
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(
            "discord mirror: held-rows file unreadable (%s) for run %r; "
            "treating as empty -- un-posted rows in it are LOST (the cursor "
            "is already past them)\n" % (exc.__class__.__name__, runid)
        )
        return {}
    if not isinstance(data, dict):
        sys.stderr.write(
            "discord mirror: held-rows file for run %r is not an object; "
            "treating as empty\n" % runid
        )
        return {}
    return {k: v for k, v in data.items() if isinstance(v, list)}


def _save_held(runid, lane, buckets):
    """Persist the held rows durably (see _atomic_write_json: fsync before
    the rename, so the bytes beat the cursor to the platter). Buckets that are empty are dropped rather than
    written as [], so a drained thread leaves no residue.

    Writes NOTHING when there is nothing held and no file exists yet -- the
    same no-orphan-file rule _mirror_pass applies to the cursor, so a run
    that never held a row never grows a state file.
    """
    buckets = {k: v for k, v in buckets.items() if v}
    path = _held_path(runid, lane)
    if not buckets and not os.path.exists(path):
        return
    os.makedirs(_mirror_dir(lane), exist_ok=True)
    _atomic_write_json(path, buckets, path + ".tmp." + str(os.getpid()))


def _row_identity(row):
    """A stable identity for one row, over the fields its AUTHOR wrote.

    Used to make the held merge idempotent (PR #51 review, Kimi). The held
    file is written before the cursor is saved, so a crash -- or a
    _save_cursor that raises -- in between means the next pass re-reads the
    same fresh rows against the old cursor and would append a second copy of
    each, compounding once per crash and double-posting all of them when the
    document finally goes alive.

    The read-time source tag is deliberately NOT part of this: held rows are
    stored as their author wrote them (no tag), so including it would make
    every comparison fail and the dedupe would inspect nothing while looking
    like it worked.

    What this cannot distinguish is two rows from one seat with identical
    text, kind, topic and `at` -- to the microsecond. One seat's appends are
    sequential and `at` comes from the clock, so that is a row no reader
    could tell from a duplicate anyway; collapsing it costs nothing, where
    the alternative costs a duplicate per crash forever.
    """
    return json.dumps(swarm_mailbox.without_source(row), sort_keys=True)


def _bucket_rows(held, fresh):
    """{thread_key: [rows in `at` order]} from the held file plus this pass's
    fresh rows.

    Fresh rows are stripped of their read-time source tag before they land in
    a bucket (swarm_mailbox.without_source): the held file is a durable copy
    of rows as their AUTHOR wrote them, and a tag naming this machine's inode
    has no business surviving into it -- the same rule the skipped log
    follows.

    IDEMPOTENT: a fresh row already present in its bucket is skipped, so a
    pass that re-reads rows it already held (the cursor never got saved) adds
    nothing. See _row_identity.

    Sorted by `at`, stably, so held rows keep their place ahead of fresh rows
    written in the same instant. The sort is the drain's contract: the
    backlog posts oldest-first, which is the only order a human can read.
    """
    buckets = {k: list(v) for k, v in held.items()}
    for key, rows in swarm_threads.group_by_thread(fresh).items():
        bucket = buckets.setdefault(key, [])
        seen = {_row_identity(row) for row in bucket}
        for row in rows:
            row = swarm_mailbox.without_source(row)
            identity = _row_identity(row)
            if identity in seen:
                continue  # already held -- see _row_identity
            seen.add(identity)
            bucket.append(row)
    for rows in buckets.values():
        rows.sort(key=lambda r: str(r.get("at", "")))
    return {k: v for k, v in buckets.items() if v}


def _apply_hold_cap(runid, lane, buckets):
    """Trim every bucket to HOLD_MAX rows, OLDEST DROPPED FIRST, recording
    each dropped row in the skipped log (which also shouts one stderr line).

    Oldest first because the newest rows are the ones that can still make a
    key go alive; the oldest are the ones a human would least miss. The cap
    exists to bound a key that NEVER goes alive -- one seat monologuing into
    a document -- which otherwise grows this file without limit.
    """
    cap = _env_int(HOLD_MAX_VAR, HOLD_MAX_DEFAULT)
    for key, rows in buckets.items():
        if cap >= 0 and len(rows) > cap:
            dropped = rows[: len(rows) - cap]
            buckets[key] = rows[len(rows) - cap:]
            _log_skipped(
                runid,
                dropped,
                "thread hold cap reached for %s (%s=%d)" % (key, HOLD_MAX_VAR, cap),
                lane,
            )
    return buckets


def thread_title(key):
    """The human-visible Discord thread name for a thread key: the key
    without its "doc:" prefix ("doc:comms/a.md" -> "comms/a.md"), capped at
    Discord's THREAD_NAME_CAP. The prefix is a namespace for machines; a
    person reading a forum sidebar wants the path."""
    title = comms_render.thread_title(key, audience())
    return title[:THREAD_NAME_CAP] or str(key)[:THREAD_NAME_CAP]


def _thread_url(url, thread_id):
    """The webhook URL that posts INTO an existing thread."""
    return url + ("&" if "?" in url else "?") + "thread_id=" + str(thread_id)


def _without_rows(rows, delivered):
    """`rows` minus exactly the row objects in `delivered` (identity, not
    equality: two rows of one seat can be byte-identical, and dropping both
    when one was posted is a silent loss)."""
    done = {id(row) for row in delivered}
    return [row for row in rows if id(row) not in done]


def _board_pass(runid, lane, url):
    """One board-lane pass, always under the (runid, lane) lock. The ORDER is
    the design (issue #40, D1) and is the whole reason a row cannot be lost:

      1. load held        -- what this lane owes but has not posted
      2. collect_new      -- what it has not read
      3. bucket held + fresh by thread key, `at` order
      4. WRITE HELD, including the buckets about to post
      5. save cursor
      6. for each ALIVE key: get/create its thread, post the whole backlog
      7. rewrite held minus what each chunk delivered, AFTER EACH CHUNK

    Step 4 before step 5 is the load-bearing pair. The cursor advancing means
    "I have read these"; held existing means "I still owe these". Making held
    durable FIRST is what lets the cursor move safely -- a crash between them
    re-posts, at worst. The reverse order defines a row that is read, not
    posted, and remembered nowhere: the one failure this repo refuses.

    TWO STATE FILES, TWO QUESTIONS, on purpose. The cursor could not answer
    "post this later": its keep-predicate is a bool over one row and a
    rejected row is simply dropped. Teaching it to defer would make one file
    answer two questions whose right answers diverge.
    """
    machine = machine_label()
    _warn_seat_collisions(runid)
    old_cursor = _load_cursor(runid, lane)
    held = _load_held(runid, lane)                              # 1
    fresh, new_cursor = collect_new(runid, lane)                # 2
    buckets = _apply_hold_cap(runid, lane, _bucket_rows(held, fresh))  # 3
    _save_held(runid, lane, buckets)                            # 4
    if new_cursor != old_cursor:                                # 5
        _save_cursor(runid, new_cursor, lane)
    if not buckets:
        return 0
    return _drain(runid, lane, url, buckets, machine)           # 6, 7


def _drain(runid, lane, url, buckets, machine):
    """Post every ALIVE key's WHOLE backlog into its thread, rewriting the
    held file after each chunk. Returns 1 if any chunk was skipped.

    THE WHOLE BACKLOG, not the row that tripped `alive`: the point of holding
    rows is that they arrive before the conversation is visibly a
    conversation, so the moment it becomes one, everything said so far has to
    land -- oldest first, across as many POSTs as the seat changes and the
    content cap force.

    ALIVE IS A ONE-WAY TRANSITION, AND THE THREAD MAP IS ITS RECORD (PR #51
    review). `alive` decides whether to OPEN a thread; it never decides
    whether to deliver into one that already exists. A key already in the map
    posts its bucket directly, without consulting the predicate at all.
    Re-asking it every pass was a silent stall: a drained thread leaves no
    rows behind, so its liveness history is gone, and the next lone row from
    one seat would sit in held forever looking exactly like a row that is
    merely waiting its turn. That is README rehearsal step 13, and it is also
    what makes the fleet-wide map pay off -- once ANY run opens a document's
    thread, every run's rows have a destination.

    HELD IS REWRITTEN PER CHUNK, not once at the end (a small deviation from
    the design note, which allowed once-per-drain). A drain of a long backlog
    is many POSTs and many seconds; rewriting after each one means a crash or
    a failed chunk in the middle costs only the chunks not yet delivered. The
    once-at-the-end version re-posts the ENTIRE backlog next pass every time
    one POST fails, which is a duplicate storm proportional to how long the
    thread was held.

    A CHUNK THAT WILL NOT DELIVER is recorded in the skipped log and dropped
    from held -- never retried forever, because one poisoned batch must not
    wedge every later row behind it -- and the drain of THAT thread stops
    there, leaving its remainder held for the next pass. Other threads in the
    same pass are unaffected.
    """
    identities = swarm_arm.seat_identities(runid)
    window_s = _env_int(ALIVE_SECONDS_VAR, ALIVE_SECONDS_DEFAULT)
    min_seats = _env_int(ALIVE_SEATS_VAR, ALIVE_SEATS_DEFAULT)
    poster = threads.webhook_poster(url)
    # One read of the fleet-wide map per pass: a key already in it has
    # ALREADY gone alive, here or in another run, and needs no predicate.
    known = threads.load_map(lane)
    skipped = False
    for key in sorted(buckets):
        rows = buckets[key]
        thread_id = known.get(key)
        if not thread_id:
            if not swarm_threads.alive(rows, window_s=window_s, min_seats=min_seats):
                continue
            thread_id = threads.thread_for(key, thread_title(key), lane, poster)
            if thread_id is None:
                continue  # D6: every thread_for failure leaves the rows held
        target = _thread_url(url, thread_id)
        for author, content, chunk in chunk_rows(rows, machine, identities=identities):
            delivered = post_content(
                target, content, username=author, allowed_mentions=NO_MENTIONS
            )
            if not delivered:
                _log_skipped(runid, chunk, "webhook delivery failed", lane)
                skipped = True
            buckets[key] = _without_rows(buckets[key], chunk)
            _save_held(runid, lane, buckets)
            if not delivered:
                break  # this thread's remainder waits for the next pass
    return 1 if skipped else 0


def _warn_seat_collisions(runid):
    """ONE stderr line per pass when two agents share a seat name (issue #42,
    resolved as detect-don't-reject in #40's D5). Nothing is blocked: the
    rows still post, the seat still renders. The line is the only thing that
    makes a duplicate seat visible at all -- without it, "@alpha" quietly
    fans out to two agents and both render as one."""
    collisions = swarm_arm.seat_collisions(runid)
    if not collisions:
        return
    detail = "; ".join(
        "%s <- %s" % (seat, ", ".join(ids)) for seat, ids in sorted(collisions.items())
    )
    sys.stderr.write(
        "discord mirror: seat name claimed by more than one agent in run %r "
        "(%s); rows render under the first agent's identity and a unicast "
        "reaches both\n" % (runid, detail)
    )


def run_once(runid, lane=DEFAULT_LANE):
    """Mirror everything new in one lane, once. Exit-code semantics of
    main(). Raises SystemExit(2) via resolve_webhook_url if this lane's
    secret is missing -- callers that must not exit (--follow, --follow-all)
    catch that themselves; see module docstring, LAUNCHD SAFETY.

    ONE POLLER PER (RUN, LANE): the whole pass -- read cursor, post, save
    cursor -- runs under this (runid, lane)'s exclusive flock. If another
    poller holds it, this pass posts nothing, leaves the cursor alone, names
    the condition on one stderr line and returns 0; the rows are the ones the
    other poller is posting right now, and anything newer arrives on the next
    poll. See the lock section above _lock_path for why not blocking.

    The secret check stays FIRST, ahead of the lock: exit 2 on a missing
    secret is --once's contract with a human, and a lock some other poller
    happens to hold must not turn that into a quiet 0.

    HOW MANY TIMES A ROW CAN POST, precisely: exactly once across CONCURRENT
    pollers (that is what the lock buys), at least once across a CRASH. The
    cursor is saved after the posts, so a process that dies -- or whose
    _save_cursor raises -- between a delivered chunk and the save re-posts
    that chunk on the next pass. That is deliberate, and it is the same trade
    every other cursor in this repo makes (see swarm_mailbox.read_delta, and
    the slice 2 design note on issue #40, D1): committing the cursor first
    would turn a duplicate, which a human reading the channel can see and
    ignore, into a lost row, which nobody can see at all. Recovery for a
    duplicate is nothing; recovery for a lost row does not exist."""
    url = resolve_webhook_url(lane)  # before anything else: missing secret = 2 always
    lock_fd = _acquire_pass_lock(runid, lane)
    if lock_fd is None:
        sys.stderr.write(
            "discord mirror: another poller holds run %r lane %r; skipping "
            "this pass (its rows are that poller's to post)\n" % (runid, lane)
        )
        return 0
    try:
        if lane == BOARD_LANE:
            return _board_pass(runid, lane, url)
        return _mirror_pass(runid, lane, url)
    finally:
        os.close(lock_fd)  # releases the flock, even on an exception


def _mirror_pass(runid, lane, url):
    """One pass's actual work, always under the (runid, lane) lock: read the
    cursor, post what is fresh, advance. Split from run_once so the locking is
    one unnested block that cannot be read past, and so the pass body has
    exactly one exit path to the release in run_once's finally."""
    machine = machine_label()
    _warn_seat_collisions(runid)
    old_cursor = _load_cursor(runid, lane)
    fresh, new_cursor = collect_new(runid, lane)
    skipped = False
    if fresh:
        # Enrollment identity, read once per pass and joined by seat in
        # format_row. A run with no arm state (or a pre-identity roster)
        # yields {} and every line renders in the identity-free format.
        identities = swarm_arm.seat_identities(runid)
        for author, content, chunk in chunk_rows(fresh, machine, identities=identities):
            if not post_content(url, content, username=author):
                _log_skipped(runid, chunk, "webhook delivery failed", lane)
                skipped = True
    # Advance past everything, delivered or skipped: the skipped file is the
    # durable record, and never advancing would wedge the mirror forever
    # behind one undeliverable batch. But skip the write entirely when the
    # cursor did not actually change (S6/S7): a lane filter can still
    # advance it with nothing posted, so this is not just "if not fresh" --
    # it is "did new_cursor differ from what's on disk". Guarding this
    # avoids an idle rewrite on every poll of a quiet run, AND avoids
    # creating an orphan cursor file for a run with no rows at all.
    if new_cursor != old_cursor:
        _save_cursor(runid, new_cursor, lane)
    return 1 if skipped else 0


def _warn_missing_secret(lane):
    sys.stderr.write(
        "discord mirror: webhook secret missing for lane %r; retrying in %ds\n"
        % (lane, MISSING_SECRET_RETRY_SECONDS)
    )


def _run_once_logged(runid, lane):
    """run_once, but ANY exception (a bad row, an unwritable state dir, a
    transient OSError -- not just the missing-secret case _find_webhook_url
    already guards against) is caught, named with the runid and exception
    class on one stderr line, and swallowed (S1). Without this, one broken
    run kills the whole --follow/--follow-all poll loop, and a launchd
    KeepAlive job restarts it -- the exact crash-loop LAUNCHD SAFETY exists
    to prevent, just triggered by a different cause than a missing secret.
    SystemExit is NOT caught here (it is not an Exception subclass): a
    missing-secret exit from resolve_webhook_url is handled by the caller's
    _find_webhook_url pre-check, not by this wrapper."""
    try:
        return run_once(runid, lane)
    except Exception as exc:
        sys.stderr.write(
            "discord mirror: run_once failed for run %r (%s: %s); continuing\n"
            % (runid, exc.__class__.__name__, exc)
        )
        return 1


def follow(runid, interval, lane=DEFAULT_LANE):
    """Poll one run forever. LAUNCHD SAFETY: checks for the secret BEFORE
    calling run_once, so a missing secret never reaches resolve_webhook_url
    (whose multi-line drop-in message is meant for a one-shot --once, not
    once per poll) -- instead ONE stderr line, then a 60s backoff, so a
    launchd KeepAlive job waits quietly instead of crash-looping. Any OTHER
    exception from run_once (S1) is caught by _run_once_logged so it never
    kills this loop either. See module docstring, LAUNCHD SAFETY."""
    rc = 0
    while True:
        if _find_webhook_url(lane) is None:
            _warn_missing_secret(lane)
            sleep_for = MISSING_SECRET_RETRY_SECONDS
        else:
            rc = max(rc, _run_once_logged(runid, lane))
            sleep_for = interval
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            return rc


def _tail_ingest_logged(url):
    """Run adapters/discord/ingest_mirror.py's tail_once under the same S1
    per-pass exception guard as _run_once_logged, so a broken beat in the
    heartbeat-telemetry tailer never kills this loop either. Imported HERE,
    not at module scope, to avoid a load-time cycle (ingest_mirror imports
    this module for post_content/build_author/machine_label -- see its
    docstring)."""
    try:
        import ingest_mirror
        return ingest_mirror.tail_once(url)
    except Exception as exc:
        sys.stderr.write(
            "discord mirror: ingest tail failed (%s); continuing\n"
            % exc.__class__.__name__
        )
        return 1


def follow_all(interval, lane=DEFAULT_LANE):
    """Poll EVERY run under the mailbox root, once per pass, forever.
    Discovery is swarm_mailbox.run_ids() so a newly-armed run is picked up
    without restarting the process. LAUNCHD SAFETY: the secret is a
    lane-wide condition (not per-run), so it is checked once per pass,
    before touching any run -- warns once and backs off 60s exactly like
    follow() when absent, instead of re-discovering the same failure once
    per run. A per-run exception (S1) is caught by _run_once_logged so one
    broken run does not stop the pass from reaching the rest.

    INGEST WIRE-UP: when lane is "convo", each pass ALSO tails the
    swarm-heartbeat telemetry log (adapters/discord/ingest_mirror.py) once,
    after the mailbox-row runs, posting a "read N row(s)" event per delivery
    -- one process, no second launchd job. The heartbeat log is fleet-wide,
    not per-run (it has no run-scoped tail point), so this lives in
    follow_all's whole-fleet pass rather than follow()'s single-run one;
    `follow(<runid>, lane="convo")` mirrors that run's mailbox rows only and
    does not tail ingest -- a deliberate scope choice, not an oversight (see
    PR description)."""
    rc = 0
    while True:
        url = _find_webhook_url(lane)
        if url is None:
            _warn_missing_secret(lane)
            sleep_for = MISSING_SECRET_RETRY_SECONDS
        else:
            for runid in swarm_mailbox.run_ids():
                rc = max(rc, _run_once_logged(runid, lane))
            if lane == "convo":
                rc = max(rc, _tail_ingest_logged(url))
            sleep_for = interval
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            return rc


def main(argv):
    args = list(argv[1:])
    try:
        pin_audience()  # a typo is a usage error: 2, before any webhook is touched
    except ValueError as exc:
        _report_bad_audience(exc)
        return 2
    interval = float(os.environ.get("COMMS_MIRROR_INTERVAL", "5"))
    lane = DEFAULT_LANE
    if "--interval" in args:
        i = args.index("--interval")
        try:
            interval = float(args[i + 1])
        except (IndexError, ValueError):
            sys.stderr.write("--interval needs a number\n")
            return 2
        del args[i : i + 2]
    if "--lane" in args:
        i = args.index("--lane")
        if i + 1 >= len(args):
            sys.stderr.write("--lane needs a value\n")
            return 2
        lane = args[i + 1]
        del args[i : i + 2]
        if lane not in LANE_SECRET_VARS:
            sys.stderr.write(
                "--lane must be one of: %s\n" % ", ".join(sorted(LANE_SECRET_VARS))
            )
            return 2
    if len(args) == 1 and args[0] == "--follow-all":
        try:
            return follow_all(interval, lane)
        except KeyboardInterrupt:
            return 0
    if len(args) == 2 and args[0] == "--once":
        return run_once(args[1], lane)
    if len(args) == 2 and args[0] == "--follow":
        try:
            return follow(args[1], interval, lane)
        except KeyboardInterrupt:
            return 0
    sys.stderr.write(
        "usage: mirror.py --once <runid> [--lane <name>]\n"
        "       mirror.py --follow <runid> [--interval <seconds>] [--lane <name>]\n"
        "       mirror.py --follow-all [--interval <seconds>] [--lane <name>]\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
