#!/usr/bin/env python3
"""discord mirror: tail one run's local comms mailbox and post each row as a
one-liner to a single Discord channel via webhook.

WHY: each machine runs its OWN local mailbox (no cross-machine file sync).
Discord is the merge point and the dashboard: every machine's mirror posts into
the same channel, so a human watching one channel sees the whole fleet's
conversation, prefixed [machine/seat] so provenance survives the merge.

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
           zero-width characters (see _sanitize_author) -- identity is
           display-only prose and never gates anything.
  content: one leading emoji chosen by the row's shape (see KIND_EMOJI and
           build_content's docstring) -- the "posted to mailbox" verb. The
           "agent born" verb (the ambient session-started status row) and
           the "heard from mailbox" verb (posted by adapters/discord/
           ingest_mirror.py from the heartbeat telemetry log, NOT this
           module's mailbox tail) round out the three visible verbs.

Because a single POST's Discord `username` is one value, rows batched into
one message must share an author -- chunk_rows batches per (seat), never
mixing two seats into the same POST even when they would fit under the
content cap.

WHAT IS NOT MIRRORED: claims, arming, subscriptions, cursors -- machine-local
state stays machine-local. Command direction (Discord -> machine) is out of
scope; durable commands go through the GitHub board.

READ PATH: reuses swarm_mailbox.read_siblings (never a second parser).
read_siblings excludes the named seat's own file, so the mirror reads as a
reserved observer seat ("discord-mirror") that never posts; it therefore sees
every real seat's rows. Do not name a real seat "discord-mirror" -- its rows
would be invisible to the mirror by construction.

CURSOR: per-run JSON file in $COMMS_STATE_DIR/discord-mirror/ mapping seat ->
count of rows already mirrored. Seat files are append-only with one writer, so
a per-seat row count is a stable cursor: restarts never repost, new rows are
exactly rows[count:]. Written via tmp + os.replace so a crash never leaves a
half-written cursor.

FAILURE: a row that cannot be delivered after the retry budget is NEVER
dropped silently -- it is written to <runid>.skipped.jsonl in the state dir
and shouted to stderr, then the cursor advances past it (the skipped file is
the durable record; re-posting forever would wedge the mirror behind one bad
batch). 429 honours Retry-After.

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
failure path, just parameterized by lane name.

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

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

SELF_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SELF_DIR))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
sys.path.insert(0, os.path.join(REPO_ROOT, "adapters", "remote"))
import comms_machine  # noqa: E402  (machine_label, re-exported below)
import swarm_arm  # noqa: E402  (one roster reader; see IDENTITY below)
import swarm_mailbox  # noqa: E402  (one parser; see READ PATH above)
# is_mirror_seat, the naming rule for a PULLED row's mirror file (issue #20,
# see _is_remote_mirror_row below) -- imported rather than a second copy of
# "remote~" living here to drift against. The reverse import (sync.py pulling
# in this module) is what test_sync_does_not_import_the_discord_adapter
# guards against; a display adapter reading a sync adapter's naming rule is
# the safe direction.
import sync as remote_sync  # noqa: E402

# Reserved observer seat name handed to read_siblings so the mirror sees every
# REAL seat's rows (read_siblings excludes only the named seat's own file).
OBSERVER_SEAT = "discord-mirror"

TEXT_CAP = 300  # chars of row text per line
# Discord hard-caps message content at 2000 chars; stay under with headroom.
CONTENT_CAP = 1900
MAX_RETRIES = 3  # additional attempts after the first
SECRET_VAR = "DISCORD_COMMS_WEBHOOK_URL"
DEFAULT_LANE = "all"

# Lane name -> secret var. The default lane keeps the pre-lane var so an
# un-flagged invocation is byte-identical to before this feature existed.
LANE_SECRET_VARS = {
    DEFAULT_LANE: SECRET_VAR,
    "convo": "DISCORD_COMMS_CONVO_WEBHOOK_URL",
}

# Lane name -> state subdir. Separate dirs so cursors and skipped-rows logs
# never mix between lanes (mixing would let one lane's cursor accidentally
# skip rows the other lane never delivered).
LANE_STATE_DIRS = {
    DEFAULT_LANE: "discord-mirror",
    "convo": "discord-mirror-convo",
}

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


def _is_remote_mirror_row(row):
    """True iff this row was PULLED onto this machine by adapters/remote/sync.py
    (issue #20) -- i.e. its source file is a "remote~<hub>" mirror file, never a
    first-class seat file.

    WHY THE FILE AND NOT THE SEAT: a pulled row's `seat` is qualified with the
    hub's label the same way a pushed row's is (e.g. both can look like
    "alpha~macbook") -- qualify() and pull()'s per-hub mirror file diverge on
    PURPOSE, not on shape. A pushed row lives ONLY in the hub's first-class
    "alpha~macbook.jsonl" (the hub's mirror is its ONLY mirror, and must keep
    posting it); a pulled row lives ONLY in the spoke's "remote~<hub>.jsonl"
    (the hub's OWN mirror already posted the original once, so re-posting here
    would double it -- see the issue's first-pull incident, 167 rows). The
    source file is the one place that distinction still exists by the time a
    row reaches this filter; the seat string alone would conflate the two.

    `row` carries swarm_mailbox.SRC_FILE_KEY, stamped in-memory by
    _all_sibling_rows over THIS machine's own mailbox dir -- collect_new never
    reads across the network, so there is no risk of trusting a remote-supplied
    value here."""
    src = row.get(swarm_mailbox.SRC_FILE_KEY, "")
    if not src.endswith(".jsonl"):
        return False
    return remote_sync.is_mirror_seat(src[: -len(".jsonl")])


def _find_webhook_url(lane=DEFAULT_LANE):
    """Resolve this lane's webhook URL with NO side effects (no stderr, no
    exit): returns the URL or None. Env var first, else parsed from the
    secrets file. Factored out of resolve_webhook_url so a caller that must
    not exit or print on every poll -- the follow loops' launchd-safety
    check, see module docstring LAUNCHD SAFETY -- can test for presence
    quietly instead of triggering the multi-line drop-in message once per
    poll."""
    var = LANE_SECRET_VARS[lane]
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


def resolve_webhook_url(lane=DEFAULT_LANE):
    """Env var first, else parse the secrets file. Exits 2 with the exact
    drop-in line for THIS lane's var when absent. Never prints the value."""
    url = _find_webhook_url(lane)
    if url:
        return url
    var = LANE_SECRET_VARS[lane]
    sys.stderr.write(
        "discord mirror: no webhook configured.\n"
        "  1. open -e ~/.secrets/comms.env\n"
        "  2. add line: %s=<paste webhook URL from "
        "Discord channel settings>\n"
        "  3. chmod 600 ~/.secrets/comms.env\n" % var
    )
    sys.exit(2)


# machine_label lives in lib/comms_machine.py and is RE-EXPORTED here, not
# reimplemented: it moved out when adapters/remote/ began writing the label
# into seat names that cross the network (a sync adapter must not import a
# display adapter to learn its own machine's name). The name stays bound in
# this module's namespace because ingest_mirror.py, landings.py, and the test
# suite all reach it as mirror.machine_label().
machine_label = comms_machine.machine_label


# ---- author (Discord webhook `username`) ----------------------------------
# Zero-width chars a seat/model name could smuggle in to make a rendered
# author line invisible-but-present; stripped before ever leaving this
# process. @everyone/@here are stripped too -- Discord's webhook username
# field is plain text, not a mention, but a seat literally named "@everyone"
# must never render as one in a human's eye.
_ZERO_WIDTH_CHARS = "​‌‍﻿"  # ZWSP, ZWNJ, ZWJ, BOM/ZWNBSP
_ZERO_WIDTH_RE = re.compile("[" + _ZERO_WIDTH_CHARS + "]")
_MENTION_RE = re.compile(r"@(everyone|here)", re.IGNORECASE)


def _sanitize_author(author):
    author = _ZERO_WIDTH_RE.sub("", author)
    author = _MENTION_RE.sub(lambda m: m.group(0).replace("@", ""), author)
    return author


def build_author(seat, identity, machine):
    """This message's Discord webhook `username` -- one seat's authorship per
    POST (see chunk_rows). With identity (swarm_arm seat_identities:
    {model, project, area}, declared keys only):

        <seat> · <model> on <project> (<machine>)

    Any subset of model/project renders (absent parts drop out); without
    identity at all:

        <seat> (<machine>)

    Sanitized against @everyone/@here and zero-width characters -- see
    _sanitize_author.
    """
    identity = identity or {}
    parts = []
    if identity.get("model"):
        parts.append(str(identity["model"]))
    if identity.get("project"):
        parts.append("on %s" % identity["project"])
    if parts:
        author = "%s · %s (%s)" % (seat, " ".join(parts), machine)
    else:
        author = "%s (%s)" % (seat, machine)
    return _sanitize_author(author)


# ---- content (kind -> emoji prefix) ----------------------------------------
# Values are the literal emoji this event kind renders with in Discord (Drake's
# explicit ask -- content only, never in a comment describing them, so this
# dict is the one place their glyphs appear in this file).
KIND_EMOJI = {
    "finding": "\U0001f4ec✅",       # mailbox-with-mail + check mark: broadcast finding
    "comment": "\U0001f4ec\U0001f4ac",   # mailbox-with-mail + speech balloon: broadcast comment
    "reply": "↩️",             # leftwards arrow with hook: reply
    "claim": "\U0001f4cc",               # pushpin: claim
    "blocker": "\U0001f6a7",             # construction sign: blocker
    "status": "ℹ️",            # information source: status (non-ambient)
}

# The ambient "session started" status row's exact text shape (see
# adapters/claude-code's ambient status post); parsed out so it can render as
# the "agent born" verb instead of the generic status emoji.
_SESSION_STARTED_RE = re.compile(r"^session started in (.+)$")

# The sendmessage-bridge's row shape: "-> <target>: <summary>". <target> is
# either a real seat name or a bare agent_id (the complaint this feature
# exists to fix -- see PR description). Agent ids observed in this run's
# roster are 17 lowercase-hex characters; matched generically so any future
# id of the same shape is caught, not just ones seen so far.
_BRIDGE_RE = re.compile(r"^-> ([^:]+): (.*)$")
_AGENT_ID_RE = re.compile(r"^[0-9a-f]{17}$")


def build_content(row):
    """This row's Discord message CONTENT (no author -- that is the webhook
    `username`, see build_author): first TEXT_CAP chars of text, one leading
    emoji chosen by event shape, in this precedence:

      1. the ambient "session started in <dir>" status row -> the "agent
         born" verb: hatching-chick emoji + "I am awake in <dir>"
      2. a unicast (topic starts with "@") -> incoming-envelope emoji +
         "to <seat>: <text>"
      3. a sendmessage-bridge row (text starts with "-> ") -> the target
         rendered readably; a bare agent_id target is NEVER the bare object
         of the sentence (a raw 17-hex id means nothing to a human) --
         shortened to its first 8 chars and phrased as "a subagent (<short>)"
      4. otherwise: kind's emoji (KIND_EMOJI, default the info-source emoji)
         + text
    """
    text = str(row.get("text", ""))[:TEXT_CAP].replace("\n", " ")
    kind = row.get("kind", "?")
    topic = str(row.get("topic", ""))

    if kind == "status":
        m = _SESSION_STARTED_RE.match(text)
        if m:
            return "\U0001f423 I am awake in %s" % m.group(1)

    if topic.startswith("@"):
        return "\U0001f4e8 to %s: %s" % (topic[1:], text)

    m = _BRIDGE_RE.match(text)
    if m:
        target, summary = m.group(1), m.group(2)
        if _AGENT_ID_RE.match(target):
            rendered = "sent to a subagent (%s): %s" % (target[:8], summary)
        else:
            rendered = "sent to %s: %s" % (target, summary)
        return "%s %s" % (KIND_EMOJI.get(kind, "ℹ️"), rendered)

    return "%s %s" % (KIND_EMOJI.get(kind, "ℹ️"), text)


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


def _save_cursor(runid, cursor, lane=DEFAULT_LANE):
    os.makedirs(_mirror_dir(lane), exist_ok=True)
    path = _cursor_path(runid, lane)
    tmp = _cursor_tmp_path(runid, lane)
    with open(tmp, "w") as fh:
        json.dump(cursor, fh)
    os.replace(tmp, path)


def collect_new(runid, lane=DEFAULT_LANE):
    """Return (fresh_rows, new_cursor). Per-seat row counts are the cursor:
    seat files are append-only single-writer, so row N of a seat is stable
    forever and 'new' is exactly indices >= cursor[seat]. read_siblings sorts
    by `at` with a stable sort, so per-seat file order is preserved.

    LANE FILTER: the cursor always advances over EVERY row (seen[] counts
    every row regardless of lane), but only rows matching this lane's filter
    are returned in `fresh` for posting. The default lane's filter drops only
    remote-mirror rows (see REMOTE-MIRROR FILTER below); the convo lane
    additionally requires _is_convo_row. A row a lane filters out still
    counts against that lane's cursor -- see module docstring, LANES.

    REMOTE-MIRROR FILTER (issue #20, both lanes): a row whose source file is
    adapters/remote/sync.py's "remote~<hub>" pull mirror is dropped from
    `fresh` -- the hub's OWN mirror already posted it once, so returning it
    here would double-post it to the same Discord channel. The cursor still
    advances over it (same count-but-skip shape the convo lane already used
    for non-convo rows), so it is never re-scanned. See
    _is_remote_mirror_row for why the source FILE and not the seat string is
    the test.

    The counting itself is swarm_mailbox.fresh_rows_by_seat (moved there when
    adapters/remote/sync.py needed the same arithmetic over another machine's
    rows -- one cursor rule, two adapters). This function keeps what is
    Discord-specific: which run to read, which lane's cursor, and which rows
    that lane wants."""
    rows = swarm_mailbox.read_siblings(runid, OBSERVER_SEAT)
    cursor = _load_cursor(runid, lane)
    lane_keep = _is_convo_row if lane == "convo" else None

    def keep(row):
        if _is_remote_mirror_row(row):
            return False
        return lane_keep(row) if lane_keep is not None else True

    return swarm_mailbox.fresh_rows_by_seat(rows, cursor, keep=keep)


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


def post_content(url, content, username=None):
    """POST one message. Honours 429 Retry-After, caps retries. Returns True
    delivered / False gave up. Never raises for HTTP-level failure and never
    prints the URL.

    `username` (optional): per-message Discord webhook author override --
    the "post as this seat" mechanism (see build_author). Omitted -> the
    webhook's own configured name, exactly the pre-authorship behavior."""
    payload = {"content": content}
    if username:
        payload["username"] = username
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
            sys.stderr.write(
                "discord mirror: webhook POST failed (HTTP %d) after %d attempt(s)\n"
                % (exc.code, attempt + 1)
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
    durable state-dir file, then the cursor may advance past them."""
    os.makedirs(_mirror_dir(lane), exist_ok=True)
    path = _skipped_path(runid, lane)
    with open(path, "a") as fh:
        for row in rows:
            fh.write(json.dumps({"reason": reason, "row": row}) + "\n")
    sys.stderr.write(
        "discord mirror: SKIPPED %d row(s) (%s); recorded in %s\n"
        % (len(rows), reason, path)
    )


def run_once(runid, lane=DEFAULT_LANE):
    """Mirror everything new in one lane, exactly once. Exit-code semantics
    of main(). Raises SystemExit(2) via resolve_webhook_url if this lane's
    secret is missing -- callers that must not exit (--follow, --follow-all)
    catch that themselves; see module docstring, LAUNCHD SAFETY."""
    url = resolve_webhook_url(lane)  # before anything else: missing secret = 2 always
    machine = machine_label()
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
            "discord mirror: run_once failed for run %r (%s); continuing\n"
            % (runid, exc.__class__.__name__)
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
