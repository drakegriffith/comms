#!/usr/bin/env python3
"""remote sync: carry mailbox rows between two machines over plain ssh, with
one machine's mailbox as the hub.

THE PROBLEM: every machine runs its own file-backed mailbox, so a laptop seat
and a Studio seat can both post and neither can ever read the other
(claude-harness#150). The Discord mirror does not solve this -- it is
display-only, one direction, into a human's eyeballs.

THE SHAPE: the always-on machine (the Studio) is the HUB and holds the source
of truth. The intermittent machine (the laptop) drives both directions:

  post  -- run the HUB's OWN `bin/comms post` over ssh; the row lands in the
           hub mailbox, authored by this machine's seat.
  pull  -- run the HUB's OWN `bin/comms read` over ssh; new rows land in a
           local mirror file and become ordinary sibling rows here.

NOTHING NEW RUNS ON THE HUB. The remote side is `bin/comms post` and
`bin/comms read`, stable since PR #4/#6. That is not an accident of
convenience: when this was built, the hub's checkout was three merged PRs
behind this one, and it is the machine nobody is sitting at. A design whose
remote half must be deployed and kept in version lockstep with this file
would have been broken on arrival. See README.md, Design record.

DIRECTION IS ASYMMETRIC ON PURPOSE. Only laptop->hub ssh exists (the reverse
needs Remote Login enabled on the laptop, which is a machine-security
decision, not this adapter's). So inbound is a poll, not a push, and the hub
needs no knowledge that this adapter exists.

PROVENANCE / ECHO / LOOPS -- one convention does all three: a seat name that
crosses a machine boundary is qualified with its machine, `alpha` ->
`alpha~laptop` (see qualify(); idempotent, so a row never collects two tags).
  * One writer per file survives the network: only one process on one machine
    ever appends to `alpha~laptop.jsonl`, wherever that file lives.
  * Echo suppression is structural: pull drops hub rows whose seat ends in
    this machine's own suffix -- rows we pushed, coming back. We wrote that
    suffix ourselves, so the test is about our own bookkeeping, not a guess.
  * Loops cannot form: pulled rows land in `remote~<hub>.jsonl` and pull skips
    any hub row already in a `remote~*` file, so a third machine's rows are
    pulled once and never re-exported.

PUSH IS EXPLICIT, PULL IS A MIRROR. post() sends ONE named row; it never tails
the local mailbox and forwards it. A forwarding tail on both ends is how a
sync loop is born, and it would also put every purely-local exchange on the
network. Addressing the other machine is a choice, the same way `--to` is.

OFFLINE IS NORMAL, NOT AN ERROR. The laptop closes at the end of the day, so
post() ALWAYS appends to a local outbox first and only then tries to flush the
whole outbox in order. A row is durable on local disk before any network call,
a fresh row can never jump ahead of one queued this morning, and a failed
flush stops at the first failure with the remainder intact. An outbox line that
will not parse is QUARANTINED to outbox.bad and counted, never silently
skipped: an undeliverable queued row is exactly the silent loss this module
promises not to have.

DELIVERY IS AT-LEAST-ONCE, AND THE DUPLICATE IS MADE VISIBLE. There is no
transaction spanning "the hub appended the row" and "we wrote that down", so
an ambiguous transport failure (the connection dies after the hub committed)
or a crash mid-flush can resend a row. Two mitigations, both needed: the
outbox is rewritten after EVERY successful send, which bounds a crash to one
duplicated row instead of a whole batch; and every queued row carries a stable
delivery id, stamped into its text, so a surviving duplicate is detectable at
read time (`dupes` subcommand, and the dupes= counter on every pull) instead
of being an invisible second copy. Retrying on an ambiguous failure is
deliberate -- a duplicate is cheap and a lost message is not.

EXIT CODES (the two success shapes are different codes on purpose):
  0  delivered / pulled. `pull` reporting inspected=0 still means it REACHED
     the hub and the hub had nothing new.
  1  queued, not delivered -- the hub is unreachable and the row is on local
     disk awaiting the next flush. Durable "later", not a crash.
  2  usage error, or COULD NOT INSPECT (pull could not reach the hub, remote
     CLI missing/erroring). Never a pass.
`pull` prints inspected/mirrored/echo/skipped/dupes counts every pass, because
"the sync never reached the hub" and "the hub had nothing" are otherwise
identical silence. `echo` (our own rows coming back) and `skipped` (another
machine's mirror file) are separate counters: they are different facts, and one
label over both made a third machine's rows read as ours.

LAUNCHD SAFETY: same shape as adapters/discord/mirror.py -- --follow catches
per-pass errors, writes one stderr line and keeps polling, rather than exiting
nonzero into a KeepAlive restart loop. An unreachable hub is the EXPECTED
condition for this adapter, so --follow treats it as a quiet retry, not news.

CLI:
  sync.py post <runid> <seat> <kind> <text> [--topic <name> | --to <seat>] [--host <h>]
  sync.py flush [--host <h>]
  sync.py pull <runid> [--host <h>]
  sync.py sync <runid> [--host <h>]              # flush + pull
  sync.py dupes <runid> [--host <h>]             # read-time duplicate report
  sync.py --follow <runid> [--interval N] [--host <h>]
"""

