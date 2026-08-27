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

THREADS ARE ABOUT-NESS AND CAN ALSO BE SUBSCRIBED. A row may carry `thread`, a key naming
the DOCUMENT it concerns (thread_key turns a path into "doc:<repo>/<relpath>").
topic/to answer "who receives this"; thread answers "what is this about". They
are orthogonal and compose: a unicast can be threaded, a fan-out can be
threaded, and a row with no thread behaves exactly as before. A subscription
may name a thread key, making read_for return rows about that document whatever
topic they use. Renderers also consume threads: adapters/discord's board lane
groups rows by them, and lib/swarm_threads decides which groups are live enough
to render.

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

THE CLI READ IS INCREMENTAL. `read` hands a seat only the rows it has not been
handed before and remembers the position in COMMS_STATE_DIR, so a poll loop that
reads after every work step sees each row once instead of re-reading the whole
board (which grows without bound and floods the reader's context). Pass --replay
for the old whole-board behavior -- see the CLI READ CURSOR section below for
what the cursor is keyed on and when it moves.

A CONSUMER THAT CAN CONFIRM DELIVERY KEEPS ITS OWN CURSOR, not the CLI's: the
CLI commits when the rows reach stdout, which for a driver that pushes rows
into some other runtime is before the delivery is known to have worked, so a
failed push would become a silent drop. Those consumers read with --replay and
keep a DeliveryCursor (see CONFIRMED-DELIVERY CURSOR below), which advances
only when they say the rows landed. One cursor owns a delivery; two cursors
over one stream is how rows go missing quietly.

CLI:
  swarm_mailbox.py init <runid>
  swarm_mailbox.py subscribe <runid> <seat> <topic> [<topic> ...]      # register a seat's topic set
  swarm_mailbox.py post <runid> <seat> <kind> <text> [--topic <name> | --to <seat>] [--thread <key>]  # kind: finding|claim|blocker|comment|reply|status
  swarm_mailbox.py read <runid> <seat> [--topic <name>]               # NEW rows from every OTHER seat (one topic or all)
  swarm_mailbox.py read <runid> <seat> --subs                          # only this seat's subscribed slice + its unicasts
  swarm_mailbox.py read <runid> <seat> --replay                        # every row again; cursor neither read nor moved
"""

import datetime
import hashlib
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


# ---- THREAD KEY ------------------------------------------------------------
#
# WHAT A THREAD IS: rows about the SAME DOCUMENT, grouped so a human reading
# Discord sees one conversation per file instead of one flat firehose. The key
# is derived from a path, not chosen by the poster, so two seats editing the
# same file land in the same thread without coordinating a name.
THREAD_KEY_PREFIX = "doc:"

# The entry (dir OR file) whose presence makes a directory a repo root. It is
# a FILE in a linked worktree ("gitdir: ..."), a DIRECTORY in a normal clone.
# Testing only isdir would unthread every row written from a worktree.
_REPO_MARKER = ".git"

# What a linked worktree's `.git` FILE contains: one line, "gitdir: <path>",
# pointing at <main checkout>/.git/worktrees/<name>. That path is the only
# on-disk link from a worktree back to the checkout it belongs to.
_GITDIR_PREFIX = "gitdir:"
_WORKTREES_SEGMENT = os.sep + _REPO_MARKER + os.sep + "worktrees" + os.sep

# A `.git` file is one short line. Cap the read: this runs per Write/Edit, and
# a caller should never be able to make a hook slurp an arbitrary file.
_GITDIR_READ_CAP = 4096


def _repo_name(root):
    """The repo NAME for a directory holding a `.git` entry.

    Normally the directory's own basename. For a LINKED WORKTREE -- where
    `.git` is a file reading "gitdir: <main>/.git/worktrees/<name>" -- it is
    the MAIN CHECKOUT's basename instead, because a worktree is the same repo
    in a different directory and keying on the worktree's name threads one
    document separately per worktree. That is not hypothetical: the hook leg
    that calls thread_key runs from worktrees routinely.

    Every failure degrades to the local basename, never to None: a thread
    named after the worktree is a mis-grouping a human can SEE, while no
    thread at all is a row that silently never renders.

    A `.git` file that is NOT a worktree pointer -- a submodule's, which
    reads ".git/modules/<name>" -- keeps the local basename on purpose. A
    submodule checkout is its own document space; borrowing the
    superproject's name would merge two projects' threads.
    """
    marker = os.path.join(root, _REPO_MARKER)
    if not os.path.isfile(marker):
        return os.path.basename(root)
    try:
        with open(marker) as fh:
            line = fh.read(_GITDIR_READ_CAP).strip()
    except OSError:
        return os.path.basename(root)
    if not line.startswith(_GITDIR_PREFIX):
        return os.path.basename(root)
    gitdir = line[len(_GITDIR_PREFIX):].strip()
    if not gitdir:
        return os.path.basename(root)
    if not os.path.isabs(gitdir):
        # git writes an absolute path today; a relative one is legal, and a
        # moved checkout produces one. It is relative to the worktree root.
        gitdir = os.path.normpath(os.path.join(root, gitdir))
    head, sep, _ = gitdir.partition(_WORKTREES_SEGMENT)
    if not sep:
        return os.path.basename(root)  # a submodule, or something else
    return os.path.basename(head) or os.path.basename(root)


def thread_key(path):
    """The thread name for `path`: "doc:<repo>/<relpath>", or None if `path`
    lives outside any repo.

    <repo> is the basename of the NEAREST ancestor holding a `.git` entry;
    <relpath> is the POSIX-spelled path from that ancestor down. `path` is
    realpath'd first, so two spellings of one file (a symlink, /tmp vs
    /private/tmp, a `..` segment) produce ONE key -- two keys for one document
    is two Discord threads for one conversation, which is the whole failure
    this function exists to prevent.

    NEAREST ancestor, not outermost: a vendored repo inside a repo is its own
    document space, and keying its files on the outer repo would merge two
    projects' threads.

    A LINKED WORKTREE keys on the MAIN CHECKOUT's name (see _repo_name): a
    worktree is the same repo in a different directory, and the relpath is
    identical in both, so one document must not thread twice just because it
    was edited from a worktree.

    NO SUBPROCESS, deliberately: the caller is a per-Write/Edit hook, and
    `git rev-parse` costs a process spawn on every keystroke-scale edit (it
    also appears nowhere else in this repo). The marker test is a stat.

    ONE ARGUMENT, deliberately: no repo_root= override. A test builds a real
    directory with a real `.git`; an override would widen a production
    interface so a test could avoid making one.

    OUTSIDE ANY REPO IS None, never a fabricated key like "doc:tmp/x.md": a
    row with no thread takes the unthreaded path, which is a visible
    non-grouping. A made-up key is an invisible mis-grouping.

    The repo root itself keys on the repo alone ("doc:comms"), not
    "doc:comms/." -- a deviation from the design note's literal formula,
    because this string becomes a human-visible Discord thread name and a
    trailing "." carries no information.
    """
    real = os.path.realpath(path)
    cur = real if os.path.isdir(real) else os.path.dirname(real)
    while True:
        if os.path.exists(os.path.join(cur, _REPO_MARKER)):
            repo = _repo_name(cur)
            rel = os.path.relpath(real, cur)
            if rel == os.curdir:
                return THREAD_KEY_PREFIX + repo
            return "%s%s/%s" % (THREAD_KEY_PREFIX, repo, rel.replace(os.sep, "/"))
        parent = os.path.dirname(cur)
        if parent == cur:  # hit the filesystem root without finding a marker
            return None
        cur = parent


def post(runid, seat, kind, text, topic=None, to=None, thread=None):
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

    `thread` (optional) is a THREAD KEY -- see thread_key -- naming the
    document this row is about. It is ORTHOGONAL to topic/to: topic answers
    "who receives this", thread answers "what is this about", and a row can
    carry both, either, or neither. Written ONLY when given, so a row posted
    without it is byte-identical to the pre-thread format. Empty string reads
    as absent for the same reason thread_key returns None outside a repo: an
    empty key would bucket unrelated rows into one nameless thread.
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
    if thread:
        row["thread"] = thread
    path = _seat_path(runid, seat)
    # Append-only, one writer (this seat) per file. "a" opens at end atomically
    # per write for a single line, so a seat's own sequential appends never
    # interleave with themselves, and different seats write different files.
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


# ---- SOURCE-FILE IDENTITY --------------------------------------------------
#
# WHY A ROW NEEDS TO REMEMBER WHICH FILE IT CAME OUT OF:
#   read_siblings merges every seat file in a run into one `at`-sorted stream,
#   which erases the one fact two live bugs turned on. After adapters/remote
#   landed, ONE seat string can own rows in TWO files on a machine -- its
#   first-class `<seat>.jsonl` and the pull mirror `remote~<hub>.jsonl` -- and
#   then (a) a per-SEAT count cursor is not stable, because a newly pulled row
#   with an older `at` shifts that seat's merged index sequence and pushes an
#   already-delivered row back under the cursor (issue #23), and (b) a reader
#   cannot tell a row it must deliver from a COPY of a row the hub's own
#   reader already delivered (issue #20). Direction is the discriminator, and
#   direction lives in the filename, not in the row.
#
# NON-PERSISTED AND OPT-IN: the annotation is a READ-TIME fact about this
#   machine's disk, not part of the row a peer authored. append_mirrored
#   writes rows VERBATIM to another machine's mirror file and the CLI prints
#   them as JSON to an agent, so an always-on annotation would leak a local
#   detail into both. Callers that key a cursor ask for it (with_source=True);
#   everyone else sees byte-identical rows to before.
SOURCE_KEY = "_src"

# Separates the seat from its source tag in a cursor key. It has to be "/":
# that is the ONE character neither half can contain -- _valid_seat rejects it
# in a seat name, and no filename holds it -- while "@" and "#" are both legal
# in a seat name and in a machine label (so `beta~studio#2` is a nameable seat
# whose mirror file is `remote~studio#2.jsonl`). A separator either half can
# contain makes a key ambiguous, and an ambiguous key is a miscounted cursor.
# A legacy cursor's keys are bare seat names, so a key containing "/" is
# unambiguously a post-#39 one (see fresh_rows_by_seat's migration).
CURSOR_KEY_SEP = "/"

# The pull-mirror file's name shape, as adapters/remote/sync.py spells it
# (MIRROR_PREFIX + QUALIFIER + <hub label>). RECOGNIZED here, NAMED there --
# tests/test_swarm_mailbox.py asserts the two agree, because two spellings of
# one convention is exactly how #20 comes back.
MIRROR_FILE_PREFIX = "remote~"


def source_tag(fh, name):
    """This open file's identity: "<name>#<inode>", or "<name>" if unstattable.

    IDENTITY, NOT NAME, and the difference is a silent drop: a purged and
    re-created `<seat>.jsonl` reuses the filename while restarting its row
    numbering at zero, so a name-keyed count cursor would carry the old count
    forward and skip the new file's first rows forever. A new inode reads as a
    new source, whose count starts at zero -- the new rows post again instead
    of vanishing. That direction is the mailbox's standing rule: a visible
    duplicate beats an invisible loss.

    Stats the OPEN FILE (not the path) so the tag names the bytes actually
    read, even if the name is replaced mid-pass.
    """
    try:
        return "%s#%d" % (name, os.fstat(fh.fileno()).st_ino)
    except OSError:
        return name


def source_of(row):
    """The source tag a with_source read stamped on this row, or None."""
    return row.get(SOURCE_KEY)


def source_name(src):
    """The FILE NAME inside a source tag: "alpha.jsonl#8912345" ->
    "alpha.jsonl", and a tag that never got an inode (source_tag degrades to
    the bare name when fstat fails) -> itself.

    Split from the right and checked, not `split("#")[0]`: "#" is legal in a
    seat name and in a machine label, so `remote~studio#2.jsonl` is a
    nameable file and cutting at the FIRST "#" would hand back
    "remote~studio" -- a name that fails the .jsonl test and takes a pulled
    row's rows back into the post path (#20, all over again).
    """
    head, sep, tail = src.rpartition("#")
    if sep and tail.isdigit() and head.endswith(".jsonl"):
        return head
    return src


def without_source(row):
    """A copy of `row` as its author wrote it, with any read-time source tag
    removed. Use before persisting or forwarding a row that came from a
    with_source read (the skipped-rows log, any re-export)."""
    return {k: v for k, v in row.items() if k != SOURCE_KEY}


def is_mirror_source(row):
    """True if this row was READ OUT OF a pull-mirror file (`remote~<hub>.jsonl`),
    i.e. it is a copy of some other machine's row whose original that machine's
    own readers already handled.

    The test is the SOURCE FILE, never the seat string: a pushed row lands on
    the hub as a first-class `alpha~macbook.jsonl` and the hub is its only
    mirror, so "skip any seat containing ~" would silence exactly the rows
    that most need posting. An untagged row (a read without with_source, or a
    row from a non-file source like adapters/remote's subprocess) reads as
    False -- not-known is never treated as not-deliverable.

    The `~<label>` suffix is REQUIRED, and the label must be non-empty: a
    plain `remote.jsonl` is a first-class seat file that some agent could
    legitimately own (adapters/remote reserves the name in prose, _valid_seat
    does not enforce it), and matching it here would silently drop that
    seat's every row. Erring toward "not a mirror" costs a duplicate at
    worst; erring the other way loses rows.
    """
    src = source_of(row)
    if not src:
        return False
    name = source_name(src)
    if not name.endswith(".jsonl"):
        return False
    stem = name[: -len(".jsonl")]
    return stem.startswith(MIRROR_FILE_PREFIX) and len(stem) > len(MIRROR_FILE_PREFIX)


def cursor_key(row):
    """The identity a count cursor must key on: "<seat>/<source tag>" for a
    tagged row, the bare seat for an untagged one. The separator is "/" for
    the reason CURSOR_KEY_SEP gives: it is the one character neither a seat
    name nor a filename can contain, so the key is never ambiguous.

    The seat stays in the key even though the file is already there, because
    ONE file can carry MANY seats: `remote~<hub>.jsonl` holds every seat the
    hub exported. Counting per (seat, file) is what makes a count stable --
    within one file, one seat's rows are appended in `at` order by a single
    writer, so sorting the merged stream by `at` reproduces that file's order
    for that seat exactly.

    The bare-seat fallback is load-bearing back-compat: adapters/remote/sync.py
    counts rows read off a subprocess's stdout, where no source file exists,
    and its cursor keeps the pre-#39 flat {seat: count} shape.
    """
    seat = row.get("seat", "?")
    src = source_of(row)
    return seat if not src else seat + CURSOR_KEY_SEP + src


def _all_sibling_rows(runid, seat, with_source=False):
    """Parse every OTHER seat's .jsonl into a flat, UNFILTERED, UNSORTED list.

    The one parser shared by read_siblings (topic filter) and read_for
    (subscription filter) so the two delivery paths cannot drift. Never returns
    the caller's own rows -- a seat reads siblings, not itself. Malformed lines (a
    partially-flushed final line from a concurrent writer) are skipped rather than
    crashing the reader.

    with_source=True stamps each row with SOURCE_KEY, the identity of the file
    it was parsed out of (see source_tag). Off by default: the rows are handed
    to agents and to other machines verbatim.
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
                tag = source_tag(fh, name) if with_source else None
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if tag is not None and isinstance(row, dict):
                        row[SOURCE_KEY] = tag
                    rows.append(row)
        except OSError:
            continue
    return rows


