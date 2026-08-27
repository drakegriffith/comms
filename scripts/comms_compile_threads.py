#!/usr/bin/env python3
"""comms_compile_threads: turn the mailbox's threaded rows into vault notes a
human (or an embedding index) can read, one note per board per local date.

WHY THIS EXISTS: the mailbox (lib/swarm_mailbox) is machine-local, JSONL, and
grows without bound; nobody reads it as prose. lib/swarm_threads answers "is
this document's conversation alive"; this script answers a different
question -- "what did the alive-or-not conversations actually SAY, filed by
day, so a human can catch up without tailing JSON". It reads the mailbox
ONLY -- never Discord, never the board lane's held/thread-map state under
adapters/discord/threads.py -- because those are RENDERING decisions (do we
have a Discord thread yet) and this is a RECORD-KEEPING one (what happened).
The two must never merge: a document nobody has posted about twice yet still
gets a compiled note once time runs long enough, on this script's own
schedule, independent of whether Discord ever opened a thread for it at all.

WHAT A "BOARD" IS: the repo half of a thread key. lib/swarm_mailbox.thread_key
produces "doc:<repo>/<relpath>" (or "doc:<repo>" for the repo root); the board
is <repo>. Design note P2 (issue #43's synthesis, file 15) names the routing
topic for this "board:<repo>", but no writer in this repo emits that topic yet
-- doc-subscription topics are deferred to a later, unbuilt slice (P3). Rather
than depend on plumbing that does not exist, the board is read directly off
the `thread` field every threaded row already carries today. If a future slice
starts writing topic="board:<repo>", it will describe the exact same string
this script already derives, and this script needs no change.

WHAT NEVER GETS COMPILED: any row whose kind is the STATUS_KIND
(lib/swarm_threads.STATUS_KIND, "status") -- the ambient "session started"
birth announcement, never a contribution to a conversation. This is checked
by comparing against swarm_threads.STATUS_KIND, imported, not a second
hardcoded string -- the closed-vocabulary rule this repo already enforces for
kinds (swarm_mailbox.VALID_KINDS) means a kind check written twice is two
places for a new kind to be forgotten in one of them.

NOTE SHAPE: one file per (board, local date):
  <vault>/research/agent-threads/<board>/<date>.md
Front matter (the vault's real convention -- title/type/description/date/
tags/index_mode -- plus this note's own positive control):
  title, type, description, date, tags, index_mode: raw,
  board, threads_inspected, threads_alive
Body: one H2 per thread key that has a row on this date, in the order the
key's EARLIEST row that date appears; rows under it in `at` order as
  "- HH:MM seat: text"
(HH:MM in local time, matching the date bucketing, which is also local).
A key whose most recent PRIOR appearance was on an earlier date gets one line
at the top of its section: "continues: <that date>.md".

THE WATERMARK (P5: "close is a watermark, never a mutation"): one file per
board, $COMMS_STATE_DIR/thread-compile/<board>.watermark.json, holding a
per-date DIGEST -- sha256 of the last text this script rendered for that
date -- never an `at` high-water mark. A mark keyed on "the newest `at` seen
so far" can only move forward, so a row whose `at` sorts BEFORE the mark (a
late sync from another machine, a clock-skewed writer, a backdated post)
never crosses it and is silently never rendered, on this run or any later
one, even though every pass reads it off the full board. A digest has no
"before" or "after": every pass recomputes each date's full note from
whatever rows currently exist for it and writes only when that changes the
digest, so a late-arriving row is compiled the very next time this script
runs, not never. This does mean every pass walks EVERY date the board has
ever had activity on, not just "new" ones -- accepted, because content is a
pure function of a date's row set and the digest check keeps the actual disk
write (and the reported notes_written) to only the dates that changed.
Nothing about the mailbox is ever touched -- purging or truncating a seat's
jsonl is not this script's job and it never attempts it. Re-running with no
changed dates writes no note files, and re-running from a wiped watermark
regenerates byte-identical notes, because a note's content is a pure
function of that date's full row set, walked in the same chronological
order every time (thread_dates, the continuation map, is likewise rebuilt
from scratch each pass rather than carried in the watermark, for the same
reason -- it is cheap to re-derive and a second persisted copy is a second
place for the two to disagree).

WHY THIS MAKES 18:00 AND 01:00 THE SAME CODE PATH: nothing here is a clock
check -- "close at 1 a.m." (P5) is just this script's SECOND scheduled
invocation each day (see adapters/launchd/com.comms.thread-compile.plist),
not a special branch here. Whatever has changed gets rewritten, regardless
of what time it is when this runs.

THE POSITIVE CONTROL: exit 2 if zero threaded, non-status rows exist ANYWHERE
on the board this pass -- a compile that inspected nothing is not a quiet,
healthy pass, it is indistinguishable from a wiped mailbox (COMMS_ROOT
defaults to /tmp; see file 15's P1 guard) and must say so loudly rather than
writing nothing and exiting 0.

AFTERWARDS: if <vault>/scripts/sync_embed_mirror.sh exists and is executable,
it is run (so a compiled note is searchable without a separate cron); if not,
one stderr line says so and the compile still exits 0 -- a vault without an
embed mirror script is not this script's failure to report.

ENV: COMMS_ROOT / COMMS_STATE_DIR (same names and defaults as every other
module in this stack); COMMS_VAULT_ROOT (default ~/brain-actual-intelligence,
overridable so tests never touch the real vault).

CLI: comms_compile_threads.py            # compile everything new, then exit
Exit: 0 compiled (or nothing new to compile) | 2 zero rows inspected.
"""

