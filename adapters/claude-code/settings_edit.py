#!/usr/bin/env python3
"""settings_edit.py -- the one safe writer for hook entries in a JSON settings
file (Claude Code's ~/.claude/settings.json, Codex's ~/.codex/hooks.json).

WHY THIS MODULE EXISTS
  A corrupted or truncated settings.json breaks EVERY Claude session on the
  machine, not just comms. Three installers in this repo were each doing their
  own read-modify-write of that file, and each one had the same hole: it parsed
  the file, mutated the object, and wrote it back with no check that the bytes
  on disk were still the bytes it had read. Any change another process made in
  that window -- a concurrent `claude` session persisting a setting, a second
  installer, the operator's editor -- was silently discarded. Losing a hooks
  block that way is indistinguishable from corruption to everyone downstream.

  This module is that write, done once, defensively, so no installer open-codes
  it again.

THE WRITE PROTOCOL (in order; any step failing means NOTHING is written)
  1. LOCK. Take <path>.comms-lock with O_CREAT|O_EXCL. Serialises the comms
     installers against each other. A lock older than STALE_LOCK_SECS is
     reported and broken, so a crashed installer cannot wedge the machine
     forever.
  2. SNAPSHOT. Read the raw bytes, record sha256 + size.
  3. PARSE. Unparseable JSON is REFUSED (exit 3), never rewritten. Refusing
     beats clobbering: a rewrite of a broken file destroys whatever the broken
     bytes were, and those bytes are the only evidence of what went wrong.
  4. MUTATE in memory. If nothing changed, stop -- an idempotent re-run must
     not rewrite the file at all, not even byte-identically (a rewrite churns
     mtime and races a concurrent reader for no benefit).
  5. BACKUP. copy2 to <path>.comms-backup.<timestamp>.
  6. STAGE. Serialise to <path>.comms-tmp in the SAME directory (os.replace is
     only atomic within a filesystem), fsync it, and re-parse the staged file
     before it is allowed anywhere near the live path.
  7. RECHECK. Re-read the live file and recompute sha256. If it differs from
     step 2, a concurrent writer won: delete the staging file, leave the
     concurrent content untouched, and REFUSE (exit 4). The caller re-runs.
  8. REPLACE. os.replace(tmp, path) -- atomic; a crash leaves either the whole
     old file or the whole new one, never a half-write. fsync the directory so
     the rename survives a power loss.

  HONEST LIMIT, STATED ON PURPOSE: Claude Code itself does not take our lock.
  Step 7 therefore narrows the clobber window to the microseconds between the
  recheck and the rename; it does not close it. That is strictly better than
  the unbounded window the old code had (parse, run other work, then write),
  and refusing on detection is the only behaviour that never loses data. A
  fully closed window needs the runtime to cooperate on a lock, which is not
  something this repo can grant itself.

TEST SEAM
  $COMMS_SETTINGS_RACE_HOOK, if set, is run as a shell command immediately
  after the snapshot in step 2. It exists so the parity suite can make a
  concurrent write happen deterministically at the one instant that matters.
  It is inert unless the variable is set.

CLI
  settings_edit.py add    --file P --event E [--matcher M] --command CMD
                          [--match-substring S] [--check]
  settings_edit.py remove --file P [--event E] --contains S [--check]
  settings_edit.py list   --file P [--event E]

  add is idempotent on the EXACT command string, and additionally treats
  --match-substring as "an equivalent entry already exists" so that a wiring
  installed under a different but working spelling (e.g. routed through a
  dispatch shim) is detected and LEFT ALONE rather than duplicated.

EXIT CODES
  0 changed, or already in the desired state (both are success for an
    idempotent installer)   1 bad usage/IO   3 refused: unparseable JSON
  4 refused: concurrent modification   5 refused: could not take the lock
"""

import argparse
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

STALE_LOCK_SECS = 300
E_USAGE = 1
E_UNPARSEABLE = 3
E_CONCURRENT = 4
E_LOCK = 5


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


