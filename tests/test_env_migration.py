"""Env decoupling and migration-compatibility tests.

The canonical knobs are COMMS_ROOT (mailbox root) and COMMS_STATE_DIR
(arming/claims/telemetry state). Each falls back to its pre-extraction name --
CLAUDE_SWARM_ROOT and SWARM_ARM_STATE_DIR respectively -- as migration
compatibility, so a machine mid-migration keeps working; the canonical name
always wins when both are set. These tests pin that contract in BOTH
directions: the new name works alone, the old name still works alone, and
precedence is canonical-first. The legacy env names below are the one place in
core allowed to spell them (they ARE the subject under test).
"""

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"),
)
import swarm_arm
import swarm_mailbox as mb

LEGACY_ROOT = "CLAUDE_SWARM_ROOT"      # migration compatibility: pre-extraction name
LEGACY_STATE = "SWARM_ARM_STATE_DIR"   # migration compatibility: pre-extraction name


def _clear(monkeypatch):
    for var in ("COMMS_ROOT", LEGACY_ROOT, "COMMS_STATE_DIR", LEGACY_STATE):
        monkeypatch.delenv(var, raising=False)


# ---- mailbox root ----------------------------------------------------------

def test_comms_root_is_honored(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("COMMS_ROOT", str(tmp_path))
    assert mb._root() == str(tmp_path)


def test_legacy_root_still_works_when_canonical_unset(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv(LEGACY_ROOT, str(tmp_path))
    assert mb._root() == str(tmp_path)


def test_canonical_root_wins_over_legacy(monkeypatch, tmp_path):
    _clear(monkeypatch)
    new = tmp_path / "new"
    old = tmp_path / "old"
    monkeypatch.setenv("COMMS_ROOT", str(new))
    monkeypatch.setenv(LEGACY_ROOT, str(old))
    assert mb._root() == str(new)


def test_root_defaults_to_tmp_when_nothing_set(monkeypatch):
    _clear(monkeypatch)
    assert mb._root() == "/tmp"


def test_run_dir_is_comms_prefixed(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("COMMS_ROOT", str(tmp_path))
    d = mb.init("mig1")
    assert os.path.isdir(d)
    assert os.path.basename(d) == "comms-mig1"


# ---- state dir -------------------------------------------------------------

def test_comms_state_dir_is_honored(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("COMMS_STATE_DIR", str(tmp_path))
    swarm_arm.arm("mig2")  # no explicit state_dir: must resolve from the env
    assert os.path.isfile(str(tmp_path / "swarm-arm" / "mig2" / "meta.json"))


def test_legacy_state_dir_still_works_when_canonical_unset(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv(LEGACY_STATE, str(tmp_path))
    swarm_arm.arm("mig3")
    assert os.path.isfile(str(tmp_path / "swarm-arm" / "mig3" / "meta.json"))


def test_canonical_state_dir_wins_over_legacy(monkeypatch, tmp_path):
    _clear(monkeypatch)
    new = tmp_path / "new"
    old = tmp_path / "old"
    monkeypatch.setenv("COMMS_STATE_DIR", str(new))
    monkeypatch.setenv(LEGACY_STATE, str(old))
    swarm_arm.arm("mig4")
    assert os.path.isfile(str(new / "swarm-arm" / "mig4" / "meta.json"))
    assert not os.path.exists(str(old))


def test_default_state_dir_is_home_dot_comms(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))  # expanduser reads HOME on posix
    assert swarm_arm._default_state_dir() == str(tmp_path / ".comms" / "state")
