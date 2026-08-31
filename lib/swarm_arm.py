#!/usr/bin/env python3
"""swarm_arm: PER-PARTICIPANT arming for the swarm heartbeat.

WHY THIS REPLACES A GLOBAL ARM FILE
  The predecessor arm was ONE machine-global file. While it existed, the
  heartbeat hook injected a run's mailbox rows into EVERY subagent on the
  machine -- including subagents of unrelated sessions that never joined the
  swarm. That is machine-global contamination: arming a run for three
  participants leaked its board into every bystander on the box.

  This module makes arming PER-PARTICIPANT. An armed run is a directory
  <state>/swarm-arm/<runid>/ whose participants/ subdir holds one file per
  ENROLLED agent_id. The heartbeat injects a run's rows to an agent ONLY
  when that agent_id is enrolled in that run. An armed run with an empty roster
  reaches nobody -- bystanders are silent BY DEFAULT, which is the property the
  old global file could not provide. There is no single switch that turns
  injection on for every subagent at once.

ENROLLMENT -- WHY IT IS SELF-SERVICE, NOT PARENT-DECLARED
  A parent cannot pre-populate the roster: a subagent's agent_id is assigned at
  SPAWN, so the parent does not know it when it writes the brief. The value only
  becomes visible in the hook payload the heartbeat reads. So enrollment is done
  BY THE HEARTBEAT, on a participant's OWN beat, keyed on the agent_id in that
  payload. The heartbeat enrolls an agent into run R the first time it observes
  that agent OPT IN to R -- i.e. issue a swarm helper command naming R's runid
  (swarm_mailbox.py <runid> ... or swarm_arm.py enroll <runid>). A bystander in
  an unrelated session never issues a command naming R's runid, so it never
  enrolls and never receives injection. The parent controls who participates the
  only way it can: by telling participants -- and no one else -- to work run R
  (put `swarm_arm.py enroll <runid>` as the first line of each participant brief
  to enroll on the first beat without waiting for a poll).

  This is a DESIGNED HANDSHAKE on an exact, unique runid token, NOT a fuzzy
  grammar over incidental output: the signal is a deliberately-issued command
  carrying a runid the parent planted in participant briefs.

STATE DIR
  Every function takes state_dir (default $COMMS_STATE_DIR, then the
  pre-extraction $SWARM_ARM_STATE_DIR, then ~/.comms/state) so the heartbeat
  can pass its own state dir and tests can isolate every write to a temp dir.
  One implementation of the roster lives here; the heartbeat IMPORTS it rather
  than re-deriving it, so the two can never drift.
"""

import datetime
import json
import os
import shutil
import sys


def _default_state_dir():
    return (
        os.environ.get("COMMS_STATE_DIR")
        or os.environ.get("SWARM_ARM_STATE_DIR")  # migration compatibility: pre-extraction env name
        or os.path.expanduser("~/.comms/state")
    )


def _safe(s):
    """Filesystem-safe token. Same alphabet the heartbeat uses for agent_ids, so
    a name sanitized here matches the name it looks up there."""
    s = "".join(c for c in (s or "") if c.isalnum() or c in "-_.")
    return s or "unknown"


def _arm_root(state_dir=None):
    return os.path.join(state_dir or _default_state_dir(), "swarm-arm")


def _run_dir(runid, state_dir=None):
    return os.path.join(_arm_root(state_dir), _safe(runid))


def _participants_dir(runid, state_dir=None):
    return os.path.join(_run_dir(runid, state_dir), "participants")


def _meta_path(runid, state_dir=None):
    return os.path.join(_run_dir(runid, state_dir), "meta.json")


