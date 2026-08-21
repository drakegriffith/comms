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

IDENTITY: a seat that enrolled with --model/--project/--area (lib/swarm_arm)
renders as human-readable prose -- "[machine] Kimi K3 on agent-os (hooks/) |
seat kimi1 | finding: ..." -- joined row.seat -> roster at format time. A seat
without identity renders in the pre-identity "[machine/seat] kind: text"
format, byte-identical. Identity is display-only and never gates anything.

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
import socket
import sys
import time
import urllib.error
import urllib.request

SELF_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SELF_DIR))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
import swarm_arm  # noqa: E402  (one roster reader; see IDENTITY below)
import swarm_mailbox  # noqa: E402  (one parser; see READ PATH above)

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
    starts with swarm_mailbox's "@" prefix) or its kind is comment/reply.
    Kind alone catches broadcast conversational traffic (e.g. the ambient
    sendmessage bridge posts kind=comment on a non-"@" topic); topic alone
    catches a direct message posted with any kind."""
    topic = str(row.get("topic", ""))
    return topic.startswith("@") or row.get("kind") in ("comment", "reply")


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


def machine_label():
    return os.environ.get("COMMS_MACHINE_LABEL") or socket.gethostname().split(".")[0]


def format_row(row, machine, identity=None):
    """One line per row, first 300 chars of text.

    IDENTITY: `identity` is the seat's enrollment metadata (swarm_arm
    seat_identities: {model, project, area}, declared keys only), joined to
    the row BY SEAT at format time. It is display-only prose for the human
    watching the channel -- never parsed, never gating. With identity:

        [machine] Kimi K3 on agent-os (hooks/) | seat kimi1 | finding: text

    Any subset of the three fields renders (absent parts drop out). Without
    identity the line is the pre-identity format, byte-identical:

        [machine/seat] finding: text
    """
    text = str(row.get("text", ""))[:TEXT_CAP].replace("\n", " ")
    seat = row.get("seat", "?")
    kind = row.get("kind", "?")
    if identity:
        parts = []
        if identity.get("model"):
            parts.append(str(identity["model"]))
        if identity.get("project"):
            parts.append("on %s" % identity["project"])
        if identity.get("area"):
            parts.append("(%s)" % identity["area"])
        return "[%s] %s | seat %s | %s: %s" % (
            machine, " ".join(parts), seat, kind, text
        )
    return "[%s/%s] %s: %s" % (machine, seat, kind, text)


def _load_cursor(runid, lane=DEFAULT_LANE):
    try:
        with open(_cursor_path(runid, lane)) as fh:
            cur = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return cur if isinstance(cur, dict) else {}


def _save_cursor(runid, cursor, lane=DEFAULT_LANE):
    os.makedirs(_mirror_dir(lane), exist_ok=True)
    path = _cursor_path(runid, lane)
    tmp = path + ".tmp"
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
    are returned in `fresh` for posting. The default lane's filter is a
    no-op (every new row is fresh, exactly the pre-lane behavior); the convo
    lane additionally requires _is_convo_row. A row a lane filters out still
    counts against that lane's cursor -- see module docstring, LANES."""
    rows = swarm_mailbox.read_siblings(runid, OBSERVER_SEAT)
    cursor = _load_cursor(runid, lane)
    seen = {}
    fresh = []
    for row in rows:
        seat = row.get("seat", "?")
        idx = seen.get(seat, 0)
        seen[seat] = idx + 1
        if idx >= int(cursor.get(seat, 0)):
            if lane == "convo" and not _is_convo_row(row):
                continue
            fresh.append(row)
    new_cursor = dict(cursor)
    for seat, count in seen.items():
        new_cursor[seat] = max(count, int(cursor.get(seat, 0)))
    return fresh, new_cursor


def chunk_rows(rows, machine, cap=CONTENT_CAP, identities=None):
    """Batch rows into as few Discord messages as fit under the content cap.
    Returns a list of (content, rows_in_chunk) so a failed POST can name
    exactly which rows it skipped. `identities` (optional) maps seat ->
    enrollment identity for format_row; omitted = identity-free rendering."""
    chunks = []
    cur_lines, cur_rows, size = [], [], 0
    for row in rows:
        line = format_row(row, machine, (identities or {}).get(row.get("seat")))
        if cur_lines and size + 1 + len(line) > cap:
            chunks.append(("\n".join(cur_lines), cur_rows))
            cur_lines, cur_rows, size = [], [], 0
        cur_lines.append(line)
        cur_rows.append(row)
        size += len(line) + (1 if size else 0)
    if cur_lines:
        chunks.append(("\n".join(cur_lines), cur_rows))
    return chunks


def post_content(url, content):
    """POST one message. Honours 429 Retry-After, caps retries. Returns True
    delivered / False gave up. Never raises for HTTP-level failure and never
    prints the URL."""
    body = json.dumps({"content": content}).encode("utf-8")
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
    fresh, new_cursor = collect_new(runid, lane)
    if not fresh:
        # Still persist: a lane filter can advance the cursor (rows seen and
        # skipped by the filter) even when nothing was posted this pass.
        _save_cursor(runid, new_cursor, lane)
        return 0
    # Enrollment identity, read once per pass and joined by seat in
    # format_row. A run with no arm state (or a pre-identity roster) yields
    # {} and every line renders in the identity-free format.
    identities = swarm_arm.seat_identities(runid)
    skipped = False
    for content, chunk in chunk_rows(fresh, machine, identities=identities):
        if not post_content(url, content):
            _log_skipped(runid, chunk, "webhook delivery failed", lane)
            skipped = True
    # Advance past everything, delivered or skipped: the skipped file is the
    # durable record, and never advancing would wedge the mirror forever
    # behind one undeliverable batch.
    _save_cursor(runid, new_cursor, lane)
    return 1 if skipped else 0


def _warn_missing_secret(lane):
    sys.stderr.write(
        "discord mirror: webhook secret missing for lane %r; retrying in %ds\n"
        % (lane, MISSING_SECRET_RETRY_SECONDS)
    )


def follow(runid, interval, lane=DEFAULT_LANE):
    """Poll one run forever. LAUNCHD SAFETY: checks for the secret BEFORE
    calling run_once, so a missing secret never reaches resolve_webhook_url
    (whose multi-line drop-in message is meant for a one-shot --once, not
    once per poll) -- instead ONE stderr line, then a 60s backoff, so a
    launchd KeepAlive job waits quietly instead of crash-looping. See module
    docstring, LAUNCHD SAFETY."""
    rc = 0
    while True:
        if _find_webhook_url(lane) is None:
            _warn_missing_secret(lane)
            sleep_for = MISSING_SECRET_RETRY_SECONDS
        else:
            rc = max(rc, run_once(runid, lane))
            sleep_for = interval
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            return rc


def follow_all(interval, lane=DEFAULT_LANE):
    """Poll EVERY run under the mailbox root, once per pass, forever.
    Discovery is swarm_mailbox.run_ids() so a newly-armed run is picked up
    without restarting the process. LAUNCHD SAFETY: the secret is a
    lane-wide condition (not per-run), so it is checked once per pass,
    before touching any run -- warns once and backs off 60s exactly like
    follow() when absent, instead of re-discovering the same failure once
    per run."""
    rc = 0
    while True:
        if _find_webhook_url(lane) is None:
            _warn_missing_secret(lane)
            sleep_for = MISSING_SECRET_RETRY_SECONDS
        else:
            for runid in swarm_mailbox.run_ids():
                rc = max(rc, run_once(runid, lane))
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