import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import time

SELF_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SELF_DIR))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
import comms_machine  # noqa: E402  (ONE label per machine, not two)
import swarm_mailbox  # noqa: E402  (one mailbox writer/parser; never a second)

# This machine's label, RE-EXPORTED from lib/comms_machine.py. It used to be
# imported from adapters/discord/mirror.py, which meant cross-machine
# correctness depended on a display adapter for a chat service: if Discord were
# removed or moved, the echo filter would break even though nothing here talks
# to Discord. The label is not decoration in this module -- it is written into
# seat names that cross the network and is what is_echo() tests against.
machine_label = comms_machine.machine_label

# The machine tag separator inside a seat name. "~" and not "@": "@" already
# means "unicast topic" in swarm_mailbox (SELF_TOPIC_PREFIX), and a seat name
# that reads like an address is a trap for a human skimming a board.
QUALIFIER = "~"

# Delivery is AT-LEAST-ONCE, so every queued row carries a stable id and a
# duplicate is CHEAP AND VISIBLE rather than prevented. See MSGID below.
MSGID_MARKER = " [#%s]"
MSGID_RE = re.compile(r"\s*\[#([0-9a-f]{8})\]\s*$")

# Rows pulled from the hub are appended to "<MIRROR_PREFIX>~<hub label>.jsonl".
# Reserved: do NOT name a real seat "remote" or "remote~anything" -- pull skips
# rows from such seats to keep a third machine's rows from being re-exported.
MIRROR_PREFIX = "remote"

# Reserved observer seat handed to the HUB's `comms read`. read_siblings
# excludes only the named seat's own file, so reading as a name no real seat
# uses means the pull sees every real seat's rows. Do not name a seat this.
OBSERVER_SEAT = "remote-sync"

DEFAULT_HOST = "studio"
DEFAULT_REMOTE_BIN = "~/code/comms/bin/comms"
DEFAULT_SSH_TIMEOUT = 10  # seconds; offline is the steady state, not news
DEFAULT_INTERVAL = 30  # seconds between --follow passes

STATE_SUBDIR = "remote-sync"


class RemoteUnreachable(Exception):
    """The hub could not be reached, or its CLI failed. Distinct from "the hub
    had nothing to say" -- conflating the two is how a sync that has been dead
    for a week reads as a quiet week."""


# ---- configuration -------------------------------------------------------
# Each knob below exists because the value is a fact about ANOTHER machine or
# about ~/.ssh/config, which this process cannot compute. machine_label is the
# exception and is therefore NOT a new knob: it is lib/comms_machine.py's,
# shared with every other consumer.


def remote_host():
    return os.environ.get("COMMS_REMOTE_HOST") or DEFAULT_HOST


def remote_bin():
    return os.environ.get("COMMS_REMOTE_BIN") or DEFAULT_REMOTE_BIN


def remote_label(host=None):
    """Provenance label written into pulled seat names. Defaults to the ssh
    alias, which is already the human's name for that machine."""
    return os.environ.get("COMMS_REMOTE_LABEL") or (host or remote_host())


def ssh_timeout():
    try:
        return int(os.environ.get("COMMS_REMOTE_SSH_TIMEOUT") or DEFAULT_SSH_TIMEOUT)
    except ValueError:
        return DEFAULT_SSH_TIMEOUT


# ---- seat naming ---------------------------------------------------------


def qualify(seat, label):
    """Tag a seat name with the machine it came from: alpha -> alpha~laptop.

    behavior: idempotent -- a seat that already carries a QUALIFIER is returned
      unchanged, so a row relayed twice never collects a second machine tag.
    in: seat name; machine label.
    out: the qualified seat name.
    errors: none. An empty label returns the seat unchanged (a tag that says
      nothing is worse than no tag: it would still break the echo test).
    """
    if not label or QUALIFIER in seat:
        return seat
    return seat + QUALIFIER + label


