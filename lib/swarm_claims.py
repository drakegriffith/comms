#!/usr/bin/env python3
"""swarm_claims: run-scoped write-set claims arbiter for a swarm.

WHY THIS EXISTS
  Seats announce claims on the mailbox, but an announcement is not arbitration:
  nothing could say "you did not get this path". A predecessor scratch-board
  arbiter proved the verbs but had one measured defect: its claims dir was not
  wave-scoped and had no expiry, so 34 dead wave-2 claims read as HELD to every
  wave-3 bucket. Three buckets independently posted claim-conflict about it. An
  arbiter that cannot expire its own answers hands out confident stale
  ownership, which is worse than not answering.

RUN-SCOPING IS THE EXPIRY
  A claim lives INSIDE the run dir swarm_arm.py already owns:
  <state>/swarm-arm/<runid>/claims/. When the run is disarmed, its claims go
  with it -- structurally unreachable, not swept. This keys expiry on a fact
  that is KNOWN (the run ended) rather than one that must be guessed (is the
  holder alive). There is deliberately NO timeout: a slow seat and a dead one
  look identical, so age never decides anything here. `reap` takes the owner id
  explicitly -- the caller asserts death, the arbiter records who was blamed.

  Claiming therefore REQUIRES an armed run, the same rule as enrollment: you
  cannot claim inside a run that does not exist. That is what prevents the
  scratch board's failure from being re-manufactured -- there is no runless,
  eternal claims dir to go stale.

ATOMICITY
  One claim = one os.mkdir, which the filesystem arbitrates: exactly one caller
  creates the dir, everyone else is told who holds it. No locks, no races, the
  same mechanism the scratch board used. The owner file is written by the
  winner only.

WHO MAY DO WHAT (all refusals are LOUD, never silent no-ops)
  * claim: first writer wins; a loser is told the holder's name and exits 1.
  * release: an owner may release only its OWN claims. Releasing a peer's is
    how a coordination structure becomes a way to stomp a peer -- refused,
    naming the holder.
  * reap: sweeps every claim of an owner the CALLER asserts is dead. Reaping
    zero claims exits 1 -- "I reaped nothing" is a result, not a pass.

PATH ENCODING
  The predecessor mapped "/" to "%", which collides a path containing a literal
  "%" ("a%b" and "a/b" encoded identically). Keys here are percent-encoded with
  no safe chars (urllib quote), which is reversible and collision-free. This
  diverges from the predecessor on purpose.

WHAT IS NOT HERE
  The predecessor's post/read board half is NOT absorbed: the durable findings
  channel is swarm_mailbox.py (topics, subscriptions, heartbeat injection).
  This module is only the arbiter. swarm_arm.py is IMPORTED for the armed-run
  check -- one implementation, never re-derived (same rule the heartbeat
  follows).

CLI (exit codes: 0 ok, 1 conflict/refused/nothing-reaped, 2 usage, 3 not-armed)
  swarm_claims.py claim   <runid> <owner> <path> [<path> ...]
  swarm_claims.py release <runid> <owner> <path> [<path> ...]
  swarm_claims.py reap    <runid> <owner>
  swarm_claims.py who     <runid> [<path>]
"""

import os
import shutil
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swarm_arm  # noqa: E402  (the one armed-run implementation)


def _claims_dir(runid, state_dir=None):
    return os.path.join(swarm_arm._run_dir(runid, state_dir), "claims")


def _key(path):
    """Reversible, collision-free filesystem key for a claimed path."""
    return urllib.parse.quote(path, safe="")


def _unkey(key):
    return urllib.parse.unquote(key)


def _owner_of(claim_dir):
    try:
        with open(os.path.join(claim_dir, "owner")) as fh:
            return fh.read().strip()
    except OSError:
        return None


def claim(runid, owner, paths, state_dir=None):
    """Try to claim each path for `owner`. Returns a list of (path, status,
    holder) where status is "ok" | "ok-self" | "held", holder is the current
    owner. Raises LookupError if the run is not armed -- you cannot claim inside
    a run that does not exist, and answering quietly would recreate the eternal
    claims dir this module exists to remove.
    """
    if not owner:
        raise ValueError("owner must be non-empty")
    if not swarm_arm.is_armed(runid, state_dir):
        raise LookupError("not-armed: %s" % runid)
    cdir = _claims_dir(runid, state_dir)
    os.makedirs(cdir, exist_ok=True)
    out = []
    for p in paths:
        k = os.path.join(cdir, _key(p))
        try:
            os.mkdir(k)  # the atomic arbitration: exactly one caller succeeds
        except FileExistsError:
            holder = _owner_of(k)
            status = "ok-self" if holder == owner else "held"
            out.append((p, status, holder))
            continue
        with open(os.path.join(k, "owner"), "w") as fh:
            fh.write(owner)
        out.append((p, "ok", owner))
    return out


