#!/usr/bin/env python3
"""swarm_mailbox: a file-backed mailbox for agent-to-agent comms among the
seats of one multi-agent run. Runtime-agnostic: any agent that can run a shell
command can post and read.

WHY A FILE MAILBOX AND NOT A RUNTIME MESSAGING PRIMITIVE:
  Direct agent-to-agent send primitives are runtime-specific and can silently
  false-succeed -- report the send worked when the peer never receives it
  (measured on one Claude Code binary version; Claude Code is one supported
  runtime among several, and other runtimes have no send primitive at all).
  This helper has no version dependency and no addressing failure mode: every
  seat WRITES its own file and READS its siblings', so nothing can be lost by
  a transport that lies about delivery -- on any runtime, including one nobody
  has measured yet.

ESCALATION TO A PARENT/ORCHESTRATOR stays on whatever channel the runtime
  provides (or the parent polls this mailbox itself). Seat<->seat is this
  mailbox.

COLLISION-FREE BY CONSTRUCTION:
  Each seat writes ONLY its own <seat>.jsonl and only ever appends. One writer
  per file means concurrent posts by different seats touch different files and
  never race. Do NOT let two seats share a file -- that reintroduces the race
  this design removes. Within a seat, appends are ordered by that seat's own
  single thread of execution.

NO NOTIFICATION EXISTS. read_siblings is a poll: a reader sees whatever its
siblings have flushed so far. Call it again to see more. Push-style delivery is
an adapter's job (see adapters/ in this repo); polling is the universal
baseline every runtime can do.

TOPICS ARE THE SCALE LEVER. Every row carries a `topic` (default "default"). A
reader can pull only its own slice -- read_siblings(runid, seat, topic="X")
returns only topic-X rows -- instead of the whole board. Without topics every
reader sees every peer's every message, so injected context grows with the
square of the swarm; with topics each reader pulls the fraction addressed to its
concern, which is what keeps a large swarm's per-reader context bounded. topic
is optional and backward-compatible: a row written without one, and a read
without one, behave exactly as before.

SUBSCRIPTIONS ARE HOW A 50-AGENT SWARM STOPS FUNNELLING EVERYTHING INTO EVERYONE.
A single `--topic` on the read is one-topic-per-reader; that is too coarse once a
swarm spans several projects, because the common case is "my project's channel
PLUS a broadcast channel", i.e. a SET of topics, not one. So a seat declares the
set of topics it wants -- subscribe(runid, seat, topics) -- and read_for(runid,
seat) returns only rows in that set. The addressing model is topic/channel
subscription (pub/sub with exact-match bindings): a POST names one topic (or a
recipient), a READ names a subscription set, and delivery is the intersection.
This decouples the poster from the roster: a post to a project's topic reaches
whoever is subscribed to it, without the poster knowing (or maintaining) the list
of seat names in that project -- which at 50 agents churning across ~8 projects
is stale constantly. An explicit recipient list (to:[a,b,c]) would push that
churning O(N) roster onto every poster; it is kept ONLY for the rare direct
message to one known peer (see UNICAST). Chosen over a broker/binding scheme
because a topic string already IS the routing key and a subscription set already
IS the exact-match binding -- no broker process is needed.

UNICAST: a message to exactly one seat rides a reserved topic "@<seat>". Every
seat is implicitly subscribed to its OWN "@<seat>" whether or not it lists it, so
a direct message can never be lost to a stale subscription. post(..., to="seatC")
writes topic "@seatC"; only seatC's read_for surfaces it. Real topics never begin
with "@", so unicast and fan-out never collide and old rows (no "@" topic) are
unaffected.

BACKWARD COMPATIBLE: a seat that never calls subscribe() has NO registered
subscription, and read_for then returns every sibling row -- exactly the
pre-subscription whole-board behavior. read_siblings and its --topic filter are
unchanged. New rows are byte-identical to old ones except a unicast row carries
one extra "to" key; a fan-out row is unchanged.

DISCOVERY: run_ids() lists every runid currently under the mailbox root, for
callers that mirror or poll ALL runs rather than one named runid (e.g. a
--follow-all loop that does not know the runid set up front and must notice a
new run appearing). Like every path helper in this module it reads _root()
fresh on each call rather than caching it -- a cached root is the same footgun
_root()'s own docstring warns about: an import-time snapshot pins whatever
COMMS_ROOT was (or was not) set before the first import, so a caller that sets
COMMS_ROOT afterward -- the normal case for a test fixture or a per-invocation
env override -- would silently keep discovering runs in the wrong root.

CLI:
  swarm_mailbox.py init <runid>
  swarm_mailbox.py subscribe <runid> <seat> <topic> [<topic> ...]      # register a seat's topic set
  swarm_mailbox.py post <runid> <seat> <kind> <text> [--topic <name> | --to <seat>]  # kind: finding|claim|blocker|comment|reply|status
  swarm_mailbox.py read <runid> <seat> [--topic <name>]               # rows from every OTHER seat (one topic or all)
  swarm_mailbox.py read <runid> <seat> --subs                          # only this seat's subscribed slice + its unicasts
"""

