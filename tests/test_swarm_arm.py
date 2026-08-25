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
import sys

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