def read_siblings(runid, seat, topic=None, with_source=False):
    """Return sibling rows sorted by `at`. When `topic` is given, return only rows
    in that topic (a row with no topic key counts as "default", so old rows filter
    coherently); when None, return every topic. Unchanged from the pre-
    subscription API -- read_for is the subscription-honoring reader.

    with_source=True additionally stamps each row with the identity of the file
    it came from (SOURCE_KEY / source_of) -- what a caller keeping a count
    cursor over the merged stream needs, and what tells a pulled copy from a
    first-class row. Default off, so every existing caller's rows are
    byte-identical to what their author wrote.
    """
    rows = _all_sibling_rows(runid, seat, with_source=with_source)
    if topic is not None:
        rows = [r for r in rows if (r.get("topic") or "default") == topic]
    rows.sort(key=lambda r: r.get("at", ""))
    return rows


def row_reaches(row, subs):
    """Return whether one mailbox row reaches a subscription view.

    behavior: accepts every row when subs is None; otherwise accepts a row when
      its topic (missing/empty means "default") or its non-empty thread is an
      exact member of subs.
    in: row, a mapping with optional topic and thread keys; subs, a container
      supporting membership tests or None for the backward-compatible whole
      board view.
    out: bool.
    side effects: none.
    errors: propagates mapping-access or membership errors from invalid inputs.
    """
    return (
        subs is None
        or (row.get("topic") or "default") in subs
        or (row.get("thread") or "") in subs
    )


