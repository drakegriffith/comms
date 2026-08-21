"""Tests for swarm_claims: run-scoped arbitration, loud refusals, structural expiry.

Every test isolates state to a tmp dir via the state_dir parameter -- nothing
here may touch the real state dir (a self-test writing production state is the
half-isolation failure this stack's history documents).
"""

import subprocess
import sys
import os

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"),
)
import swarm_arm
import swarm_claims

HELPER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "lib",
    "swarm_claims.py",
)


@pytest.fixture
def state(tmp_path):
    d = str(tmp_path)
    swarm_arm.arm("r1", state_dir=d)
    return d


def test_claim_on_unarmed_run_refuses(tmp_path):
    with pytest.raises(LookupError):
        swarm_claims.claim("ghost", "A", ["x.py"], state_dir=str(tmp_path))


def test_first_writer_wins_and_loser_is_told_the_holder(state):
    assert swarm_claims.claim("r1", "A", ["src/x.py"], state_dir=state) == [
        ("src/x.py", "ok", "A")
    ]
    assert swarm_claims.claim("r1", "B", ["src/x.py"], state_dir=state) == [
        ("src/x.py", "held", "A")
    ]


def test_reclaim_by_self_is_idempotent(state):
    swarm_claims.claim("r1", "A", ["x"], state_dir=state)
    assert swarm_claims.claim("r1", "A", ["x"], state_dir=state) == [
        ("x", "ok-self", "A")
    ]


def test_release_own_claim_makes_path_claimable_again(state):
    swarm_claims.claim("r1", "A", ["x"], state_dir=state)
    assert swarm_claims.release("r1", "A", ["x"], state_dir=state) == [
        ("x", "released", "A")
    ]
    assert swarm_claims.claim("r1", "B", ["x"], state_dir=state) == [("x", "ok", "B")]


def test_release_of_peers_claim_is_refused_naming_the_holder(state):
    swarm_claims.claim("r1", "A", ["x"], state_dir=state)
    assert swarm_claims.release("r1", "B", ["x"], state_dir=state) == [
        ("x", "refused", "A")
    ]
    # and the claim survives the attempted stomp
    assert swarm_claims.holder("r1", "x", state_dir=state) == "A"


def test_release_of_unheld_path_reports_not_held(state):
    assert swarm_claims.release("r1", "A", ["never"], state_dir=state) == [
        ("never", "not-held", None)
    ]


def test_reap_sweeps_only_the_named_owner(state):
    swarm_claims.claim("r1", "A", ["a1", "a2"], state_dir=state)
    swarm_claims.claim("r1", "B", ["b1"], state_dir=state)
    assert swarm_claims.reap("r1", "A", state_dir=state) == ["a1", "a2"]
    assert swarm_claims.who("r1", state_dir=state) == [("B", "b1")]


def test_reap_of_owner_with_nothing_returns_empty(state):
    assert swarm_claims.reap("r1", "nobody", state_dir=state) == []


def test_runs_do_not_share_claims(state):
    # The defect this module exists for: a predecessor's claims outlived their wave.
    swarm_arm.arm("r2", state_dir=state)
    swarm_claims.claim("r1", "A", ["shared/path.py"], state_dir=state)
    assert swarm_claims.claim("r2", "B", ["shared/path.py"], state_dir=state) == [
        ("shared/path.py", "ok", "B")
    ]


def test_disarm_makes_claims_structurally_unreachable(state):
    swarm_claims.claim("r1", "A", ["x"], state_dir=state)
    swarm_arm.disarm("r1", state_dir=state)
    assert swarm_claims.who("r1", state_dir=state) == []
    assert swarm_claims.holder("r1", "x", state_dir=state) is None
    # a fresh arm of the same runid starts with an empty board
    swarm_arm.arm("r1", state_dir=state)
    assert swarm_claims.claim("r1", "B", ["x"], state_dir=state) == [("x", "ok", "B")]


def test_percent_in_path_does_not_collide_with_slash(state):
    # A tr '/' '%' encoding made these two the same key; quote() must not.
    swarm_claims.claim("r1", "A", ["a/b"], state_dir=state)
    assert swarm_claims.claim("r1", "B", ["a%b"], state_dir=state) == [
        ("a%b", "ok", "B")
    ]
    assert sorted(swarm_claims.who("r1", state_dir=state)) == [
        ("A", "a/b"),
        ("B", "a%b"),
    ]


def test_mixed_claim_batch_reports_per_path(state):
    swarm_claims.claim("r1", "A", ["taken"], state_dir=state)
    rows = swarm_claims.claim("r1", "B", ["taken", "free"], state_dir=state)
    assert rows == [("taken", "held", "A"), ("free", "ok", "B")]


def _cli(args, state_dir):
    env = dict(os.environ, COMMS_STATE_DIR=state_dir)
    return subprocess.run(
        [sys.executable, HELPER] + args, capture_output=True, text=True, env=env
    )


def test_cli_exit_codes(state):
    assert _cli(["claim", "r1", "A", "x"], state).returncode == 0
    r = _cli(["claim", "r1", "B", "x"], state)
    assert r.returncode == 1 and "HELD BY A" in r.stdout
    assert _cli(["claim", "ghost", "A", "x"], state).returncode == 3
    assert _cli(["release", "r1", "B", "x"], state).returncode == 1
    assert _cli(["who", "r1", "x"], state).returncode == 0
    assert _cli(["who", "r1", "nope"], state).returncode == 1
    assert _cli(["reap", "r1", "A"], state).returncode == 0
    assert _cli(["reap", "r1", "A"], state).returncode == 1  # nothing left: not a pass
    assert _cli(["bogus", "r1"], state).returncode == 2


def test_sub_from_command_reads_the_enroll_declaration():
    # Heartbeat enrollment path: the marker command carries the subscription.
    p = {"tool_input": {"command": "python3 lib/swarm_arm.py enroll r9 --topics proj-x,broadcast --seat alpha"}}
    assert swarm_arm.sub_from_command(p) == (["proj-x", "broadcast"], "alpha")
    # No declaration => exactly the old behavior (subscribe-all, no seat).
    p2 = {"tool_input": {"command": "python3 swarm_arm.py enroll r9"}}
    assert swarm_arm.sub_from_command(p2) == ([], None)
    # Unbalanced quote must not crash the hook's beat.
    p3 = {"tool_input": {"command": "swarm_arm.py enroll r9 --seat beta '"}}
    assert swarm_arm.sub_from_command(p3)[1] == "beta"


def test_heartbeat_enroll_records_declared_subscription(state):
    # End-to-end through the enroll() write: what participant_sub reads back is
    # what the command declared, so the injection filter is fed for real.
    p = {"tool_name": "Bash", "tool_input": {"command": "python3 swarm_arm.py enroll r1 --topics proj-x --seat gamma"}}
    assert swarm_arm.enroll_signal(p, "r1")
    t, s = swarm_arm.sub_from_command(p)
    swarm_arm.enroll("r1", "agent-123", topics=t, seat=s, state_dir=state)
    assert swarm_arm.participant_sub("r1", "agent-123", state_dir=state) == (["proj-x"], "gamma")
