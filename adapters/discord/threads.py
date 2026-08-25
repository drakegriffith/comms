#!/usr/bin/env python3
"""discord threads: the one question "which Discord thread does this document
key belong to", and everything ugly required to answer it.

ONE DEEP INTERFACE: thread_for(key, name, lane, poster) -> thread_id | None.
Behind it sit the forum-webhook create-vs-reuse decision, the map file's path,
shape, atomic write and lock, and every HTTP failure mode. A caller decides
WHETHER to render a thread; it never learns how a thread comes to exist.

WHY A MAP FILE AT ALL: a forum webhook can CREATE a thread (POST with
`thread_name`) or post INTO one (POST with `?thread_id=`), and nothing in the
webhook API answers "does a thread named X already exist". So this machine
remembers. Forget, and every pass opens a new thread for the same document.

THE MAP IS FLEET-WIDE PER LANE, not per run (issue #40, D3): one document is
discussed by seats in DIFFERENT runs, so a per-runid map would let run A and
run B each open a thread for one file. The path is
$COMMS_STATE_DIR/<lane state dir>/threads.json, shape {thread_key: thread_id}.
Written tmp + os.replace (PID-suffixed tmp) so a crash never leaves half a
map, and a corrupt or unreadable map reads as {} -- the recovery cost is at
most one duplicate thread, where trusting a half-parsed map is a wrong id and
rows posted into someone else's conversation.

THE LOCK IS HELD ACROSS READ -> CREATE -> PERSIST. Two pollers on one machine
(the mirror's own per-(runid, lane) lock does NOT cover this: a thread key
spans runs, deliberately) would otherwise both read an empty map, both create,
and the loser's thread is orphaned forever. The lock makes check-then-act
atomic. It is a SIDECAR file, threads.lock, NOT the map itself -- a deviation
from the design note with a mechanical reason: the map is replaced by
os.replace, so an fd locked on the map names an inode that stops being the map
the instant it is saved, and the next process opens the NEW inode and locks
nothing. Cross-MACHINE races stay unfixed (a local file cannot serialize two
machines); the accepted cost is at most one duplicate thread per key per
machine, and the revisit condition is a second machine running the board lane.

EVERY FAILURE IS None (D6's table). No exception leaves this module: a create
that 4xx'd, a body that was not JSON, a map that would not persist -- all None,
which the caller reads as "not renderable yet" and leaves its rows in the held
file for the next pass. A raise here would stop every OTHER thread's delivery
in the same pass.

THE POSTER IS INJECTED, at the composition root (adapters/discord/mirror.py's
board branch builds webhook_poster(url) from the resolved forum webhook). That
is what lets the tests drive every branch of this file without a network, and
it keeps the secret's lifetime in the one module that already resolves
secrets.
"""

import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.request

# Lane -> state subdir. DEFINED HERE because the map file is this module's
# file; adapters/discord/mirror.py imports this dict into its own
# LANE_STATE_DIRS so the board lane's cursor, skipped log, lock, held file and
# thread map all land in one directory spelled exactly once.
STATE_DIRS = {"board": "discord-mirror-board"}

MAP_FILENAME = "threads.json"
LOCK_FILENAME = "threads.lock"

# A forum thread must be created WITH a starter message -- the API has no
# empty-thread create. This is that message: the document key, so a human
# scrolling the forum can tell what the thread is even before the first row
# lands in it.
SEED_CONTENT = "\U0001f9f5 %s"

# Never let a rendered document key turn into a ping. A thread title or key
# containing "@everyone" is display text, not an instruction.
ALLOWED_MENTIONS = {"parse": []}

MAX_RETRIES = 3  # additional attempts after the first
HTTP_TIMEOUT = 15


def _state_dir():
    # Same default chain as lib/swarm_arm.py and mirror.py.
    return os.environ.get("COMMS_STATE_DIR") or os.path.expanduser("~/.comms/state")