import datetime
import json
import os
import sys

VALID_KINDS = ("finding", "claim", "blocker", "comment", "reply", "status")

# The subset of VALID_KINDS that count as agent-to-agent CONVERSATION rather
# than a status/progress broadcast -- the discord mirror's convo lane uses
# this (plus any unicast row, any kind) to decide what mirrors to the
# conversation channel. Lives next to VALID_KINDS, not in the mirror, so the
# two vocabularies (what a row CAN be vs. what counts as chatter) stay next
# to each other and a future kind addition forces a look at both.
CONVO_KINDS = ("comment", "reply")

# A unicast row (a message to one seat) rides a reserved topic "@<seat>". A real
# topic must never start with this, or a fan-out topic could impersonate a direct
# address. Enforced in post() for the `to` path; real topics are caller-supplied
# and by convention never begin with "@".
SELF_TOPIC_PREFIX = "@"


def _root():
    """The mailbox root, read at CALL TIME not import time.

    A module-level constant here is a footgun: every caller imports before it
    could set COMMS_ROOT, so an import-time read pins /tmp and the documented
    override silently does nothing (it also broke test isolation -- fixed
    writes leaked across runs and a green suite went red on its 2nd run).
    Reading per call makes the knob real.
    """
    return (
        os.environ.get("COMMS_ROOT")
        or os.environ.get("CLAUDE_SWARM_ROOT")  # migration compatibility: pre-extraction env name
        or "/tmp"
    )


def _dir(runid):
    return os.path.join(_root(), "comms-%s" % runid)


def run_ids():
    """Sorted list of every runid currently present under the mailbox root
    (every "comms-*" directory, prefix stripped).

    Discovery helper for adapters that mirror across ALL runs at once (e.g.
    a --follow-all poll loop) instead of one runid at a time. Calls _root()
    fresh -- same reason as every other call in this module: caching it
    would pin whatever root was set at import time and silently ignore
    COMMS_ROOT set afterward (see _root docstring).
    """
    root = _root()
    try:
        names = os.listdir(root)
    except OSError:
        return []
    prefix = "comms-"
    ids = [
        name[len(prefix):]
        for name in names
        if name.startswith(prefix) and os.path.isdir(os.path.join(root, name))
    ]
    return sorted(ids)


def init(runid):
    """Create the run's mailbox directory (idempotent). Returns its path."""
    d = _dir(runid)
    os.makedirs(d, exist_ok=True)
    return d


def _valid_seat(seat):
    """A seat name must be a safe single path segment (it becomes a filename)."""
    return bool(seat) and "/" not in seat and os.sep not in seat and seat not in (".", "..")


def _seat_path(runid, seat):
    if not _valid_seat(seat):
        raise ValueError("invalid seat name %r" % seat)
    return os.path.join(_dir(runid), "%s.jsonl" % seat)


def _subs_path(runid, seat):
    # Subscription files are "<seat>.subs", a DIFFERENT suffix from the ".jsonl"
    # message files, so read_siblings never mistakes a subs file for a mailbox.
    if not _valid_seat(seat):
        raise ValueError("invalid seat name %r" % seat)
    return os.path.join(_dir(runid), "%s.subs" % seat)


