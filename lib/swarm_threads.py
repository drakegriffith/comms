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
import json
import os
import sys

# See the module docstring: knobs, with the mirror reading the env vars.
DEFAULT_WINDOW_S = 1800
DEFAULT_MIN_SEATS = 2

# The kind that announces a seat rather than saying anything in the thread.
STATUS_KIND = "status"

THREAD_FIELD = "thread"

# The env var NAMES for the two knobs above (issue #40's config table). Fixed
# spellings, not derived: adapters/discord/mirror.py's board lane reads these
# same two names into its own ALIVE_SECONDS_VAR/ALIVE_SEATS_VAR constants, and
# `threads` below (this module's own CLI) reads them a second time rather
# than importing mirror.py -- mirror.py pulls in a webhook adapter's worth of
# Discord-specific machinery (secrets, HTTP, a thread map) that a terminal
# metric command has no business depending on. Two constants holding one
# string apiece is not the drift D1 warns about: that ban is on reimplementing
# a DECISION (the alive predicate), not on two modules agreeing to read the
# same environment variable name.
ALIVE_SECONDS_VAR = "COMMS_THREAD_ALIVE_SECONDS"
ALIVE_SEATS_VAR = "COMMS_THREAD_ALIVE_SEATS"


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


# =============================================================================
# EVERYTHING BELOW THIS LINE DOES I/O. The predicate above stays pure -- no
# clock, no files, no env -- because two consumers (this module's own `threads`
# CLI, and scripts/comms_compile_threads.py) both need "every row on the
# board", and if that read lived twice, one copy drifting from the other would
# be invisible in exactly the way D1 already warned about for `alive` itself.
# So the read lives once, here, and both callers import it.
# =============================================================================

# Reserved observer seat name handed to swarm_mailbox.read_siblings, exactly
# the way adapters/discord/mirror.py's OBSERVER_SEAT works: read_siblings
# excludes only the NAMED seat's own file, so a name no real seat will ever
# use makes every real seat's rows visible. A second, differently-spelled
# reserved name (not "discord-mirror") -- an accidental real seat named after
# ONE observer would only go blind to that one reader, never both at once.
OBSERVER_SEAT = "comms-threads-cli"


def full_board_rows(swarm_mailbox, run=None):
    """Every row currently on the mailbox board: one named run's rows, or
    every run's if `run` is None.

    `swarm_mailbox` is passed in rather than imported at module load, so the
    predicate half of this file (above) never gains an import-time dependency
    on it -- a caller that only wants group_by_thread/alive still imports a
    module that touches no filesystem until this function is actually called.

    in: swarm_mailbox, the lib.swarm_mailbox module (its read_siblings and
      run_ids); run, an optional single runid, or None for every run
      swarm_mailbox.run_ids() currently lists.
    out: a flat list of row dicts, sorted by `at` WITHIN each run's own read
      (read_siblings' own guarantee) but not re-sorted ACROSS runs -- callers
      that care about a merged `at` order (group_by_thread's alive() sorts
      internally) get it for free; callers that only bucket by `thread` do
      not need it at all.
    side effects: none of its own (delegates every file read to
      swarm_mailbox); errors: none -- an unreadable run's directory reads as
      no rows for that run, the same degrade read_siblings already makes.
    """
    runids = [run] if run else swarm_mailbox.run_ids()
    rows = []
    for runid in runids:
        rows.extend(swarm_mailbox.read_siblings(runid, OBSERVER_SEAT))
    return rows


def last_gap_s(rows):
    """Seconds between the last two SPEAKING (non-status, dated) rows in
    `rows`, in `at` order, or None if fewer than two exist.

    A separate question from alive()'s gap: alive() asks whether SOME
    consecutive pair from two different seats lands inside a window; this
    asks how long it has been since the two most recent things anyone said,
    same seat or not -- what a human staring at `comms threads` wants to know
    about a thread that already failed the alive test ("how cold is this,
    exactly").
    """
    dated = _speaking_rows(rows)
    if len(dated) < 2:
        return None
    (at_a, _), (at_b, _) = dated[-2], dated[-1]
    return (at_b - at_a).total_seconds()


def _env_int(var, default):
    """This knob's value from the environment, or `default` if unset or
    unparseable. A typo in a launchd plist or a shell profile must degrade to
    the documented default, never take the command down -- see
    adapters/discord/mirror.py's own _env_int, which this mirrors in spelling
    but not by import (an 8-line env-parsing utility is not the kind of
    decision D1 bans duplicating; the alive predicate is)."""
    raw = os.environ.get(var)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        sys.stderr.write(
            "comms threads: %s=%r is not a number; using %d\n" % (var, raw, default)
        )
        return default


def _usage():
    # sys.stderr is looked up HERE, at call time, not bound as a default
    # argument at function-definition time -- a default of `stream=sys.stderr`
    # would capture the real stderr object once at import, before a test's
    # capsys fixture ever gets a chance to swap it out from under this
    # module.
    sys.stderr.write(
        "usage: swarm_threads.py threads [--alive SECONDS] [--seats N] "
        "[--run RUNID | --all-runs] [--json]\n"
    )


