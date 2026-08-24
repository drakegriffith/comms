"""Tests for the isolation fixtures in conftest.py (issue #13 part 1).

Two things earn direct tests here rather than trust-by-inspection:

1. The DEFAULT actually isolates -- a test/module that asks for nothing
   still lands under the per-test tmp_path root, never real /tmp.
2. A test/fixture that sets its OWN COMMS_ROOT (etc.) still wins -- the
   autouse default must not clobber a later, more specific override.

The leak sentinel itself gets a positive control: a throwaway test file that
deliberately writes into real /tmp is run via pytest's own `pytester`
fixture (a nested pytest invocation against a scratch directory, never this
repo's working tree -- two concurrent suite runs on one checkout must not
collide, and an untracked victim file must never be sweepable by `git add
-A`), loaded with a copy of this repo's actual tests/conftest.py so the
REAL sentinel logic is what's under test, not a reimplementation of it. The
positive control asserts that run FAILS with the sentinel's message.
Silence would mean the sentinel exists but never actually fires -- a gate
that never inspects anything is not a passing gate.
"""

import os
import shutil
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(os.path.dirname(_TESTS_DIR), "lib")
sys.path.insert(0, _LIB)

import swarm_mailbox as mb  # noqa: E402

# The leak-victim test below deliberately creates this real /tmp dir to
# prove the sentinel catches it. Its name starts with "isofix-" so
# conftest.py's _TEST_SHAPED_PREFIXES classifies it as a hard failure, not
# just a warning (see tests/conftest.py). Removed before AND after the
# run: a prior run killed mid-test (SIGKILL/Ctrl-C) can leave it behind,
# and a stale copy sitting here before we start would make the CHILD
# pytest's own sentinel see it in its "before" snapshot, count it as
# pre-existing, and pass -- which would make THIS test fail for the wrong
# reason ("sentinel did not fire") instead of the right one.
_LEAK_DIR = "/tmp/comms-isofix-meta-leak-sentinel-check"


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


def test_leak_sentinel_fires_on_a_real_tmp_write(pytester):
    """Positive control: prove the sentinel actually fails a test that
    leaks into real /tmp, by running one via pytester -- a throwaway
    directory, never this repo's tests/ tree -- loaded with a copy of our
    actual conftest.py so the real sentinel logic is what's exercised.
    """
    # Before: a leftover from a killed prior run must not make the CHILD
    # run's own "before" snapshot already contain it (see module docstring).
    shutil.rmtree(_LEAK_DIR, ignore_errors=True)
    try:
        real_conftest = os.path.join(_TESTS_DIR, "conftest.py")
        with open(real_conftest) as fh:
            pytester.makeconftest(fh.read())
        pytester.makepyfile(
            test_victim="""
            import os

            def test_this_leaks_into_real_tmp_on_purpose():
                os.makedirs(%r, exist_ok=True)
            """
            % _LEAK_DIR
        )
        result = pytester.runpytest_subprocess()
        combined = "\n".join(result.outlines)
        assert result.ret != 0, (
            "sentinel did not fail the leaking test; output:\n" + combined
        )
        assert "test isolation breach" in combined
        assert _LEAK_DIR in combined
    finally:
        # After: this test's OWN deliberate leak must not still be sitting
        # in real /tmp when we return -- our OUTER sentinel (wrapping this
        # very test, since it's autouse) would otherwise flag it too, and
        # a leftover here would poison the next run's "before" snapshot the
        # same way described above.
        shutil.rmtree(_LEAK_DIR, ignore_errors=True)
