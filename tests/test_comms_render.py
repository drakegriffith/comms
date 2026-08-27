"""Interface tests for the runtime-agnostic render vocabulary."""

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "adapters", "discord"))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import comms_render  # noqa: E402
import mirror  # noqa: E402


@pytest.mark.parametrize(
    "audience,row,expected",
    [
        ("engineer", {"kind": "finding", "text": "found a leak"}, "📬✅ found a leak"),
        ("everyone", {"kind": "finding", "text": "found a leak"}, "✅ Found something: found a leak"),
        ("engineer", {"kind": "claim", "text": "taking it"}, "📌 taking it"),
        ("everyone", {"kind": "claim", "text": "taking it"}, "📌 Taking this on: taking it"),
        ("engineer", {"kind": "comment", "text": "threaded", "thread": "doc:lib/a.py"}, "📬💬 threaded"),
        ("everyone", {"kind": "comment", "text": "threaded", "thread": "doc:lib/a.py"}, "💬 threaded"),
        ("engineer", {"kind": "reply", "text": "direct", "topic": "@bravo"}, "📨 to bravo: direct"),
        ("everyone", {"kind": "reply", "text": "direct", "topic": "@bravo"}, "📨 Message to bravo: direct"),
        ("engineer", {"kind": "status", "text": "session started in /tmp/work"}, "🐣 I am awake in /tmp/work"),
        ("everyone", {"kind": "status", "text": "session started in /tmp/work"}, "👋 Joined, working in work"),
    ],
)
def test_build_content_uses_pinned_vocabulary(audience, row, expected):
    assert comms_render.build_content(row, audience) == expected


@pytest.mark.parametrize(
    "audience,expected",
    [
        ("engineer", "everyone-alpha · model on project (machine)"),
        ("everyone", "everyone-alpha · model, working on project"),
    ],
)
def test_build_author_sanitizes_and_uses_pinned_shape(audience, expected):
    assert comms_render.build_author(
        "@everyone\u200b-alpha", {"model": "model", "project": "project"}, "machine", audience
    ) == expected


@pytest.mark.parametrize(
    "audience,n,seats,expected",
    [
        ("engineer", 2, ["alpha", "bravo"], "👁️ read 2 row(s) from alpha, bravo"),
        ("engineer", 0, [], "👁️ read 0 row(s) from unknown sender(s)"),
        ("everyone", 2, ["alpha", "bravo"], "👀 Read 2 new messages from alpha and bravo"),
        ("everyone", 0, [], "👀 Read 0 new messages"),
    ],
)
def test_build_read_content_uses_pinned_vocabulary(audience, n, seats, expected):
    assert comms_render.build_read_content(n, seats, audience) == expected


@pytest.mark.parametrize(
    "audience,key,expected",
    [
        ("engineer", "doc:comms/a/b.md", "comms/a/b.md"),
        ("everyone", "doc:comms/a/b.md", "b.md · comms"),
        ("engineer", "", ""),
        ("everyone", "", ""),
    ],
)
def test_thread_title_uses_pinned_vocabulary_and_empty_fallback(audience, key, expected):
    assert comms_render.thread_title(key, audience) == expected


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
