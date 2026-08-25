#!/usr/bin/env python3
"""swarm_threads: which mailbox rows belong to the same document, and which of
those groups are a LIVE CONVERSATION worth rendering.

WHY THIS IS ITS OWN MODULE, PURE, WITH NO I/O: two consumers need the same
answer -- adapters/discord's board lane (which posts a thread to Discord) and
`bin/comms threads` (which shows a human the same list in a terminal). Two
copies of a liveness rule drift, and the drift is invisible: the board renders
a thread the CLI says is dead, or the reverse, and neither is wrong on its own
terms. One predicate, imported twice. Nothing here opens a file, reads an env
var, or asks the clock -- every input arrives as an argument, which is what
makes the rule testable as a truth table instead of a fixture.

WHAT A THREAD KEY IS: lib/swarm_mailbox.thread_key turns a path into
"doc:<repo>/<relpath>"; a row that carries one has a `thread` field. This
module never derives a key -- it only groups rows that already have one.

THE ALIVE PREDICATE (P4), in words: a thread is alive when at least
`min_seats` DISTINCT seats have said something non-status in it, AND some
consecutive pair of those rows comes from two DIFFERENT seats no more than
`window_s` apart. Both halves are load-bearing:

  * distinct seats alone would render a thread where one seat posted and
    another posted a week later -- two speakers, no conversation.
  * a timely pair alone would render a thread where one seat posted twice in
    a minute -- a monologue with good rhythm.

Together they mean "somebody answered somebody, recently". That is the thing
a human reading a board wants a thread for; everything else is a log line,
and the un-threaded channel already carries those.

STATUS ROWS DO NOT COUNT, either as a speaker or as a step in the sequence.
The ambient "session started in <dir>" row is a birth announcement; counting
it would make every document one agent so much as opened look like a
two-party conversation. They are filtered out BEFORE the consecutive pairs
are walked, so a status row landing between two speakers cannot break an
exchange that really happened.

DEFAULTS ARE HUMAN JUDGMENT, NOT MEASUREMENTS, which is why they are
parameters and not constants baked into the body: 30 minutes because a
heartbeat-scale exchange lands well inside it, 2 seats because that is the
smallest number that can be a conversation. adapters/discord/mirror.py reads
COMMS_THREAD_ALIVE_SECONDS / COMMS_THREAD_ALIVE_SEATS and passes them
through; this module stays env-free.
"""

import datetime

# See the module docstring: knobs, with the mirror reading the env vars.
DEFAULT_WINDOW_S = 1800
DEFAULT_MIN_SEATS = 2

# The kind that announces a seat rather than saying anything in the thread.
STATUS_KIND = "status"

THREAD_FIELD = "thread"


def group_by_thread(rows):
    """{thread_key: [rows in input order]} for every row carrying a non-empty
    `thread`.

    Rows WITHOUT a thread are dropped, not collected under a None key: they
    belong to the un-threaded rendering path, a different destination
    entirely, and a None bucket would eventually be posted into a thread
    named after nothing. Rows are referenced, never copied -- a caller that
    persists a bucket is persisting the rows it was handed.
    """
    groups = {}
    for row in rows:
        key = row.get(THREAD_FIELD)
        if not key:
            continue
        groups.setdefault(key, []).append(row)
    return groups


def _parsed_at(row):
    """This row's `at` as an AWARE datetime, or None if it has none or it does
    not parse.

    Aware, always: mixing naive and aware datetimes raises TypeError on both
    comparison and subtraction, so one hand-written row without an offset
    would take down a whole mirror pass. A naive stamp reads as UTC because
    every writer in this repo emits UTC -- that is the true reading, not a
    guess. A stamp that does not parse at all yields None and its row is
    simply not counted: these rows come off disk (a held file, another
    machine's export) and a predicate that raised on malformed input would
    convert a bad row into an outage.
    """
    at = row.get("at")
    if not isinstance(at, str):
        return None
    text = at[:-1] + "+00:00" if at.endswith("Z") else at
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _speaking_rows(rows):
    """`rows` that count toward liveness -- non-status, with a readable `at`
    -- sorted by that `at`. Sorted here rather than trusted: callers merge
    two sources (a held file and a fresh read), and an unsorted merge reads
    as a negative gap, which fails the window test and silently never
    renders."""
    dated = [
        (at, row)
        for at, row in ((_parsed_at(r), r) for r in rows)
        if at is not None and row.get("kind") != STATUS_KIND
    ]
    dated.sort(key=lambda pair: pair[0])
    return dated


def alive(rows, window_s=DEFAULT_WINDOW_S, min_seats=DEFAULT_MIN_SEATS):
    """True if `rows` (one thread's rows) are a live conversation: at least
    `min_seats` distinct non-status seats, AND some consecutive pair from two
    different seats separated by 0 < gap <= window_s.

    in: rows, a list of row dicts (any order, any mix of kinds); window_s,
      seconds; min_seats, a count.
    out: True/False. Never raises for ordinary input -- a row with a missing
      or unparseable `at` is skipped, not fatal.
    side effects: none. Pure: no clock, no files, no env, and the rows are
      neither mutated nor reordered in place.

    WHY THE GAP MUST BE STRICTLY POSITIVE: two rows sharing a timestamp to
    the microsecond are one writer emitting both -- a replay, an import, a
    fixture -- not one seat answering another. The pair is skipped, which
    never vetoes a real exchange elsewhere in the same thread ("SOME
    consecutive pair", not "every pair").

    WHY CONSECUTIVE AND NOT ANY PAIR: any-pair would call a thread alive
    because seat A spoke on Monday and seat B on Monday-plus-ten-minutes,
    with an hour of one-sided noise in between -- the window is meant to
    catch a reply, and a reply is the NEXT thing said, not merely a nearby
    thing said.
    """
    dated = _speaking_rows(rows)
    seats = {row.get("seat") for _, row in dated}
    if len(seats) < min_seats:
        return False
    window = datetime.timedelta(seconds=window_s)
    for (at_a, row_a), (at_b, row_b) in zip(dated, dated[1:]):
        if row_a.get("seat") == row_b.get("seat"):
            continue
        gap = at_b - at_a
        if gap > datetime.timedelta(0) and gap <= window:
            return True
    return False