def subscribe(runid, seat, topics):
    """Register the set of topics this seat wants delivered to it, and return the
    cleaned, sorted set actually written.

    ADDRESSING: a subscription is how a reader declares which channels reach it,
    the read-side dual of a post naming a topic. The common case is a two-element
    set: ["<project>", "broadcast"]. Overwrites the seat's OWN <seat>.subs file --
    one writer per file, so subscriptions stay collision-free the same way the
    message log does. Written via a temp-file + os.replace so a concurrent reader
    never sees a half-written subscription. A seat is ALWAYS delivered its own
    unicast topic "@<seat>" (added at read time by subscriptions()), whether or
    not it appears here, so a direct message survives any subscription set.
    """
    if isinstance(topics, str):
        topics = [topics]
    if not _valid_seat(seat):
        raise ValueError("invalid seat name %r" % seat)
    init(runid)
    clean = sorted({t for t in topics if t})
    path = _subs_path(runid, seat)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(clean, fh)
    os.replace(tmp, path)  # atomic overwrite; no reader sees a partial set
    return clean


def subscriptions(runid, seat):
    """Return this seat's subscribed topic set, with its own unicast topic
    "@<seat>" always included, or None if the seat never subscribed.

    None is meaningful: it means "no subscription registered", which read_for
    reads as "see the whole board" -- the pre-subscription behavior, so a seat
    that never enrolls is unaffected by this layer.
    """
    path = _subs_path(runid, seat)
    try:
        with open(path) as fh:
            topics = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return set(topics) | {SELF_TOPIC_PREFIX + seat}


def post(runid, seat, kind, text, topic=None, to=None):
    """Append one JSON-line row to the caller's OWN <seat>.jsonl.

    kind must be one of finding|claim|blocker|comment|reply|status (a closed
    vocabulary -- an unknown kind is a hard error, never a silent accept).
    comment/reply carry mid-run conversational exchange between live seats;
    status carries progress notes. Routing is one of two shapes,
    never both at once:
      * FAN-OUT: topic="X" (or nothing -> "default"). Reaches every seat
        subscribed to X. This is the common path.
      * UNICAST: to="seatC". Reaches only seatC. Implemented as topic "@seatC";
        passing both `to` and a conflicting `topic` is a hard error.
    A fan-out row is byte-identical to the pre-subscription format; a unicast row
    additionally carries a "to" key (for rendering and audit). Returns the row.
    """
    if kind not in VALID_KINDS:
        raise ValueError(
            "invalid kind %r; must be one of %s" % (kind, "|".join(VALID_KINDS))
        )
    if to is not None:
        if not _valid_seat(to):
            raise ValueError("invalid recipient seat %r" % to)
        unicast_topic = SELF_TOPIC_PREFIX + to
        if topic is not None and topic != unicast_topic:
            raise ValueError("pass either topic= or to=, not both")
        topic = unicast_topic
    init(runid)  # ensure the dir exists even if init() was skipped
    row = {
        "seat": seat,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "kind": kind,
        "text": text,
        "topic": topic if topic else "default",
    }
    if to is not None:
        row["to"] = to
    path = _seat_path(runid, seat)
    # Append-only, one writer (this seat) per file. "a" opens at end atomically
    # per write for a single line, so a seat's own sequential appends never
    # interleave with themselves, and different seats write different files.
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def _all_sibling_rows(runid, seat):
    """Parse every OTHER seat's .jsonl into a flat, UNFILTERED, UNSORTED list.

    The one parser shared by read_siblings (topic filter) and read_for
    (subscription filter) so the two delivery paths cannot drift. Never returns
    the caller's own rows -- a seat reads siblings, not itself. Malformed lines (a
    partially-flushed final line from a concurrent writer) are skipped rather than
    crashing the reader.
    """
    d = _dir(runid)
    if not os.path.isdir(d):
        return []
    own = os.path.basename(_seat_path(runid, seat))
    rows = []
    for name in os.listdir(d):
        if not name.endswith(".jsonl") or name == own:
            continue
        try:
            with open(os.path.join(d, name)) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    return rows


def read_siblings(runid, seat, topic=None):
    """Return sibling rows sorted by `at`. When `topic` is given, return only rows
    in that topic (a row with no topic key counts as "default", so old rows filter
    coherently); when None, return every topic. Unchanged from the pre-
    subscription API -- read_for is the subscription-honoring reader.
    """
    rows = _all_sibling_rows(runid, seat)
    if topic is not None:
        rows = [r for r in rows if (r.get("topic") or "default") == topic]
    rows.sort(key=lambda r: r.get("at", ""))
    return rows


