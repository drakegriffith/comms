"""Unconditional test isolation for the comms suite (issue #13).

WHY THIS EXISTS: swarm_mailbox._root() (and the state-dir equivalents in
swarm_arm/swarm_claims) fall back to real /tmp -- and $HOME/.comms/state --
whenever COMMS_ROOT / COMMS_STATE_DIR / CLAUDE_SWARM_ROOT are unset. Before
this file, isolation depended on EVERY test remembering to set those knobs
itself. That is a half-isolation failure: one test file that forgets, or a
mutation-gate mutant that makes _root() (or its env plumbing) ignore the
override mid-suite, silently redirects writes onto the LIVE board -- this is
exactly how test rows (fixture seat myproj-sess, "landed the fix", x-strings)
ended up in /tmp/comms-machine-ops.

Two independent layers, on purpose:

1. _isolated_comms_env (below) makes isolation the DEFAULT for every test,
   unconditionally, whether or not the test asks for it. It only supplies
   defaults -- a test that sets its own COMMS_ROOT/COMMS_STATE_DIR (via
   monkeypatch.setenv, a raw os.environ[...] assignment, or its own fixture)
   still wins, because that assignment happens later, during the test body
   or a fixture requested after this one.

2. _real_tmp_leak_sentinel is a WATCHDOG, not a guarantee: it reads the real
   filesystem at /tmp/comms-* directly, never through swarm_mailbox._root()
   or any env var. A mutant that guts layer 1 (or new code that never reads
   COMMS_ROOT at all) still gets caught here, because this check does not
   trust the thing it is checking.
"""

import glob
import os

import pytest

_ISOLATION_VARS = ("COMMS_ROOT", "COMMS_STATE_DIR", "CLAUDE_SWARM_ROOT")


@pytest.fixture(autouse=True)
def _real_tmp_leak_sentinel():
    """Fail loudly if a test causes a new comms-* directory to appear under
    the REAL /tmp -- regardless of what COMMS_ROOT/_root() claim the root
    is. Declared before _isolated_comms_env so it wraps it: this fixture's
    before-snapshot runs first (outermost setup) and its after-snapshot runs
    last (outermost teardown), so it sees the filesystem before any other
    fixture's env override is applied and after all of them unwind.
    """
    before = set(glob.glob("/tmp/comms-*"))
    yield
    after = set(glob.glob("/tmp/comms-*"))
    leaked = after - before
    if leaked:
        raise AssertionError(
            "test isolation breach: new dir(s) appeared under real /tmp "
            "during this test: %s -- COMMS_ROOT/COMMS_STATE_DIR/"
            "CLAUDE_SWARM_ROOT override was not honored (or code wrote to "
            "/tmp directly)" % sorted(leaked)
        )


@pytest.fixture(autouse=True)
def _isolated_comms_env(tmp_path, monkeypatch):
    """Default every isolation knob to a per-test tmp_path root, before the
    test (or any of its own fixtures) runs. A test/fixture that sets these
    itself afterward overrides these defaults in the normal env-var way --
    this fixture never re-asserts a value once the test has started.
    """
    root = tmp_path / "comms-root"
    state = tmp_path / "comms-state"
    root.mkdir()
    state.mkdir()
    monkeypatch.setenv("COMMS_ROOT", str(root))
    monkeypatch.setenv("COMMS_STATE_DIR", str(state))
    # Legacy pre-extraction name for COMMS_ROOT (migration compatibility,
    # see test_env_migration.py) -- default it too so a test that reads it
    # instead of the canonical name is isolated unconditionally as well.
    monkeypatch.setenv("CLAUDE_SWARM_ROOT", str(root))
    yield