import datetime
import hashlib
import json
import os
import subprocess
import sys

SELF_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SELF_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
import swarm_mailbox  # noqa: E402  (the one mailbox parser; thread_key's prefix)
import swarm_threads  # noqa: E402  (full_board_rows, group_by_thread, alive, STATUS_KIND)

VAULT_ROOT_VAR = "COMMS_VAULT_ROOT"
DEFAULT_VAULT_ROOT = os.path.expanduser("~/brain-actual-intelligence")

NOTES_SUBDIR = ("research", "agent-threads")
STATE_SUBDIR = "thread-compile"

# The repo half of a thread key: "doc:<repo>/<relpath>" or "doc:<repo>". See
# module docstring, WHAT A "BOARD" IS.
_KEY_PREFIX = swarm_mailbox.THREAD_KEY_PREFIX


def _vault_root():
    return os.environ.get(VAULT_ROOT_VAR) or DEFAULT_VAULT_ROOT


def _state_dir():
    # Same default chain as adapters/discord/mirror.py._state_dir and
    # lib/swarm_arm.py, so every comms component's state lands under one
    # root without this script inventing a second convention.
    return os.environ.get("COMMS_STATE_DIR") or os.path.expanduser("~/.comms/state")


def _watermark_dir():
    return os.path.join(_state_dir(), STATE_SUBDIR)


def _safe(name):
    # A board name is a git repo's basename -- normally filesystem-safe
    # already -- but this mirrors adapters/discord/mirror.py's own _safe
    # rather than assuming: a repo directory can be named almost anything.
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(name))


def _watermark_path(board):
    return os.path.join(_watermark_dir(), _safe(board) + ".watermark.json")


def board_of(thread_key):
    """The repo name out of a thread key, or None if `thread_key` is not one
    of ours (defensive -- every row group_by_thread hands back already has a
    non-empty `thread`, but a key spelled by a future writer that does not
    start with "doc:" must not crash the compile over one row)."""
    if not thread_key or not thread_key.startswith(_KEY_PREFIX):
        return None
    rest = thread_key[len(_KEY_PREFIX):]
    board, _, _ = rest.partition("/")
    return board or None


def _parsed_at(row):
    # Reuses swarm_threads' own parse rather than a second copy: same
    # Z-suffix and naive-reads-as-UTC handling, same "unparseable is skipped,
    # not fatal" contract.
    return swarm_threads.parsed_at(row)


def _local_date_and_time(dt):
    """(local ISO date string, local "HH:MM" string) for aware datetime
    `dt`. .astimezone() with no argument converts to the SYSTEM's local
    timezone (Python's documented behavior since 3.3) -- the same "local" a
    human reading the vault on this machine experiences, and what "close at
    1 a.m." (a wall-clock instant) means in the first place."""
    local = dt.astimezone()
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


