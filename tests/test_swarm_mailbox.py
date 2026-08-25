#!/usr/bin/env python3
"""Self-test for swarm_mailbox -- the positive control that siblings exchange
rows and never read their own, collision-free under interleaved writes."""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(os.path.dirname(_HERE), "lib")
_spec = importlib.util.spec_from_file_location(
    "swarm_mailbox", os.path.join(_LIB, "swarm_mailbox.py")
)
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)


class TestMailbox(unittest.TestCase):
    def setUp(self):
        # conftest.py's autouse fixture already points COMMS_ROOT at a fresh
        # per-test tmp_path dir before setUp runs; adopt it rather than
        # minting our own via tempfile.mkdtemp() (its default dir is the
        # platform tempdir, which IS /tmp on Linux when TMPDIR is unset --
        # that used to write real dirs into /tmp on CI).
        self.tmp = os.environ["COMMS_ROOT"]

    def test_init_creates_dir(self):
        d = mb.init("run1")
        self.assertTrue(os.path.isdir(d))
        self.assertTrue(d.endswith("comms-run1"))

    def test_two_seats_read_each_other_never_self(self):
        mb.init("run2")
        mb.post("run2", "seatA", "finding", "A found X")
        mb.post("run2", "seatB", "claim", "B claims Y")

        a_view = mb.read_siblings("run2", "seatA")
        b_view = mb.read_siblings("run2", "seatB")

        # A sees only B's row; B sees only A's row.
        self.assertEqual([r["seat"] for r in a_view], ["seatB"])
        self.assertEqual([r["seat"] for r in b_view], ["seatA"])
        self.assertEqual(a_view[0]["text"], "B claims Y")
        self.assertEqual(b_view[0]["text"], "A found X")

    def test_invalid_kind_refused(self):
        mb.init("run3")
        with self.assertRaises(ValueError):
            mb.post("run3", "seatA", "gossip", "not a valid kind")

    def test_invalid_unicast_recipient_refused(self):
        """A unicast to a garbage seat must fail loudly, never mint a
        malformed @topic (kills BRANCH_FORCE_FALSE on the _valid_seat guard)."""
        mb.init("run3u")
        with self.assertRaises(ValueError) as ctx:
            mb.post("run3u", "seatA", "reply", "text", to="../escape")
        self.assertIn("invalid recipient seat", str(ctx.exception))

    def test_every_valid_kind_posts(self):
        """The full closed vocabulary, enumerated here on purpose (checker
        duplicates the list rather than importing intent from prose)."""
        mb.init("run3k")
        expected = ("finding", "claim", "blocker", "comment", "reply", "status")
        self.assertEqual(mb.VALID_KINDS, expected)
        for kind in expected:
            row = mb.post("run3k", "seatA", kind, "text for %s" % kind)
            self.assertEqual(row["kind"], kind)
        view = mb.read_siblings("run3k", "seatB")
        self.assertEqual(sorted(r["kind"] for r in view), sorted(expected))

    def test_unknown_kind_banana_still_loud(self):
        """Extending the tuple must not have opened the vocabulary."""
        mb.init("run3b")
        with self.assertRaises(ValueError) as ctx:
            mb.post("run3b", "seatA", "banana", "free text kind")
        self.assertIn("invalid kind", str(ctx.exception))
        self.assertIn("banana", str(ctx.exception))

    def test_rows_sorted_by_at(self):
        mb.init("run4")
        # Post several from one seat; a reader from another seat sees them ordered.
        for i in range(5):
            mb.post("run4", "writer", "finding", "row %d" % i)
        view = mb.read_siblings("run4", "reader")
        ats = [r["at"] for r in view]
        self.assertEqual(ats, sorted(ats))
        self.assertEqual(len(view), 5)

    def test_interleaved_writes_are_collision_free(self):
        """Two seats each append many rows concurrently. Because each seat owns
        its own file, every row survives and lands in the right file."""
        mb.init("run5")
        N = 200

        def writer(seat):
            for i in range(N):
                mb.post("run5", seat, "finding", "%s-%d" % (seat, i))

        t1 = threading.Thread(target=writer, args=("s1",))
        t2 = threading.Thread(target=writer, args=("s2",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # A third seat reads both files: exactly 2N rows, N per seat, none lost.
        view = mb.read_siblings("run5", "reader")
        self.assertEqual(len(view), 2 * N)
        s1_rows = [r for r in view if r["seat"] == "s1"]
        s2_rows = [r for r in view if r["seat"] == "s2"]
        self.assertEqual(len(s1_rows), N)
        self.assertEqual(len(s2_rows), N)
        # Each seat's own texts are all present (no lost/overwritten appends).
        self.assertEqual({r["text"] for r in s1_rows}, {"s1-%d" % i for i in range(N)})
        self.assertEqual({r["text"] for r in s2_rows}, {"s2-%d" % i for i in range(N)})


class TestTopics(unittest.TestCase):
    def setUp(self):
        # See TestMailbox.setUp: adopt the isolated root conftest.py already set.
        self.tmp = os.environ["COMMS_ROOT"]

    def test_row_carries_default_topic_when_unset(self):
        mb.init("t1")
        row = mb.post("t1", "seatA", "finding", "no topic given")
        self.assertEqual(row["topic"], "default")

    def test_reader_filtering_a_topic_sees_only_that_topic(self):
        mb.init("t2")
        mb.post("t2", "writer", "finding", "in A", topic="alpha")
        mb.post("t2", "writer", "finding", "in B", topic="beta")

        a_only = mb.read_siblings("t2", "reader", topic="alpha")
        self.assertEqual([r["text"] for r in a_only], ["in A"])
        self.assertTrue(all(r["topic"] == "alpha" for r in a_only))

    def test_topic_none_sees_every_topic(self):
        mb.init("t3")
        mb.post("t3", "writer", "finding", "in A", topic="alpha")
        mb.post("t3", "writer", "finding", "in B", topic="beta")
        mb.post("t3", "writer", "finding", "unset")  # default topic

        allrows = mb.read_siblings("t3", "reader")  # topic=None
        self.assertEqual(len(allrows), 3)
        self.assertEqual(
            {r["text"] for r in allrows}, {"in A", "in B", "unset"}
        )

    def test_default_topic_matches_rows_written_without_one(self):
        mb.init("t4")
        mb.post("t4", "writer", "finding", "explicit default", topic="default")
        mb.post("t4", "writer", "finding", "implicit default")  # no topic

        d = mb.read_siblings("t4", "reader", topic="default")
        self.assertEqual(
            {r["text"] for r in d}, {"explicit default", "implicit default"}
        )

    def test_cli_post_and_read_with_topic_flag(self):
        import subprocess

        script = os.path.join(_LIB, "swarm_mailbox.py")
        env = dict(os.environ, COMMS_ROOT=self.tmp)

        def run(args):
            return subprocess.run(
                [sys.executable, script] + args,
                capture_output=True, text=True, env=env,
            )

        # A backward-compatible post (no --topic) still works and lands "default".
        r0 = run(["post", "t5", "wr", "finding", "legacy"])
        self.assertEqual(r0.returncode, 0, r0.stderr)
        self.assertEqual(json.loads(r0.stdout)["topic"], "default")

        # A --topic post lands in its slice.
        r1 = run(["post", "t5", "wr", "finding", "scoped", "--topic", "gamma"])
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(json.loads(r1.stdout)["topic"], "gamma")

        # Reader filtering gamma sees only the scoped row.
        r2 = run(["read", "t5", "rd", "--topic", "gamma"])
        self.assertEqual(r2.returncode, 0, r2.stderr)
        texts = [json.loads(l)["text"] for l in r2.stdout.splitlines() if l.strip()]
        self.assertEqual(texts, ["scoped"])

        # Reader with no --topic sees both.
        r3 = run(["read", "t5", "rd"])
        texts_all = [json.loads(l)["text"] for l in r3.stdout.splitlines() if l.strip()]
        self.assertEqual(set(texts_all), {"legacy", "scoped"})


class TestSubscriptions(unittest.TestCase):
    """The routing layer: a reader gets ONLY its subscribed topics plus its own
    unicasts, never the whole board -- the 50-agent scale lever."""

    def setUp(self):
        # See TestMailbox.setUp: adopt the isolated root conftest.py already set.
        self.tmp = os.environ["COMMS_ROOT"]

    def test_unregistered_seat_sees_whole_board(self):
        # Backward compat: no subscribe() => read_for behaves like whole-board.
        mb.init("s0")
        mb.post("s0", "w1", "finding", "a", topic="projA")
        mb.post("s0", "w2", "finding", "b", topic="projB")
        self.assertIsNone(mb.subscriptions("s0", "reader"))
        view = mb.read_for("s0", "reader")
        self.assertEqual({r["text"] for r in view}, {"a", "b"})

    def test_subscriber_sees_only_its_topics(self):
        mb.init("s1")
        mb.subscribe("s1", "reader", ["projA", "broadcast"])
        mb.post("s1", "wA", "finding", "in A", topic="projA")
        mb.post("s1", "wB", "finding", "in B", topic="projB")   # NOT subscribed
        mb.post("s1", "wX", "finding", "hello all", topic="broadcast")

        view = mb.read_for("s1", "reader")
        self.assertEqual({r["text"] for r in view}, {"in A", "hello all"})
        # The non-subscribed project's row is absent -- cross-project isolation.
        self.assertNotIn("in B", {r["text"] for r in view})

    def test_non_subscriber_does_not_see_a_topic(self):
        mb.init("s2")
        mb.subscribe("s2", "readerB", ["projB"])   # only projB
        mb.post("s2", "wA", "finding", "secret A", topic="projA")
        view = mb.read_for("s2", "readerB")
        self.assertEqual(view, [])   # projA is not in readerB's slice

    def test_subscribe_overwrites_not_appends(self):
        mb.init("s3")
        mb.subscribe("s3", "r", ["projA"])
        mb.subscribe("s3", "r", ["projB"])   # replaces, does not accumulate
        self.assertEqual(mb.subscriptions("s3", "r"), {"projB", "@r"})

    def test_unicast_reaches_only_the_addressee(self):
        mb.init("u1")
        mb.subscribe("u1", "seatC", ["projA"])   # C does NOT subscribe to any @topic
        mb.subscribe("u1", "seatD", ["projA"])
        mb.post("u1", "sender", "finding", "for C only", to="seatC")

        c_view = mb.read_for("u1", "seatC")
        d_view = mb.read_for("u1", "seatD")
        self.assertEqual([r["text"] for r in c_view], ["for C only"])
        self.assertEqual([r["text"] for r in c_view][0], "for C only")
        self.assertEqual(c_view[0].get("to"), "seatC")
        # D shares the project subscription but must NOT see C's direct message.
        self.assertEqual([r["text"] for r in d_view], [])

    def test_unicast_lands_even_without_subscribing(self):
        # A seat's own "@<seat>" is always delivered, even with no subscribe().
        mb.init("u2")
        mb.post("u2", "sender", "finding", "ping C", to="seatC")
        view = mb.read_for("u2", "seatC")   # seatC never called subscribe
        # No subscription => whole board => the unicast is included.
        self.assertEqual([r["text"] for r in view], ["ping C"])

    def test_unicast_reserved_topic_and_row_shape(self):
        mb.init("u3")
        row = mb.post("u3", "sender", "finding", "hi", to="seatC")
        self.assertEqual(row["topic"], "@seatC")
        self.assertEqual(row["to"], "seatC")

    def test_fanout_row_has_no_to_key(self):
        # A plain topic post stays byte-compatible with the pre-routing format.
        mb.init("u4")
        row = mb.post("u4", "sender", "finding", "hi", topic="projA")
        self.assertNotIn("to", row)

    def test_post_rejects_topic_and_to_conflict(self):
        mb.init("u5")
        with self.assertRaises(ValueError):
            mb.post("u5", "s", "finding", "x", topic="projA", to="seatC")

    def test_broadcast_reaches_every_subscriber(self):
        mb.init("b1")
        for s in ("r1", "r2", "r3"):
            mb.subscribe("b1", s, ["proj-%s" % s, "broadcast"])
        mb.post("b1", "hub", "finding", "all hands", topic="broadcast")
        for s in ("r1", "r2", "r3"):
            view = mb.read_for("b1", s)
            self.assertIn("all hands", {r["text"] for r in view})

    def test_cli_subscribe_and_read_subs(self):
        import subprocess

        script = os.path.join(_LIB, "swarm_mailbox.py")
        env = dict(os.environ, COMMS_ROOT=self.tmp)

        def run(args):
            return subprocess.run(
                [sys.executable, script] + args,
                capture_output=True, text=True, env=env,
            )

        self.assertEqual(run(["subscribe", "c1", "rd", "projA", "broadcast"]).returncode, 0)
        run(["post", "c1", "wA", "finding", "in A", "--topic", "projA"])
        run(["post", "c1", "wB", "finding", "in B", "--topic", "projB"])
        run(["post", "c1", "wX", "finding", "hi all", "--topic", "broadcast"])
        run(["post", "c1", "snd", "finding", "direct", "--to", "rd"])

        r = run(["read", "c1", "rd", "--subs"])
        self.assertEqual(r.returncode, 0, r.stderr)
        texts = {json.loads(l)["text"] for l in r.stdout.splitlines() if l.strip()}
        self.assertEqual(texts, {"in A", "hi all", "direct"})   # NOT "in B"


class TestDeliveryCursor(unittest.TestCase):
    """The shared confirmed-delivery cursor (issue #30).

    The contract these pin is the ORDER: a consumer takes rows, delivers them,
    and only then confirms. The failure mode they exist to catch is a
    reimplementation that commits when the rows are TAKEN -- which turns every
    failed delivery into a silently dropped row, and which is why three
    adapters each carrying their own copy of this pair was worth collapsing.
    """

    def setUp(self):
        self.state = os.environ["COMMS_STATE_DIR"]
        self.path = os.path.join(self.state, "delivery", "consumer.json")

    def _rows(self, seat, *texts):
        return [{"seat": seat, "at": "2026-08-25T00:00:0%d" % i, "text": t}
                for i, t in enumerate(texts)]

    def _cursor(self):
        return mb.DeliveryCursor(self.path)

    # ---- the happy path: confirmed rows are never handed over twice --------

    def test_take_returns_every_row_when_there_is_no_cursor_yet(self):
        fresh, _confirm = self._cursor().take(self._rows("alpha", "one", "two"))
        self.assertEqual([r["text"] for r in fresh], ["one", "two"])

    def test_confirmed_rows_do_not_come_back(self):
        rows = self._rows("alpha", "one")
        _fresh, confirm = self._cursor().take(rows)
        confirm()
        fresh, _ = self._cursor().take(rows)
        self.assertEqual(fresh, [])

    def test_only_rows_added_after_the_confirm_come_back(self):
        _fresh, confirm = self._cursor().take(self._rows("alpha", "one"))
        confirm()
        fresh, _ = self._cursor().take(self._rows("alpha", "one", "two"))
        self.assertEqual([r["text"] for r in fresh], ["two"])

    # ---- THE FAILED-DELIVERY PATH -----------------------------------------

    def test_unconfirmed_rows_are_delivered_again(self):
        """The whole point: delivery failed, so confirm was never called, so
        the cursor did not move and the rows come back next pass."""
        rows = self._rows("alpha", "one", "two")
        fresh, _confirm = self._cursor().take(rows)          # never confirmed
        self.assertEqual([r["text"] for r in fresh], ["one", "two"])
        again, _ = self._cursor().take(rows)
        self.assertEqual([r["text"] for r in again], ["one", "two"])

    def test_take_writes_nothing_at_all(self):
        """Not just 'the counts did not move' -- no file appears, so a crash
        between take and confirm is indistinguishable from never having run."""
        self._cursor().take(self._rows("alpha", "one"))
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(os.listdir(self.state), [])

    def test_a_partial_batch_that_fails_re_delivers_the_whole_batch(self):
        """Counts cannot express 'row 1 landed, row 2 did not', so a caller
        that cannot confirm the batch re-delivers all of it. Pinned because
        the alternative -- confirming what 'probably' landed -- drops rows."""
        rows = self._rows("alpha", "one", "two")
        try:
            _fresh, confirm = self._cursor().take(rows)
            raise RuntimeError("delivery blew up after row one")
        except RuntimeError:
            pass  # confirm deliberately not called
        again, _ = self._cursor().take(rows)
        self.assertEqual([r["text"] for r in again], ["one", "two"])

    # ---- persistence and failure behavior ---------------------------------

    def test_confirm_creates_the_cursor_under_the_state_dir(self):
        _fresh, confirm = self._cursor().take(self._rows("alpha", "one"))
        confirm()
        self.assertTrue(os.path.isfile(self.path))
        self.assertTrue(self.path.startswith(self.state))
        with open(self.path) as fh:
            self.assertEqual(json.load(fh), {"alpha": 1})

    def test_confirm_leaves_no_tmp_file_behind(self):
        _fresh, confirm = self._cursor().take(self._rows("alpha", "one"))
        confirm()
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["consumer.json"])

    def test_confirm_is_idempotent(self):
        _fresh, confirm = self._cursor().take(self._rows("alpha", "one"))
        confirm()
        confirm()
        self.assertEqual(self._cursor().load(), {"alpha": 1})

    def test_a_malformed_cursor_file_replays_rather_than_skips(self):
        """Corruption must fail toward re-delivery: seeing a row twice is
        recoverable, never seeing it is not."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as fh:
            fh.write("{not json")
        self.assertEqual(self._cursor().load(), {})
        fresh, _ = self._cursor().take(self._rows("alpha", "one"))
        self.assertEqual([r["text"] for r in fresh], ["one"])

    def test_a_cursor_file_holding_the_wrong_type_replays(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as fh:
            json.dump(["alpha", 1], fh)
        self.assertEqual(self._cursor().load(), {})

    def test_load_of_a_missing_cursor_is_empty_not_an_error(self):
        self.assertEqual(self._cursor().load(), {})

    def test_confirm_raises_oserror_when_the_path_is_unwritable(self):
        """The caller decides whether an unwritable state dir is fatal (the
        CLI says no, the rows already reached stdout), so this must surface as
        OSError rather than being swallowed here."""
        blocker = os.path.join(self.state, "blocker")
        with open(blocker, "w") as fh:
            fh.write("i am a file, not a directory")
        cursor = mb.DeliveryCursor(os.path.join(blocker, "sub", "cursor.json"))
        _fresh, confirm = cursor.take(self._rows("alpha", "one"))
        with self.assertRaises(OSError):
            confirm()

    # ---- keys, filters, and who owns what ---------------------------------

    def test_two_paths_are_two_independent_consumers(self):
        """The key is the caller's business: one consumer confirming must not
        mark another's stream delivered (the discord mirror's lanes and the
        remote sync's hosts are exactly this)."""
        rows = self._rows("alpha", "one")
        _fresh, confirm = self._cursor().take(rows)
        confirm()
        other = mb.DeliveryCursor(os.path.join(self.state, "delivery", "other.json"))
        fresh, _ = other.take(rows)
        self.assertEqual([r["text"] for r in fresh], ["one"])

    def test_the_cursor_advances_over_rows_the_keep_filter_rejected(self):
        """A filtered row is SEEN, just not returned -- otherwise the consumer
        re-scans it on every pass forever (the mirror's lane rule)."""
        rows = self._rows("alpha", "keep", "drop")
        fresh, confirm = self._cursor().take(rows, keep=lambda r: r["text"] == "keep")
        self.assertEqual([r["text"] for r in fresh], ["keep"])
        confirm()
        self.assertEqual(self._cursor().load(), {"alpha": 2})

    def test_per_seat_counts_so_one_seats_traffic_does_not_hide_anothers(self):
        first = self._rows("alpha", "a1")
        _fresh, confirm = self._cursor().take(first)
        confirm()
        fresh, _ = self._cursor().take(first + self._rows("beta", "b1"))
        self.assertEqual([r["text"] for r in fresh], ["b1"])


class TestReadCursor(unittest.TestCase):
    """The CLI read hands each row over ONCE (issue #33).

    The defect these pin: `comms read` called read_siblings/read_for directly
    and kept no cursor at all, so a second consecutive read for the same
    (runid, seat) replayed everything -- while two adapter READMEs promised
    "repeated reads never replay old rows". Every assertion below fails
    against that code.
    """

    def setUp(self):
        # Both knobs come from conftest.py's autouse isolation fixture: the
        # mailbox under COMMS_ROOT, the cursor under COMMS_STATE_DIR.
        self.tmp = os.environ["COMMS_ROOT"]
        self.state = os.environ["COMMS_STATE_DIR"]

    def _drain(self, runid, seat, **kw):
        """One complete read: take the fresh rows AND commit the cursor."""
        rows, advance = mb.read_delta(runid, seat, **kw)
        advance()
        return [r["text"] for r in rows]

    def test_second_read_returns_nothing_new(self):
        mb.post("test-rc1", "alpha", "finding", "first row")
        self.assertEqual(self._drain("test-rc1", "reader"), ["first row"])
        self.assertEqual(self._drain("test-rc1", "reader"), [])

    def test_only_rows_posted_after_the_cursor_come_back(self):
        mb.post("test-rc2", "alpha", "finding", "old")
        self._drain("test-rc2", "reader")
        mb.post("test-rc2", "alpha", "finding", "new")
        self.assertEqual(self._drain("test-rc2", "reader"), ["new"])

    def test_cursor_is_per_seat_not_per_run(self):
        # One seat draining the board must not consume another seat's mail.
        mb.post("test-rc3", "alpha", "finding", "shared")
        self.assertEqual(self._drain("test-rc3", "readerA"), ["shared"])
        self.assertEqual(self._drain("test-rc3", "readerB"), ["shared"])

    def test_rows_not_committed_are_delivered_again(self):
        # advance() is the caller's "I delivered these"; skipping it (a crash
        # mid-print) must re-deliver, never drop.
        mb.post("test-rc4", "alpha", "finding", "undelivered")
        rows, _advance = mb.read_delta("test-rc4", "reader")
        self.assertEqual([r["text"] for r in rows], ["undelivered"])
        self.assertEqual(self._drain("test-rc4", "reader"), ["undelivered"])

    def test_topic_read_does_not_consume_other_topics(self):
        # THE FILTER INTERACTION: a --topic read advances only ITS view's
        # cursor. If it advanced one shared cursor over every row it walked
        # past, "off-topic" would be marked delivered here and never appear in
        # any later read -- a row lost with no error anywhere.
        mb.post("test-rc5", "alpha", "finding", "on-topic", topic="proj")
        mb.post("test-rc5", "alpha", "finding", "off-topic", topic="other")
        self.assertEqual(self._drain("test-rc5", "reader", topic="proj"), ["on-topic"])
        self.assertEqual(self._drain("test-rc5", "reader", topic="proj"), [])
        self.assertEqual(
            sorted(self._drain("test-rc5", "reader")), ["off-topic", "on-topic"]
        )

    def test_each_topic_keeps_its_own_cursor(self):
        mb.post("test-rc6", "alpha", "finding", "a1", topic="a")
        mb.post("test-rc6", "alpha", "finding", "b1", topic="b")
        self.assertEqual(self._drain("test-rc6", "reader", topic="a"), ["a1"])
        self.assertEqual(self._drain("test-rc6", "reader", topic="b"), ["b1"])

    def test_subs_view_has_its_own_cursor(self):
        mb.subscribe("test-rc7", "reader", ["proj"])
        mb.post("test-rc7", "alpha", "finding", "mine", topic="proj")
        mb.post("test-rc7", "alpha", "finding", "theirs", topic="elsewhere")
        self.assertEqual(self._drain("test-rc7", "reader", subs=True), ["mine"])
        self.assertEqual(self._drain("test-rc7", "reader", subs=True), [])
        # The unfiltered view never saw either row, so it still gets both.
        self.assertEqual(
            sorted(self._drain("test-rc7", "reader")), ["mine", "theirs"]
        )

    def test_resubscribing_redelivers_the_new_slice(self):
        # A count cursor cannot tell "row 2 of alpha in the old slice" from
        # "row 2 in the new one", so the subs cursor is keyed on the
        # subscription set itself: widening it re-delivers rather than
        # silently skipping rows that predate the change.
        mb.subscribe("test-rc8", "reader", ["proj"])
        mb.post("test-rc8", "alpha", "finding", "in-proj", topic="proj")
        mb.post("test-rc8", "alpha", "finding", "in-ops", topic="ops")
        self.assertEqual(self._drain("test-rc8", "reader", subs=True), ["in-proj"])
        mb.subscribe("test-rc8", "reader", ["proj", "ops"])
        self.assertEqual(
            sorted(self._drain("test-rc8", "reader", subs=True)),
            ["in-ops", "in-proj"],
        )

    def test_cursor_state_lives_under_the_state_dir(self):
        mb.post("test-rc9", "alpha", "finding", "x")
        self._drain("test-rc9", "reader")
        found = []
        for dirpath, _dirnames, filenames in os.walk(self.state):
            found.extend(os.path.join(dirpath, f) for f in filenames)
        self.assertTrue(found, "read_delta wrote no cursor under COMMS_STATE_DIR")
        self.assertTrue(
            all(p.startswith(self.state) for p in found),
            "cursor written outside COMMS_STATE_DIR: %s" % found,
        )
        # And nothing landed in the mailbox root: cursors are machine-local
        # state, not mailbox content the remote sync would mirror.
        self.assertEqual(sorted(os.listdir(self.tmp)), ["comms-test-rc9"])

    def test_no_cursor_is_written_before_the_rows_are_taken(self):
        mb.post("test-rc10", "alpha", "finding", "x")
        mb.read_delta("test-rc10", "reader")
        self.assertEqual(os.listdir(self.state), [])


class TestReadCursorCLI(unittest.TestCase):
    """The two-read reproduction from issue #33, at the level it was reported:
    the CLI, one process per read, cursor surviving between them."""

    def setUp(self):
        self.env = dict(os.environ)  # already isolated by conftest.py
        self.script = os.path.join(_LIB, "swarm_mailbox.py")

    def _run(self, args):
        import subprocess

        return subprocess.run(
            [sys.executable, self.script] + args,
            capture_output=True, text=True, env=self.env,
        )

    def _texts(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(l)["text"] for l in result.stdout.splitlines() if l.strip()]

    def test_two_consecutive_reads_do_not_replay(self):
        self._run(["post", "test-cli1", "alpha", "finding", "row one"])
        self.assertEqual(self._texts(self._run(["read", "test-cli1", "rd"])), ["row one"])
        self.assertEqual(self._texts(self._run(["read", "test-cli1", "rd"])), [])

    def test_replay_flag_returns_everything_and_moves_nothing(self):
        self._run(["post", "test-cli2", "alpha", "finding", "row one"])
        self._run(["read", "test-cli2", "rd"])                      # cursor to end
        replayed = self._texts(self._run(["read", "test-cli2", "rd", "--replay"]))
        self.assertEqual(replayed, ["row one"])
        # --replay neither consumed the row nor rewound the cursor.
        self.assertEqual(self._texts(self._run(["read", "test-cli2", "rd"])), [])

    def test_replay_before_any_incremental_read_leaves_the_cursor_unset(self):
        self._run(["post", "test-cli3", "alpha", "finding", "row one"])
        self.assertEqual(
            self._texts(self._run(["read", "test-cli3", "rd", "--replay"])), ["row one"]
        )
        # An auditor's --replay must not eat a real reader's first delivery.
        self.assertEqual(
            self._texts(self._run(["read", "test-cli3", "rd"])), ["row one"]
        )

    def test_subs_and_topic_together_still_refused(self):
        r = self._run(["read", "test-cli4", "rd", "--subs", "--topic", "x"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("not both", r.stderr)


class TestDeliveryCursorCLI(unittest.TestCase):
    """`cursor take` / `cursor confirm` -- the same confirmed-delivery pair,
    split across two processes so a SHELL driver can hold it (issue #29).

    The thing that has to survive the split is the ORDER, and the property that
    makes the order safe: take writes nothing, so a driver whose delivery
    command failed simply never runs confirm. These pin that across process
    boundaries, where the closure cannot help and a receipt line has to.
    """

    def setUp(self):
        self.script = os.path.join(_LIB, "swarm_mailbox.py")
        self.path = os.path.join(os.environ["COMMS_STATE_DIR"], "cli-cursor.json")

    def _run(self, args, stdin=""):
        import subprocess

        return subprocess.run(
            [sys.executable, self.script] + args,
            input=stdin, capture_output=True, text=True, env=dict(os.environ),
        )

    def _rows(self, seat, *texts):
        return [{"seat": seat, "at": "2026-08-25T00:00:0%d" % i, "text": t}
                for i, t in enumerate(texts)]

    def _jsonl(self, rows):
        return "".join(json.dumps(r) + "\n" for r in rows)

    def _take(self, rows):
        r = self._run(["cursor", "take", self.path], self._jsonl(rows))
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        return lines[0], [json.loads(l)["text"] for l in lines[1:] if l.strip()]

    def test_take_prints_the_receipt_first_then_the_fresh_rows(self):
        receipt, texts = self._take(self._rows("alpha", "one", "two"))
        self.assertEqual(json.loads(receipt), {"alpha": 2})
        self.assertEqual(texts, ["one", "two"])

    def test_take_writes_nothing_at_all(self):
        self._take(self._rows("alpha", "one"))
        self.assertFalse(os.path.exists(self.path))

    def test_a_receipt_is_printed_even_when_nothing_is_fresh(self):
        """One shape for the caller's parse: line 1 is always the receipt."""
        receipt, texts = self._take([])
        self.assertEqual(json.loads(receipt), {})
        self.assertEqual(texts, [])

    def test_without_confirm_the_same_rows_come_back(self):
        rows = self._rows("alpha", "one")
        self._take(rows)          # a delivery that failed: no confirm follows
        _receipt, texts = self._take(rows)
        self.assertEqual(texts, ["one"])

    def test_confirm_commits_the_receipt_and_the_rows_stop_coming_back(self):
        rows = self._rows("alpha", "one")
        receipt, _ = self._take(rows)
        r = self._run(["cursor", "confirm", self.path, receipt])
        self.assertEqual(r.returncode, 0, r.stderr)
        _receipt, texts = self._take(rows)
        self.assertEqual(texts, [])

    def test_the_receipt_is_exactly_what_the_in_process_pair_would_write(self):
        """The two forms are one mechanism, not two: if this drifts, a bash
        driver and a python driver on one board disagree about what is
        delivered."""
        rows = self._rows("alpha", "one", "two")
        receipt, _ = self._take(rows)
        _fresh, confirm = mb.DeliveryCursor(self.path).take(rows)
        self.assertEqual(json.loads(receipt), confirm.cursor)

    def test_a_stale_receipt_cannot_rewind_the_cursor(self):
        self._run(["cursor", "confirm", self.path, '{"alpha": 3}'])
        self._run(["cursor", "confirm", self.path, '{"alpha": 1}'])
        self.assertEqual(mb.DeliveryCursor(self.path).load(), {"alpha": 3})

    def test_a_receipt_naming_another_seat_does_not_disturb_this_one(self):
        self._run(["cursor", "confirm", self.path, '{"alpha": 2}'])
        self._run(["cursor", "confirm", self.path, '{"beta": 1}'])
        self.assertEqual(mb.DeliveryCursor(self.path).load(), {"alpha": 2, "beta": 1})

    def test_malformed_input_fails_loudly_instead_of_skipping_a_row(self):
        """A silently skipped line is a silently dropped row -- the exact
        failure a delivery cursor exists to prevent."""
        r = self._run(["cursor", "take", self.path], "not json\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("JSONL", r.stderr)
        self.assertFalse(os.path.exists(self.path))

    def test_a_malformed_receipt_is_refused(self):
        r = self._run(["cursor", "confirm", self.path, "not json"])
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(self.path))

    def test_a_receipt_that_is_not_a_map_is_refused(self):
        r = self._run(["cursor", "confirm", self.path, "[1, 2]"])
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(self.path))

    def test_unknown_verb_and_missing_arguments_are_refused(self):
        self.assertEqual(self._run(["cursor"]).returncode, 1)
        self.assertEqual(self._run(["cursor", "sideways", self.path]).returncode, 1)
        self.assertEqual(self._run(["cursor", "take"]).returncode, 1)
        self.assertEqual(self._run(["cursor", "confirm", self.path]).returncode, 1)

    def test_a_receipt_is_not_parsed_for_flags_on_its_way_past(self):
        """Receipts are opaque caller text; flag extraction must not touch
        them, or a cursor path or receipt containing --topic would be eaten."""
        r = self._run(["cursor", "confirm", self.path, '{"--topic": 1}'])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(mb.DeliveryCursor(self.path).load(), {"--topic": 1})


class TestConvoKinds(unittest.TestCase):
    """CONVO_KINDS is the kind-half of the discord mirror's convo-lane
    predicate (S5): it must never name a kind VALID_KINDS does not allow,
    or the mirror would treat an impossible kind as conversational."""

    def test_convo_kinds_is_subset_of_valid_kinds(self):
        self.assertTrue(set(mb.CONVO_KINDS) <= set(mb.VALID_KINDS))

    def test_convo_kinds_contains_comment_and_reply(self):
        self.assertEqual(set(mb.CONVO_KINDS), {"comment", "reply"})


class TestRunIds(unittest.TestCase):
    """run_ids() is the discovery helper --follow-all needs to find every
    run under the mailbox root without a caller having to know the runids
    up front."""

    def setUp(self):
        # See TestMailbox.setUp: adopt the isolated root conftest.py already set.
        self.tmp = os.environ["COMMS_ROOT"]

    def test_empty_root_yields_empty_list(self):
        self.assertEqual(mb.run_ids(), [])

    def test_nonexistent_root_yields_empty_list_not_none(self):
        # os.listdir on a root that does not exist at all raises OSError --
        # the except branch must still return a (possibly empty) LIST, the
        # type every caller iterates over, never None.
        os.environ["COMMS_ROOT"] = os.path.join(self.tmp, "does-not-exist")
        result = mb.run_ids()
        self.assertEqual(result, [])
        self.assertIsInstance(result, list)

    def test_discovers_every_comms_dir_sorted(self):
        mb.init("zeta")
        mb.init("alpha")
        mb.init("mid")
        self.assertEqual(mb.run_ids(), ["alpha", "mid", "zeta"])

    def test_ignores_non_comms_entries(self):
        mb.init("run1")
        # A stray file and a differently-prefixed dir must not surface.
        with open(os.path.join(self.tmp, "not-a-run.txt"), "w") as fh:
            fh.write("x")
        os.makedirs(os.path.join(self.tmp, "other-thing"), exist_ok=True)
        self.assertEqual(mb.run_ids(), ["run1"])

    def test_reads_root_per_call_not_at_import(self):
        # Same footgun test_root() protects against: COMMS_ROOT set AFTER
        # module import must still be honored, because _root() reads env
        # every call rather than caching at import time.
        # Nested inside self.tmp (itself under conftest.py's isolated
        # tmp_path tree), not the ambient platform tempdir -- see
        # TestMailbox.setUp for why that matters on Linux.
        second_root = tempfile.mkdtemp(prefix="comms-test-root2-", dir=self.tmp)
        os.environ["COMMS_ROOT"] = second_root
        mb.init("only-in-second-root")
        self.assertEqual(mb.run_ids(), ["only-in-second-root"])
        os.environ["COMMS_ROOT"] = self.tmp  # restore for other tests


if __name__ == "__main__":
    unittest.main(verbosity=2)