def _lane_dir(lane):
    return os.path.join(_state_dir(), STATE_DIRS[lane])


def map_path(lane):
    """This lane's fleet-wide thread map: {thread_key: thread_id}."""
    return os.path.join(_lane_dir(lane), MAP_FILENAME)


def lock_path(lane):
    """The sidecar lock guarding read -> create -> persist on the map. See the
    module docstring for why it is not the map file itself."""
    return os.path.join(_lane_dir(lane), LOCK_FILENAME)


def load_map(lane):
    """The lane's map, or {} if it is absent, unreadable, corrupt, or not a
    dict. Corruption is LOUD (one stderr line) but never fatal: recreating a
    thread costs one duplicate, and honoring a half-parsed map costs rows
    posted into the wrong conversation."""
    path = map_path(lane)
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(
            "discord threads: thread map unreadable (%s); treating as empty, "
            "threads may be recreated\n" % exc.__class__.__name__
        )
        return {}
    if not isinstance(data, dict):
        sys.stderr.write(
            "discord threads: thread map is not an object; treating as empty\n"
        )
        return {}
    return data


def _save_map(lane, data):
    """Durable write: PID-suffixed tmp in the same dir, flush + fsync, then
    os.replace, then a best-effort fsync of the directory.

    The fsync is not ceremony (PR #51 review, Codex 3): thread_for returns an
    id to a caller that is about to drop those rows from its held file, so
    the mapping has to be on the platter before the rows stop being. A map
    that loses its last entry to a power cut re-creates the thread AND the
    rows are gone.

    Raises OSError to the caller, which is the ONE failure this module does
    not swallow silently -- see thread_for."""
    path = map_path(lane)
    tmp = path + ".tmp." + str(os.getpid())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(os.path.dirname(path), os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass  # some filesystems refuse a directory fsync; the
                # rename has already happened, so this is best-effort
            finally:
                os.close(dir_fd)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _acquire_map_lock(lane):
    """Block until this machine's map lock is ours. Returns the open fd; the
    caller closes it, which releases the lock even if the process dies.

    RAISES OSError on a state dir it cannot create, a lock file it cannot
    open, or a flock the filesystem refuses. thread_for catches all three --
    see its errors: contract -- rather than this function guessing which of
    them is survivable.

    BLOCKING, unlike the mirror's per-pass lock: there the loser's rows are
    exactly what the winner is posting, so skipping costs nothing. Here the
    loser has rows of its OWN to deliver into a thread the winner is about to
    create, and the wait is one HTTP round trip.
    """
    os.makedirs(_lane_dir(lane), exist_ok=True)
    fd = os.open(lock_path(lane), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    return fd


def thread_for(key, name, lane, poster):
    """The Discord thread id for document `key`, creating the thread if this
    machine has never seen the key. None if it cannot be had right now.

    in: key, a thread key from swarm_mailbox.thread_key ("doc:<repo>/<path>");
      name, the human-visible thread title; lane, a key of STATE_DIRS;
      poster, a callable(name, content) -> thread_id or None (see
      webhook_poster) injected by the caller.
    out: a thread id string, or None.
    side effects: may create a Discord thread and rewrite the lane's map file;
      takes and releases the lane's map lock.
    errors: none propagate -- INCLUDING failures taking the lock itself
      (an unwritable state dir, a refused flock, a descriptor table that is
      full). Every failure is None plus one stderr line, because this is
      called once per document in a loop and one broken document must not
      stop the rest of the pass.

    ORDER IS THE CONTRACT: read map -> (miss) create -> PERSIST -> return.
    Persisting before returning is what stops the next pass, or the next
    process, from opening a second thread for the same document.

    A CREATE THAT SUCCEEDS BUT CANNOT BE RECORDED returns None on purpose.
    Returning the id would post rows into a thread nothing remembers, so the
    next pass opens another one AND those rows are already gone from the
    caller's held file. None keeps the rows held; the cost is one leaked empty
    thread, which Discord auto-archives.
    """
    try:
        lock_fd = _acquire_map_lock(lane)
    except OSError as exc:
        # PR #51 review, Codex 4. This used to sit outside the try and
        # propagate: an unwritable state dir, a refused flock, or an
        # exhausted descriptor table aborted the entire mirror pass, so ONE
        # broken lock stopped every OTHER document's thread from draining
        # too. None is the same answer every other failure here gives -- the
        # rows stay held and the next pass tries again.
        sys.stderr.write(
            "discord threads: could not take the thread-map lock (%s); no "
            "thread for %r this pass, its rows stay held\n"
            % (exc.__class__.__name__, key)
        )
        return None
    try:
        current = load_map(lane)
        existing = current.get(key)
        if existing:
            return existing
        try:
            thread_id = poster(name, SEED_CONTENT % key)
        except Exception as exc:
            sys.stderr.write(
                "discord threads: creating a thread for %r failed (%s); rows "
                "stay held for the next pass\n" % (key, exc.__class__.__name__)
            )
            return None
        if not thread_id:
            return None
        current[key] = thread_id
        try:
            _save_map(lane, current)
        except OSError as exc:
            sys.stderr.write(
                "discord threads: thread map unwritable (%s); the thread for "
                "%r was created but NOT recorded, so it is leaked (it will "
                "auto-archive) and its rows stay held\n"
                % (exc.__class__.__name__, key)
            )
            return None
        return thread_id
    finally:
        os.close(lock_fd)  # releases the flock, even on an exception


def webhook_poster(url):
    """Build the real creating-POST for a forum webhook `url`.

    Returns callable(name, content) -> thread_id or None. The URL is captured
    in the closure and NEVER printed, logged, or included in an error message
    -- a webhook URL is the secret itself, not a pointer to one.

    WIRE SHAPE: POST <url>?wait=true with {"thread_name", "content",
    "allowed_mentions"}. `thread_name` is what makes a forum webhook CREATE a
    thread rather than post to the channel; `wait=true` is what makes Discord
    return the created message instead of an empty 204, which is the only way
    to learn the new thread's id. The starter message's `channel_id` IS the
    new thread; `id` (the message id) is the documented fallback.
    """

    def post(name, content):
        payload = {
            "thread_name": name,
            "content": content,
            "allowed_mentions": ALLOWED_MENTIONS,
        }
        body = json.dumps(payload).encode("utf-8")
        target = url + ("&" if "?" in url else "?") + "wait=true"
        attempt = 0
        while True:
            req = urllib.request.Request(
                target,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "comms-discord-mirror",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                    raw = resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < MAX_RETRIES:
                    time.sleep(min(_retry_after(exc), 30))
                    attempt += 1
                    continue
                sys.stderr.write(
                    "discord threads: create-thread POST failed (HTTP %d) "
                    "after %d attempt(s)\n" % (exc.code, attempt + 1)
                )
                return None
            except (urllib.error.URLError, OSError) as exc:
                if attempt < MAX_RETRIES:
                    time.sleep(1)
                    attempt += 1
                    continue
                sys.stderr.write(
                    "discord threads: create-thread POST failed (%s) after "
                    "%d attempt(s)\n" % (exc.__class__.__name__, attempt + 1)
                )
                return None
            return _thread_id_from(raw)

    return post


def _retry_after(exc):
    try:
        return float(exc.headers.get("Retry-After") or "1")
    except (AttributeError, ValueError):
        return 1.0


def _thread_id_from(raw):
    """The new thread's id out of a create response body, or None.

    A body that is not JSON, or JSON without either id, is None rather than a
    guess: an id this module invented would send every later row into a
    thread that does not exist, and those POSTs fail silently from the
    reader's point of view.
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        sys.stderr.write(
            "discord threads: create-thread response was not JSON; no id\n"
        )
        return None
    if not isinstance(data, dict):
        return None
    return data.get("channel_id") or data.get("id") or None
