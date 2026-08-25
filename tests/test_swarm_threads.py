#!/usr/bin/env python3
"""Tests for lib/swarm_threads.py -- the alive predicate and the grouping.

This module is PURE: no files, no env, no clock. So these tests need no
fixtures and no isolation beyond conftest's -- every input is a literal row
dict, which is the point of the module boundary (issue #40, D1): the Discord
mirror and `bin/comms threads` both call this one predicate instead of each
writing their own "is this conversation live" rule.
"""

import datetime
import json
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"),
)
import swarm_mailbox as mb  # noqa: E402
import swarm_threads as st  # noqa: E402

BASE = datetime.datetime(2026, 8, 25, 12, 0, 0, tzinfo=datetime.timezone.utc)


def row(seat, offset_s, kind="comment", thread="doc:comms/a.md", text="t"):
    """One row `offset_s` seconds after BASE."""
    return {
        "seat": seat,
        "at": (BASE + datetime.timedelta(seconds=offset_s)).isoformat(),
        "kind": kind,
        "text": text,
        "topic": "default",
        "thread": thread,
    }


# ---- closed vocabulary: STATUS_KIND is a real swarm_mailbox kind ----------
#
# swarm_mailbox.VALID_KINDS is the CLOSED vocabulary post() enforces; a kind
# spelled here that is not in it would make the "status rows never count"
# rule silently vacuous the moment a status row could never legally exist.


def test_status_kind_is_a_valid_mailbox_kind():
    assert st.STATUS_KIND in mb.VALID_KINDS


# ---- group_by_thread -------------------------------------------------------


def test_group_by_thread_buckets_by_the_thread_field():
    rows = [
        row("a", 0, thread="doc:comms/a.md"),
        row("b", 1, thread="doc:comms/b.md"),
        row("c", 2, thread="doc:comms/a.md"),
    ]
    groups = st.group_by_thread(rows)
    assert sorted(groups) == ["doc:comms/a.md", "doc:comms/b.md"]
    assert [r["seat"] for r in groups["doc:comms/a.md"]] == ["a", "c"]


def test_group_by_thread_preserves_input_order_inside_a_bucket():
    rows = [row("a", 10), row("b", 0), row("c", 5)]
    assert [r["seat"] for r in st.group_by_thread(rows)["doc:comms/a.md"]] == [
        "a",
        "b",
        "c",
    ]


def test_group_by_thread_drops_rows_with_no_thread():
    # A row without `thread` is not "thread None" -- it belongs to the
    # unthreaded path, which is a different renderer entirely. Bucketing it
    # under a None key would post it into a thread named after nothing.
    rows = [row("a", 0), {"seat": "b", "at": BASE.isoformat(), "kind": "finding"}]
    groups = st.group_by_thread(rows)
    assert list(groups) == ["doc:comms/a.md"]


def test_group_by_thread_drops_an_empty_thread_string():
    rows = [row("a", 0, thread="")]
    assert st.group_by_thread(rows) == {}


def test_group_by_thread_of_nothing_is_an_empty_dict():
    assert st.group_by_thread([]) == {}


def test_group_by_thread_does_not_mutate_or_copy_its_rows():
    r = row("a", 0)
    groups = st.group_by_thread([r])
    assert groups["doc:comms/a.md"][0] is r  # same object, not a copy


# ---- alive: the two halves of P4 ------------------------------------------


def test_two_seats_inside_the_window_is_alive():
    assert st.alive([row("a", 0), row("b", 60)]) is True


def test_one_seat_twice_is_not_alive():
    # The predicate is "a conversation", not "activity". One seat talking to
    # itself is a log, and a thread per monologue is how the board fills with
    # threads nobody reads.
    assert st.alive([row("a", 0), row("a", 60)]) is False


def test_a_single_row_is_not_alive():
    assert st.alive([row("a", 0)]) is False


def test_no_rows_is_not_alive():
    assert st.alive([]) is False


def test_gap_larger_than_the_window_is_not_alive():
    assert st.alive([row("a", 0), row("b", 1801)]) is False


def test_gap_exactly_the_window_is_alive():
    # Inclusive upper bound, stated: 0 < gap <= window.
    assert st.alive([row("a", 0), row("b", 1800)]) is True