def arm(runid, topic=None, state_dir=None):
    """Arm run `runid` (optionally scoped to `topic`). Idempotent. Roster starts
    EMPTY, so arming alone injects to nobody until participants enroll."""
    d = _run_dir(runid, state_dir)
    os.makedirs(os.path.join(d, "participants"), exist_ok=True)
    meta = {
        "runid": runid,
        "topic": (topic or None),
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    with open(_meta_path(runid, state_dir), "w") as fh:
        json.dump(meta, fh)
    return d


def disarm(runid, state_dir=None):
    """Tear a run down: remove its arm dir, roster and all. Idempotent."""
    shutil.rmtree(_run_dir(runid, state_dir), ignore_errors=True)


def is_armed(runid, state_dir=None):
    return os.path.isfile(_meta_path(runid, state_dir))


def armed_runs(state_dir=None):
    """Every currently-armed runid (a run dir carrying a meta.json)."""
    root = _arm_root(state_dir)
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return [n for n in names if os.path.isfile(os.path.join(root, n, "meta.json"))]


def meta(runid, state_dir=None):
    try:
        with open(_meta_path(runid, state_dir)) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _as_topics(topics):
    """Normalize a comma string OR a list into a clean topic list."""
    if not topics:
        return []
    if isinstance(topics, str):
        topics = topics.split(",")
    return [t.strip() for t in topics if t and t.strip()]


IDENTITY_FIELDS = ("model", "project", "area")


def enroll(runid, agent_id, topics=None, seat=None, state_dir=None,
           model=None, project=None, area=None):
    """Record agent_id as a participant of runid. Idempotent, write-once so a
    later empty subscription never clobbers an earlier one. Returns True on
    enroll, False if the run is not armed (you cannot join a run that does not
    exist).

    `topics` is this participant's SUBSCRIPTION -- a topic set (comma string or
    list). Empty => subscribe to every topic. This is the per-agent source for
    the subscription-set filter the heartbeat header documents; it subsumes the
    single-topic case (a one-element set) and the comma-set case. `seat`
    (optional) lets the reader also receive its own unicast topic "@<seat>".

    `model`/`project`/`area` are IDENTITY metadata: free-text prose describing
    who this seat is for a human reading a dashboard (e.g. "Kimi K3" working
    "agent-os" in "hooks/"). Display-only by design -- identity never gates
    routing, delivery, or enrollment, so it is an open vocabulary, not a
    closed one. Written only when declared, so a roster row enrolled without
    identity stays byte-identical to the pre-identity format. New kwargs sit
    AFTER state_dir so every existing positional caller is untouched.
    """
    if not is_armed(runid, state_dir):
        return False
    pdir = _participants_dir(runid, state_dir)
    os.makedirs(pdir, exist_ok=True)
    path = os.path.join(pdir, _safe(agent_id))
    if not os.path.exists(path):
        data = {"topics": _as_topics(topics), "seat": (seat or None)}
        for key, val in zip(IDENTITY_FIELDS, (model, project, area)):
            if val:
                data[key] = val
        with open(path, "w") as fh:
            json.dump(data, fh)
    return True


def _locks_dir(runid, state_dir=None):
    """Sidecar lock dir for a run, a SIBLING of participants/ rather than a
    file inside it.

    participants/ has exactly one invariant that three readers depend on --
    every entry is one enrolled agent_id (is_participant does a bare
    os.path.exists on a name there; seat_identities, seat_collisions and
    `status` all iterate the dir). Dropping "<agent>.lock" beside the roster
    row would put a non-participant into that listing: the JSON readers would
    skip it silently, but `status` would print it as a participant. A separate
    dir keeps the roster single-purpose, which is the cheaper of the two costs
    (one extra mkdir per add vs. a lie in the operator-facing listing).
    """
    return os.path.join(_run_dir(runid, state_dir), "locks")


def add_topics(runid, agent_id, topics, state_dir=None):
    """Union `topics` into an ALREADY-ENROLLED agent's subscription. Returns
    (topics_list, changed): the resulting list (existing order preserved, new
    topics appended) and whether THIS call is the one that wrote it.

    `changed` IS DECIDED UNDER THE LOCK, and that is the whole point of
    returning it. A caller cannot compute it by reading the list before and
    after its own call: between those two reads another beat can add the same
    key, so both callers see it absent beforehand, both see it present
    afterwards, and both report an enrolment that happened once. The only
    place that question has a single answer is inside the critical section, so
    the answer is returned from there rather than reconstructed outside it.

    WHY THIS IS NOT A FLAG ON enroll(). enroll() is write-once by design (the
    `if not os.path.exists(path)` gate above), so a second enroll carrying an
    empty subscription cannot clobber a real one. Growing a subscription is
    the opposite operation -- read, merge, write -- and folding it into enroll
    would delete the write-once property that gate exists to provide. Two
    operations, two functions.

    THE CALLER THIS EXISTS FOR is the heartbeat's doc-enrol leg: when an agent
    Writes or Edits a file, that file's thread key (swarm_mailbox.thread_key)
    becomes one of its subscribed topics, so sibling rows ABOUT that document
    reach the agent that is working on it. See swarm-heartbeat.sh's header.

    NOT ENROLLING IS THE POINT. A missing participant file returns ([], False)
    and creates NOTHING. An agent that never opted in must not become a
    participant by the side effect of writing a file -- that would re-create,
    through the back door, exactly the machine-global contamination this
    module's per-participant roster removed. Same for an unarmed run, an
    unreadable file, or a malformed one: ([], False) and no write.

    A SUBSCRIBE-ALL PARTICIPANT IS NEVER NARROWED, and this is the subtlest
    rule here. An EMPTY topics list means "every topic" everywhere in this
    module (see enroll and participant_sub). So adding one topic to an empty
    list would not widen that agent's reach by one document -- it would COLLAPSE
    it from the whole board down to that single document, hiding every other
    row from an agent that was receiving all of them. The guard lives here,
    not in the caller, because this module is where "empty means all" is
    DEFINED; a caller that had to remember it would eventually not. Such a
    participant gets ([], False) and no write, the same shape as "nothing to
    add to" -- both mean the subscription is unchanged and there is nothing to
    do about it.

    NO-OP MEANS NO WRITE. When every topic is already present the file is not
    touched at all -- not rewritten with identical bytes. This runs on every
    Write/Edit beat of every participant, and an agent editing one file in a
    loop would otherwise churn its roster row (and its mtime) forever.

    CONCURRENCY. An exclusive fcntl.flock on a sidecar lock file (see
    _locks_dir) is held across the whole read-modify-write, so two beats
    racing on one roster row cannot lose an update -- without it each reads
    the pre-write list and os.replace's its own single-topic result over the
    other's. The write itself is tmp + flush + fsync + os.replace, so a READER
    (the heartbeat's own participant_sub, mid-beat) sees either the whole old
    file or the whole new one, never a partial one; readers therefore need no
    lock of their own.

    A LOCK WE CANNOT TAKE MEANS WE DO NOT WRITE. Failure to create the lock
    dir, open the lock file, or acquire the flock returns
    (current_topics, False) plus ONE stderr line, and writes nothing. Falling
    through to an unlocked read-modify-write is not a degraded write, it is a
    lost-update generator -- exactly the failure the lock exists to prevent,
    reintroduced at the moment the safeguard breaks. Refusing costs one
    un-grown subscription and the next Write/Edit beat retries; proceeding
    costs another writer's topics, silently and permanently.

    UNBOUNDED BY DESIGN, FOR NOW: a long session subscribes one topic per file
    it touches. Accepted for v1 (the list is read once per beat and holds
    short strings); the revisit condition is a roster row large enough to show
    up in beat latency, at which point this wants an LRU cap, not a smaller
    key.

    in: runid; agent_id (raw, sanitized here the same way enroll does);
      topics (comma string or list, same normalization as everywhere else);
      state_dir (default $COMMS_STATE_DIR chain).
    out: (topics_list, changed). changed is True on exactly the calls that
      rewrote the file. ([], False) whenever there is nothing to add to.
    side effects: at most one atomic rewrite of the participant file, plus the
      lock dir/file, plus one stderr line if the lock could not be taken.
      Never creates a participant.
    """
    import fcntl

    new = _as_topics(topics)
    pdir = _participants_dir(runid, state_dir)
    path = os.path.join(pdir, _safe(agent_id))
    if not os.path.exists(path):
        return ([], False)

    lock_fh = None
    try:
        os.makedirs(_locks_dir(runid, state_dir), exist_ok=True)
        lock_fh = open(
            os.path.join(_locks_dir(runid, state_dir), _safe(agent_id) + ".lock"), "a"
        )
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
    except Exception as exc:
        # Broad on purpose: however the lock failed, the answer is the same --
        # do not write. See "A LOCK WE CANNOT TAKE" above.
        if lock_fh is not None:
            try:
                lock_fh.close()
            except OSError:
                pass
        sys.stderr.write(
            "swarm_arm: add_topics could not lock %s (%s); subscription NOT grown\n"
            % (path, exc)
        )
        return (own_topics(runid, agent_id, state_dir=state_dir), False)

    try:
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return ([], False)
        if not isinstance(data, dict):
            return ([], False)
        current = _as_topics(data.get("topics"))
        if not current:
            # subscribe-all: adding would NARROW it, see above
            return ([], False)
        merged = list(current)
        for t in new:
            if t not in merged:
                merged.append(t)
        if merged == current:
            return (current, False)  # NO-OP MEANS NO WRITE

        data["topics"] = merged
        tmp = path + ".tmp." + str(os.getpid())
        try:
            with open(tmp, "w") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return (current, False)  # the list on disk is still the old one
        return (merged, True)
    finally:
        if lock_fh is not None:
            try:
                lock_fh.close()  # releases the flock
            except OSError:
                pass


def own_topics(runid, agent_id, state_dir=None):
    """The topics this participant DECLARED FOR ITSELF, with no run-level
    fallback. [] means either "not enrolled" or "subscribe-all".

    This is deliberately NOT participant_sub. participant_sub answers a
    DELIVERY question -- "which topics should this agent receive" -- and to
    answer it, it substitutes the run's default subscription from meta.json
    when the agent declared none. A MUTATOR must not see that substitution:
    add_topics would union the doc key into a borrowed default and write the
    result back as the agent's own list, freezing a run-level default into a
    per-agent one and losing every later change to meta. Two questions, two
    functions.

    The heartbeat's doc-enrol leg uses this to decide whether a topic is
    genuinely new before calling add_topics, so a re-Write of the same file
    emits no telemetry.
    """
    try:
        with open(
            os.path.join(_participants_dir(runid, state_dir), _safe(agent_id))
        ) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    return _as_topics(data.get("topics"))


def seat_identities(runid, state_dir=None):
    """Map seat -> identity dict ({model, project, area}, declared keys only)
    for every enrolled participant that declared a seat and any identity field.

    This is the read side of the identity metadata above: a consumer (the
    Discord mirror) joins mailbox rows to it BY SEAT NAME at format time.
    Participants without a seat or without identity are simply absent, so a
    pre-identity roster yields {} and every consumer falls back to its
    identity-free rendering.

    SEAT UNIQUENESS IS A CONVENTION, NOT AN INVARIANT (issue #42). Nothing
    stops two agents enrolling on one seat name -- enroll() deliberately
    accepts it, see below -- so this function states its RESOLUTION RULE
    instead of assuming the question cannot arise: participants are read in
    sorted filename order and the FIRST occurrence of a seat wins. The
    winner is therefore deterministic, never arbitrary.

    THE CONSEQUENCE, NAMED -- AND IT DEPENDS ON WHICH READ PATH: when two
    agents share seat "alpha", a unicast to "@alpha" reaches both on any
    STATELESS read (read_for/read_siblings), and both agents' rows render
    under the first one's identity, so a human reading the board sees one
    agent where there are two.

    That much is accepted, not fixed here: rejecting a duplicate seat in
    enroll() would kill session start (enroll runs at SessionStart and
    returns a bool) and would make a legitimate re-enroll after a crash --
    same seat, new agent_id -- run UNENROLLED, i.e. invisible, which is
    strictly worse. seat_collisions() below is the detector that makes the
    condition visible instead. That ruling stands.

    CORRECTION (this docstring used to say "a duplication, not a drop -- both
    receive it"; that is FALSE and was already false when written). It holds
    only where the cursor is keyed by agent_id. Two cursors exist:

      * heartbeat injection -- <state>/swarm-cursor/<runid>/<agent_id>.
        Keyed by AGENT_ID, so colliding seats keep INDEPENDENT positions and
        both really do receive the row. Duplication, no drop.
      * `comms read` delivery -- swarm_mailbox._read_cursor_path, i.e.
        <state>/read-cursor/<runid>/<seat>.<view>.json. Keyed by SEAT ALONE,
        with no agent_id in it. Two agents on one seat therefore SHARE one
        cursor: whichever reads first calls advance() and the second gets
        ZERO rows. That is a silent DROP, not a duplication.

    The seat-keyed delivery cursor landed in d0f61f8 (#33) about two hours
    before this docstring was written in 190c22e (#40 D5, #42), so the
    "never a drop" claim never covered the whole system. Reproduced by
    tests/test_swarm_arm.py::test_two_agents_on_one_seat_share_a_delivery_cursor.

    NOT FIXED HERE, ON PURPOSE: re-keying that cursor to include agent_id is
    a change to lib/swarm_mailbox.py's cursor path, which this seat does not
    own -- it is a cross-seat dependency on the write path, not a swarm_arm
    change. Recorded here so the next reader does not re-derive it.
    """
    pdir = _participants_dir(runid, state_dir)
    try:
        names = sorted(os.listdir(pdir))
    except OSError:
        return {}
    out = {}
    for name in names:
        try:
            with open(os.path.join(pdir, name)) as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        seat = data.get("seat")
        ident = {k: data[k] for k in IDENTITY_FIELDS if data.get(k)}
        if seat and ident and seat not in out:
            out[seat] = ident
    return out


def seat_collisions(runid, state_dir=None):
    """{seat: [agent_id, ...]} for every seat claimed by MORE THAN ONE
    enrolled agent. Empty dict when the roster is clean (the normal case).

    WHY DETECT AND NOT REJECT (issue #42, resolved in issue #40's D5): see
    seat_identities' docstring above. Enforcement belongs nowhere on the
    enroll path; visibility is what was actually missing. The Discord mirror
    calls this once per pass and writes ONE stderr line when it is non-empty,
    which turns a silent mis-render into a thing a human can read.

    agent_ids come back in SORTED FILENAME ORDER -- the same order
    seat_identities() resolves in -- so the first id in a list is the one
    whose identity currently wins the render. A detector that reported a
    different order than the resolver uses would name the wrong winner.

    Reports on the SEAT, not on identity: a participant that declared no
    model/project/area is absent from seat_identities but is still a full
    claimant of its seat here. Two unidentified agents on one seat is the
    same bug as two identified ones.

    in: runid; state_dir (default $COMMS_STATE_DIR chain).
    out: dict, possibly empty. Seatless participants are ignored -- enrolling
      without a seat is normal for a read-only participant, and two of them
      are not a collision on the seat named None.
    side effects: none -- a pure read of the participants dir. An unarmed run,
      an unreadable dir, or a malformed participant file yields {} or is
      skipped: a detector that raised would take down the pass it is meant to
      annotate.
    """
    pdir = _participants_dir(runid, state_dir)
    try:
        names = sorted(os.listdir(pdir))
    except OSError:
        return {}
    claims = {}
    for name in names:
        try:
            with open(os.path.join(pdir, name)) as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        seat = data.get("seat")
        if not seat:
            continue
        claims.setdefault(seat, []).append(name)
    return {seat: ids for seat, ids in claims.items() if len(ids) > 1}


def is_participant(runid, agent_id, state_dir=None):
    return os.path.exists(
        os.path.join(_participants_dir(runid, state_dir), _safe(agent_id))
    )


def participant_sub(runid, agent_id, state_dir=None):
    """This agent's (topics_list, seat). topics_list empty => subscribe-all.
    Falls back to the run-level default subscription from meta when the agent
    enrolled without its own topic set."""
    try:
        with open(
            os.path.join(_participants_dir(runid, state_dir), _safe(agent_id))
        ) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return ([], None)
    topics = _as_topics(data.get("topics"))
    seat = data.get("seat")
    if not topics:
        topics = _as_topics(meta(runid, state_dir).get("topic"))
    return (topics, seat)


def enroll_signal(payload, runid):
    """True iff this hook payload is an opt-in to run `runid`.

    The one place the enrollment handshake is defined, so the heartbeat and any
    future caller agree on it. A Bash command that (a) names this exact runid and
    (b) invokes a swarm helper is the participant deliberately working run R. A
    bystander's commands never name R's runid, so this is False for them.
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("tool_name") != "Bash":
        return False
    cmd = ((payload.get("tool_input") or {}).get("command")) or ""
    if not runid or runid not in cmd:
        return False
    # Mailbox and claims commands are participant work; any of them opts in.
    if ("swarm_mailbox" in cmd) or ("swarm_claims" in cmd):
        return True
    # swarm_arm commands opt in ONLY as an enroll invocation. Measured live
    # (wave swarmw-0821a, 2026-08-21): the bare "swarm_arm" match made the
    # PARENT's own `arm` and `status` commands an opt-in, so the orchestrator
    # self-enrolled subscribe-all and received the whole board (32 rows).
    # arm/disarm/status are run-ADMINISTRATION, not participation.
    return ("swarm_arm" in cmd) and ("enroll" in cmd)


def sub_from_command(payload):
    """Extract (topics, seat) that the enroll command itself declared, so the
    heartbeat can enroll the agent WITH its subscription instead of without one.

    Before this existed the self-service path enrolled every agent with empty
    topics (= subscribe-all): participant_sub honored per-agent topic sets, but
    the only enrollment path production uses never populated them, so the
    per-agent injection filter was reachable in tests and unreached in life.
    The marker command already carries the declaration -- `swarm_arm.py enroll
    <runid> --topics a,b --seat alpha` -- this just reads it.

    Tokenizes with shlex (falling back to whitespace on unbalanced quotes) and
    takes the value after the LAST `--topics`/`--seat`, so a compound command
    resolves to the enroll invocation's own flags. Returns ([], None) when the
    command declares nothing -- enroll then behaves exactly as before.
    """
    import shlex

    cmd = (((payload or {}).get("tool_input") or {}).get("command")) or ""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = cmd.split()
    topics, seat = [], None
    for i, t in enumerate(toks):
        if t == "--topics" and i + 1 < len(toks):
            topics = _as_topics(toks[i + 1])
        elif t == "--seat" and i + 1 < len(toks):
            seat = toks[i + 1]
    return (topics, seat)


_FLAGS = ("--topic", "--topics", "--agent-id", "--seat", "--model", "--project", "--area")


def _usage():
    sys.stderr.write(
        "usage: swarm_arm.py arm <runid> [--topic <set>]\n"
        "       swarm_arm.py disarm <runid>\n"
        "       swarm_arm.py enroll <runid> [--agent-id <id>] [--topics <set>] [--seat <name>]\n"
        "                    [--model <name>] [--project <repo>] [--area <path>]\n"
        "       swarm_arm.py is-participant <runid> <agent_id>\n"
        "       swarm_arm.py status [<runid>]   (adds \"seat_collisions\" when two agents share a seat)\n"
    )


def main(argv):
    if len(argv) < 2:
        _usage()
        return 2
    cmd = argv[1]
    rest = argv[2:]

    def opt(flag):
        if flag in rest:
            i = rest.index(flag)
            if i + 1 < len(rest):
                return rest[i + 1]
        return None

    pos = [a for i, a in enumerate(rest) if not a.startswith("--") and (i == 0 or rest[i - 1] not in _FLAGS)]

    if cmd == "arm":
        if not pos:
            _usage()
            return 2
        print(arm(pos[0], topic=opt("--topic")))
        return 0
    if cmd == "disarm":
        if not pos:
            _usage()
            return 2
        disarm(pos[0])
        return 0
    if cmd == "enroll":
        if not pos:
            _usage()
            return 2
        runid = pos[0]
        agent_id = opt("--agent-id")
        if agent_id:
            # Direct enroll (test / caller that knows the id). Real subagents run
            # this WITHOUT --agent-id: the command is then only the observable
            # opt-in marker, and the heartbeat does the roster write with the
            # agent_id from the live payload.
            ok = enroll(runid, agent_id, topics=opt("--topics"), seat=opt("--seat"),
                        model=opt("--model"), project=opt("--project"), area=opt("--area"))
            print("enrolled" if ok else "not-armed")
            return 0 if ok else 1
        # Marker-only invocation.
        if is_armed(runid):
            print("enrollment signalled for %s; the heartbeat will enroll this agent on this beat" % runid)
            return 0
        print("not-armed: %s" % runid)
        return 1
    if cmd == "is-participant":
        if len(pos) < 2:
            _usage()
            return 2
        return 0 if is_participant(pos[0], pos[1]) else 1
    if cmd == "status":
        if pos:
            m = meta(pos[0])
            parts = sorted(os.listdir(_participants_dir(pos[0]))) if is_armed(pos[0]) else []
            out = {"runid": pos[0], "armed": is_armed(pos[0]), "meta": m, "participants": parts}
            # Seat collisions are surfaced HERE, not only in the Discord mirror.
            # Before this, seat_collisions() had exactly one consumer -- an
            # optional adapter -- so on a board with no Discord webhook a
            # duplicate seat was detectable in principle and invisible in
            # practice. status is the one surface every operator already runs.
            # ADDITIVE AND ABSENT WHEN CLEAN: the key appears only when the
            # roster actually collides, so a clean run's JSON stays
            # byte-identical to every previous version's and an older hub
            # parsing this output sees no new field. Detection, not
            # enforcement -- exit code is still 0, nothing is blocked.
            collisions = seat_collisions(pos[0])
            if collisions:
                out["seat_collisions"] = collisions
            print(json.dumps(out))
        else:
            print(json.dumps({"armed_runs": armed_runs()}))
        return 0
    _usage()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
