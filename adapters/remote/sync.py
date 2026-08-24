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
flush stops at the first failure with the remainder intact.

EXIT CODES (the two success shapes are different codes on purpose):
  0  delivered / pulled. `pull` reporting inspected=0 still means it REACHED
     the hub and the hub had nothing new.
  1  queued, not delivered -- the hub is unreachable and the row is on local
     disk awaiting the next flush. Durable "later", not a crash.
  2  usage error, or COULD NOT INSPECT (pull could not reach the hub, remote
     CLI missing/erroring). Never a pass.
`pull` prints inspected/mirrored/echo counts every pass, because "the sync
never reached the hub" and "the hub had nothing" are otherwise identical
silence.

LAUNCHD SAFETY: same shape as adapters/discord/mirror.py -- --follow catches
per-pass errors, writes one stderr line and keeps polling, rather than exiting
nonzero into a KeepAlive restart loop. An unreachable hub is the EXPECTED
condition for this adapter, so --follow treats it as a quiet retry, not news.

CLI:
  sync.py post <runid> <seat> <kind> <text> [--topic <name> | --to <seat>] [--host <h>]
  sync.py flush [--host <h>]
  sync.py pull <runid> [--host <h>]
  sync.py sync <runid> [--host <h>]              # flush + pull
  sync.py --follow <runid> [--interval N] [--host <h>]
"""

import datetime
import json
import os
import shlex
import subprocess
import sys
import time

SELF_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SELF_DIR))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
sys.path.insert(0, os.path.join(REPO_ROOT, "adapters", "discord"))
import mirror  # noqa: E402  (machine_label -- ONE label per machine, not two)
import swarm_mailbox  # noqa: E402  (one mailbox writer/parser; never a second)

# The machine tag separator inside a seat name. "~" and not "@": "@" already
# means "unicast topic" in swarm_mailbox (SELF_TOPIC_PREFIX), and a seat name
# that reads like an address is a trap for a human skimming a board.
QUALIFIER = "~"

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
# exception and is therefore NOT a new knob: it is mirror.py's, reused.


def remote_host():
    return os.environ.get("COMMS_REMOTE_HOST") or DEFAULT_HOST


def remote_bin():
    return os.environ.get("COMMS_REMOTE_BIN") or DEFAULT_REMOTE_BIN


def remote_label(host=None):
    """Provenance label written into pulled seat names. Defaults to the ssh
    alias, which is already the human's name for that machine."""
    return os.environ.get("COMMS_REMOTE_LABEL") or (host or remote_host())


def machine_label():
    """This machine's label -- mirror.machine_label(), not a second copy. One
    machine must not be "studio" to Discord and something else to the mailbox."""
    return mirror.machine_label()


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


def _should_mirror(row, label):
    seat = str(row.get("seat", ""))
    return not is_echo(seat, label) and not is_mirror_seat(seat)


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


def load_outbox(host=None):
    """Every queued record, oldest first. A missing outbox is an empty queue,
    not an error. A malformed line is SKIPPED rather than fatal -- one corrupt
    row must not wedge every later row behind it forever."""
    path = _outbox_path(host or remote_host())
    records = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return records


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
    """
    argv = [
        remote_bin(),
        "post",
        record["runid"],
        record["seat"],
        record["kind"],
        record["text"],
    ]
    if record.get("to"):
        argv += ["--to", record["to"]]
    elif record.get("topic"):
        argv += ["--topic", record["topic"]]
    return argv


def flush(host=None):
    """Deliver queued rows to the hub in order. Returns (delivered, remaining).

    behavior: STOPS AT THE FIRST FAILURE and keeps every remaining record,
      order intact. Draining past a failure would reorder the queue; dropping
      the failed record would lose it silently. The outbox is rewritten once,
      atomically, only if something was delivered.
    in: host, the ssh alias.
    out: (delivered_count, remaining_count). remaining > 0 means the hub is
      unreachable right now, which is the expected evening state, not an error.
    side effects: up to one ssh call per queued record; one outbox rewrite.
    errors: none raised. A remote CLI that REJECTS a row (nonzero rc that is
      not a transport failure) is still a stop, not a drop -- a row the hub
      refuses is a bug to be seen, and this function does not get to decide
      that a message the caller wrote may be discarded.
    """
    host = host or remote_host()
    records = load_outbox(host)
    if not records:
        return 0, 0
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
    if delivered:
        _save_outbox(host, records[delivered:])
    return delivered, len(records) - delivered


def post(runid, seat, kind, text, topic=None, to=None, host=None):
    """Send one row to the hub's mailbox, queueing it if the hub is offline.

    behavior: validates, QUALIFIES the seat with this machine's label, appends
      to the outbox, then flushes the whole outbox in order. Enqueue-then-flush
      (rather than try-then-queue-on-failure) is what makes ordering free: a
      fresh row physically cannot overtake one queued earlier.
    in: runid; seat, this machine's local seat name (unqualified is normal);
      kind, from swarm_mailbox.VALID_KINDS; text; topic OR to, never both;
      host.
    out: (delivered, remaining) from the flush.
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
        "queued_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    enqueue(record, host=host)
    return flush(host=host)


