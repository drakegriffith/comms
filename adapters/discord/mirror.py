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

CLI:
  mirror.py --once <runid>                  # mirror new rows, exit
  mirror.py --follow <runid> [--interval N] # poll loop (default 5s)
Exit: 0 mirrored (or nothing new) | 1 some rows skipped after retries |
      2 usage or missing webhook secret.
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


def _state_dir():
    # Same default chain as lib/swarm_arm.py so every comms component keeps
    # its state under one root.
    return os.environ.get("COMMS_STATE_DIR") or os.path.expanduser("~/.comms/state")


def _mirror_dir():
    return os.path.join(_state_dir(), "discord-mirror")


def _safe(runid):
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(runid))


def _cursor_path(runid):
    return os.path.join(_mirror_dir(), _safe(runid) + ".cursor.json")


def _skipped_path(runid):
    return os.path.join(_mirror_dir(), _safe(runid) + ".skipped.jsonl")


def resolve_webhook_url():
    """Env var first, else parse the secrets file. Exits 2 with the exact
    drop-in line when absent. Never prints the value."""
    url = os.environ.get(SECRET_VAR)
    if url:
        return url
    path = os.environ.get("COMMS_SECRETS_FILE") or os.path.expanduser(
        "~/.secrets/comms.env"
    )
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(SECRET_VAR + "="):
                    val = line.split("=", 1)[1].strip().strip("'\"")
                    if val:
                        return val
    except OSError:
        pass
    sys.stderr.write(
        "discord mirror: no webhook configured.\n"
        "  1. open -e ~/.secrets/comms.env\n"
        "  2. add line: DISCORD_COMMS_WEBHOOK_URL=<paste webhook URL from "
        "Discord channel settings>\n"
        "  3. chmod 600 ~/.secrets/comms.env\n"
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


def _load_cursor(runid):
    try:
        with open(_cursor_path(runid)) as fh:
            cur = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return cur if isinstance(cur, dict) else {}


def _save_cursor(runid, cursor):
    os.makedirs(_mirror_dir(), exist_ok=True)
    path = _cursor_path(runid)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cursor, fh)
    os.replace(tmp, path)


def collect_new(runid):
    """Return (fresh_rows, new_cursor). Per-seat row counts are the cursor:
    seat files are append-only single-writer, so row N of a seat is stable
    forever and 'new' is exactly indices >= cursor[seat]. read_siblings sorts
    by `at` with a stable sort, so per-seat file order is preserved."""
    rows = swarm_mailbox.read_siblings(runid, OBSERVER_SEAT)
    cursor = _load_cursor(runid)
    seen = {}
    fresh = []
    for row in rows:
        seat = row.get("seat", "?")
        idx = seen.get(seat, 0)
        seen[seat] = idx + 1
        if idx >= int(cursor.get(seat, 0)):
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


def _log_skipped(runid, rows, reason):
    """The never-drop-silently channel: skipped rows go to stderr AND a
    durable state-dir file, then the cursor may advance past them."""
    os.makedirs(_mirror_dir(), exist_ok=True)
    path = _skipped_path(runid)
    with open(path, "a") as fh:
        for row in rows:
            fh.write(json.dumps({"reason": reason, "row": row}) + "\n")
    sys.stderr.write(
        "discord mirror: SKIPPED %d row(s) (%s); recorded in %s\n"
        % (len(rows), reason, path)
    )


def run_once(runid):
    """Mirror everything new, exactly once. Exit-code semantics of main()."""
    url = resolve_webhook_url()  # before anything else: missing secret = 2 always
    machine = machine_label()
    fresh, new_cursor = collect_new(runid)
    if not fresh:
        return 0
    # Enrollment identity, read once per pass and joined by seat in
    # format_row. A run with no arm state (or a pre-identity roster) yields
    # {} and every line renders in the identity-free format.
    identities = swarm_arm.seat_identities(runid)
    skipped = False
    for content, chunk in chunk_rows(fresh, machine, identities=identities):
        if not post_content(url, content):
            _log_skipped(runid, chunk, "webhook delivery failed")
            skipped = True
    # Advance past everything, delivered or skipped: the skipped file is the
    # durable record, and never advancing would wedge the mirror forever
    # behind one undeliverable batch.
    _save_cursor(runid, new_cursor)
    return 1 if skipped else 0


def follow(runid, interval):
    rc = 0
    while True:
        rc = max(rc, run_once(runid))
        try:
            time.sleep(interval)
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
    if len(args) == 2 and args[0] == "--once":
        return run_once(args[1])
    if len(args) == 2 and args[0] == "--follow":
        try:
            return follow(args[1], interval)
        except KeyboardInterrupt:
            return 0
    sys.stderr.write(
        "usage: mirror.py --once <runid>\n"
        "       mirror.py --follow <runid> [--interval <seconds>]\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
