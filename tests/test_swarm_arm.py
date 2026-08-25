"""Direct tests for swarm_arm.py's CLI entrypoint, main(argv).

Before this file, main() had zero direct tests: every other test exercised
the swarm_arm functions directly (arm(), enroll(), ...) or drove the CLI
through a real subprocess in the shell suites. That left main()'s own argv
parsing, flag extraction, and per-subcommand exit codes completely
unguarded -- the gate measured 12 of 12 mutants surviving there (CRAP 600).
Each test below calls swarm_arm.main([...]) directly and isolates state via
COMMS_STATE_DIR pointed at tmp_path (main()'s subcommand handlers call the
module functions with no explicit state_dir, so they resolve state purely
from the environment -- see _default_state_dir).
"""

import json
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"),
)
import swarm_arm  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SWARM_ARM_STATE_DIR", raising=False)
    yield tmp_path


# ---- usage / no subcommand -------------------------------------------------


def test_main_no_args_exits_2_with_usage(capsys):
    assert swarm_arm.main(["swarm_arm.py"]) == 2
    assert "usage" in capsys.readouterr().err


def test_main_unknown_subcommand_exits_2_with_usage(capsys):
    assert swarm_arm.main(["swarm_arm.py", "bogus"]) == 2
    assert "usage" in capsys.readouterr().err


# ---- arm --------------------------------------------------------------


def test_main_arm_no_runid_exits_2(capsys):
    assert swarm_arm.main(["swarm_arm.py", "arm"]) == 2
    assert "usage" in capsys.readouterr().err
    assert swarm_arm.armed_runs() == []


def test_main_arm_runid_arms_it():
    assert swarm_arm.main(["swarm_arm.py", "arm", "r1"]) == 0
    assert swarm_arm.is_armed("r1")


def test_main_arm_with_topic_is_recorded_in_meta():
    assert swarm_arm.main(["swarm_arm.py", "arm", "r1", "--topic", "projA"]) == 0
    assert swarm_arm.meta("r1")["topic"] == "projA"


def test_main_arm_trailing_flag_with_no_value_still_finds_the_runid():
    # "--topic" as the LAST token, no value following: the leading positional
    # (i == 0 in main()'s pos-extraction comprehension) must still be found
    # unconditionally at position 0 -- it must never fall back to inspecting
    # rest[-1] (the last token) to decide whether position 0 counts.
    rc = swarm_arm.main(["swarm_arm.py", "arm", "r1", "--topic"])
    assert rc == 0
    assert swarm_arm.is_armed("r1")
    assert swarm_arm.meta("r1").get("topic") is None


# ---- disarm -----------------------------------------------------------


def test_main_disarm_no_runid_exits_2(capsys):
    assert swarm_arm.main(["swarm_arm.py", "disarm"]) == 2
    assert "usage" in capsys.readouterr().err


def test_main_disarm_tears_down_an_armed_run():
    swarm_arm.arm("r1")
    assert swarm_arm.main(["swarm_arm.py", "disarm", "r1"]) == 0
    assert not swarm_arm.is_armed("r1")


# ---- enroll -------------------------------------------------------------


def test_main_enroll_no_runid_exits_2(capsys):
    assert swarm_arm.main(["swarm_arm.py", "enroll"]) == 2
    assert "usage" in capsys.readouterr().err


