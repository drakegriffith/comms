#!/usr/bin/env python3
"""Tests for lib/swarm_threads.py -- the alive predicate and the grouping.

This module is PURE: no files, no env, no clock. So these tests need no
fixtures and no isolation beyond conftest's -- every input is a literal row
dict, which is the point of the module boundary (issue #40, D1): the Discord
mirror and `bin/comms threads` both call this one predicate instead of each
writing their own "is this conversation live" rule.
"""

import datetime
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"),
)
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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