# ---- pull ----------------------------------------------------------------


def _load_cursor(host, runid):
    try:
        with open(_cursor_path(host, runid)) as fh:
            cursor = json.load(fh)
    except (OSError, ValueError):
        return {}
    return cursor if isinstance(cursor, dict) else {}


def _save_cursor(host, runid, cursor):
    _write_atomic(_cursor_path(host, runid), json.dumps(cursor))


def fetch_remote_rows(runid, host=None):
    """Every row currently in the HUB's mailbox for this run, as dicts.

    behavior: runs the hub's own `comms read` as OBSERVER_SEAT (a name no real
      seat uses, so nothing is excluded) and parses its JSONL stdout. A
      malformed line is skipped, never fatal -- the same tolerance
      _all_sibling_rows applies to a concurrently-written file, for the same
      reason.
    out: list of row dicts, in the hub's own `at` order.
    errors: RemoteUnreachable if ssh or the remote CLI returns nonzero. This
      is the channel that keeps "could not look" from being reported as
      "nothing there".
    """
    host = host or remote_host()
    rc, out, err = _ssh([remote_bin(), "read", runid, OBSERVER_SEAT], host=host)
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
    in: runid; host.
    out: {"inspected": n, "mirrored": n, "echo": n} -- inspected is asserted,
      not implied, because a pull that reached a hub with nothing new and a
      pull that never reached anything are otherwise identical silence.
    side effects: one ssh call; appends to the local mirror file; writes a
      cursor.
    errors: RemoteUnreachable, propagated from fetch_remote_rows.
    """
    host = host or remote_host()
    label = remote_label(host)
    mine = machine_label()
    rows = fetch_remote_rows(runid, host=host)
    cursor = _load_cursor(host, runid)
    fresh, new_cursor = swarm_mailbox.fresh_rows_by_seat(rows, cursor)
    keep = [r for r in fresh if _should_mirror(r, mine)]
    qualified = []
    for row in keep:
        row = dict(row)
        row["seat"] = qualify(str(row.get("seat", "?")), label)
        qualified.append(row)
    swarm_mailbox.append_mirrored(runid, mirror_seat(label), qualified)
    _save_cursor(host, runid, new_cursor)
    return {
        "inspected": len(rows),
        "mirrored": len(qualified),
        "echo": len(fresh) - len(qualified),
    }


# ---- CLI -----------------------------------------------------------------

USAGE = """usage:
  sync.py post <runid> <seat> <kind> <text> [--topic <name> | --to <seat>] [--host <h>]
  sync.py flush [--host <h>]
  sync.py pull <runid> [--host <h>]
  sync.py sync <runid> [--host <h>]
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


def _report_flush(delivered, remaining):
    sys.stdout.write("delivered=%d queued=%d\n" % (delivered, remaining))
    return 0 if remaining == 0 else 1


def _do_pull(runid, host):
    counts = pull(runid, host=host)
    sys.stdout.write(
        "inspected=%d mirrored=%d echo=%d\n"
        % (counts["inspected"], counts["mirrored"], counts["echo"])
    )
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
            delivered, remaining = flush(host=host)
            counts = pull(runid, host=host)
            sys.stdout.write(
                "delivered=%d queued=%d inspected=%d mirrored=%d echo=%d\n"
                % (
                    delivered,
                    remaining,
                    counts["inspected"],
                    counts["mirrored"],
                    counts["echo"],
                )
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
            delivered, remaining = post(
                rest[1], rest[2], rest[3], rest[4],
                topic=flags["topic"], to=flags["to"], host=host,
            )
            return _report_flush(delivered, remaining)
        if cmd == "flush":
            if len(rest) != 1:
                sys.stderr.write(USAGE)
                return 2
            return _report_flush(*flush(host=host))
        if cmd == "pull":
            if len(rest) != 2:
                sys.stderr.write(USAGE)
                return 2
            return _do_pull(rest[1], host)
        if cmd == "sync":
            if len(rest) != 2:
                sys.stderr.write(USAGE)
                return 2
            delivered, remaining = flush(host=host)
            sys.stdout.write("delivered=%d queued=%d\n" % (delivered, remaining))
            rc = _do_pull(rest[1], host)
            # A queued row means the hub was unreachable during the flush, so
            # report that rather than the pull's success.
            return rc if remaining == 0 else 1
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