def read_for(runid, seat, with_source=False):
    """Subscription-honoring read: return only the sibling rows this seat is
    subscribed to (its topic slice) plus any unicast rows addressed to it,
    sorted by `at`.

    This is the SCALE path: a reader pulls its slice, never the whole board. If
    the seat has no registered subscription (never called subscribe()), returns
    EVERY sibling row -- identical to read_siblings(topic=None), so an un-enrolled
    seat keeps the old behavior. A seat's own unicast topic "@<seat>" is always in
    its subscription set, so a direct message always lands.

    with_source=True stamps each row with its source file's identity, exactly
    as in read_siblings -- the same opt-in, for the same cursor-keying reason.
    """
    subs = subscriptions(runid, seat)  # None if unregistered; else includes @self
    rows = _all_sibling_rows(runid, seat, with_source=with_source)
    rows = [r for r in rows if row_reaches(r, subs)]
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

    WHY THE KEY IS (SEAT, SOURCE FILE) AND NOT THE SEAT (issue #23): the
    sentence above holds only while a seat's rows live in ONE file. After
    adapters/remote landed they can live in two on one machine -- the seat's
    own file and the pull mirror `remote~<hub>.jsonl` -- and a pulled row with
    an OLDER `at` inserts INTO the middle of that seat's merged sequence,
    shifting every later index down one and pushing an already-delivered row
    back under the cursor, where it posts a second time. Counting per
    (seat, file) removes the coupling: what lands in one file cannot move
    another file's indices. A row carrying no source tag (see cursor_key) keys
    on the bare seat exactly as before, which is what keeps
    adapters/remote/sync.py -- counting rows read off a subprocess's stdout,
    where no file exists -- byte-identical.

    Lives here rather than in adapters/discord/mirror.py (where it was written)
    because a SECOND adapter now needs the identical arithmetic over a different
    source -- adapters/remote/sync.py, over another machine's rows, arriving as
    a subprocess's stdout instead of a local read. Two copies of a cursor rule
    are two things to keep in step; this is one.

    BACK-COMPAT / MIGRATION IN PLACE: a cursor written before the key changed
    holds bare seat names, and one of those counts means exactly "the first N
    rows of this seat, IN MERGED ORDER, are already seen". So a bare count is
    spent as a per-seat budget over that seat's tagged rows in the order this
    pass walks them -- the same reading the old code would have made on this
    same pass -- and the bare key is then retired from the returned cursor.
    Migration therefore neither re-posts nor skips: it is the old answer,
    re-expressed per file, once. The alternative considered and rejected was
    assigning the whole legacy count to the seat's first-class file, which
    would skip as many of that file's undelivered rows as the seat had
    mirror-file rows -- a silent drop, the one failure this mailbox refuses.

    ONE MIGRATION CASE IS DELIBERATELY NOT BEHAVIOR-PRESERVING: a legacy count
    LARGER than the rows now visible for that seat (its file was truncated,
    replaced, or restored from a shorter copy). The budget is spent down to
    what is there, the legacy key is retired, and rows appended later POST --
    where the old code would have kept treating them as already counted. That
    is the #23 fix doing its job rather than a regression: the surplus count
    was earned by rows that no longer exist, and banking it would skip live
    rows to honor dead ones. Same trade as source_tag's inode, in the same
    direction: a visible duplicate beats an invisible loss.

    behavior: walks rows in order tracking a per-(seat, source) index; a row at
      index >= cursor[key] is fresh. THE CURSOR ADVANCES OVER EVERY ROW,
      including rows `keep` rejects -- a filtered row is seen, just not
      returned, and a cursor that skipped it would re-scan it on every pass
      forever.
    in: rows, a list of row dicts; cursor, a {key: count} dict where key is
      cursor_key(row) (a missing key reads as 0, and a legacy bare-seat key is
      migrated as described above); keep, an optional predicate selecting which
      FRESH rows to return.
    out: (fresh_rows, new_cursor). new_cursor is a new dict -- the caller's is
      never mutated -- and never moves a key's count backwards. Keys for
      sources not seen this pass are preserved untouched (a momentarily
      unreadable file must not lose its place and re-post its whole history).
    side effects: none, this is pure. Persisting new_cursor is the caller's job.
    errors: none for ordinary input; a cursor value that is not int-able
      propagates its own ValueError.
    """
    seen = {}
    fresh = []
    legacy_left = {}   # seat -> unspent legacy budget, filled on first sight
    tagged_seats = set()
    for row in rows:
        seat = row.get("seat", "?")
        key = cursor_key(row)
        if key != seat:
            tagged_seats.add(seat)
        idx = seen.get(key, 0)
        seen[key] = idx + 1
        if idx < int(cursor.get(key, 0)):
            continue
        if key != seat and cursor.get(key) is None:
            # This source has no count of its own yet: spend the seat's
            # legacy per-seat count, if any, before calling anything fresh.
            if seat not in legacy_left:
                legacy_left[seat] = int(cursor.get(seat, 0))
            if legacy_left[seat] > 0:
                legacy_left[seat] -= 1
                continue
        if keep is not None and not keep(row):
            continue
        fresh.append(row)
    new_cursor = dict(cursor)
    for key, count in seen.items():
        new_cursor[key] = max(count, int(cursor.get(key, 0)))
    for seat in tagged_seats:
        # Retire the migrated bare-seat key -- leaving it would spend the same
        # budget again on every later pass. Only when that seat has no untagged
        # rows this pass, where the bare key is still a live count.
        if seat in new_cursor and seat not in seen:
            del new_cursor[seat]
    return fresh, new_cursor


# ---- CONFIRMED-DELIVERY CURSOR ---------------------------------------------


class DeliveryCursor:
    """Per-seat row counts recording which rows a consumer has already gotten
    OUT to its destination, moved ONLY when that consumer confirms the
    delivery worked.

    WHY THIS IS SHARED (issue #30): several readers in this repo keep a cursor
    over the same board and each had to answer "which of these have I already
    handed on?" -- the CLI read per (runid, seat, view), the Discord mirror per
    (run, lane), the remote sync per (host, run), the kimi driver per
    (run, seat). The COUNTING rule (fresh_rows_by_seat) already had exactly one
    copy; the load/commit pair around it had three, and three copies is three
    places for "commit after the delivery succeeded" to drift into "commit when
    the rows were read". That drift is a silent drop, which is the one failure
    this mailbox refuses.

    DELIVER FIRST, COMMIT AFTER, and the interface makes the order the only
    one available: take() writes nothing at all. It returns the fresh rows and
    a `confirm` callable, and the cursor moves only when the caller runs it. A
    caller whose delivery failed just does not call confirm, and the same rows
    come back next pass -- re-delivery is visible and recoverable, a drop is
    neither. This is exactly what separates a delivery cursor from
    `comms read`'s own cursor, which commits at PRINT time because the CLI has
    no acknowledgement to wait for (see CLI READ CURSOR below): a caller that
    needs confirmed delivery reads with --replay and keeps one of these
    instead. Two cursors over one stream is one too many.

    BASH CONSUMERS GET THE SAME PAIR OVER TWO PROCESSES (issue #29). A shell
    driver cannot hold `confirm` across its delivery -- the process that read
    the rows has exited by the time the delivery command runs -- so the CLI
    splits the pair: `comms cursor take <path>` reads rows as JSONL on stdin
    and prints a RECEIPT line (the position it would write) followed by the
    fresh rows, writing nothing; `comms cursor confirm <path> <receipt>` writes
    that position. The order and the failure behavior are identical to the
    in-process pair, which is the point: `bin/comms-poll-driver` and
    `adapters/kimi/poll-driver.sh` get confirmed delivery without a fourth
    private copy of the arithmetic. The receipt is the only extra surface, and
    confirm_receipt() max-merges it so a stale one cannot rewind.

    behavior: load() returns the persisted {seat: count} map. take(rows)
      splits rows into the ones past those counts (via fresh_rows_by_seat, so
      the arithmetic and its "the cursor advances over filtered rows too" rule
      stay in one place) and returns them with a confirm callable that
      persists the advanced cursor. confirm() writes unconditionally and is
      idempotent -- calling it twice writes the same counts twice; NOT calling
      it leaves the file exactly as it was, including not creating it.
    in: path, the file these counts persist to. The KEY is the caller's
      business, because what makes two reads different views of one board is
      caller-specific (a lane, a host, a topic filter), and only the caller can
      name it; what is NOT the caller's business, and is why this class exists,
      is the arithmetic, the atomic write, and the advance-only-on-confirm
      order. The path belongs under COMMS_STATE_DIR: cursors are machine-local
      state, never mailbox content, and are not mirrored across machines.
      keep, an optional predicate passed straight through to
      fresh_rows_by_seat, selecting which fresh rows are RETURNED while the
      cursor still advances over the rest.
    out: take() -> (fresh_rows, confirm). fresh_rows is a list in the order
      given; confirm is a zero-argument callable that is safe to drop.
    side effects: none until confirm(), which creates the path's parent
      directory and writes one file (tmp + os.replace, tmp name PID-suffixed
      so two consumers racing on one path never collide on the tmp file).
    errors: load() never raises -- an absent, unreadable, or malformed cursor
      file reads as {} and replays the stream from the start, because seeing a
      row twice is recoverable and never seeing it is not. confirm() raises
      OSError when the state dir cannot be written; the caller decides whether
      that is fatal (for the CLI it is not -- the rows already reached stdout).
    preconditions: ONE CONSUMER PER PATH. Two processes confirming one cursor
      file is the same broken invariant as two seats sharing a mailbox file:
      the atomic write keeps the file well-formed, it does not keep the two
      consumers from each delivering the other's rows.
    limitations: counts, not row ids -- a half-delivered batch cannot be
      recorded as "rows 1 and 3 landed". Confirm a batch whole or not at all,
      and re-deliver the rest. Also inherits fresh_rows_by_seat's requirement
      that rows arrive in each seat's own file order (read_siblings sorts
      stably by `at`, which preserves it).
    """

    def __init__(self, path):
        self.path = path

    def load(self):
        """The persisted {seat: count} map, or {} when there is none yet.

        An unreadable or malformed file reads as {} -- i.e. replay this stream
        from the start. That direction is deliberate: the recoverable failure
        is seeing a row twice, the unrecoverable one is never seeing it.
        """
        try:
            with open(self.path) as fh:
                cursor = json.load(fh)
        except (OSError, ValueError):
            return {}
        return cursor if isinstance(cursor, dict) else {}

    def take(self, rows, keep=None):
        """The rows past this cursor, plus the callable that records having
        delivered them. Writes nothing; see the class docstring.

        `confirm.cursor` is the POSITION confirm would write -- the receipt.
        An in-process caller never needs it and should just call confirm(). It
        exists for the out-of-process pair below (`cursor take` / `cursor
        confirm`), where the delivery happens in a different process than the
        read and the position has to survive as text between the two. Exposing
        it beats letting that CLI re-run fresh_rows_by_seat itself, which would
        be a second copy of the arithmetic this class exists to hold.
        """
        fresh, new_cursor = fresh_rows_by_seat(rows, self.load(), keep=keep)

        def confirm():
            self._commit(new_cursor)

        confirm.cursor = new_cursor
        return fresh, confirm

    def confirm_receipt(self, receipt):
        """Commit a position handed back by an EARLIER process's
        `take().cursor`, merged so no seat's count ever moves backwards.

        WHY THIS EXISTS: a shell consumer cannot hold a Python closure across
        its delivery -- one `comms cursor take` invocation has already exited
        by the time the delivery command runs. So the pair is split: take
        prints the receipt, the caller delivers, and confirm writes the receipt
        only if the delivery exited 0. The in-process `confirm()` is still the
        preferred form; this is the same commit with the closure replaced by a
        line of text.
        in: receipt, a {seat: count} dict from a take() on THIS path.
        behavior: max-merges with what is currently persisted. A stale receipt
          (a slow deliverer confirming after a faster one) therefore cannot
          rewind the cursor and re-deliver rows already confirmed, matching
          fresh_rows_by_seat's own never-backwards rule. It CANNOT defend
          against a forged receipt claiming rows that were never delivered --
          that is the one-consumer-per-path precondition's job.
        errors: ValueError if the receipt is not a {seat: int-able} map;
          OSError if the state dir cannot be written.
        """
        if not isinstance(receipt, dict):
            raise ValueError("receipt must be a JSON object of {seat: count}")
        merged = dict(self.load())
        for seat, count in receipt.items():
            merged[seat] = max(int(count), int(merged.get(seat, 0)))
        self._commit(merged)
        return merged

    def _commit(self, cursor):
        """Persist atomically: tmp + os.replace, tmp name PID-suffixed so two
        concurrent consumers of one path never collide on the tmp file."""
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp." + str(os.getpid())
        with open(tmp, "w") as fh:
            json.dump(cursor, fh)
        os.replace(tmp, self.path)


# ---- CLI READ CURSOR -------------------------------------------------------
#
# WHY THE CURSOR SITS AT THE CLI AND NOT INSIDE read_siblings/read_for:
#   those two are pure queries, and three in-repo readers already keep their
#   OWN cursor over them on their own key and their own schedule -- the discord
#   mirror (per run+lane), the remote sync (per host+run), the Claude Code
#   heartbeat (per run+agent_id). A stateful query would advance all of those
#   cursors out from under their owners. The CLI is where an agent's poll
#   happens, so the CLI is where "what have I already been handed" belongs.
#
# ONE CURSOR PER VIEW, NOT PER SEAT:
#   a read is keyed on (runid, seat, VIEW), where the view is the filter that
#   read used: the whole board, one --topic, or the seat's --subs slice. The
#   alternative -- one cursor per (runid, seat) advanced over every row the
#   reader saw, which is what fresh_rows_by_seat's `keep` predicate is for --
#   would let `read RUN seat --topic parser` mark rows on OTHER topics
#   delivered, and those rows would then never appear in a later unfiltered
#   read. Dropping a row invisibly is the one failure this mailbox refuses;
#   handing the same row to two different views is merely redundant. So the
#   cursor advances ONLY over the rows the chosen view actually selected.
#   Consequence, and it is intended: a row inside topic X is delivered once to
#   `read RUN seat` and once to `read RUN seat --topic X`. Pick one view per
#   reader and stay on it.
#
# TRUNCATING A READ TRUNCATES THE OUTPUT, NOT THE CURSOR:
#   `comms read ... | head -5` prints 5 rows and still advances over every row
#   the view selected, because the cursor commits once the rows are written to
#   stdout and this process cannot know what the far end of the pipe kept.
#   Making the cursor track consumption would need an acknowledgement the CLI
#   does not have. A caller that HAS one -- it invoked a runtime and saw it
#   succeed -- keeps its own DeliveryCursor (above) and reads with --replay,
#   which is the whole point of that helper. For the CLI itself this stays
#   documented in README.md and bin/comms, with --replay as the recovery.
#
# THE SUBS VIEW IS KEYED ON THE SUBSCRIPTION SET ITSELF (a digest of it), so
#   re-subscribing a seat to a different topic set starts that set's own
#   cursor at zero and re-delivers its slice, rather than silently skipping
#   rows that predate the change (a count cursor cannot tell "row 3 of seatA
#   in the old slice" from "row 3 in the new one").
READ_CURSOR_SUBDIR = "read-cursor"


def _state_dir():
    """The machine-local state root (cursors live here), read at CALL TIME.

    Same footgun as _root(): an import-time snapshot would pin whatever
    COMMS_STATE_DIR was set before the first import and silently ignore a
    later override, which is exactly how a test would write cursors into the
    live state dir. Same default chain as lib/swarm_arm.py and the adapters so
    every comms component keeps its state under one root. No legacy fallback
    name on purpose: this cursor is new state, it never existed under a
    pre-extraction name.
    """
    return os.environ.get("COMMS_STATE_DIR") or os.path.expanduser("~/.comms/state")


def _slug(text):
    """One filename-safe path segment for an arbitrary selector string.

    Readable when the selector is already a plain name (the common case: a
    runid or a topic like "parser-work"), and disambiguated with a short digest
    when it is not, so two DIFFERENT selectors can never land on one cursor
    file. Sharing a cursor between two views is the silent-drop bug this whole
    section exists to avoid, and "a@b" and "a_b" flattening onto one file would
    be exactly that.
    """
    safe = "".join(c if (c.isalnum() or c in "-_.@") else "_" for c in text)
    if safe != text or not safe:
        safe = (safe or "x") + "-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return safe


def _read_cursor_path(runid, seat, view):
    return os.path.join(
        _state_dir(),
        READ_CURSOR_SUBDIR,
        _slug(runid),
        "%s.%s.json" % (_slug(seat), view),
    )


def read_delta(runid, seat, topic=None, subs=False):
    """The rows this seat has NOT been handed yet in this view, plus the
    callable that records having handed them over.

    behavior: selects rows exactly as the equivalent whole-board read would
      (read_for when subs, else read_siblings with the optional topic filter),
      then hands them to this view's DeliveryCursor, which keeps only the ones
      past its persisted per-seat counts. The returned `advance` is that
      cursor's confirm: nothing is written until it is called, so a caller
      delivers the rows FIRST and commits after. A crash between the two
      re-delivers on the next read (visible), where committing first would lose
      the rows (invisible). What is view-specific -- which rows the view
      selects and where its counts live -- is here; the load/split/commit
      arithmetic is DeliveryCursor's, shared with every other consumer that
      keeps a cursor over this board.
    in: runid; seat, the reader; topic, an optional single-topic filter; subs,
      True to read the seat's subscribed slice instead.
    out: (rows, advance). rows is the fresh slice, sorted by `at`; advance is a
      zero-argument callable, safe to skip if the rows were not delivered.
    side effects: none until advance() is called, which creates
      <COMMS_STATE_DIR>/read-cursor/<runid>/ and writes one file.
    errors: none here; advance() raises OSError if the state dir is not
      writable (the caller decides whether that is fatal -- for the CLI it is
      not, the rows were already printed and will simply replay).
    """
    if subs:
        rows = read_for(runid, seat)
        registered = subscriptions(runid, seat)
        selector = "null" if registered is None else json.dumps(sorted(registered))
        view = "subs-" + hashlib.sha1(selector.encode("utf-8")).hexdigest()[:12]
    elif topic is not None:
        rows = read_siblings(runid, seat, topic=topic)
        view = "topic-" + _slug(topic)
    else:
        rows = read_siblings(runid, seat)
        view = "all"
    return DeliveryCursor(_read_cursor_path(runid, seat, view)).take(rows)


def _extract_flags(args):
    """Pull optional `--topic <name>`, `--to <seat>`, `--thread <key>`, and the
    booleans `--subs` and `--replay` out of a positional arg list.

    Returns (remaining_args, {topic, to, thread, subs, replay}). Flags may
    appear anywhere; existing calls that pass none are untouched, so the
    fixed-arity checks below still hold for the common case.
    """
    topic = None
    to = None
    thread = None
    subs = False
    replay = False
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
        if args[i] == "--thread":
            if i + 1 >= len(args):
                raise ValueError("--thread needs a value")
            thread = args[i + 1]
            i += 2
            continue
        if args[i] == "--subs":
            subs = True
            i += 1
            continue
        if args[i] == "--replay":
            replay = True
            i += 1
            continue
        out.append(args[i])
        i += 1
    return out, {
        "topic": topic,
        "to": to,
        "thread": thread,
        "subs": subs,
        "replay": replay,
    }


def _cmd_read(runid, seat, topic=None, subs=False, replay=False):
    """The CLI's read: print this seat's NEW rows as JSONL, then advance the
    cursor for the view that was read.

    PRINT FIRST, COMMIT AFTER (the heartbeat and the remote sync order their
    writes the same way, for the same reason): if this process dies between the
    two, the rows replay next time -- visible and recoverable -- where the
    other order would drop rows that were never delivered.

    A cursor that cannot be written is a WARNING, not a failure: the rows did
    reach stdout, so the read succeeded; only the "do not replay" promise is
    unmet, and saying so on stderr beats both a silent skip and an exit code
    that tells the caller a successful read failed.
    """
    if replay:
        rows = read_for(runid, seat) if subs else read_siblings(runid, seat, topic=topic)
        advance = None
    else:
        rows, advance = read_delta(runid, seat, topic=topic, subs=subs)
    for row in rows:
        print(json.dumps(row))
    sys.stdout.flush()
    if advance is not None:
        try:
            advance()
        except OSError as exc:
            sys.stderr.write(
                "warning: read cursor not saved (%s); these rows will replay\n" % exc
            )
    return 0


def _read_jsonl(stream):
    """Row dicts from a JSONL stream, blank lines skipped.

    A malformed line is a hard ValueError, not a skip: this is the input of a
    delivery cursor, and a line quietly dropped here is a row quietly dropped
    from the stream -- the exact failure the cursor exists to prevent. The read
    that feeds it emits one json.dumps per row, so a bad line means something
    else wrote into the pipe and the caller needs to know.
    """
    rows = []
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            raise ValueError("cursor take: stdin is not JSONL: %.80s" % line)
    return rows


def _cmd_cursor(rest):
    """The out-of-process half of DeliveryCursor, for shell drivers.

      cursor take    <path>              rows as JSONL on stdin
                                         -> receipt line, then the fresh rows
      cursor confirm <path> <receipt>    -> commits that receipt

    take() WRITES NOTHING -- no file appears -- so a driver whose delivery
    command failed simply never runs confirm and the rows come back next pass.
    The receipt is always printed, even when no rows are fresh, so the caller's
    parse is one shape: line 1 is the receipt, lines 2..N are rows.

    Read the rows with `comms read ... --replay`. The CLI's own read cursor
    commits at PRINT time, which for a driver is before delivery is known to
    have worked; two cursors over one stream is one too many and the loser is
    a row nobody sees.
    """
    if not rest:
        raise ValueError("cursor needs take|confirm")
    verb = rest[0]
    if verb == "take":
        if len(rest) != 2:
            raise ValueError("cursor take needs <path> (rows as JSONL on stdin)")
        cursor = DeliveryCursor(rest[1])
        fresh, confirm = cursor.take(_read_jsonl(sys.stdin))
        print(json.dumps(confirm.cursor, sort_keys=True))
        for row in fresh:
            print(json.dumps(row))
        return 0
    if verb == "confirm":
        if len(rest) != 3:
            raise ValueError("cursor confirm needs <path> <receipt>")
        try:
            receipt = json.loads(rest[2])
        except ValueError:
            raise ValueError("cursor confirm: receipt is not JSON: %.80s" % rest[2])
        DeliveryCursor(rest[1]).confirm_receipt(receipt)
        return 0
    raise ValueError("cursor: unknown verb %r (want take|confirm)" % verb)


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(
            "usage: swarm_mailbox.py init <runid>\n"
            "       swarm_mailbox.py subscribe <runid> <seat> <topic> [<topic> ...]\n"
            "       swarm_mailbox.py post <runid> <seat> <kind> <text> [--topic <name> | --to <seat>] [--thread <key>]\n"
            "       swarm_mailbox.py read <runid> <seat> [--topic <name> | --subs] [--replay]\n"
            "       swarm_mailbox.py cursor take <path>   (rows as JSONL on stdin)\n"
            "       swarm_mailbox.py cursor confirm <path> <receipt>\n"
        )
        return 2
    cmd = argv[1]
    try:
        if cmd == "cursor":
            # Dispatched BEFORE flag extraction: a receipt is opaque caller
            # text and must not be parsed for --topic/--subs on its way past.
            return _cmd_cursor(argv[2:])
        rest, flags = _extract_flags(argv[2:])
        topic, to, subs = flags["topic"], flags["to"], flags["subs"]
        replay = flags["replay"]
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
            row = post(
                rest[0], rest[1], rest[2], rest[3],
                topic=topic, to=to, thread=flags["thread"],
            )
            print(json.dumps(row))
            return 0
        if cmd == "read":
            if len(rest) != 2:
                raise ValueError("read needs <runid> <seat>")
            if subs and topic is not None:
                raise ValueError("pass either --topic or --subs, not both")
            return _cmd_read(
                rest[0], rest[1], topic=topic, subs=subs, replay=replay
            )
    except ValueError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    sys.stderr.write("unknown command %r\n" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