def _atomic_write_text(path, text):
    """tmp + fsync + os.replace, PID-suffixed tmp so two concurrent compiles
    (there should only ever be one, but launchd's KeepAlive-free
    StartCalendarInterval jobs are not mutually exclusive by construction)
    never collide on the tmp name."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _atomic_write_json(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w") as fh:
        json.dump(data, fh, sort_keys=True, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _load_watermark(board):
    """{"digests": {date: sha256 hex}} for `board`, or the empty shape if
    there is no watermark file yet or it is unreadable/corrupt -- a corrupt
    watermark degrades to "every date looks changed, re-render all of them",
    which re-derives correct notes (content is a pure function of the row
    set); the other direction (treating corrupt as "everything already
    compiled") would silently stop compiling that board forever.

    WHY A DIGEST, NOT A HIGH-WATER `at` MARK (the shape this replaced): see
    the module docstring's THE WATERMARK section -- a mark keyed on "newest
    `at` seen" can never notice a row whose `at` sorts before it (a late
    sync, clock skew, a backdated post), because such a row never crosses
    the mark. A digest is recomputed from the CURRENT row set every pass, so
    a late row changes the digest and forces a re-render no matter where its
    `at` falls in the sort order.
    """
    try:
        with open(_watermark_path(board)) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"digests": {}}
    if not isinstance(data, dict):
        return {"digests": {}}
    return {"digests": dict(data.get("digests") or {})}


def _save_watermark(board, watermark):
    _atomic_write_json(_watermark_path(board), watermark)


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _note_path(board, date):
    return os.path.join(_vault_root(), *NOTES_SUBDIR, board, date + ".md")


def _render_note(board, date, groups, window_s, min_seats, thread_dates_before):
    """The full text of one (board, date) note.

    `groups`: {thread_key: [rows]} for every threaded row dated `date` for
    `board` (ALL rows, not just ones new this pass -- see module docstring,
    idempotence). `thread_dates_before`: the watermark's thread_dates map AS
    OF BEFORE this date was processed (the continuation pointer looks
    backward only, never at a date being written in the same pass that comes
    later chronologically).

    Returns (text, updated_thread_dates) -- the caller folds
    updated_thread_dates into the watermark it eventually persists.
    """
    keys = sorted(
        groups,
        key=lambda k: min(_parsed_at(r) or datetime.datetime.max.replace(
            tzinfo=datetime.timezone.utc
        ) for r in groups[k]),
    )
    threads_inspected = len(keys)
    threads_alive = 0
    threads_exchange = 0
    sections = []
    updated = dict(thread_dates_before)
    for key in keys:
        rows = groups[key]
        dated = sorted(
            ((_parsed_at(r), r) for r in rows if _parsed_at(r) is not None),
            key=lambda pair: pair[0],
        )
        is_alive = swarm_threads.alive(
            rows, window_s=window_s, min_seats=min_seats
        )
        is_exchange = swarm_threads.exchange(
            rows, window_s=window_s, min_seats=min_seats
        )
        if is_alive:
            threads_alive += 1
        if is_exchange:
            threads_exchange += 1
        lines = ["## %s" % key]
        lines.append("exchange: %s" % ("yes" if is_exchange else "no"))
        prev_date = thread_dates_before.get(key)
        if prev_date and prev_date != date:
            lines.append("continues: %s.md" % prev_date)
            lines.append("")
        for at, row in dated:
            _, hhmm = _local_date_and_time(at)
            lines.append(
                "- %s %s: %s" % (hhmm, row.get("seat", "?"), row.get("text", ""))
            )
        sections.append("\n".join(lines))
        updated[key] = date

    description = "Compiled agent-to-agent thread activity for %s on %s: " \
        "%d thread(s), %d alive, %d exchange." % (
            board, date, threads_inspected, threads_alive, threads_exchange
        )
    front = [
        "---",
        'title: "%s thread log -- %s"' % (board, date),
        "type: Research Note",
        'description: "%s"' % description,
        "date: %s" % date,
        "tags: [comms, agent-threads, %s]" % board,
        "index_mode: raw",
        "board: %s" % board,
        "threads_inspected: %d" % threads_inspected,
        "threads_alive: %d" % threads_alive,
        "threads_exchange: %d" % threads_exchange,
        "---",
        "",
        "# %s -- %s" % (board, date),
        "",
    ]
    text = "\n".join(front) + "\n" + "\n\n".join(sections) + "\n"
    return text, updated


def compile_once():
    """Compile every board's un-compiled threaded rows into vault notes.

    Returns (rows_inspected, notes_written) -- rows_inspected is the total
    count of non-status, threaded rows found ANYWHERE on the board this pass
    (the positive control's own number, computed before any per-board
    watermark filtering); notes_written is how many (board, date) files were
    actually rewritten.
    """
    all_rows = swarm_threads.full_board_rows(swarm_mailbox)
    threaded_rows = [
        r
        for r in all_rows
        if r.get("kind") != swarm_threads.STATUS_KIND and r.get(swarm_threads.THREAD_FIELD)
    ]
    rows_inspected = len(threaded_rows)
    if rows_inspected == 0:
        return 0, 0

    window_s = swarm_threads.env_int(
        swarm_threads.ALIVE_SECONDS_VAR, swarm_threads.DEFAULT_WINDOW_S
    )
    min_seats = swarm_threads.env_int(
        swarm_threads.ALIVE_SEATS_VAR, swarm_threads.DEFAULT_MIN_SEATS
    )

    by_board = {}
    for row in threaded_rows:
        board = board_of(row.get(swarm_threads.THREAD_FIELD))
        if board is None:
            continue
        by_board.setdefault(board, []).append(row)

    notes_written = 0
    for board in sorted(by_board):
        board_rows = by_board[board]
        watermark = _load_watermark(board)
        old_digests = watermark["digests"]

        by_date = {}
        for row in board_rows:
            at = _parsed_at(row)
            if at is None:
                continue
            date, _ = _local_date_and_time(at)
            by_date.setdefault(date, []).append(row)

        # EVERY date this board has ever had activity on is walked every
        # pass (not just ones touched by a naive "since last time" filter --
        # see module docstring, THE WATERMARK): a late row's date must be
        # re-considered even when that date is not the most recent one. The
        # continuation map is rebuilt from scratch in this same ascending
        # walk, so a late row on an OLD date can still shift a LATER date's
        # "continues" pointer correctly if it changes which date a key was
        # last seen on.
        thread_dates = {}
        new_digests = {}
        for date in sorted(by_date):
            groups = swarm_threads.group_by_thread(by_date[date])
            text, thread_dates = _render_note(
                board, date, groups, window_s, min_seats, thread_dates
            )
            digest = _digest(text)
            new_digests[date] = digest
            if old_digests.get(date) != digest:
                _atomic_write_text(_note_path(board, date), text)
                notes_written += 1

        _save_watermark(board, {"digests": new_digests})

    return rows_inspected, notes_written


def _run_sync_embed_mirror():
    path = os.path.join(_vault_root(), "scripts", "sync_embed_mirror.sh")
    if os.path.isfile(path) and os.access(path, os.X_OK):
        try:
            subprocess.run([path], check=False)
        except OSError as exc:
            sys.stderr.write(
                "comms_compile_threads: sync_embed_mirror.sh failed to start: %s\n"
                % exc
            )
        return
    sys.stderr.write(
        "comms_compile_threads: %s not present or not executable -- skipped\n"
        % path
    )


def main(argv):
    if len(argv) > 1:
        sys.stderr.write("usage: comms_compile_threads.py\n")
        return 2
    rows_inspected, notes_written = compile_once()
    if rows_inspected == 0:
        sys.stderr.write(
            "comms_compile_threads: rows_inspected=0 -- inspected nothing, "
            "not a pass\n"
        )
        return 2
    print(
        "rows_inspected=%d notes_written=%d" % (rows_inspected, notes_written)
    )
    _run_sync_embed_mirror()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