def mirror_seat(label):
    """The local file that rows pulled from `label` are appended to."""
    return MIRROR_PREFIX + QUALIFIER + label


def is_echo(seat, label):
    """True if this seat name is one THIS machine exported -- a row we pushed,
    read back off the hub. Tests our own suffix, never the row's content."""
    return bool(label) and seat.endswith(QUALIFIER + label)


def is_mirror_seat(seat):
    """True if this seat is some machine's mirror file rather than a real seat.
    Pulling these would re-export a third machine's rows on every hop."""
    return seat == MIRROR_PREFIX or seat.startswith(MIRROR_PREFIX + QUALIFIER)


def classify(row, label):
    """Why a hub row is or is not mirrored home: "mirror" | "echo" | "skipped".

    behavior: "echo" means THIS machine exported the row and it came back;
      "skipped" means some OTHER machine's mirror file, which must not be
      re-exported; "mirror" means a genuine peer row to bring home.
    in: a row dict; this machine's label.
    out: one of three literal strings. The vocabulary is CLOSED and every
      caller must handle all three -- a fourth reason must not silently join
      whichever bucket happens to be the default.
    errors: none.

    Split out of a single boolean because the two rejection reasons were being
    reported under one counter called "echo", which made a third machine's
    mirror rows read as this machine's own traffic coming back. Two different
    facts sharing a label is how a metric stops being a measurement.
    """
    seat = str(row.get("seat", ""))
    if is_echo(seat, label):
        return "echo"
    if is_mirror_seat(seat):
        return "skipped"
    return "mirror"


# ---- delivery ids: at-least-once, with duplicates made visible ------------
#
# MSGID. Delivery over ssh cannot be exactly-once, and pretending otherwise is
# how rows get silently dropped. Two unavoidable windows:
#   * AMBIGUOUS TRANSPORT FAILURE -- the hub appends the row and the connection
#     dies before the success reaches us. We saw a nonzero rc; the row exists
#     over there.
#   * CRASH between a successful send and the outbox rewrite.
# The doctrine is at-least-once with CHEAP DUPLICATES, never silent loss: keep
# retrying on an ambiguous failure, and make the duplicate harmless and
# VISIBLE. So every queued row is stamped at enqueue with a stable random id
# that survives any number of resends, and a duplicate is detectable at READ
# time by anyone looking at the board -- see duplicate_msgids() and the `dupes`
# subcommand.
#
# The id rides in the row's TEXT because that is the only field the hub's
# existing `comms post` CLI will carry, and requiring a new field would mean
# deploying code to the hub -- the one thing this adapter's whole design
# refuses to do. Cost, stated: rows carry a visible " [#a1b2c3d4]" suffix, and
# a row whose text ALREADY ends in that exact shape would be misread as
# stamped. The id is advisory -- a detector, never a gate -- so a false read
# costs a wrong count in a query, nothing more.


def new_msgid():
    return os.urandom(4).hex()


def stamp_msgid(text, msgid):
    """Attach a delivery id to a row's text. Idempotent for the SAME id, so a
    resend of an already-stamped record never stacks two markers."""
    existing = read_msgid(text)
    if existing == msgid:
        return text
    return text + (MSGID_MARKER % msgid)


def read_msgid(text):
    """The delivery id in this text, or None. Reads only the trailing marker."""
    match = MSGID_RE.search(text or "")
    return match.group(1) if match else None


def strip_msgid(text):
    """The text without its delivery-id marker -- for renderers and for tests
    that care about what a human wrote, not how it was delivered."""
    return MSGID_RE.sub("", text or "")


def duplicate_msgids(rows):
    """Map every delivery id that appears MORE THAN ONCE to its occurrence
    count -- the read-time duplicate detector.

    behavior: scans row texts for the trailing marker. Rows with no id (posted
      locally on the hub, or by a machine not running this adapter) are ignored
      rather than counted as one big untagged group.
    in: an iterable of row dicts.
    out: {msgid: count}, only for counts >= 2. Empty dict = no duplicates SEEN,
      which is a real result over the rows inspected, not a guarantee.
    side effects: none.
    errors: none.
    """
    counts = {}
    for row in rows:
        msgid = read_msgid(str(row.get("text", "")))
        if msgid:
            counts[msgid] = counts.get(msgid, 0) + 1
    return {k: v for k, v in counts.items() if v >= 2}


def redundant_row_count(rows):
    """How many rows are surplus copies: 3 rows sharing one id is 2 redundant.
    The number a human wants when asking "how much did retrying cost me"."""
    return sum(count - 1 for count in duplicate_msgids(rows).values())