def test_a_zero_gap_between_two_seats_is_not_alive_on_its_own():
    # 0 < gap is the design note's spelling. Two rows sharing a timestamp to
    # the microsecond are one writer emitting both (a replay, an import, a
    # fixture), not one seat answering another.
    assert st.alive([row("a", 0), row("b", 0)]) is False


def test_a_zero_gap_pair_does_not_veto_a_later_real_exchange():
    # "SOME consecutive pair", not "every pair": the same-timestamp pair is
    # skipped and the a->b exchange 60s later still carries the thread.
    rows = [row("a", 0), row("a", 0), row("b", 60)]
    assert st.alive(rows) is True


def test_rows_out_of_order_are_sorted_before_the_gaps_are_measured():
    # Callers hand these in from two places (the held file and a fresh read);
    # a predicate that trusted the input order would read a negative gap as
    # "not within the window" and silently never render.
    assert st.alive([row("b", 60), row("a", 0)]) is True


def test_two_seats_far_apart_with_a_close_pair_in_between_is_alive():
    rows = [row("a", 0), row("b", 30), row("a", 100000)]
    assert st.alive(rows) is True


def test_two_seats_whose_every_consecutive_pair_is_same_seat_is_not_alive():
    # a,a,...,b where the a->b step is beyond the window: two distinct seats
    # (half one passes), no timely exchange (half two fails).
    rows = [row("a", 0), row("a", 10), row("b", 99999)]
    assert st.alive(rows) is False


# ---- alive: status rows are excluded --------------------------------------


def test_a_status_birth_row_does_not_count_as_a_second_seat():
    # The ambient "session started" row is a birth announcement, not a
    # contribution. Counting it would make every thread one agent touched
    # look like a two-party conversation.
    rows = [
        row("a", 0),
        row("b", 60, kind="status", text="session started in /x"),
    ]
    assert st.alive(rows) is False


def test_status_rows_do_not_break_consecutiveness_between_two_real_seats():
    # They are filtered out BEFORE the pairs are walked, so a status row
    # landing between two speakers cannot make a live exchange read as dead.
    rows = [row("a", 0), row("c", 30, kind="status"), row("b", 60)]
    assert st.alive(rows) is True


def test_two_real_seats_plus_status_noise_is_still_alive():
    rows = [
        row("s", 0, kind="status"),
        row("a", 10),
        row("b", 40),
        row("s", 50, kind="status"),
    ]
    assert st.alive(rows) is True


# ---- alive: the two knobs -------------------------------------------------


def test_min_seats_3_rejects_a_two_seat_exchange():
    assert st.alive([row("a", 0), row("b", 60)], min_seats=3) is False


def test_min_seats_3_accepts_a_three_seat_exchange():
    rows = [row("a", 0), row("b", 60), row("c", 120)]
    assert st.alive(rows, min_seats=3) is True


def test_min_seats_3_still_needs_a_timely_pair():
    # Three distinct seats, but every consecutive step is beyond the window.
    rows = [row("a", 0), row("b", 100000), row("c", 200000)]
    assert st.alive(rows, min_seats=3) is False


def test_min_seats_1_lets_a_monologue_through_only_with_a_timely_pair():
    # Documented consequence of the knob, not an accident: at min_seats=1 the
    # seat-count half is vacuous, so the exchange half is the whole predicate
    # -- and it still requires TWO DIFFERENT seats in a consecutive pair.
    assert st.alive([row("a", 0), row("a", 60)], min_seats=1) is False
    assert st.alive([row("a", 0), row("b", 60)], min_seats=1) is True


def test_window_s_narrower_than_the_gap_rejects():
    assert st.alive([row("a", 0), row("b", 60)], window_s=30) is False


def test_window_s_wider_than_the_default_accepts_a_slow_exchange():
    assert st.alive([row("a", 0), row("b", 3600)], window_s=7200) is True


def test_defaults_are_the_documented_1800_and_2():
    assert st.DEFAULT_WINDOW_S == 1800
    assert st.DEFAULT_MIN_SEATS == 2


# ---- alive: malformed input never raises ----------------------------------


def test_a_row_with_no_at_is_ignored_not_fatal():
    # These rows come off disk (a held file a human may have edited, a peer
    # machine's export). A predicate that raised would take the whole mirror
    # pass down with it.
    rows = [{"seat": "a", "kind": "comment"}, row("b", 0), row("c", 60)]
    assert st.alive(rows) is True