def _extract_threads_flags(args):
    """Pull `--alive SECONDS`, `--seats N`, `--run RUNID`, `--all-runs`, and
    `--json` out of a positional arg list. Returns (remaining_args, flags).
    Mirrors swarm_mailbox._extract_flags's shape (flags may appear anywhere,
    unknown leftovers are returned rather than swallowed) so a caller sees
    the same "unexpected argument" failure mode as every other subcommand in
    this stack.
    """
    alive_s = None
    seats = None
    run = None
    all_runs = False
    want_json = False
    out = []
    i = 0
    while i < len(args):
        if args[i] == "--alive":
            if i + 1 >= len(args):
                raise ValueError("--alive needs a value")
            alive_s = int(args[i + 1])
            i += 2
            continue
        if args[i] == "--seats":
            if i + 1 >= len(args):
                raise ValueError("--seats needs a value")
            seats = int(args[i + 1])
            i += 2
            continue
        if args[i] == "--run":
            if i + 1 >= len(args):
                raise ValueError("--run needs a value")
            run = args[i + 1]
            i += 2
            continue
        if args[i] == "--all-runs":
            all_runs = True
            i += 1
            continue
        if args[i] == "--json":
            want_json = True
            i += 1
            continue
        out.append(args[i])
        i += 1
    if run is not None and all_runs:
        raise ValueError("pass either --run or --all-runs, not both")
    return out, {
        "alive": alive_s,
        "seats": seats,
        "run": run,
        "all_runs": all_runs,
        "json": want_json,
    }


def _inspect_threads(swarm_mailbox, window_s, min_seats, run):
    """(threads_inspected, threads_alive, results) for one CLI invocation.

    results is a list of dicts, one per thread key, sorted by key for a
    deterministic terminal (and test) output: {"thread", "alive", "seats",
    "rows", "last_gap_s"}. `rows` counts every row group_by_thread bucketed
    under that key (status rows included -- the row-count column answers "how
    much traffic", not "how much counted toward liveness"); `seats` counts
    only the non-status speakers, the same set alive() computes liveness over.
    """
    rows = full_board_rows(swarm_mailbox, run=run)
    groups = group_by_thread(rows)
    threads_inspected = len(groups)
    results = []
    for key in sorted(groups):
        bucket = groups[key]
        seats = {
            r.get("seat") for r in bucket if r.get("kind") != STATUS_KIND
        }
        results.append(
            {
                "thread": key,
                "alive": alive(bucket, window_s=window_s, min_seats=min_seats),
                "seats": len(seats),
                "rows": len(bucket),
                "last_gap_s": last_gap_s(bucket),
            }
        )
    threads_alive = sum(1 for r in results if r["alive"])
    return threads_inspected, threads_alive, results


def _run_threads(swarm_mailbox, args):
    try:
        rest, flags = _extract_threads_flags(args)
    except ValueError as exc:
        sys.stderr.write("swarm_threads.py threads: %s\n" % exc)
        _usage()
        return 2
    if rest:
        sys.stderr.write(
            "swarm_threads.py threads: unexpected argument(s): %s\n"
            % " ".join(rest)
        )
        _usage()
        return 2

    window_s = (
        flags["alive"]
        if flags["alive"] is not None
        else _env_int(ALIVE_SECONDS_VAR, DEFAULT_WINDOW_S)
    )
    min_seats = (
        flags["seats"]
        if flags["seats"] is not None
        else _env_int(ALIVE_SEATS_VAR, DEFAULT_MIN_SEATS)
    )

    threads_inspected, threads_alive, results = _inspect_threads(
        swarm_mailbox, window_s, min_seats, flags["run"]
    )

    # THE POSITIVE CONTROL: a metric that inspected zero threads never had the
    # chance to say anything true about liveness -- exit 2 names that state
    # instead of printing "threads_inspected=0 threads_alive=0" and looking
    # exactly like a quiet, healthy board (issue #43; see the epistemics
    # invariant this whole command exists to satisfy: silence is not
    # evidence).
    if threads_inspected == 0:
        sys.stderr.write(
            "swarm_threads.py threads: threads_inspected=0 -- inspected "
            "nothing, not a pass\n"
        )
        return 2

    if flags["json"]:
        print(
            json.dumps(
                {
                    "threads_inspected": threads_inspected,
                    "threads_alive": threads_alive,
                }
            )
        )
        for r in results:
            print(json.dumps(r))
    else:
        print(
            "threads_inspected=%d threads_alive=%d"
            % (threads_inspected, threads_alive)
        )
        for r in results:
            gap = "-" if r["last_gap_s"] is None else "%.0f" % r["last_gap_s"]
            print(
                "%s: alive=%s seats=%d rows=%d last_gap_s=%s"
                % (r["thread"], r["alive"], r["seats"], r["rows"], gap)
            )
    return 0


def main(argv):
    """CLI entry point: `swarm_threads.py threads [flags]`.

    A LATER bin/comms case is meant to be a pure `exec python3
    .../swarm_threads.py threads "$@"` (see bin/comms's existing dispatch
    pattern for swarm_mailbox/swarm_arm/swarm_claims) -- so this function
    takes argv in that same shape (argv[0] is the script path, argv[1] is the
    subcommand) and does no argv[0]-stripping games that would only work when
    invoked one particular way.

    swarm_mailbox is imported HERE, not at module load, for the same reason
    full_board_rows takes it as a parameter: importing it only when a CLI
    command actually runs keeps `import swarm_threads` free of any
    filesystem dependency for every caller that only wants the predicate.
    """
    if len(argv) < 2 or argv[1] != "threads":
        _usage()
        return 2
    import swarm_mailbox

    return _run_threads(swarm_mailbox, argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