# ---- the seam: every ssh call goes through this one function -------------


def _ssh(remote_argv, host=None):
    """Run one argv on the remote host and return (rc, stdout, stderr).

    THE ONE SEAM. Every remote call in this module goes through here, so a
    test fakes ssh once (a script named `ssh` earlier on PATH, or this
    function itself) rather than per call site.

    behavior: joins remote_argv into a single command string for the REMOTE
      shell (ssh concatenates its trailing arguments and hands the result to a
      shell over there, so this module owns the quoting).
      ARGUMENT 0 IS A COMMAND PATH FROM CONFIGURATION AND IS PASSED
      UNQUOTED; EVERY LATER ARGUMENT IS DATA AND IS SHELL-QUOTED. Quoting
      argv[0] too is the obvious-looking version and it is WRONG: the default
      remote bin is "~/code/comms/bin/comms", and `'~/code/...'` is a literal
      directory named "~" to the remote shell, not the remote $HOME (measured:
      rc=127, "no such file or directory"). The remote checkout lives under a
      different username, so a tilde (or $HOME) is the only portable way to
      name it, and expansion is the point. The trust boundary that makes this
      safe is that argv[0] is COMMS_REMOTE_BIN -- operator configuration, the
      same trust level as PATH -- while a row's text, seat, and topic are data
      from other agents and are quoted without exception.
      BatchMode=yes guarantees no password prompt can hang an unattended poll.
    in: remote_argv, a list of strings, argv[0] being the remote command path;
      host, the ssh alias (default COMMS_REMOTE_HOST).
    out: (returncode, stdout, stderr) as text.
    side effects: one subprocess; whatever the remote command does.
    errors: a connect failure is NOT raised here -- it is ssh's own nonzero rc,
      returned like any other, and classified by the caller. A subprocess that
      outlives its timeout is reported as rc 255, ssh's own transport-failure
      code, because a wedged connection and a refused one mean the same thing
      to every caller: the hub is unreachable right now.
    """
    timeout = ssh_timeout()
    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=%d" % timeout,
        host or remote_host(),
        " ".join([remote_argv[0]] + [shlex.quote(a) for a in remote_argv[1:]]),
    ]
    try:
        cp = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout * 3,
        )
    except subprocess.TimeoutExpired:
        return 255, "", "ssh: timed out after %ds" % (timeout * 3)
    except OSError as exc:  # ssh not installed / not executable
        return 255, "", "ssh: %s" % exc
    return (
        cp.returncode,
        cp.stdout.decode("utf-8", "replace"),
        cp.stderr.decode("utf-8", "replace"),
    )


# ---- state paths ---------------------------------------------------------


def _state_dir():
    # Same default chain as lib/swarm_arm.py and mirror.py so every comms
    # component keeps its state under one root.
    return os.environ.get("COMMS_STATE_DIR") or os.path.expanduser("~/.comms/state")


def _safe(name):
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(name))


def _host_dir(host):
    return os.path.join(_state_dir(), STATE_SUBDIR, _safe(host))


def _outbox_path(host):
    """One outbox per HOST, not per run: ordering is a property of the wire,
    and interleaving two runs' rows across two queues would let a run posted
    later be delivered first."""
    return os.path.join(_host_dir(host), "outbox.jsonl")


def _cursor_path(host, runid):
    """One cursor per (host, run): a per-seat row count, same shape and same
    rationale as mirror.py's."""
    return os.path.join(_host_dir(host), _safe(runid) + ".cursor.json")


