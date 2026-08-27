"""Interface tests for the runtime-agnostic render vocabulary."""

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "adapters", "discord"))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import comms_render  # noqa: E402
import mirror  # noqa: E402


@pytest.mark.parametrize("audience", ["engineer", "everyone"])
@pytest.mark.parametrize(
    "row",
    [
        {"seat": "alpha", "kind": "finding", "text": "found it", "topic": "default"},
        {"seat": "alpha", "kind": "comment", "text": "threaded", "thread": "doc:lib/a.py"},
        {"seat": "alpha", "kind": "reply", "text": "direct", "topic": "@bravo"},
        {"seat": "alpha", "kind": "claim", "text": "taking it", "topic": "default"},
    ],
)
def test_mirror_and_library_body_parity(monkeypatch, audience, row):
    monkeypatch.setenv("COMMS_AUDIENCE", audience)
    assert mirror.build_content(row) == comms_render.build_content(row, audience)


def test_unknown_audience_names_both_legal_values():
    with pytest.raises(ValueError) as exc:
        comms_render.build_content({}, "simple")
    assert "engineer" in str(exc.value)
    assert "everyone" in str(exc.value)


def test_library_is_uncapped_and_mirror_keeps_text_cap(monkeypatch):
    monkeypatch.setenv("COMMS_AUDIENCE", "engineer")
    row = {"seat": "alpha", "kind": "finding", "text": "x" * 3000, "topic": "default"}
    direct = comms_render.build_content(row, "engineer")
    adapted = mirror.build_content(row)
    assert direct.endswith("x" * 3000)
    assert adapted.endswith("x" * mirror.TEXT_CAP)
    assert len(direct) > len(adapted)
