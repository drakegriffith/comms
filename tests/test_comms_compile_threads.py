#!/usr/bin/env python3
"""Tests for scripts/comms_compile_threads.py -- mailbox rows compiled into
one vault note per (board, local date).

Isolation: conftest.py's autouse fixture already points COMMS_ROOT and
COMMS_STATE_DIR at a per-test tmp dir. This file additionally isolates
COMMS_VAULT_ROOT (never the real vault) and TZ (deterministic local-date
bucketing across whatever machine runs the suite -- UTC, so "local" and the
UTC `at` strings this suite writes agree without a conversion in every
assertion).
"""

import json
import os
import stat
import sys
import time

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"),
)
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
    ),
)
import swarm_mailbox as mb  # noqa: E402
import comms_compile_threads as cct  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_vault_and_tz(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMS_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield


def _post_at(runid, seat, thread, at, kind="comment", text="t"):
    row = mb.post(runid, seat, kind, text, thread=thread)
    path = mb._seat_path(runid, seat)
    with open(path) as fh:
        lines = fh.readlines()
    last = json.loads(lines[-1])
    last["at"] = at
    lines[-1] = json.dumps(last) + "\n"
    with open(path, "w") as fh:
        fh.writelines(lines)
    return last


def _note_text(board, date):
    with open(cct._note_path(board, date)) as fh:
        return fh.read()


def _watermark(board):
    with open(cct._watermark_path(board)) as fh:
        return json.load(fh)


# ---- the positive control ---------------------------------------------------


def test_compile_once_on_an_empty_mailbox_inspects_zero_rows():
    assert cct.compile_once() == (0, 0)


def test_main_on_an_empty_mailbox_exits_2(capsys):
    rc = cct.main(["comms_compile_threads.py"])
    assert rc == 2
    assert "rows_inspected=0" in capsys.readouterr().err


# ---- board derivation --------------------------------------------------------


def test_board_of_a_repo_root_key():
    assert cct.board_of("doc:comms") == "comms"


def test_board_of_a_nested_key():
    assert cct.board_of("doc:comms/lib/swarm_threads.py") == "comms"


def test_board_of_none_or_empty_is_none():
    assert cct.board_of(None) is None
    assert cct.board_of("") is None


def test_board_of_a_non_doc_key_is_none():
    assert cct.board_of("weird:not-a-thread-key") is None


# ---- births (kind=status) are never compiled --------------------------------


def test_a_status_birth_is_excluded_from_rows_inspected_and_the_note():
    _post_at("test-r1", "a", "doc:repoA/x.md", "2026-08-25T10:00:00+00:00")
    _post_at(
        "test-r1",
        "b",
        "doc:repoA/x.md",
        "2026-08-25T10:05:00+00:00",
        kind="status",
        text="session started in /x",
    )
    rows_inspected, notes_written = cct.compile_once()
    assert rows_inspected == 1
    assert notes_written == 1
    text = _note_text("repoA", "2026-08-25")
    assert "session started" not in text
    assert " b:" not in text


# ---- note shape ---------------------------------------------------------------


def test_note_front_matter_and_body_shape():
    _post_at("test-r1", "a", "doc:repoA/x.md", "2026-08-25T10:00:00+00:00", text="hello")
    _post_at("test-r1", "b", "doc:repoA/x.md", "2026-08-25T10:05:00+00:00", text="hi back")
    cct.compile_once()
    text = _note_text("repoA", "2026-08-25")
    assert text.startswith("---\n")
    assert "type: Research Note" in text
    assert "date: 2026-08-25" in text
    assert "index_mode: raw" in text
    assert "board: repoA" in text
    assert "threads_inspected: 1" in text
    assert "threads_alive: 1" in text
    assert "## doc:repoA/x.md" in text
    assert "- 10:00 a: hello" in text
    assert "- 10:05 b: hi back" in text
    # a rows in `at` order: a before b
    assert text.index("hello") < text.index("hi back")


def test_a_lone_seat_thread_is_compiled_but_not_alive():
    _post_at("test-r1", "a", "doc:repoA/lonely.md", "2026-08-25T10:00:00+00:00")
    cct.compile_once()
    text = _note_text("repoA", "2026-08-25")
    assert "threads_inspected: 1" in text
    assert "threads_alive: 0" in text


# ---- two boards, multiple threads -------------------------------------------


def test_two_boards_get_two_separate_note_trees():
    _post_at("test-r1", "a", "doc:repoA/x.md", "2026-08-25T10:00:00+00:00")
    _post_at("test-r1", "a", "doc:repoB/y.md", "2026-08-25T11:00:00+00:00")
    rows_inspected, notes_written = cct.compile_once()
    assert rows_inspected == 2
    assert notes_written == 2
    assert os.path.isfile(cct._note_path("repoA", "2026-08-25"))
    assert os.path.isfile(cct._note_path("repoB", "2026-08-25"))


def test_three_threads_one_board_all_sections_present():
    _post_at("test-r1", "a", "doc:repoA/x.md", "2026-08-25T09:00:00+00:00")
    _post_at("test-r1", "a", "doc:repoA/y.md", "2026-08-25T10:00:00+00:00")
    _post_at("test-r1", "a", "doc:repoA/z.md", "2026-08-25T11:00:00+00:00")
    cct.compile_once()
    text = _note_text("repoA", "2026-08-25")
    assert "## doc:repoA/x.md" in text
    assert "## doc:repoA/y.md" in text
    assert "## doc:repoA/z.md" in text
    assert "threads_inspected: 3" in text


# ---- watermark: written, and honored on the next pass -----------------------


def test_watermark_is_written_after_a_compile():
    _post_at("test-r1", "a", "doc:repoA/x.md", "2026-08-25T10:00:00+00:00")
    cct.compile_once()
    wm = _watermark("repoA")
    assert wm["last_at"] == "2026-08-25T10:00:00+00:00"
    assert wm["thread_dates"]["doc:repoA/x.md"] == "2026-08-25"


def test_a_second_compile_with_no_new_rows_writes_no_notes():
    _post_at("test-r1", "a", "doc:repoA/x.md", "2026-08-25T10:00:00+00:00")
    cct.compile_once()
    rows_inspected, notes_written = cct.compile_once()
    # the row is still there (nothing is ever deleted from the mailbox) --
    # rows_inspected counts the whole board again, but the watermark means
    # nothing NEW needs writing.
    assert rows_inspected == 1
    assert notes_written == 0


def test_a_second_compile_leaves_the_first_note_byte_identical():
    _post_at("test-r1", "a", "doc:repoA/x.md", "2026-08-25T10:00:00+00:00")
    cct.compile_once()
    before = _note_text("repoA", "2026-08-25")
    cct.compile_once()
    after = _note_text("repoA", "2026-08-25")
    assert before == after


def test_rerunning_from_a_wiped_watermark_regenerates_byte_identical_output():
    _post_at("test-r1", "a", "doc:repoA/x.md", "2026-08-25T10:00:00+00:00")
    _post_at("test-r1", "b", "doc:repoA/x.md", "2026-08-25T10:05:00+00:00")
    cct.compile_once()
    original = _note_text("repoA", "2026-08-25")
    os.remove(cct._watermark_path("repoA"))
    cct.compile_once()
    assert _note_text("repoA", "2026-08-25") == original


def test_a_new_row_on_a_new_date_does_not_rewrite_the_old_dated_note():
    _post_at("test-r1", "a", "doc:repoA/x.md", "2026-08-25T10:00:00+00:00")
    cct.compile_once()
    day1_before = _note_text("repoA", "2026-08-25")
    _post_at("test-r1", "a", "doc:repoA/y.md", "2026-08-26T10:00:00+00:00")
    cct.compile_once()
    assert _note_text("repoA", "2026-08-25") == day1_before
    assert os.path.isfile(cct._note_path("repoA", "2026-08-26"))


# ---- continues pointer: a thread key spanning midnight -----------------------


def test_a_cross_midnight_thread_gets_a_continues_pointer():
    _post_at("test-r1", "a", "doc:repoA/x.md", "2026-08-25T23:50:00+00:00")
    _post_at("test-r1", "b", "doc:repoA/x.md", "2026-08-26T00:30:00+00:00")
    cct.compile_once()
    day1 = _note_text("repoA", "2026-08-25")
    day2 = _note_text("repoA", "2026-08-26")
    assert "continues" not in day1
    assert "continues: 2026-08-25.md" in day2
    # both dated rows land in their own day's section
    assert "23:50" in day1
    assert "00:30" in day2


def test_continues_pointer_only_names_the_immediately_prior_date():
    _post_at("test-r1", "a", "doc:repoA/x.md", "2026-08-24T23:50:00+00:00")
    cct.compile_once()
    _post_at("test-r1", "a", "doc:repoA/x.md", "2026-08-25T23:50:00+00:00")
    cct.compile_once()
    _post_at("test-r1", "a", "doc:repoA/x.md", "2026-08-26T00:10:00+00:00")
    cct.compile_once()
    day3 = _note_text("repoA", "2026-08-26")
    assert "continues: 2026-08-25.md" in day3


# ---- sync_embed_mirror.sh -----------------------------------------------------


def test_sync_embed_mirror_is_run_when_present_and_executable(tmp_path):
    vault = tmp_path / "vault"
    scripts_dir = vault / "scripts"
    scripts_dir.mkdir(parents=True)
    sentinel = tmp_path / "sync-ran"
    script = scripts_dir / "sync_embed_mirror.sh"
    script.write_text("#!/bin/sh\ntouch %s\n" % sentinel)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    _post_at("test-r1", "a", "doc:repoA/x.md", "2026-08-25T10:00:00+00:00")
    rc = cct.main(["comms_compile_threads.py"])
    assert rc == 0
    # subprocess is async-ish only in appearance -- subprocess.run blocks
    # until the child exits, so the sentinel exists by the time main() returns.
    assert sentinel.exists()


def test_sync_embed_mirror_missing_is_reported_on_stderr_not_fatal(capsys):
    _post_at("test-r1", "a", "doc:repoA/x.md", "2026-08-25T10:00:00+00:00")
    rc = cct.main(["comms_compile_threads.py"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "sync_embed_mirror.sh" in err
    assert "skipped" in err


def test_sync_embed_mirror_present_but_not_executable_is_skipped_not_run(tmp_path):
    vault = tmp_path / "vault"
    scripts_dir = vault / "scripts"
    scripts_dir.mkdir(parents=True)
    sentinel = tmp_path / "sync-ran"
    script = scripts_dir / "sync_embed_mirror.sh"
    script.write_text("#!/bin/sh\ntouch %s\n" % sentinel)
    # deliberately NOT chmod +x

    _post_at("test-r1", "a", "doc:repoA/x.md", "2026-08-25T10:00:00+00:00")
    cct.main(["comms_compile_threads.py"])
    assert not sentinel.exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