def test_an_unparseable_at_is_ignored_not_fatal():
    rows = [{"seat": "a", "at": "yesterday", "kind": "comment"}, row("b", 0)]
    assert st.alive(rows) is False


def test_every_row_unparseable_is_not_alive():
    rows = [{"seat": "a", "at": "?"}, {"seat": "b", "at": "?"}]
    assert st.alive(rows) is False


def test_a_trailing_Z_timestamp_parses():
    # Not what post() writes (+00:00), but what a hand-written or
    # foreign-tool row commonly carries.
    rows = [
        {"seat": "a", "at": "2026-08-25T12:00:00Z", "kind": "comment"},
        {"seat": "b", "at": "2026-08-25T12:01:00Z", "kind": "comment"},
    ]
    assert st.alive(rows) is True


def test_a_naive_timestamp_is_read_as_utc_not_a_crash():
    # Mixing naive and aware datetimes raises TypeError on both comparison
    # and subtraction in Python, so an un-normalized parse takes the whole
    # mirror pass down. Naive reads as UTC: every writer in this repo emits
    # UTC, so that is the true reading, not a guess.
    rows = [
        {"seat": "a", "at": "2026-08-25T12:00:00", "kind": "comment"},
        row("b", 60),
    ]
    assert st.alive(rows) is True


def test_alive_does_not_mutate_the_rows_it_is_given():
    rows = [row("a", 0), row("b", 60)]
    before = [dict(r) for r in rows]
    st.alive(rows)
    assert rows == before


# ---- full_board_rows: the shared "read everything" I/O helper -------------
#
# These tests DO touch files -- conftest's autouse fixture points COMMS_ROOT
# at a per-test tmp dir, so nothing here can reach the real mailbox.


def _post(runid, seat, thread, kind="comment", text="t", at=None):
    row = mb.post(runid, seat, kind, text, thread=thread)
    if at is not None:
        # Overwrite the `at` this test wants deterministically, by rewriting
        # the seat's own file -- post() always stamps "now", and these tests
        # need control over ordering/gaps the way the pure-predicate tests
        # above get it from the `row()` helper's `offset_s`.
        path = mb._seat_path(runid, seat)
        with open(path) as fh:
            lines = fh.readlines()
        import json as _json

        last = _json.loads(lines[-1])
        last["at"] = at
        lines[-1] = _json.dumps(last) + "\n"
        with open(path, "w") as fh:
            fh.writelines(lines)
        row = last
    return row


def test_full_board_rows_reads_every_run_by_default():
    _post("test-r1", "a", "doc:x/a.md")
    _post("test-r2", "b", "doc:x/a.md")
    rows = st.full_board_rows(mb)
    assert len(rows) == 2


def test_full_board_rows_restricted_to_one_run():
    _post("test-r1", "a", "doc:x/a.md")
    _post("test-r2", "b", "doc:x/a.md")
    rows = st.full_board_rows(mb, run="test-r1")
    assert len(rows) == 1
    assert rows[0]["seat"] == "a"


def test_full_board_rows_of_an_empty_mailbox_is_empty():
    assert st.full_board_rows(mb) == []


# ---- last_gap_s -------------------------------------------------------------


def test_last_gap_s_of_two_rows_is_the_gap_between_them():
    rows = [row("a", 0), row("b", 90)]
    assert st.last_gap_s(rows) == 90


def test_last_gap_s_ignores_status_rows_at_the_end():
    rows = [row("a", 0), row("b", 90), row("c", 200, kind="status")]
    assert st.last_gap_s(rows) == 90


def test_last_gap_s_of_a_single_row_is_none():
    assert st.last_gap_s([row("a", 0)]) is None


def test_last_gap_s_of_no_rows_is_none():
    assert st.last_gap_s([]) is None


# ---- CLI: `swarm_threads.py threads` ---------------------------------------


def test_main_no_args_exits_2_with_usage(capsys):
    assert st.main(["swarm_threads.py"]) == 2
    assert "usage" in capsys.readouterr().err


def test_main_unknown_subcommand_exits_2_with_usage(capsys):
    assert st.main(["swarm_threads.py", "bogus"]) == 2
    assert "usage" in capsys.readouterr().err