class Refused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class SettingsFile:
    """A JSON settings file edited under lock, with a concurrency recheck."""

    def __init__(self, path, check=False):
        # SYMLINK-SAFE. os.replace(tmp, link) does NOT write through a symlink:
        # it unlinks the link and drops a plain file in its place, silently
        # severing it. Dotfile-managed machines routinely symlink these config
        # paths into a managed repo -- on this very machine ~/.codex/AGENTS.md
        # is a symlink into a separate harness repo -- so a writer that ignores
        # this destroys the link and orphans whatever the real file carried.
        # Resolving to the physical path first means the edit lands where the
        # link points, which is what the operator meant, and the link survives.
        self.given_path = os.path.abspath(path)
        self.path = os.path.realpath(self.given_path)
        if self.path != self.given_path:
            sys.stderr.write(
                "note: %s is a symlink; editing its target %s (the link is preserved)\n"
                % (self.given_path, self.path)
            )
        self.check = check
        self.lock_path = self.path + ".comms-lock"
        self._lock_fd = None
        self.raw = b""
        self.digest = None
        self.existed = False
        self.data = {}

    # -- step 1 -------------------------------------------------------------
    def _acquire(self):
        d = os.path.dirname(self.path)
        if d and not self.check:
            os.makedirs(d, exist_ok=True)
        if self.check or not d:
            # --check writes nothing at all, including a lock file.
            return
        for _ in range(50):  # ~5s
            try:
                self._lock_fd = os.open(
                    self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                os.write(self._lock_fd, ("%d\n" % os.getpid()).encode())
                return
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise Refused(E_LOCK, "cannot create %s: %s" % (self.lock_path, exc))
                try:
                    age = time.time() - os.stat(self.lock_path).st_mtime
                except FileNotFoundError:
                    continue  # holder released it; retry immediately
                if age > STALE_LOCK_SECS:
                    sys.stderr.write(
                        "note: breaking a stale lock (%ds old) at %s\n"
                        % (int(age), self.lock_path)
                    )
                    try:
                        os.unlink(self.lock_path)
                    except FileNotFoundError:
                        pass
                    continue
                time.sleep(0.1)
        raise Refused(
            E_LOCK,
            "another comms installer holds %s; wait for it to finish, or remove "
            "that file if no installer is running" % self.lock_path,
        )

    def _release(self):
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
            try:
                os.unlink(self.lock_path)
            except FileNotFoundError:
                pass

    # -- steps 2 and 3 ------------------------------------------------------
    def load(self):
        self._acquire()
        try:
            with open(self.path, "rb") as fh:
                self.raw = fh.read()
            self.existed = True
        except FileNotFoundError:
            self.raw = b""
            self.existed = False
        self.digest = _digest(self.raw)

        race = os.environ.get("COMMS_SETTINGS_RACE_HOOK")
        if race:
            sys.stderr.write("note: COMMS_SETTINGS_RACE_HOOK is set (test seam)\n")
            subprocess.run(race, shell=True)

        if not self.existed or not self.raw.strip():
            self.data = {}
            return
        try:
            self.data = json.loads(self.raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise Refused(
                E_UNPARSEABLE,
                "refusing to edit %s: not valid JSON (%s). Nothing was written; "
                "the file is untouched. Fix or restore it, then re-run."
                % (self.path, exc),
            )
        if not isinstance(self.data, dict):
            raise Refused(
                E_UNPARSEABLE,
                "refusing to edit %s: top level is %s, expected an object"
                % (self.path, type(self.data).__name__),
            )

    # -- steps 5 through 8 --------------------------------------------------
    def save(self):
        body = json.dumps(self.data, indent=2) + "\n"
        if self.check:
            print("--check: nothing written to %s" % self.path)
            return
        if self.existed:
            backup = self.path + ".comms-backup." + time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(self.path, backup)
            print("backup: %s" % backup)

        tmp = self.path + ".comms-tmp"
        with open(tmp, "w") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        with open(tmp) as fh:  # the staged file must parse before it goes live
            json.load(fh)

        # step 7: the concurrency recheck.
        try:
            with open(self.path, "rb") as fh:
                now = _digest(fh.read())
        except FileNotFoundError:
            now = _digest(b"")
        if now != self.digest:
            os.unlink(tmp)
            raise Refused(
                E_CONCURRENT,
                "refusing to write %s: it CHANGED since this installer read it "
                "(concurrent modification -- another process, likely a live "
                "Claude session, wrote it). That process's content is intact "
                "and nothing was overwritten. Re-run the installer."
                % self.path,
            )

        os.replace(tmp, self.path)
        d = os.path.dirname(self.path)
        if d:
            dfd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        print("wrote: %s (atomic replace)" % self.path)

    def close(self):
        self._release()

    # Context manager, so the lock is released on EVERY exit path including a
    # refusal or an unexpected exception. A leaked lock file makes every later
    # run of any comms installer stall and then refuse -- the installer would
    # wedge itself after one use.
    def __enter__(self):
        try:
            self.load()
        except BaseException:
            # __exit__ is NOT called when __enter__ raises, so a refusal during
            # load (unparseable JSON is the common one) would otherwise leak
            # the lock and wedge every subsequent run.
            self.close()
            raise
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# -- the hooks container ----------------------------------------------------
# Two shapes in the wild: settings.json nests events under "hooks", while a
# bare hooks.json may put the event map at the top level. Honour whichever the
# file already uses; a fresh file gets the shape its caller declares.
def container(data, nested):
    if isinstance(data.get("hooks"), dict):
        return data["hooks"]
    if nested:
        return data.setdefault("hooks", {})
    return data


def commands_in(cont, event):
    out = []
    for entry in cont.get(event, []) or []:
        if isinstance(entry, dict):
            for h in entry.get("hooks", []) or []:
                if isinstance(h, dict):
                    out.append(h.get("command") or "")
    return out


def cmd_add(args):
    with SettingsFile(args.file, args.check) as sf:
        cont = container(sf.data, not args.flat)
        have = commands_in(cont, args.event)

        if args.command in have:
            print("%s: already wired (exact match), left untouched" % args.event)
            return 0
        for sub in args.match_substring or []:
            equivalent = [c for c in have if sub in c]
            if equivalent:
                print(
                    "%s: an equivalent entry is already wired, left untouched: %s"
                    % (args.event, equivalent[0])
                )
                return 0

        entry = {"hooks": [{"type": "command", "command": args.command}]}
        if args.matcher:
            entry = {"matcher": args.matcher, "hooks": entry["hooks"]}
        lst = cont.setdefault(args.event, [])
        if not isinstance(lst, list):
            sys.stderr.write("refusing: %s is not a list in %s\n" % (args.event, sf.path))
            return E_USAGE
        print("%s: adding: %s" % (args.event, args.command))
        lst.append(entry)
        sf.save()
        return 0


def cmd_remove(args):
    with SettingsFile(args.file, args.check) as sf:
        cont = container(sf.data, not args.flat)
        events = [args.event] if args.event else list(cont.keys())
        removed = 0

        for event in events:
            lst = cont.get(event)
            if not isinstance(lst, list):
                continue
            keep = []
            for entry in lst:
                if not isinstance(entry, dict):
                    keep.append(entry)
                    continue
                hooks = entry.get("hooks")
                if not isinstance(hooks, list):
                    keep.append(entry)
                    continue
                survivors = [
                    h
                    for h in hooks
                    if not (isinstance(h, dict)
                            and args.contains in (h.get("command") or ""))
                ]
                if len(survivors) != len(hooks):
                    removed += len(hooks) - len(survivors)
                    # An entry whose every hook was ours goes away entirely;
                    # one that ALSO carried a foreign hook keeps that hook.
                    # Never delete a neighbour's wiring as collateral.
                    if survivors:
                        entry = dict(entry, hooks=survivors)
                    else:
                        continue
                keep.append(entry)
            if keep:
                cont[event] = keep
            else:
                del cont[event]

        if not removed:
            print("nothing matching %r was wired; nothing to remove" % args.contains)
            return 0
        print("removed %d hook command(s) matching %r" % (removed, args.contains))
        sf.save()
        return 0


def cmd_list(args):
    with SettingsFile(args.file, check=True) as sf:
        cont = container(sf.data, not args.flat)
        events = [args.event] if args.event else list(cont.keys())
        for event in events:
            for c in commands_in(cont, event):
                print("%s\t%s" % (event, c))
        return 0


def main(argv):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--flat", action="store_true",
                   help="treat the file as a bare event map, not {'hooks': {...}}")
    sub = p.add_subparsers(dest="op", required=True)

    a = sub.add_parser("add")
    a.add_argument("--file", required=True)
    a.add_argument("--event", required=True)
    a.add_argument("--matcher", default=None)
    a.add_argument("--command", required=True)
    a.add_argument("--match-substring", action="append", default=[])
    a.add_argument("--check", action="store_true")
    a.set_defaults(fn=cmd_add)

    r = sub.add_parser("remove")
    r.add_argument("--file", required=True)
    r.add_argument("--event", default=None)
    r.add_argument("--contains", required=True)
    r.add_argument("--check", action="store_true")
    r.set_defaults(fn=cmd_remove)

    l = sub.add_parser("list")
    l.add_argument("--file", required=True)
    l.add_argument("--event", default=None)
    l.set_defaults(fn=cmd_list)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except Refused as exc:
        sys.stderr.write(str(exc) + "\n")
        return exc.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