def read_for(runid, seat):
    """Subscription-honoring read: return only the sibling rows this seat is
    subscribed to (its topic slice) plus any unicast rows addressed to it,
    sorted by `at`.

    This is the SCALE path: a reader pulls its slice, never the whole board. If
    the seat has no registered subscription (never called subscribe()), returns
    EVERY sibling row -- identical to read_siblings(topic=None), so an un-enrolled
    seat keeps the old behavior. A seat's own unicast topic "@<seat>" is always in
    its subscription set, so a direct message always lands.
    """
    subs = subscriptions(runid, seat)  # None if unregistered; else includes @self
    rows = _all_sibling_rows(runid, seat)
    if subs is not None:
        rows = [r for r in rows if (r.get("topic") or "default") in subs]
    rows.sort(key=lambda r: r.get("at", ""))
    return rows


def append_mirrored(runid, mirror_seat, rows):
    """Append rows that some OTHER machine authored, verbatim, into a mirror
    file, and return how many were written.

    THE ONLY SANCTIONED WAY TO WRITE A ROW THIS PROCESS DID NOT AUTHOR.
    post() stamps a fresh `at` and writes the CALLER's own file; that is
    exactly wrong for a row pulled off another machine, whose `at` and `seat`
    are the facts being preserved. Rather than let an adapter reach around this
    module and format its own JSONL (a second writer of the mailbox's file
    format, free to drift), that need lives here, next to the layout it
    depends on. See adapters/remote/.

    behavior: appends each row as one JSON line to <mirror_seat>.jsonl inside
      the run's directory, creating the directory if needed. Rows are written
      in the order given, unmodified -- no timestamp added or rewritten, no
      field dropped. Validation happens for EVERY row before ANY row is
      written, so a bad row in a batch leaves the file untouched rather than
      half-appended.
    in: runid; mirror_seat, the file to append to; rows, an iterable of dicts.
    out: int, the number of rows appended (0 for an empty batch).
    side effects: creates the run directory; appends to one file.
    errors: ValueError for an invalid mirror_seat, a non-dict row, a row with
      no "seat", or a row whose "seat" EQUALS mirror_seat.
    preconditions: THE CALLER MUST BE THE ONLY WRITER OF <mirror_seat>.jsonl on
      this machine. One writer per file is the invariant that makes this
      mailbox race-free (see COLLISION-FREE BY CONSTRUCTION above); a mirror
      file is that invariant's remote arm, and two syncs pointed at one mirror
      file break it exactly as two seats sharing a file would. The
      seat-vs-mirror_seat check enforces the other half structurally: a mirror
      file must never carry a row in its own name, because that is what a REAL
      seat's file means and a mirror must not impersonate a seat.
    limitations: `kind` is deliberately NOT validated. The row came from
      another machine, whose checkout may run a newer (or older) VALID_KINDS
      than this one, and a mirror that enforced the local vocabulary would
      silently drop rows a peer legitimately wrote. VALID_KINDS is the
      WRITE-side gate for rows authored HERE (see post()), and stays that.
    """
    if not _valid_seat(mirror_seat):
        raise ValueError("invalid mirror seat name %r" % mirror_seat)
    rows = list(rows)
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(
                "mirrored row must be a dict, got %s" % type(row).__name__
            )
        if not row.get("seat"):
            raise ValueError("mirrored row has no seat: %r" % (row,))
        if row["seat"] == mirror_seat:
            raise ValueError(
                "row seat %r equals the mirror file %r -- a mirror file must "
                "never carry rows in its own name" % (row["seat"], mirror_seat)
            )
    if not rows:
        return 0
    init(runid)
    path = _seat_path(runid, mirror_seat)
    with open(path, "a") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return len(rows)