def _write_atomic(path, text):
    """tmp + os.replace, tmp name PID-suffixed so two concurrent syncs never
    collide on the tmp file -- same shape as mirror.py's cursor write."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp." + str(os.getpid())
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


# ---- outbox --------------------------------------------------------------


def _quarantine_path(host):
    """Where unparseable outbox lines go to be SEEN. A dropped row and a
    delivered row look identical from the outbox; this file is the difference."""
    return os.path.join(_host_dir(host), "outbox.bad")


def read_outbox(host=None):
    """Parse the outbox into (records, malformed_lines).

    behavior: returns queued records oldest first, PLUS every line that would
      not parse, verbatim. A missing outbox is an empty queue, not an error.
    in: host.
    out: (records, malformed_lines). Both lists; the second is normally empty.
    side effects: none -- this is the pure parse. Quarantining the malformed
      lines is flush()'s job, so a read cannot silently mutate state.
    errors: none.

    Two return values rather than one because a corrupt line is a QUEUED ROW
    THAT WILL NEVER BE DELIVERED. Skipping it silently -- what this did before
    -- means local disk corruption loses a message with no trace, in a module
    whose entire promise is "never silent loss".
    """
    path = _outbox_path(host or remote_host())
    records = []
    malformed = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    malformed.append(line)
    except OSError:
        return [], []
    return records, malformed


def load_outbox(host=None):
    """Just the deliverable records -- read_outbox()[0]. Kept because callers
    that only want the queue depth should not have to unpack a parse report."""
    return read_outbox(host)[0]


def quarantine(host, lines):
    """Append unparseable lines to outbox.bad and return how many were moved.

    behavior: appends verbatim, so whatever a human needs to recover the row is
      still there. Never deletes; the outbox rewrite is what removes them from
      the queue.
    side effects: creates the state dir; appends to one file.
    """
    lines = list(lines)
    if not lines:
        return 0
    path = _quarantine_path(host)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        for line in lines:
            fh.write(line + "\n")
    return len(lines)


def _save_outbox(host, records):
    _write_atomic(
        _outbox_path(host),
        "".join(json.dumps(r) + "\n" for r in records),
    )


def enqueue(record, host=None):
    """Append one record to the outbox and return the new queue length.

    behavior: a plain append -- durable before any network call is attempted.
    side effects: creates the state dir; appends one line.
    """
    host = host or remote_host()
    path = _outbox_path(host)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(record) + "\n")
    return len(load_outbox(host))


def build_post_argv(record):
    """The remote argv that posts one queued record into the hub's mailbox.

    Deliberately the hub's OWN CLI (`comms post`) rather than a payload format
    of this module's invention: the hub then validates, timestamps, and writes
    the row with its own code, so nothing here has to stay in step with the
    hub's checkout beyond four positional arguments that have not changed
    since PR #4.

    The record's delivery id is stamped onto the TEXT here rather than stored
    pre-stamped, so the queue on disk holds what the author actually wrote and
    the id is applied identically on the first send and every resend.
    """
    text = record["text"]
    if record.get("msgid"):
        text = stamp_msgid(text, record["msgid"])
    argv = [
        remote_bin(),
        "post",
        record["runid"],
        record["seat"],
        record["kind"],
        text,
    ]
    if record.get("to"):
        argv += ["--to", record["to"]]
    elif record.get("topic"):
        argv += ["--topic", record["topic"]]
    return argv


def flush(host=None):
    """Deliver queued rows to the hub in order.

    behavior: quarantines any unparseable line first, then sends each record
      and REWRITES THE OUTBOX AFTER EVERY SUCCESSFUL SEND. Stops at the first
      failure with the remainder intact and in order -- draining past a failure
      would reorder the queue, and dropping the failed record would lose it
      silently.
    in: host, the ssh alias.
    out: {"delivered": n, "remaining": n, "malformed": n}. remaining > 0 means
      the hub is unreachable right now, the expected evening state, not an
      error. malformed > 0 means rows were moved to outbox.bad and need a human.
    side effects: up to one ssh call per record; one outbox rewrite PER
      DELIVERED ROW; appends to outbox.bad if anything was unparseable.
    errors: none raised for a refused row. A remote CLI that REJECTS a row
      (nonzero rc that is not a transport failure) is still a stop, not a drop
      -- a row the hub refuses is a bug to be seen, and this function does not
      get to decide that a message the caller wrote may be discarded. A disk
      error from the outbox rewrite PROPAGATES rather than being swallowed:
      losing the ability to record progress is exactly the condition under
      which continuing to send manufactures duplicates.

    WHY REWRITE PER ROW AND NOT ONCE AT THE END: the old shape rewrote the
    outbox after the loop, so a crash anywhere in a batch of N resent ALL N
    delivered rows on the next pass. Per-row persistence bounds that to ONE
    row. It cannot reach zero -- there is no transaction spanning "the hub
    appended it" and "we wrote that down" -- which is why the surviving
    duplicate is made visible by a delivery id instead of pretended away. See
    MSGID above.
    """
    host = host or remote_host()
    records, malformed = read_outbox(host)
    if malformed:
        quarantine(host, malformed)
        # Rewrite without the bad lines so the queue reflects what is actually
        # deliverable; the verbatim copy in outbox.bad is the durable record.
        _save_outbox(host, records)
        sys.stderr.write(
            "remote-sync: %d unparseable outbox line(s) moved to %s -- these "
            "rows were NEVER delivered and need a human\n"
            % (len(malformed), _quarantine_path(host))
        )
    if not records:
        return {"delivered": 0, "remaining": 0, "malformed": len(malformed)}
    delivered = 0
    for record in records:
        rc, _out, err = _ssh(build_post_argv(record), host=host)
        if rc != 0:
            sys.stderr.write(
                "remote-sync: delivery stopped at queued row %d/%d (rc=%d): %s\n"
                % (delivered + 1, len(records), rc, err.strip())
            )
            break
        delivered += 1
        _save_outbox(host, records[delivered:])
    return {
        "delivered": delivered,
        "remaining": len(records) - delivered,
        "malformed": len(malformed),
    }


def post(runid, seat, kind, text, topic=None, to=None, host=None):
    """Send one row to the hub's mailbox, queueing it if the hub is offline.

    behavior: validates, QUALIFIES the seat with this machine's label, appends
      to the outbox, then flushes the whole outbox in order. Enqueue-then-flush
      (rather than try-then-queue-on-failure) is what makes ordering free: a
      fresh row physically cannot overtake one queued earlier.
    in: runid; seat, this machine's local seat name (unqualified is normal);
      kind, from swarm_mailbox.VALID_KINDS; text; topic OR to, never both;
      host.
    out: flush()'s report dict for the pass this post triggered.
    side effects: one outbox append; up to one ssh call per queued row.
    errors: ValueError for an invalid kind, an invalid seat, or topic AND to
      together -- raised HERE, before the row is queued, so a malformed row
      fails while the author is still watching instead of at 6am against a
      hub that will reject it.
    """
    if kind not in swarm_mailbox.VALID_KINDS:
        raise ValueError(
            "invalid kind %r; must be one of %s"
            % (kind, "|".join(swarm_mailbox.VALID_KINDS))
        )
    if not seat or "/" in seat:
        raise ValueError("invalid seat name %r" % seat)
    if to and topic:
        raise ValueError("pass either --topic or --to, not both")
    host = host or remote_host()
    record = {
        "runid": runid,
        "seat": qualify(seat, machine_label()),
        "kind": kind,
        "text": text,
        "topic": topic,
        "to": to,
        # Assigned ONCE, at enqueue, and stable across every resend -- that is
        # the whole property that makes a duplicate matchable later.
        "msgid": new_msgid(),
        "queued_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    enqueue(record, host=host)
    return flush(host=host)


# ---- pull ----------------------------------------------------------------


def _delivery_cursor(host, runid):
    """This puller's cursor for one hub run, as the shared confirmed-delivery
    helper (swarm_mailbox.DeliveryCursor, issue #30) rather than a private
    load/save pair of the same arithmetic.

    THE KEY STAYS HERE, THE RULE DOES NOT: what counts as "one stream" is
    per-(host, runid) and only this module knows that; the load, the per-seat
    split, the atomic write, and above all "the cursor moves only when the
    caller confirms" are the same in every consumer, and a second copy of them
    is a second place for the confirm order to rot into commit-on-read. That
    order is load-bearing here -- see pull()."""
    return swarm_mailbox.DeliveryCursor(_cursor_path(host, runid))


def fetch_remote_rows(runid, host=None):
    """Every row currently in the HUB's mailbox for this run, as dicts.

    behavior: runs the hub's own `comms read --replay` as OBSERVER_SEAT (a name
      no real seat uses, so nothing is excluded) and parses its JSONL stdout. A
      malformed line is skipped, never fatal -- the same tolerance
      _all_sibling_rows applies to a concurrently-written file, for the same
      reason.
      --replay IS LOAD-BEARING: `comms read` keeps a per-(runid, seat, view)
      cursor on the HUB, and this puller keeps its own per-(host, runid) cursor
      HERE. Two cursors over one stream is one too many -- the hub's would trim
      the batch to rows it had not printed before, and this side's per-seat
      COUNT cursor, reading a trimmed batch as if it started at row 0, would
      then discard those rows as already-seen. Silent loss, in the one place
      this module exists to prevent it. The local cursor stays the only one.
      VERSION FLOOR: the hub must be on the commit that added --replay or newer;
      an older hub rejects the flag with rc=1, which surfaces here as
      RemoteUnreachable naming the remote stderr. Loudly wrong beats quietly
      wrong -- see the discarded fallback in adapters/remote/README.md.
    out: list of row dicts, in the hub's own `at` order.
    errors: RemoteUnreachable if ssh or the remote CLI returns nonzero. This
      is the channel that keeps "could not look" from being reported as
      "nothing there".
    """
    host = host or remote_host()
    rc, out, err = _ssh(
        [remote_bin(), "read", runid, OBSERVER_SEAT, "--replay"], host=host
    )
    if rc != 0:
        raise RemoteUnreachable(
            "%s: `comms read %s` rc=%d: %s" % (host, runid, rc, err.strip())
        )
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def pull(runid, host=None):
    """Bring the hub's new rows into this machine's mailbox.

    behavior: reads the hub, advances a per-seat cursor over EVERY row it saw,
      drops echoes and other machines' mirror files, qualifies each surviving
      seat with the hub's label, and appends the result to this machine's
      mirror file for that hub. After this returns, those rows are ordinary
      sibling rows locally: `comms read`, the push heartbeat, and the Discord
      mirror all see them with no changes.
      THE CURSOR ADVANCES OVER DROPPED ROWS TOO -- otherwise every pass would
      re-scan the same echoes forever.
      The mirror file is written BEFORE the cursor is saved: a crash between
      the two re-mirrors rows on the next pass (visible, duplicated), where the
      other order loses them (invisible). Duplicates are the survivable failure.
      That order is now the shared confirmed-delivery contract rather than this
      module's own habit: the cursor is a swarm_mailbox.DeliveryCursor, take()
      writes nothing, and confirm() runs only after append_mirrored returned.
      Anything that raises first -- an unreachable hub, a row append_mirrored
      rejects -- leaves the cursor exactly where it was, so the next pull sees
      those rows again instead of skipping them.
    in: runid; host.
    out: {"inspected", "mirrored", "echo", "skipped", "dupes"}. `inspected` is
      asserted, not implied, because a pull that reached a hub with nothing new
      and a pull that never reached anything are otherwise identical silence.
      `echo` and `skipped` are the TWO distinct reasons a fresh row was not
      mirrored (see classify) and are counted separately: reporting a third
      machine's mirror rows under a counter named "echo" made them read as this
      machine's own traffic coming back.
      `dupes` counts surplus copies over EVERY row inspected, not just the
      fresh slice -- a duplicate is a property of the board, and the copies are
      usually this machine's own rows, which the echo filter drops before the
      fresh slice would ever see them.
    side effects: one ssh call; appends to the local mirror file; writes a
      cursor.
    errors: RemoteUnreachable, propagated from fetch_remote_rows.
    """
    host = host or remote_host()
    label = remote_label(host)
    mine = machine_label()
    rows = fetch_remote_rows(runid, host=host)
    fresh, confirm = _delivery_cursor(host, runid).take(rows)
    counts = {"mirror": 0, "echo": 0, "skipped": 0}
    qualified = []
    for row in fresh:
        verdict = classify(row, mine)
        counts[verdict] += 1  # KeyError, loudly, if classify ever grows a case
        if verdict != "mirror":
            continue
        row = dict(row)
        row["seat"] = qualify(str(row.get("seat", "?")), label)
        qualified.append(row)
    swarm_mailbox.append_mirrored(runid, mirror_seat(label), qualified)
    # Delivery here IS the local mirror write; the cursor moves only now that
    # it returned. Never move this line above append_mirrored.
    confirm()
    return {
        "inspected": len(rows),
        "mirrored": counts["mirror"],
        "echo": counts["echo"],
        "skipped": counts["skipped"],
        "dupes": redundant_row_count(rows),
    }


# ---- CLI -----------------------------------------------------------------

USAGE = """usage:
  sync.py post <runid> <seat> <kind> <text> [--topic <name> | --to <seat>] [--host <h>]
  sync.py flush [--host <h>]
  sync.py pull <runid> [--host <h>]
  sync.py sync <runid> [--host <h>]
  sync.py dupes <runid> [--host <h>]
  sync.py --follow <runid> [--interval N] [--host <h>]