def test_main_enroll_with_agent_id_on_armed_run_enrolls_and_prints_enrolled(capsys):
    swarm_arm.arm("r1")
    rc = swarm_arm.main(
        ["swarm_arm.py", "enroll", "r1", "--agent-id", "a1", "--seat", "alpha"]
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == "enrolled"
    assert swarm_arm.is_participant("r1", "a1")


def test_main_enroll_with_agent_id_on_unarmed_run_fails_loudly(capsys):
    rc = swarm_arm.main(
        ["swarm_arm.py", "enroll", "never-armed", "--agent-id", "a1"]
    )
    assert rc == 1
    assert capsys.readouterr().out.strip() == "not-armed"
    assert not swarm_arm.is_participant("never-armed", "a1")


def test_main_enroll_marker_only_on_armed_run_signals_without_writing_a_participant(capsys):
    swarm_arm.arm("r1")
    rc = swarm_arm.main(["swarm_arm.py", "enroll", "r1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "r1" in out
    # marker-only invocation carries no --agent-id: the heartbeat, not main(),
    # does the roster write, so no participant exists yet.
    assert swarm_arm.armed_runs() == ["r1"]
    assert os.listdir(swarm_arm._participants_dir("r1")) == []


def test_main_enroll_marker_only_on_unarmed_run_fails_loudly(capsys):
    rc = swarm_arm.main(["swarm_arm.py", "enroll", "never-armed"])
    assert rc == 1
    assert capsys.readouterr().out.strip() == "not-armed: never-armed"


def test_main_enroll_trailing_flag_with_no_value_degrades_gracefully(capsys):
    # "--seat" as the LAST token: opt() must return None for it (the flag
    # exists but no value follows) rather than indexing one past the end of
    # rest -- the earlier flags (--agent-id a1) still resolve normally.
    swarm_arm.arm("r1")
    rc = swarm_arm.main(["swarm_arm.py", "enroll", "r1", "--agent-id", "a1", "--seat"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "enrolled"
    assert swarm_arm.is_participant("r1", "a1")
    assert swarm_arm.seat_identities("r1") == {}  # no seat was recorded


def test_main_enroll_passes_identity_flags_through(capsys):
    swarm_arm.arm("r1")
    rc = swarm_arm.main(
        [
            "swarm_arm.py", "enroll", "r1",
            "--agent-id", "a1", "--seat", "kimi1",
            "--model", "Kimi K3", "--project", "agent-os", "--area", "hooks/",
        ]
    )
    assert rc == 0
    assert swarm_arm.seat_identities("r1") == {
        "kimi1": {"model": "Kimi K3", "project": "agent-os", "area": "hooks/"}
    }


# ---- is-participant -----------------------------------------------------


def test_main_is_participant_too_few_args_exits_2(capsys):
    assert swarm_arm.main(["swarm_arm.py", "is-participant"]) == 2
    assert "usage" in capsys.readouterr().err
    assert swarm_arm.main(["swarm_arm.py", "is-participant", "r1"]) == 2


def test_main_is_participant_present_returns_0():
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "a1", seat="alpha")
    assert swarm_arm.main(["swarm_arm.py", "is-participant", "r1", "a1"]) == 0


def test_main_is_participant_absent_returns_1():
    swarm_arm.arm("r1")
    assert swarm_arm.main(["swarm_arm.py", "is-participant", "r1", "a1"]) == 1


# ---- status ---------------------------------------------------------------


def test_main_status_with_runid_reports_armed_meta_and_participants(capsys):
    swarm_arm.arm("r1", topic="projA")
    swarm_arm.enroll("r1", "a1", seat="alpha")
    assert swarm_arm.main(["swarm_arm.py", "status", "r1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["runid"] == "r1"
    assert payload["armed"] is True
    assert payload["meta"]["topic"] == "projA"
    assert payload["participants"] == ["a1"]


def test_main_status_with_unarmed_runid_reports_armed_false_empty_participants(capsys):
    assert swarm_arm.main(["swarm_arm.py", "status", "never-armed"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["armed"] is False
    assert payload["participants"] == []


def test_main_status_no_runid_lists_every_armed_run(capsys):
    swarm_arm.arm("r1")
    swarm_arm.arm("r2")
    assert swarm_arm.main(["swarm_arm.py", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert sorted(payload["armed_runs"]) == ["r1", "r2"]


# ---- enroll() directly: "you cannot join a run that does not exist" -------
# (mutation gate: 2 of 8 mutants survived here -- both collapsed the
# not-armed guard so enroll() returned True, or wrote a participant, for a
# run nobody armed)


def test_enroll_on_unarmed_run_returns_false_and_writes_nothing():
    ok = swarm_arm.enroll("never-armed-run", "agent-x", seat="x")
    assert ok is False
    pdir = swarm_arm._participants_dir("never-armed-run")
    assert not os.path.isdir(pdir) or os.listdir(pdir) == []


# ---- seat_identities(): a malformed participant file must not crash -------
# (mutation gate: 1 of 6 survived -- the isinstance(dict) guard disabled,
# so a non-dict participant file raised AttributeError instead of being
# skipped)


def test_seat_identities_skips_a_non_dict_participant_file_without_crashing():
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", seat="alpha", model="M1")
    pdir = swarm_arm._participants_dir("r1")
    with open(os.path.join(pdir, "agent-bad"), "w") as fh:
        fh.write(json.dumps(["not", "a", "dict"]))
    assert swarm_arm.seat_identities("r1") == {"alpha": {"model": "M1"}}


# ---- seat_collisions(): detect, do not reject (issue #40 D5, issue #42) ----
#
# Two agents enrolled on ONE seat name is a real failure -- "@alpha" fans out
# to both, and both render under one identity. enroll() deliberately does NOT
# reject it: enroll runs at SessionStart and returns a bool, so a raise there
# kills session start, and a legitimate re-enroll after a crash (same seat,
# new agent_id) would then run unenrolled, i.e. invisible. So this is a
# detector: it makes the condition visible instead of silent.


def test_seat_collisions_is_empty_when_every_seat_is_unique():
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", seat="alpha")
    swarm_arm.enroll("r1", "agent-b", seat="bravo")
    assert swarm_arm.seat_collisions("r1") == {}


def test_seat_collisions_names_the_seat_and_every_agent_id_on_it():
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", seat="alpha")
    swarm_arm.enroll("r1", "agent-b", seat="alpha")
    assert swarm_arm.seat_collisions("r1") == {"alpha": ["agent-a", "agent-b"]}


def test_seat_collisions_lists_agent_ids_in_the_resolution_order():
    # Sorted filename order, which is exactly the order seat_identities()
    # resolves in -- so the FIRST id in the list is the one whose identity
    # wins the render. A detector reporting a different order than the
    # resolver uses would name the wrong winner.
    swarm_arm.arm("r1")
    for aid in ("agent-c", "agent-a", "agent-b"):
        swarm_arm.enroll("r1", aid, seat="alpha")
    assert swarm_arm.seat_collisions("r1")["alpha"] == [
        "agent-a",
        "agent-b",
        "agent-c",
    ]


def test_seat_collisions_reports_only_the_colliding_seats():
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", seat="alpha")
    swarm_arm.enroll("r1", "agent-b", seat="alpha")
    swarm_arm.enroll("r1", "agent-c", seat="bravo")
    assert list(swarm_arm.seat_collisions("r1")) == ["alpha"]


def test_seat_collisions_ignores_participants_with_no_seat():
    # Enrolling without a seat is normal (a reader that only receives).
    # Two seatless agents are not a collision on the seat named None.
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a")
    swarm_arm.enroll("r1", "agent-b")
    assert swarm_arm.seat_collisions("r1") == {}


def test_seat_collisions_counts_a_seat_with_no_identity_fields():
    # seat_identities() skips a participant that declared no model/project/
    # area; a collision is about the SEAT, so this detector must not inherit
    # that filter -- two unidentified agents on one seat is the same bug.
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", seat="alpha")
    swarm_arm.enroll("r1", "agent-b", seat="alpha", model="M1")
    assert swarm_arm.seat_collisions("r1") == {"alpha": ["agent-a", "agent-b"]}


def test_seat_collisions_on_an_unarmed_run_is_empty_not_an_error():
    assert swarm_arm.seat_collisions("never-armed-run") == {}


def test_seat_collisions_skips_a_malformed_participant_file():
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", seat="alpha")
    swarm_arm.enroll("r1", "agent-b", seat="alpha")
    pdir = swarm_arm._participants_dir("r1")
    with open(os.path.join(pdir, "agent-bad"), "w") as fh:
        fh.write("{not json")
    with open(os.path.join(pdir, "agent-worse"), "w") as fh:
        fh.write(json.dumps(["not", "a", "dict"]))
    assert swarm_arm.seat_collisions("r1") == {"alpha": ["agent-a", "agent-b"]}


def test_seat_collisions_does_not_write_anything():
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", seat="alpha")
    pdir = swarm_arm._participants_dir("r1")
    before = sorted(os.listdir(pdir))
    swarm_arm.seat_collisions("r1")
    assert sorted(os.listdir(pdir)) == before


def test_enroll_still_accepts_a_duplicate_seat():
    # The contract this detector exists BECAUSE OF: enroll must keep
    # returning True. A re-enroll after a crash (same seat, new agent_id)
    # rejected here would run unenrolled -- invisible, which is worse than
    # the duplication the collision causes.
    swarm_arm.arm("r1")
    assert swarm_arm.enroll("r1", "agent-a", seat="alpha") is True
    assert swarm_arm.enroll("r1", "agent-b", seat="alpha") is True


# ---- add_topics -- the SEPARATE read-modify-write on the participant file ---
#
# enroll() is write-once (the `if not os.path.exists(path)` gate) so a later
# empty subscription can never clobber an earlier one. add_topics is the ONLY
# way a roster row's subscription grows after enrollment, and it is a distinct
# function rather than a flag on enroll precisely so that write-once property
# stays readable at the enroll call site.


def _participant_path(runid, agent_id):
    return os.path.join(swarm_arm._participants_dir(runid), swarm_arm._safe(agent_id))


def test_add_topics_adds_a_new_topic_and_returns_the_whole_list():
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", topics="projA", seat="alpha")
    assert swarm_arm.add_topics("r1", "agent-a", ["doc:comms/x.py"]) == (
        ["projA", "doc:comms/x.py"],
        True,
    )
    topics, seat = swarm_arm.participant_sub("r1", "agent-a")
    assert topics == ["projA", "doc:comms/x.py"]
    assert seat == "alpha"


def test_add_topics_accepts_a_comma_string_like_every_other_topic_arg():
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", topics="projA")
    assert swarm_arm.add_topics("r1", "agent-a", "doc:a,doc:b") == (
        ["projA", "doc:a", "doc:b"],
        True,
    )


def test_add_topics_dedupes_and_preserves_existing_order():
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", topics="projA,projB")
    assert swarm_arm.add_topics(
        "r1", "agent-a", ["projB", "doc:comms/x.py", "projA", "doc:comms/x.py"]
    ) == (["projA", "projB", "doc:comms/x.py"], True)


def test_add_topics_preserves_seat_and_identity_fields():
    swarm_arm.arm("r1")
    swarm_arm.enroll(
        "r1", "agent-a", topics="projA", seat="alpha",
        model="Kimi K3", project="agent-os", area="hooks/",
    )
    swarm_arm.add_topics("r1", "agent-a", ["doc:comms/x.py"])
    with open(_participant_path("r1", "agent-a")) as fh:
        data = json.load(fh)
    assert data["seat"] == "alpha"
    assert data["model"] == "Kimi K3"
    assert data["project"] == "agent-os"
    assert data["area"] == "hooks/"


def test_add_topics_with_nothing_new_does_not_write_the_file():
    # The hot path: the heartbeat calls this on EVERY Write/Edit, and an agent
    # editing one file repeatedly must not rewrite its roster row each beat.
    # mtime AND inode: os.replace swaps the inode, so a same-mtime different-
    # inode file would still be a write this test must catch.
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", topics="projA,projB")
    path = _participant_path("r1", "agent-a")
    st = os.stat(path)
    os.utime(path, (st.st_atime - 60, st.st_mtime - 60))
    before = os.stat(path)
    assert swarm_arm.add_topics("r1", "agent-a", ["projA", "projB"]) == (
        ["projA", "projB"],
        False,
    )
    after = os.stat(path)
    assert after.st_mtime == before.st_mtime
    assert after.st_ino == before.st_ino


def test_add_topics_with_an_empty_topic_list_is_a_no_op():
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", topics="projA")
    path = _participant_path("r1", "agent-a")
    st = os.stat(path)
    os.utime(path, (st.st_atime - 60, st.st_mtime - 60))
    before = os.stat(path)
    assert swarm_arm.add_topics("r1", "agent-a", []) == (["projA"], False)
    assert os.stat(path).st_ino == before.st_ino


def test_add_topics_on_an_unenrolled_agent_returns_empty_and_creates_no_file():
    # LOAD-BEARING: an agent that never opted in must not be silently enrolled
    # by the act of editing a file. Creating the row here would make every
    # bystander on the machine a participant the first time it wrote to disk,
    # which is the machine-global contamination this whole module removed.
    swarm_arm.arm("r1")
    assert swarm_arm.add_topics("r1", "ghost", ["doc:comms/x.py"]) == ([], False)
    assert swarm_arm.is_participant("r1", "ghost") is False
    assert os.listdir(swarm_arm._participants_dir("r1")) == []


def test_add_topics_on_an_unarmed_run_returns_empty():
    assert swarm_arm.add_topics("never-armed-run", "agent-a", ["doc:x"]) == ([], False)


def test_add_topics_on_a_malformed_participant_file_returns_empty():
    # A detector/mutator on the beat path must never raise: a corrupt roster
    # row degrades to "no subscription grew", not to a dead heartbeat.
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", topics="projA")
    with open(_participant_path("r1", "agent-a"), "w") as fh:
        fh.write("{not json")
    assert swarm_arm.add_topics("r1", "agent-a", ["doc:x"]) == ([], False)


def test_add_topics_leaves_no_temp_file_behind():
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", topics="projA")
    swarm_arm.add_topics("r1", "agent-a", ["doc:x"])
    assert sorted(os.listdir(swarm_arm._participants_dir("r1"))) == ["agent-a"]


_CONCURRENT_ADD = (
    "import sys, time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import swarm_arm\n"
    "time.sleep(max(0.0, float(sys.argv[2]) - time.time()))\n"
    "_topics, changed = swarm_arm.add_topics('r1', 'agent-a', [sys.argv[3]])\n"
    "sys.stdout.write('CHANGED' if changed else 'UNCHANGED')\n"
)


def test_add_topics_concurrent_writers_lose_no_update(isolated_state):
    # Eight processes read-modify-write ONE roster row at the same wall-clock
    # instant. Without the flock this is a lost-update race: each reads the
    # pre-write list and os.replace's its own single-topic result over the
    # others. The barrier (a shared start deadline) is what makes the race
    # reliable instead of accidentally serialized by interpreter startup.
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", topics="projA")
    lib_dir = os.path.dirname(os.path.abspath(swarm_arm.__file__))
    env = dict(os.environ)
    env["COMMS_STATE_DIR"] = str(isolated_state)
    env.pop("SWARM_ARM_STATE_DIR", None)
    start = time.time() + 1.5
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CONCURRENT_ADD, lib_dir, repr(start), "doc:d%d" % i],
            env=env,
        )
        for i in range(8)
    ]
    for p in procs:
        assert p.wait(timeout=60) == 0
    topics, _ = swarm_arm.participant_sub("r1", "agent-a")
    assert topics[0] == "projA"
    assert sorted(topics[1:]) == sorted("doc:d%d" % i for i in range(8))


def test_add_topics_refuses_to_narrow_a_subscribe_all_participant():
    # An EMPTY topics list means "every topic" (see enroll/participant_sub).
    # Adding one topic to it would not widen the agent's reach by a document,
    # it would COLLAPSE it to that one document and hide the rest of the
    # board. The guard lives in this module because this module is where
    # "empty means all" is defined.
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a")
    path = _participant_path("r1", "agent-a")
    st = os.stat(path)
    os.utime(path, (st.st_atime - 60, st.st_mtime - 60))
    before = os.stat(path)
    assert swarm_arm.add_topics("r1", "agent-a", ["doc:comms/x.py"]) == ([], False)
    assert os.stat(path).st_ino == before.st_ino
    assert swarm_arm.participant_sub("r1", "agent-a")[0] == []


def test_add_topics_does_not_freeze_the_run_default_into_a_per_agent_list():
    # participant_sub reports the RUN-level default for an agent that declared
    # no topics of its own. add_topics must not union into that borrowed
    # default and write it back -- the agent would stop tracking meta.json.
    swarm_arm.arm("r1", topic="projDefault")
    swarm_arm.enroll("r1", "agent-a")
    assert swarm_arm.participant_sub("r1", "agent-a")[0] == ["projDefault"]
    assert swarm_arm.add_topics("r1", "agent-a", ["doc:comms/x.py"]) == ([], False)
    with open(_participant_path("r1", "agent-a")) as fh:
        assert json.load(fh)["topics"] == []
    assert swarm_arm.participant_sub("r1", "agent-a")[0] == ["projDefault"]


# ---- own_topics -- the read side a MUTATOR needs ---------------------------


def test_own_topics_returns_only_what_the_participant_declared():
    swarm_arm.arm("r1", topic="projDefault")
    swarm_arm.enroll("r1", "agent-a", topics="projA,projB")
    assert swarm_arm.own_topics("r1", "agent-a") == ["projA", "projB"]


def test_own_topics_does_not_fall_back_to_the_run_default():
    # The one behavior that distinguishes it from participant_sub.
    swarm_arm.arm("r1", topic="projDefault")
    swarm_arm.enroll("r1", "agent-a")
    assert swarm_arm.participant_sub("r1", "agent-a")[0] == ["projDefault"]
    assert swarm_arm.own_topics("r1", "agent-a") == []


def test_own_topics_on_a_missing_or_malformed_participant_is_empty():
    swarm_arm.arm("r1")
    assert swarm_arm.own_topics("r1", "ghost") == []
    assert swarm_arm.own_topics("never-armed-run", "agent-a") == []
    swarm_arm.enroll("r1", "agent-a", topics="projA")
    with open(_participant_path("r1", "agent-a"), "w") as fh:
        fh.write("{not json")
    assert swarm_arm.own_topics("r1", "agent-a") == []


def test_own_topics_sees_what_add_topics_just_wrote():
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", topics="projA")
    swarm_arm.add_topics("r1", "agent-a", ["doc:comms/x.py"])
    assert swarm_arm.own_topics("r1", "agent-a") == ["projA", "doc:comms/x.py"]


def test_add_topics_reports_changed_true_only_when_it_wrote():
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", topics="projA")
    assert swarm_arm.add_topics("r1", "agent-a", ["doc:x"])[1] is True
    assert swarm_arm.add_topics("r1", "agent-a", ["doc:x"])[1] is False


def test_add_topics_concurrent_adders_of_the_same_key_report_one_winner(
    isolated_state,
):
    # The flag must be decided UNDER THE LOCK. A caller comparing the topic
    # list before and after its own call would race: two beats adding the same
    # key both see it absent beforehand and both report "changed", so a doc
    # key that was enrolled once gets counted twice. Exactly one writer wins.
    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", topics="projA")
    lib_dir = os.path.dirname(os.path.abspath(swarm_arm.__file__))
    env = dict(os.environ)
    env["COMMS_STATE_DIR"] = str(isolated_state)
    env.pop("SWARM_ARM_STATE_DIR", None)
    start = time.time() + 1.5
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CONCURRENT_ADD, lib_dir, repr(start), "doc:same"],
            env=env,
            stdout=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    verdicts = []
    for p in procs:
        out, _ = p.communicate(timeout=60)
        assert p.returncode == 0
        verdicts.append(out.decode())
    assert sorted(verdicts) == ["CHANGED", "UNCHANGED"]
    assert swarm_arm.own_topics("r1", "agent-a") == ["projA", "doc:same"]


def test_add_topics_that_cannot_lock_does_not_write_at_all(capsys, monkeypatch):
    # A read-modify-write that proceeds WITHOUT the lock is not a degraded
    # write, it is a lost-update generator: the whole reason the lock exists.
    # Refusing costs one un-grown subscription (the next Write/Edit beat
    # retries); proceeding costs another writer's topics, silently.
    import fcntl

    swarm_arm.arm("r1")
    swarm_arm.enroll("r1", "agent-a", topics="projA")
    path = _participant_path("r1", "agent-a")
    st = os.stat(path)
    os.utime(path, (st.st_atime - 60, st.st_mtime - 60))
    before = os.stat(path)

    def _refuse(*a, **kw):
        raise OSError("no locks available")

    monkeypatch.setattr(fcntl, "flock", _refuse)
    assert swarm_arm.add_topics("r1", "agent-a", ["doc:x"]) == (["projA"], False)
    after = os.stat(path)
    assert after.st_mtime == before.st_mtime
    assert after.st_ino == before.st_ino
    err = capsys.readouterr().err
    assert "add_topics" in err
    assert "no locks available" in err
    assert err.count("\n") == 1
