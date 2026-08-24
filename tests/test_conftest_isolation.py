"""Tests for the isolation fixtures in conftest.py (issue #13 part 1).

Two things earn direct tests here rather than trust-by-inspection:

1. The DEFAULT actually isolates -- a test/module that asks for nothing
   still lands under the per-test tmp_path root, never real /tmp.
2. A test/fixture that sets its OWN COMMS_ROOT (etc.) still wins -- the
   autouse default must not clobber a later, more specific override.

The leak sentinel itself gets a positive control: a throwaway test file that
deliberately writes into real /tmp is run in a child pytest process (so
conftest.py applies to it exactly as it would to any file under tests/), and
this test asserts that run FAILS with the sentinel's message. Silence would
mean the sentinel exists but never actually fires -- a gate that never
inspects anything is not a passing gate.
"""

import os
import shutil
import subprocess
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(os.path.dirname(_TESTS_DIR), "lib")
sys.path.insert(0, _LIB)

import swarm_mailbox as mb  # noqa: E402


def test_default_env_points_at_tmp_path_not_real_tmp(tmp_path):
    """No test-local override: conftest's default still isolates."""
    assert os.environ["COMMS_ROOT"] == str(tmp_path / "comms-root")
    assert os.environ["COMMS_STATE_DIR"] == str(tmp_path / "comms-state")
    assert os.environ["CLAUDE_SWARM_ROOT"] == str(tmp_path / "comms-root")
    assert mb._root() != "/tmp"
    assert not mb._root().startswith("/tmp")


def test_mailbox_write_with_no_explicit_env_lands_under_tmp_root_not_tmp(tmp_path):
    """A mailbox write made by a test that sets NO env itself must still
    land under the isolated root, never real /tmp."""
    d = mb.init("isofix-default-write")
    assert d.startswith(str(tmp_path))
    assert not d.startswith("/tmp/comms-")
    row = mb.post("isofix-default-write", "seatA", "finding", "hello")
    assert row["seat"] == "seatA"
    # And the row is physically under the isolated root, not /tmp.
    assert not os.path.exists(os.path.join("/tmp", "comms-isofix-default-write"))


def test_local_override_still_wins_over_the_autouse_default(tmp_path, monkeypatch):
    """A test that sets its own COMMS_ROOT after the autouse fixture ran
    must see ITS value, not conftest's default."""
    custom = tmp_path / "my-own-root"
    custom.mkdir()
    monkeypatch.setenv("COMMS_ROOT", str(custom))
    assert mb._root() == str(custom)
    d = mb.init("isofix-override-write")
    assert d == os.path.join(str(custom), "comms-isofix-override-write")


def test_leak_sentinel_fires_on_a_real_tmp_write():
    """Positive control: prove the sentinel actually fails a test that
    leaks into real /tmp, by running one in a child pytest process against
    this repo's own tests/conftest.py.
    """
    victim_name = "test_isofix_meta_leak_victim.py"
    victim_path = os.path.join(_TESTS_DIR, victim_name)
    leak_dir = "/tmp/comms-isofix-meta-leak-sentinel-check"
    victim_src = (
        "import os\n"
        "\n"
        "def test_this_leaks_into_real_tmp_on_purpose():\n"
        "    os.makedirs(%r, exist_ok=True)\n" % leak_dir
    )
    assert not os.path.exists(victim_path), (
        "stale victim file from a previous run -- remove it manually"
    )
    with open(victim_path, "w") as fh:
        fh.write(victim_src)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", victim_name, "-q"],
            cwd=_TESTS_DIR,
            capture_output=True,
            text=True,
            timeout=60,
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, (
            "sentinel did not fail the leaking test; output:\n" + combined
        )
        assert "test isolation breach" in combined
        assert leak_dir in combined
    finally:
        os.remove(victim_path)
        shutil.rmtree(leak_dir, ignore_errors=True)