exit: 0 delivered/pulled | 1 queued, hub unreachable | 2 usage or could-not-inspect
"""


def _extract_flags(args):
    """Pull --topic/--to/--host/--interval out of a positional arg list.
    Same hand-rolled shape as swarm_mailbox._extract_flags -- flags anywhere,
    positionals keep their arity."""
    flags = {"topic": None, "to": None, "host": None, "interval": None}
    out = []
    i = 0
    while i < len(args):
        name = args[i][2:] if args[i].startswith("--") else None
        if name in flags:
            if i + 1 >= len(args):
                raise ValueError("%s needs a value" % args[i])
            flags[name] = args[i + 1]
            i += 2
            continue
        out.append(args[i])
        i += 1
    return out, flags


def _format_flush(report):
    return "delivered=%d queued=%d malformed=%d" % (
        report["delivered"], report["remaining"], report["malformed"],
    )


def _format_pull(counts):
    return "inspected=%d mirrored=%d echo=%d skipped=%d dupes=%d" % (
        counts["inspected"], counts["mirrored"], counts["echo"],
        counts["skipped"], counts["dupes"],
    )


def _report_flush(report):
    sys.stdout.write(_format_flush(report) + "\n")
    return 0 if report["remaining"] == 0 else 1


def _do_pull(runid, host):
    sys.stdout.write(_format_pull(pull(runid, host=host)) + "\n")
    return 0


def _do_dupes(runid, host):
    """Read-time duplicate report over the hub's board for one run.

    A QUERY, not a detector: it is answered on demand and has no invoker of its
    own, which is the correct shape for a tool a human reaches for after seeing
    a nonzero dupes= in a pull. Exit 0 whenever the hub could be read (the
    printed count is the channel), 2 when it could not.
    """
    rows = fetch_remote_rows(runid, host=host)
    dupes = duplicate_msgids(rows)
    sys.stdout.write(
        "inspected=%d duplicated_ids=%d redundant_rows=%d\n"
        % (len(rows), len(dupes), sum(c - 1 for c in dupes.values()))
    )
    by_id = {}
    for row in rows:
        msgid = read_msgid(str(row.get("text", "")))
        if msgid in dupes:
            by_id.setdefault(msgid, []).append(row)
    for msgid in sorted(by_id):
        copies = by_id[msgid]
        sys.stdout.write(
            "  %s x%d  %s: %s\n"
            % (
                msgid,
                len(copies),
                copies[0].get("seat", "?"),
                strip_msgid(str(copies[0].get("text", ""))),
            )
        )
        for copy in copies:
            sys.stdout.write("      at %s\n" % copy.get("at", "?"))
    return 0


def follow(runid, interval, host):
    """Poll loop: flush the outbox, then pull, forever.

    LAUNCHD SAFETY: an unreachable hub is this adapter's EXPECTED condition,
    not news, so a failed pass writes one stderr line and waits -- it never
    exits nonzero into a KeepAlive restart loop. See mirror.py's follow() for
    the same rationale in the Discord adapter.
    """
    while True:
        try:
            report = flush(host=host)
            counts = pull(runid, host=host)
            sys.stdout.write(
                _format_flush(report) + " " + _format_pull(counts) + "\n"
            )
            sys.stdout.flush()
        except RemoteUnreachable as exc:
            sys.stderr.write("remote-sync: %s (retrying in %ds)\n" % (exc, interval))
        except Exception as exc:  # noqa: BLE001 -- one bad pass must not kill the loop
            sys.stderr.write("remote-sync: pass failed: %s\n" % exc)
        time.sleep(interval)


def main(argv):
    args = argv[1:]
    if not args:
        sys.stderr.write(USAGE)
        return 2
    try:
        rest, flags = _extract_flags(args)
    except ValueError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2
    host = flags["host"] or remote_host()

    if rest and rest[0] == "--follow":
        if len(rest) != 2:
            sys.stderr.write(USAGE)
            return 2
        interval = int(flags["interval"] or DEFAULT_INTERVAL)
        follow(rest[1], interval, host)
        return 0  # unreachable; follow() loops forever

    cmd = rest[0] if rest else ""
    try:
        if cmd == "post":
            if len(rest) != 5:
                sys.stderr.write(USAGE)
                return 2
            return _report_flush(post(
                rest[1], rest[2], rest[3], rest[4],
                topic=flags["topic"], to=flags["to"], host=host,
            ))
        if cmd == "flush":
            if len(rest) != 1:
                sys.stderr.write(USAGE)
                return 2
            return _report_flush(flush(host=host))
        if cmd == "pull":
            if len(rest) != 2:
                sys.stderr.write(USAGE)
                return 2
            return _do_pull(rest[1], host)
        if cmd == "dupes":
            if len(rest) != 2:
                sys.stderr.write(USAGE)
                return 2
            return _do_dupes(rest[1], host)
        if cmd == "sync":
            if len(rest) != 2:
                sys.stderr.write(USAGE)
                return 2
            report = flush(host=host)
            sys.stdout.write(_format_flush(report) + "\n")
            rc = _do_pull(rest[1], host)
            # A queued row means the hub was unreachable during the flush, so
            # report that rather than the pull's success.
            return rc if report["remaining"] == 0 else 1
    except RemoteUnreachable as exc:
        # COULD NOT INSPECT is exit 2, never 0: a pull that never reached the
        # hub must not read like a hub with nothing to say.
        sys.stderr.write("remote-sync: %s\n" % exc)
        return 2
    except ValueError as exc:
        sys.stderr.write("remote-sync: %s\n" % exc)
        return 2
    sys.stderr.write(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