def holder(runid, path, state_dir=None):
    """The current owner of `path` in this run, or None. A disarmed run holds
    nothing, by construction -- its claims dir is gone."""
    return _owner_of(os.path.join(_claims_dir(runid, state_dir), _key(path)))


def who(runid, state_dir=None):
    """Every (owner, path) held in this run, sorted by path."""
    cdir = _claims_dir(runid, state_dir)
    try:
        keys = os.listdir(cdir)
    except OSError:
        return []
    rows = []
    for k in keys:
        o = _owner_of(os.path.join(cdir, k))
        if o is not None:
            rows.append((o, _unkey(k)))
    return sorted(rows, key=lambda r: r[1])


def release(runid, owner, paths, state_dir=None):
    """Release `owner`'s own claims. Returns (path, status, holder) rows with
    status "released" | "not-held" | "refused". A refusal names the real holder
    and is the caller's cue that it was about to stomp a peer."""
    out = []
    for p in paths:
        k = os.path.join(_claims_dir(runid, state_dir), _key(p))
        if not os.path.isdir(k):
            out.append((p, "not-held", None))
            continue
        h = _owner_of(k)
        if h == owner:
            shutil.rmtree(k, ignore_errors=True)
            out.append((p, "released", owner))
        else:
            out.append((p, "refused", h))
    return out


def reap(runid, owner, state_dir=None):
    """Remove every claim held by `owner`, which the CALLER asserts is dead
    (there is no liveness oracle; age never decides). Returns the reaped paths.
    An empty return means nothing was held -- the CLI exits 1 on it so a no-op
    reap cannot read as a successful cleanup."""
    cdir = _claims_dir(runid, state_dir)
    try:
        keys = os.listdir(cdir)
    except OSError:
        return []
    reaped = []
    for k in keys:
        full = os.path.join(cdir, k)
        if _owner_of(full) == owner:
            shutil.rmtree(full, ignore_errors=True)
            reaped.append(_unkey(k))
    return sorted(reaped)


def _usage():
    sys.stderr.write(
        "usage: swarm_claims.py claim   <runid> <owner> <path> [<path> ...]\n"
        "       swarm_claims.py release <runid> <owner> <path> [<path> ...]\n"
        "       swarm_claims.py reap    <runid> <owner>\n"
        "       swarm_claims.py who     <runid> [<path>]\n"
        "exit: 0 ok | 1 conflict/refused/nothing-reaped | 2 usage | 3 not-armed\n"
    )


def main(argv):
    if len(argv) < 3:
        _usage()
        return 2
    cmd, runid = argv[1], argv[2]
    rest = argv[3:]

    if cmd == "claim":
        if len(rest) < 2:
            _usage()
            return 2
        try:
            rows = claim(runid, rest[0], rest[1:])
        except LookupError as exc:
            sys.stderr.write(str(exc) + "\n")
            return 3
        rc = 0
        for p, status, h in rows:
            if status == "ok":
                print("OK       %s" % p)
            elif status == "ok-self":
                print("OK(self) %s" % p)
            else:
                print("HELD BY %s -- %s" % (h, p))
                rc = 1
        return rc
    if cmd == "release":
        if len(rest) < 2:
            _usage()
            return 2
        rc = 0
        for p, status, h in release(runid, rest[0], rest[1:]):
            if status == "released":
                print("RELEASED %s" % p)
            elif status == "not-held":
                print("NOT-HELD %s" % p)
            else:
                print("REFUSED  %s -- held by %s, not by %s" % (p, h, rest[0]))
                rc = 1
        return rc
    if cmd == "reap":
        if len(rest) != 1:
            _usage()
            return 2
        reaped = reap(runid, rest[0])
        print("reaped %d claim(s) held by %s" % (len(reaped), rest[0]))
        for p in reaped:
            print("  %s" % p)
        return 0 if reaped else 1
    if cmd == "who":
        if len(rest) > 1:
            _usage()
            return 2
        if rest:
            h = holder(runid, rest[0])
            if h is None:
                print("UNHELD   %s" % rest[0])
                return 1
            print("%-12s %s" % (h, rest[0]))
            return 0
        for o, p in who(runid):
            print("%-12s %s" % (o, p))
        return 0
    _usage()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
