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