def fresh_rows_by_seat(rows, cursor, keep=None):
    """Split already-read rows into (fresh_rows, new_cursor) using per-seat row
    COUNTS as the cursor.

    WHY COUNTS AND NOT TIMESTAMPS: seat files are append-only with exactly one
    writer, so row N of a seat is stable forever -- "new" is exactly the indices
    at or past that seat's count. A timestamp cursor has to answer "what about a
    second row with the same `at`"; a count never has to.

    Callers hand in rows sorted by `at` (read_siblings sorts, stably), which
    preserves each seat's own file order inside the merged stream, so counting
    per seat over the merged stream reconstructs each file's position exactly.

    Lives here rather than in adapters/discord/mirror.py (where it was written)
    because a SECOND adapter now needs the identical arithmetic over a different
    source -- adapters/remote/sync.py, over another machine's rows, arriving as
    a subprocess's stdout instead of a local read. Two copies of a cursor rule
    are two things to keep in step; this is one.

    behavior: walks rows in order tracking a per-seat index; a row at index >=
      cursor[seat] is fresh. THE CURSOR ADVANCES OVER EVERY ROW, including rows
      `keep` rejects -- a filtered row is seen, just not returned, and a cursor
      that skipped it would re-scan it on every pass forever.
    in: rows, a list of row dicts; cursor, a {seat: count} dict (a missing seat
      reads as 0); keep, an optional predicate selecting which FRESH rows to
      return.
    out: (fresh_rows, new_cursor). new_cursor is a new dict -- the caller's is
      never mutated -- and never moves a seat's count backwards.
    side effects: none, this is pure. Persisting new_cursor is the caller's job.
    errors: none for ordinary input; a cursor value that is not int-able
      propagates its own ValueError.
    """
    seen = {}
    fresh = []
    for row in rows:
        seat = row.get("seat", "?")
        idx = seen.get(seat, 0)
        seen[seat] = idx + 1
        if idx >= int(cursor.get(seat, 0)):
            if keep is not None and not keep(row):
                continue
            fresh.append(row)
    new_cursor = dict(cursor)
    for seat, count in seen.items():
        new_cursor[seat] = max(count, int(cursor.get(seat, 0)))
    return fresh, new_cursor


def _extract_flags(args):
    """Pull optional `--topic <name>`, `--to <seat>`, and boolean `--subs` out of
    a positional arg list.

    Returns (remaining_args, {topic, to, subs}). Flags may appear anywhere;
    existing calls that pass none are untouched, so the fixed-arity checks below
    still hold for the common case.
    """
    topic = None
    to = None
    subs = False
    out = []
    i = 0
    while i < len(args):
        if args[i] == "--topic":
            if i + 1 >= len(args):
                raise ValueError("--topic needs a value")
            topic = args[i + 1]
            i += 2
            continue
        if args[i] == "--to":
            if i + 1 >= len(args):
                raise ValueError("--to needs a value")
            to = args[i + 1]
            i += 2
            continue
        if args[i] == "--subs":
            subs = True
            i += 1
            continue
        out.append(args[i])
        i += 1
    return out, {"topic": topic, "to": to, "subs": subs}


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(
            "usage: swarm_mailbox.py init <runid>\n"
            "       swarm_mailbox.py subscribe <runid> <seat> <topic> [<topic> ...]\n"
            "       swarm_mailbox.py post <runid> <seat> <kind> <text> [--topic <name> | --to <seat>]\n"
            "       swarm_mailbox.py read <runid> <seat> [--topic <name> | --subs]\n"
        )
        return 2
    cmd = argv[1]
    try:
        rest, flags = _extract_flags(argv[2:])
        topic, to, subs = flags["topic"], flags["to"], flags["subs"]
        if cmd == "init":
            if len(rest) != 1:
                raise ValueError("init needs <runid>")
            print(init(rest[0]))
            return 0
        if cmd == "subscribe":
            if len(rest) < 3:
                raise ValueError("subscribe needs <runid> <seat> <topic> [<topic> ...]")
            written = subscribe(rest[0], rest[1], rest[2:])
            print(json.dumps(written))
            return 0
        if cmd == "post":
            if len(rest) != 4:
                raise ValueError("post needs <runid> <seat> <kind> <text>")
            row = post(rest[0], rest[1], rest[2], rest[3], topic=topic, to=to)
            print(json.dumps(row))
            return 0
        if cmd == "read":
            if len(rest) != 2:
                raise ValueError("read needs <runid> <seat>")
            if subs:
                if topic is not None:
                    raise ValueError("pass either --topic or --subs, not both")
                rows = read_for(rest[0], rest[1])
            else:
                rows = read_siblings(rest[0], rest[1], topic=topic)
            for row in rows:
                print(json.dumps(row))
            return 0
    except ValueError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    sys.stderr.write("unknown command %r\n" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
