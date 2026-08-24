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
import warnings

import pytest

# Enables the `pytester` fixture (pytest's own plugin for running a nested
# pytest invocation against throwaway files) -- used by
# test_conftest_isolation.py's positive control so the sentinel's leak-victim
# test file never has to be written into this repo's working tree.
pytest_plugins = ["pytester"]

_ISOLATION_VARS = ("COMMS_ROOT", "COMMS_STATE_DIR", "CLAUDE_SWARM_ROOT")

# Every literal runid this suite passes to swarm_mailbox (init/post/
# subscribe/read_for/read_siblings, directly or through the CLI) as of this
# writing -- gathered by grepping tests/*.py for those call sites. A new
# /tmp/comms-<runid> matching one of these is unambiguously this suite's own
# escape (a mutant nulled the COMMS_ROOT override), so it FAILS the test.
#
# MAINTENANCE: this list is a positive-control aid, not the isolation
# mechanism -- _isolated_comms_env (below) is what actually prevents the
# leak. If you add a test that calls swarm_mailbox with a new literal runid,
# either add it here, or just spell it with an "isofix-" or "test-" prefix
# (see _TEST_SHAPED_PREFIXES) so it needs no registry edit. A runid that
# matches neither still gets a WARNING naming it, not silence -- that
# warning is your cue to add it here if it really is this suite's leak.
_KNOWN_TEST_RUNIDS = frozenset(
    {
        "alpha", "b1", "c1", "mid", "mig1", "only-in-second-root",
        "run1", "run2", "run3", "run3b", "run3k", "run3u", "run4", "run5",
        "s0", "s1", "s2", "s3",
        "t1", "t2", "t3", "t4", "t5",
        "u1", "u2", "u3", "u4", "u5",
        "zeta",
        "mirror-test", "ingest-test",
    }
)

# A runid starting with either prefix is treated as this suite's own without
# a registry entry -- reserved namespace for this file's own tests
# (isofix-*) and a documented convention any future test can opt into
# (test-*) to get hard-fail coverage instead of a warning.
_TEST_SHAPED_PREFIXES = ("isofix-", "test-")


def _is_test_shaped_comms_dir(path):
    """True if `path` is a directory whose name looks like one THIS suite's
    own swarm_mailbox calls would produce (see _KNOWN_TEST_RUNIDS above),
    never based on anything read through swarm_mailbox._root() itself."""
    if not os.path.isdir(path):
        return False
    name = os.path.basename(path)
    if not name.startswith("comms-"):
        return False
    runid = name[len("comms-"):]
    return runid in _KNOWN_TEST_RUNIDS or runid.startswith(_TEST_SHAPED_PREFIXES)


@pytest.fixture(autouse=True)
def _real_tmp_leak_sentinel():
    """Fail loudly if a test causes a new, TEST-SHAPED comms-* directory to
    appear under the REAL /tmp -- regardless of what COMMS_ROOT/_root()
    claim the root is. Declared before _isolated_comms_env so it wraps it:
    this fixture's before-snapshot runs first (outermost setup) and its
    after-snapshot runs last (outermost teardown), so it sees the
    filesystem before any other fixture's env override is applied and after
    all of them unwind.

    Narrowed to directories matching this suite's own runid vocabulary
    (_is_test_shaped_comms_dir) on purpose: this machine runs live comms
    traffic concurrently (real `comms enroll` of new runs, scratch files
    like comms-pytest.out/comms-*.txt dropped by other tooling), and a
    sentinel that fails on ANY new /tmp/comms-* entry fails innocent tests
    for unrelated activity -- which, inside the mutation gate, reads as a
    false KILL and inflates the score. Anything new that is NOT test-shaped
    (a live run's directory, a stray file, kind of a coin flip which) gets a
    warning naming it instead of failing the test outright.
    """
    before = set(glob.glob("/tmp/comms-*"))
    yield
    after = set(glob.glob("/tmp/comms-*"))
    new = after - before
    if not new:
        return
    leaked = {p for p in new if _is_test_shaped_comms_dir(p)}
    noisy = new - leaked
    if noisy:
        warnings.warn(
            "new /tmp/comms-* entries appeared during this test that do not "
            "match this suite's known test-shaped runids/prefixes (see "
            "_KNOWN_TEST_RUNIDS in tests/conftest.py): %s -- treated as "
            "unrelated live traffic on this machine, not a leak from this "
            "test. If this IS this suite's leak, add its runid to the "
            "registry so it fails loudly next time." % sorted(noisy)
        )
    if leaked:
        raise AssertionError(
            "test isolation breach: new dir(s) shaped like this suite's own "
            "test runids appeared under real /tmp: %s -- COMMS_ROOT/"
            "COMMS_STATE_DIR/CLAUDE_SWARM_ROOT override was not honored (or "
            "code wrote to /tmp directly)" % sorted(leaked)
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