def test_main_threads_on_an_empty_mailbox_is_the_positive_control(capsys):
    # A metric that inspected nothing never had the chance to say anything
    # true about liveness -- exit 2, not a quiet-looking exit 0.
    rc = st.main(["swarm_threads.py", "threads"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "threads_inspected=0" in err


def test_main_threads_prints_inspected_and_alive_counts(capsys):
    _post("test-r1", "a", "doc:x/a.md")
    _post("test-r1", "b", "doc:x/a.md")
    rc = st.main(["swarm_threads.py", "threads"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "threads_inspected=1 threads_alive=1" in out
    assert "doc:x/a.md" in out


def test_main_threads_seats_flag_raises_the_bar(capsys):
    _post("test-r1", "a", "doc:x/a.md")
    _post("test-r1", "b", "doc:x/a.md")
    rc = st.main(["swarm_threads.py", "threads", "--seats", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "threads_inspected=1 threads_alive=0" in out


def test_main_threads_alive_flag_narrows_the_window(capsys, monkeypatch):
    _post("test-r1", "a", "doc:x/a.md", at="2026-08-25T12:00:00+00:00")
    _post("test-r1", "b", "doc:x/a.md", at="2026-08-25T12:05:00+00:00")
    rc = st.main(["swarm_threads.py", "threads", "--alive", "60"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "threads_alive=0" in out


def test_main_threads_env_defaults_are_read_when_no_flag_given(capsys, monkeypatch):
    _post("test-r1", "a", "doc:x/a.md", at="2026-08-25T12:00:00+00:00")
    _post("test-r1", "b", "doc:x/a.md", at="2026-08-25T12:05:00+00:00")
    monkeypatch.setenv(st.ALIVE_SECONDS_VAR, "60")
    rc = st.main(["swarm_threads.py", "threads"])
    assert rc == 0
    assert "threads_alive=0" in capsys.readouterr().out


def test_main_threads_env_seats_default(capsys, monkeypatch):
    _post("test-r1", "a", "doc:x/a.md")
    _post("test-r1", "b", "doc:x/a.md")
    monkeypatch.setenv(st.ALIVE_SEATS_VAR, "3")
    rc = st.main(["swarm_threads.py", "threads"])
    assert rc == 0
    assert "threads_alive=0" in capsys.readouterr().out


def test_main_threads_run_flag_inspects_only_the_named_run(capsys):
    _post("test-r1", "a", "doc:x/a.md")
    _post("test-r1", "b", "doc:x/a.md")
    _post("test-r2", "c", "doc:y/b.md")
    rc = st.main(["swarm_threads.py", "threads", "--run", "test-r1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "doc:x/a.md" in out
    assert "doc:y/b.md" not in out


def test_main_threads_all_runs_flag_is_the_default_made_explicit(capsys):
    _post("test-r1", "a", "doc:x/a.md")
    _post("test-r2", "b", "doc:x/a.md")
    rc = st.main(["swarm_threads.py", "threads", "--all-runs"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "threads_inspected=1" in out


def test_main_threads_run_and_all_runs_together_is_a_usage_error(capsys):
    _post("test-r1", "a", "doc:x/a.md")
    rc = st.main(
        ["swarm_threads.py", "threads", "--run", "test-r1", "--all-runs"]
    )
    assert rc == 2
    assert "usage" in capsys.readouterr().err


def test_main_threads_json_output(capsys):
    _post("test-r1", "a", "doc:x/a.md")
    _post("test-r1", "b", "doc:x/a.md")
    rc = st.main(["swarm_threads.py", "threads", "--json"])
    assert rc == 0
    lines = capsys.readouterr().out.strip().splitlines()
    summary = json.loads(lines[0])
    assert summary == {"threads_inspected": 1, "threads_alive": 1}
    detail = json.loads(lines[1])
    assert detail["thread"] == "doc:x/a.md"
    assert detail["alive"] is True
    assert detail["seats"] == 2
    assert detail["rows"] == 2


def test_main_threads_unexpected_positional_argument_exits_2(capsys):
    _post("test-r1", "a", "doc:x/a.md")
    rc = st.main(["swarm_threads.py", "threads", "extra"])
    assert rc == 2
    assert "usage" in capsys.readouterr().err


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
