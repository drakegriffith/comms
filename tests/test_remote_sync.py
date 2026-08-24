"""Tests for adapters/remote/sync.py and the two lib/swarm_mailbox.py
functions it needed (append_mirrored, fresh_rows_by_seat).

NO REAL ssh RUNS HERE. The `fake_ssh` fixture puts a script named `ssh`
earlier on PATH; it strips ssh's options and host, then executes the remote
command string against a SECOND comms root that stands in for the other
machine's disk. So the round trip is real -- the hub side is genuinely
`bin/comms post` and `bin/comms read` writing and reading real mailbox files
-- while the network is not. Shadowing PATH rather than adding a
`--ssh-command` parameter is deliberate: a seam may inject data, it must not
widen the production interface (a knob that redirects ssh is callable by every
future caller, forever).

Runids are spelled "test-*" so tests/conftest.py's leak sentinel recognizes
them without a registry edit.
"""

import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
sys.path.insert(0, os.path.join(REPO_ROOT, "adapters", "remote"))

import swarm_mailbox  # noqa: E402
import sync  # noqa: E402

COMMS_BIN = os.path.join(REPO_ROOT, "bin", "comms")

FAKE_SSH = r"""#!/bin/bash
# Stand-in for ssh: log the call, optionally fail, else run the remote command
# string against the simulated remote machine's comms root.
set -u
if [ -n "${FAKE_SSH_LOG:-}" ]; then printf '%s\n' "$*" >> "$FAKE_SSH_LOG"; fi
if [ -n "${FAKE_SSH_DOWN:-}" ] && [ -e "${FAKE_SSH_DOWN}" ]; then
  echo "ssh: connect to host: Operation timed out" >&2
  exit 255
fi
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift 2 ;;
    -*) shift ;;
    *) shift; break ;;
  esac
done
cmd="$*"
if [ -n "${FAKE_SSH_REJECT:-}" ] && [[ "$cmd" == *"$FAKE_SSH_REJECT"* ]]; then
  echo "remote: refused that row" >&2
  exit 1
fi
export COMMS_ROOT="$FAKE_REMOTE_ROOT"
export COMMS_STATE_DIR="$FAKE_REMOTE_STATE"
eval "$cmd"
rc=$?
# AMBIGUOUS TRANSPORT FAILURE: the remote command RAN and committed its write,
# and only then did the connection die. The client sees ssh's 255 and cannot
# distinguish this from "the row never arrived" -- which is the entire reason
# delivery is at-least-once instead of exactly-once.
if [ -n "${FAKE_SSH_AMBIGUOUS:-}" ] && [[ "$cmd" == *"$FAKE_SSH_AMBIGUOUS"* ]]; then
  echo "ssh: connection closed by remote host" >&2
  exit 255
fi
exit $rc
"""


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    """Shadow `ssh` on PATH and give the fake a remote root of its own.

    Returns a small handle: .remote_root (the other machine's mailbox),
    .log (every ssh invocation, one line each), .go_down()/.come_up(), and
    .reject(substring) to make the remote CLI refuse a matching row.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    script = bindir / "ssh"
    script.write_text(FAKE_SSH)
    script.chmod(0o755)

    remote_root = tmp_path / "remote-root"
    remote_state = tmp_path / "remote-state"
    remote_root.mkdir()
    remote_state.mkdir()
    log = tmp_path / "ssh.log"
    down_flag = tmp_path / "ssh-down"

    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_REMOTE_ROOT", str(remote_root))
    monkeypatch.setenv("FAKE_REMOTE_STATE", str(remote_state))
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    monkeypatch.setenv("FAKE_SSH_DOWN", "")
    monkeypatch.setenv("FAKE_SSH_REJECT", "")
    monkeypatch.setenv("FAKE_SSH_AMBIGUOUS", "")

    # Adapter config: point at the repo's own CLI as the "remote" comms.
    monkeypatch.setenv("COMMS_REMOTE_HOST", "studio")
    monkeypatch.setenv("COMMS_REMOTE_BIN", COMMS_BIN)
    monkeypatch.setenv("COMMS_REMOTE_LABEL", "studio")
    monkeypatch.setenv("COMMS_MACHINE_LABEL", "laptop")

    class Handle(object):
        remote_root = None

        def go_down(self):
            down_flag.write_text("x")
            monkeypatch.setenv("FAKE_SSH_DOWN", str(down_flag))

        def come_up(self):
            monkeypatch.setenv("FAKE_SSH_DOWN", "")

        def reject(self, substring):
            monkeypatch.setenv("FAKE_SSH_REJECT", substring)

        def ambiguous(self, substring):
            """Rows whose command matches COMMIT on the hub and THEN report a
            transport failure -- the case that makes exactly-once impossible."""
            monkeypatch.setenv("FAKE_SSH_AMBIGUOUS", substring)

        def clear_ambiguous(self):
            monkeypatch.setenv("FAKE_SSH_AMBIGUOUS", "")

        def calls(self):
            if not log.exists():
                return []
            return [ln for ln in log.read_text().splitlines() if ln.strip()]

    handle = Handle()
    handle.remote_root = remote_root
    return handle


def remote_rows(handle, runid, seat="observer"):
    """Read the simulated remote mailbox directly, without going through the
    adapter -- an independent oracle, not the code under test."""
    env = dict(os.environ)
    env["COMMS_ROOT"] = str(handle.remote_root)
    out = subprocess.run(
        [COMMS_BIN, "read", runid, seat],
        stdout=subprocess.PIPE, env=env,
    ).stdout.decode()
    return [json.loads(ln) for ln in out.splitlines() if ln.strip()]


def hub_texts(handle, runid):
    """Hub row texts with the delivery-id marker stripped -- what a human
    wrote, not how it was delivered. Every row this adapter SENDS carries an
    id (see sync.MSGID_RE); rows posted natively on the hub do not."""
    return [sync.strip_msgid(r["text"]) for r in remote_rows(handle, runid)]


def remote_post(handle, runid, seat, kind, text, topic=None, to=None):
    """Post a row on the simulated remote machine, as a native hub seat would
    (no adapter involved)."""
    env = dict(os.environ)
    env["COMMS_ROOT"] = str(handle.remote_root)
    argv = [COMMS_BIN, "post", runid, seat, kind, text]
    if to:
        argv += ["--to", to]
    elif topic:
        argv += ["--topic", topic]
    cp = subprocess.run(argv, stdout=subprocess.PIPE, env=env)
    assert cp.returncode == 0
    return json.loads(cp.stdout.decode())


# ---- lib: fresh_rows_by_seat ---------------------------------------------


def _row(seat, at, topic="default", kind="finding", text="t"):
    return {"seat": seat, "at": at, "topic": topic, "kind": kind, "text": text}


def test_fresh_rows_by_seat_first_pass_returns_everything():
    rows = [_row("a", "1"), _row("b", "2"), _row("a", "3")]
    fresh, cursor = swarm_mailbox.fresh_rows_by_seat(rows, {})
    assert len(fresh) == 3
    assert cursor == {"a": 2, "b": 1}


def test_fresh_rows_by_seat_second_pass_returns_nothing_new():
    rows = [_row("a", "1"), _row("b", "2")]
    _, cursor = swarm_mailbox.fresh_rows_by_seat(rows, {})
    fresh, cursor2 = swarm_mailbox.fresh_rows_by_seat(rows, cursor)
    assert fresh == []
    assert cursor2 == cursor


def test_fresh_rows_by_seat_sees_only_the_appended_row():
    rows = [_row("a", "1")]
    _, cursor = swarm_mailbox.fresh_rows_by_seat(rows, {})
    rows.append(_row("a", "2", text="new"))
    fresh, _ = swarm_mailbox.fresh_rows_by_seat(rows, cursor)
    assert [r["text"] for r in fresh] == ["new"]


def test_fresh_rows_by_seat_cursor_advances_over_filtered_rows():
    """A row `keep` rejects is still SEEN. Otherwise every pass re-scans it."""
    rows = [_row("a", "1", text="drop"), _row("a", "2", text="keep")]
    fresh, cursor = swarm_mailbox.fresh_rows_by_seat(
        rows, {}, keep=lambda r: r["text"] == "keep"
    )
    assert [r["text"] for r in fresh] == ["keep"]
    assert cursor == {"a": 2}


def test_fresh_rows_by_seat_never_mutates_the_caller_cursor():
    cursor = {"a": 1}
    swarm_mailbox.fresh_rows_by_seat([_row("a", "1"), _row("a", "2")], cursor)
    assert cursor == {"a": 1}


def test_fresh_rows_by_seat_never_moves_a_count_backwards():
    fresh, cursor = swarm_mailbox.fresh_rows_by_seat([_row("a", "1")], {"a": 5})
    assert fresh == []
    assert cursor == {"a": 5}


# ---- lib: append_mirrored ------------------------------------------------


def test_append_mirrored_preserves_at_and_seat_verbatim():
    row = _row("bravo~studio", "2026-08-24T00:00:00+00:00", text="hello")
    assert swarm_mailbox.append_mirrored("test-mir1", "remote~studio", [row]) == 1
    got = swarm_mailbox.read_siblings("test-mir1", "alpha")
    assert len(got) == 1
    assert got[0]["seat"] == "bravo~studio"
    assert got[0]["at"] == "2026-08-24T00:00:00+00:00"
    assert got[0]["text"] == "hello"


def test_append_mirrored_creates_the_run_directory():
    swarm_mailbox.append_mirrored("test-mir2", "remote~studio", [_row("x~studio", "1")])
    assert os.path.isdir(swarm_mailbox._dir("test-mir2"))


def test_append_mirrored_empty_batch_is_zero_and_writes_nothing():
    assert swarm_mailbox.append_mirrored("test-mir3", "remote~studio", []) == 0
    assert swarm_mailbox.read_siblings("test-mir3", "alpha") == []


def test_append_mirrored_refuses_a_row_in_the_mirrors_own_name():
    with pytest.raises(ValueError):
        swarm_mailbox.append_mirrored(
            "test-mir4", "remote~studio", [_row("remote~studio", "1")]
        )


def test_append_mirrored_refuses_a_row_with_no_seat():
    with pytest.raises(ValueError):
        swarm_mailbox.append_mirrored("test-mir5", "remote~studio", [{"at": "1"}])


def test_append_mirrored_validates_every_row_before_writing_any():
    """A bad row late in a batch must leave the file untouched, not half
    appended -- otherwise a retry duplicates the good prefix."""
    good = _row("a~studio", "1")
    with pytest.raises(ValueError):
        swarm_mailbox.append_mirrored("test-mir6", "remote~studio", [good, {"at": "2"}])
    assert swarm_mailbox.read_siblings("test-mir6", "alpha") == []


def test_append_mirrored_does_not_validate_kind():
    """The peer machine's vocabulary may be newer than ours; a mirror that
    enforced the local VALID_KINDS would silently drop legitimate rows."""
    row = _row("a~studio", "1", kind="kind-from-the-future")
    assert swarm_mailbox.append_mirrored("test-mir7", "remote~studio", [row]) == 1


# ---- seat naming ---------------------------------------------------------


def test_qualify_tags_an_unqualified_seat():
    assert sync.qualify("alpha", "laptop") == "alpha~laptop"


def test_qualify_is_idempotent():
    assert sync.qualify("alpha~laptop", "studio") == "alpha~laptop"


def test_is_echo_matches_only_our_own_suffix():
    assert sync.is_echo("alpha~laptop", "laptop")
    assert not sync.is_echo("alpha~studio", "laptop")
    assert not sync.is_echo("alpha", "laptop")


def test_is_mirror_seat():
    assert sync.is_mirror_seat("remote~studio")
    assert sync.is_mirror_seat("remote")
    assert not sync.is_mirror_seat("remotely~studio")


# ---- post / outbox / flush ----------------------------------------------


def test_post_delivers_to_the_remote_mailbox(fake_ssh):
    report = sync.post("test-x1", "alpha", "comment", "hello hub")
    assert (report["delivered"], report["remaining"]) == (1, 0)
    rows = remote_rows(fake_ssh, "test-x1")
    assert len(rows) == 1
    assert sync.strip_msgid(rows[0]["text"]) == "hello hub"


def test_post_qualifies_the_seat_with_this_machine(fake_ssh):
    sync.post("test-x2", "alpha", "comment", "hi")
    assert remote_rows(fake_ssh, "test-x2")[0]["seat"] == "alpha~laptop"


def test_post_empties_the_outbox_on_success(fake_ssh):
    sync.post("test-x3", "alpha", "comment", "hi")
    assert sync.load_outbox() == []


def test_post_carries_the_topic(fake_ssh):
    sync.post("test-x4", "alpha", "status", "hi", topic="ops")
    assert remote_rows(fake_ssh, "test-x4")[0]["topic"] == "ops"


def test_post_carries_unicast_addressing(fake_ssh):
    sync.post("test-x5", "alpha", "comment", "psst", to="bravo")
    row = remote_rows(fake_ssh, "test-x5")[0]
    assert row["topic"] == "@bravo"
    assert row["to"] == "bravo"


def test_post_text_with_shell_metacharacters_survives_the_wire(fake_ssh):
    """The remote shell re-parses whatever ssh hands it, so quoting is the
    only thing between a row's text and command substitution on the hub."""
    nasty = "it's $(echo pwned) `whoami` && rm -rf; \"quoted\""
    sync.post("test-x6", "alpha", "finding", nasty)
    assert hub_texts(fake_ssh, "test-x6") == [nasty]


def test_post_rejects_an_invalid_kind_before_queueing(fake_ssh):
    with pytest.raises(ValueError):
        sync.post("test-x7", "alpha", "gossip", "hi")
    assert sync.load_outbox() == []


def test_post_rejects_topic_and_to_together(fake_ssh):
    with pytest.raises(ValueError):
        sync.post("test-x8", "alpha", "comment", "hi", topic="ops", to="bravo")


def test_post_queues_when_the_hub_is_offline(fake_ssh):
    fake_ssh.go_down()
    report = sync.post("test-x9", "alpha", "comment", "later")
    assert (report["delivered"], report["remaining"]) == (0, 1)
    queued = sync.load_outbox()
    assert len(queued) == 1
    assert queued[0]["text"] == "later"
    assert queued[0]["seat"] == "alpha~laptop"


def test_queued_rows_flush_when_the_hub_comes_back(fake_ssh):
    fake_ssh.go_down()
    sync.post("test-xa", "alpha", "comment", "one")
    sync.post("test-xa", "alpha", "comment", "two")
    assert len(sync.load_outbox()) == 2
    fake_ssh.come_up()
    report = sync.flush()
    assert (report["delivered"], report["remaining"]) == (2, 0)
    assert hub_texts(fake_ssh, "test-xa") == ["one", "two"]
    assert sync.load_outbox() == []


def test_a_fresh_post_never_overtakes_a_queued_one(fake_ssh):
    """post() enqueues BEFORE it flushes, which is what makes ordering free."""
    fake_ssh.go_down()
    sync.post("test-xb", "alpha", "comment", "first")
    fake_ssh.come_up()
    sync.post("test-xb", "alpha", "comment", "second")
    assert hub_texts(fake_ssh, "test-xb") == ["first", "second"]


def test_flush_stops_at_the_first_failure_and_keeps_the_remainder(fake_ssh):
    fake_ssh.go_down()
    sync.post("test-xc", "alpha", "comment", "aaa")
    sync.post("test-xc", "alpha", "comment", "bbb")
    sync.post("test-xc", "alpha", "comment", "ccc")
    fake_ssh.come_up()
    fake_ssh.reject("bbb")
    report = sync.flush()
    assert (report["delivered"], report["remaining"]) == (1, 2)
    assert hub_texts(fake_ssh, "test-xc") == ["aaa"]
    assert [r["text"] for r in sync.load_outbox()] == ["bbb", "ccc"]


def test_a_rejected_row_is_never_dropped(fake_ssh):
    """A row the hub refuses is a bug to be seen, not a message this adapter
    may discard on the author's behalf."""
    fake_ssh.reject("bad")
    sync.post("test-xd", "alpha", "comment", "bad row")
    assert [r["text"] for r in sync.load_outbox()] == ["bad row"]


def test_flush_on_an_empty_outbox_makes_no_ssh_call(fake_ssh):
    assert sync.flush() == {"delivered": 0, "remaining": 0, "malformed": 0}
    assert fake_ssh.calls() == []


def test_outbox_skips_a_corrupt_line_instead_of_wedging(fake_ssh):
    fake_ssh.go_down()
    sync.post("test-xe", "alpha", "comment", "good")
    path = sync._outbox_path("studio")
    with open(path, "a") as fh:
        fh.write("{not json\n")
    assert [r["text"] for r in sync.load_outbox()] == ["good"]


# ---- at-least-once delivery: duplicates are visible, loss is not ---------
#
# Verifier finding 1 (MAJOR). There is no transaction spanning "the hub
# appended the row" and "we wrote that down", so a duplicate is possible and
# the design's answer is to make it CHEAP AND VISIBLE, never to pretend it
# away. These tests pin both halves: the crash window is one row, and the
# surviving duplicate is detectable at read time.


def test_stamp_msgid_is_idempotent_for_the_same_id():
    once = sync.stamp_msgid("hello", "a1b2c3d4")
    assert sync.stamp_msgid(once, "a1b2c3d4") == once


def test_read_and_strip_msgid_round_trip():
    stamped = sync.stamp_msgid("hello there", "0011aabb")
    assert sync.read_msgid(stamped) == "0011aabb"
    assert sync.strip_msgid(stamped) == "hello there"


def test_read_msgid_is_none_for_an_unstamped_text():
    assert sync.read_msgid("just a message") is None


def test_duplicate_msgids_ignores_unstamped_rows():
    """Rows posted natively on the hub carry no id; lumping them together
    would report one enormous phantom duplicate group."""
    rows = [_row("a", "1", text="no id"), _row("b", "2", text="also none")]
    assert sync.duplicate_msgids(rows) == {}


def test_duplicate_msgids_counts_only_repeats():
    rows = [
        _row("a", "1", text=sync.stamp_msgid("x", "aaaaaaaa")),
        _row("a", "2", text=sync.stamp_msgid("x", "aaaaaaaa")),
        _row("a", "3", text=sync.stamp_msgid("y", "bbbbbbbb")),
    ]
    assert sync.duplicate_msgids(rows) == {"aaaaaaaa": 2}
    assert sync.redundant_row_count(rows) == 1


def test_every_posted_row_carries_a_delivery_id(fake_ssh):
    sync.post("test-d1", "alpha", "comment", "tagged")
    assert sync.read_msgid(remote_rows(fake_ssh, "test-d1")[0]["text"]) is not None


def test_ambiguous_failure_leaves_the_row_queued_though_the_hub_has_it(fake_ssh):
    """The verifier's case: the hub committed, the transport died, and the
    client cannot tell. Keeping the row queued is the deliberate choice --
    dropping it here is exactly the silent loss this module refuses."""
    fake_ssh.ambiguous("ambiguous row")
    report = sync.post("test-d2", "alpha", "comment", "ambiguous row")
    assert (report["delivered"], report["remaining"]) == (0, 1)
    assert len(remote_rows(fake_ssh, "test-d2")) == 1  # it DID land
    assert len(sync.load_outbox()) == 1  # and we correctly do not know that


def test_retry_after_an_ambiguous_failure_duplicates_but_it_is_detectable(fake_ssh):
    fake_ssh.ambiguous("ambiguous row")
    sync.post("test-d3", "alpha", "comment", "ambiguous row")
    fake_ssh.clear_ambiguous()
    report = sync.flush()
    assert (report["delivered"], report["remaining"]) == (1, 0)

    rows = remote_rows(fake_ssh, "test-d3")
    assert len(rows) == 2, "at-least-once: the retry produced a second copy"
    ids = {sync.read_msgid(r["text"]) for r in rows}
    assert len(ids) == 1, "both copies must carry the SAME id, or they are not matchable"
    assert sync.redundant_row_count(rows) == 1
    assert hub_texts(fake_ssh, "test-d3") == ["ambiguous row", "ambiguous row"]


def test_pull_reports_the_duplicate_it_can_see(fake_ssh):
    """The duplicate is OUR row, so the echo filter drops it before the fresh
    slice -- which is why dupes is counted over every inspected row."""
    fake_ssh.ambiguous("ambiguous row")
    sync.post("test-d4", "alpha", "comment", "ambiguous row")
    fake_ssh.clear_ambiguous()
    sync.flush()
    counts = sync.pull("test-d4")
    assert counts["inspected"] == 2
    assert counts["echo"] == 2
    assert counts["mirrored"] == 0
    assert counts["dupes"] == 1


def test_the_delivery_id_is_stable_across_resends(fake_ssh):
    fake_ssh.go_down()
    sync.post("test-d5", "alpha", "comment", "queued")
    first = sync.load_outbox()[0]["msgid"]
    sync.flush()  # still down, still queued
    assert sync.load_outbox()[0]["msgid"] == first
    fake_ssh.come_up()
    sync.flush()
    assert sync.read_msgid(remote_rows(fake_ssh, "test-d5")[0]["text"]) == first


def test_outbox_is_rewritten_after_every_successful_send(fake_ssh, monkeypatch):
    """The fix for the crash window. Observed DURING the loop, because the
    end-of-loop rewrite the old code did is indistinguishable from this one
    once flush has returned."""
    fake_ssh.go_down()
    for text in ("aaa", "bbb", "ccc"):
        sync.post("test-d6", "alpha", "comment", text)
    fake_ssh.come_up()

    depths = []
    real_ssh = sync._ssh

    def watching_ssh(argv, host=None):
        depths.append(len(sync.load_outbox(host)))
        return real_ssh(argv, host=host)

    monkeypatch.setattr(sync, "_ssh", watching_ssh)
    sync.flush()
    # Before send 1 the queue is 3; before send 2 it must ALREADY be 2, which
    # only holds if the outbox was persisted between the two.
    assert depths == [3, 2, 1]


def test_a_crash_between_delivery_and_rewrite_costs_at_most_one_duplicate(
    fake_ssh, monkeypatch
):
    """The verifier's crash case. With an end-of-loop rewrite this resent the
    WHOLE delivered prefix; per-row persistence bounds it to the single row
    whose acknowledgement was lost."""
    fake_ssh.go_down()
    for text in ("aaa", "bbb", "ccc"):
        sync.post("test-d7", "alpha", "comment", text)
    fake_ssh.come_up()

    real_save = sync._save_outbox
    calls = {"n": 0}

    def crashing_save(host, records):
        calls["n"] += 1
        if calls["n"] == 2:  # the rewrite acknowledging "bbb" never lands
            raise OSError("simulated crash: disk gone")
        return real_save(host, records)

    monkeypatch.setattr(sync, "_save_outbox", crashing_save)
    with pytest.raises(OSError):
        sync.flush()

    monkeypatch.setattr(sync, "_save_outbox", real_save)
    sync.flush()  # restart

    texts = hub_texts(fake_ssh, "test-d7")
    assert texts == ["aaa", "bbb", "bbb", "ccc"]
    rows = remote_rows(fake_ssh, "test-d7")
    assert sync.redundant_row_count(rows) == 1, "exactly one row was resent"


def test_cli_dupes_reports_the_duplicate(fake_ssh):
    fake_ssh.ambiguous("ambiguous row")
    sync.post("test-d8", "alpha", "comment", "ambiguous row")
    fake_ssh.clear_ambiguous()
    sync.flush()
    cp = run_cli(["dupes", "test-d8"])
    assert cp.returncode == 0
    assert b"redundant_rows=1" in cp.stdout
    assert b"ambiguous row" in cp.stdout


def test_cli_dupes_on_a_clean_board_reports_zero(fake_ssh):
    sync.post("test-d9", "alpha", "comment", "only once")
    cp = run_cli(["dupes", "test-d9"])
    assert cp.returncode == 0
    assert b"redundant_rows=0" in cp.stdout


def test_cli_dupes_exits_2_when_the_hub_is_unreachable(fake_ssh):
    fake_ssh.go_down()
    assert run_cli(["dupes", "test-da"]).returncode == 2


# ---- the outbox quarantines what it cannot parse -------------------------
#
# Verifier finding 2 (LOW). A line that will not parse is a QUEUED ROW THAT
# WILL NEVER BE DELIVERED. Skipping it silently loses a message with no trace.


def _corrupt_outbox(host="studio", line="{not json"):
    with open(sync._outbox_path(host), "a") as fh:
        fh.write(line + "\n")


def test_read_outbox_separates_records_from_malformed_lines(fake_ssh):
    fake_ssh.go_down()
    sync.post("test-q1", "alpha", "comment", "good")
    _corrupt_outbox()
    records, malformed = sync.read_outbox()
    assert [r["text"] for r in records] == ["good"]
    assert malformed == ["{not json"]


def test_flush_quarantines_a_malformed_line_verbatim(fake_ssh):
    fake_ssh.go_down()
    sync.post("test-q2", "alpha", "comment", "good")
    _corrupt_outbox(line='{"half": "written"')
    report = sync.flush()
    assert report["malformed"] == 1
    bad = open(sync._quarantine_path("studio")).read()
    assert '{"half": "written"' in bad, "the row must be recoverable by a human"


def test_quarantine_removes_the_bad_line_from_the_queue(fake_ssh):
    fake_ssh.go_down()
    sync.post("test-q3", "alpha", "comment", "good")
    _corrupt_outbox()
    sync.flush()
    _records, malformed = sync.read_outbox()
    assert malformed == [], "already quarantined, must not be re-reported forever"


def test_a_malformed_line_does_not_block_the_good_rows(fake_ssh):
    fake_ssh.go_down()
    sync.post("test-q4", "alpha", "comment", "good")
    _corrupt_outbox()
    fake_ssh.come_up()
    report = sync.flush()
    assert (report["delivered"], report["malformed"]) == (1, 1)
    assert hub_texts(fake_ssh, "test-q4") == ["good"]


def test_clean_flush_reports_zero_malformed_and_writes_no_bad_file(fake_ssh):
    sync.post("test-q5", "alpha", "comment", "fine")
    assert sync.flush()["malformed"] == 0
    assert not os.path.exists(sync._quarantine_path("studio"))


def test_cli_flush_surfaces_the_malformed_count(fake_ssh):
    fake_ssh.go_down()
    sync.post("test-q6", "alpha", "comment", "good")
    _corrupt_outbox()
    fake_ssh.come_up()
    cp = run_cli(["flush"])
    assert b"malformed=1" in cp.stdout


# ---- echo and skipped are different facts --------------------------------
#
# Verifier finding 3 (LOW): both were reported under one counter named "echo",
# so a third machine's mirror rows read as this machine's own traffic.


def test_classify_names_the_three_reasons():
    assert sync.classify(_row("alpha~laptop", "1"), "laptop") == "echo"
    assert sync.classify(_row("remote~pi", "1"), "laptop") == "skipped"
    assert sync.classify(_row("bravo~studio", "1"), "laptop") == "mirror"


def test_pull_counts_echo_and_skipped_separately(fake_ssh):
    """The verifier's exact fixture: a real peer seat plus a third machine's
    mirror file. Only one of the two dropped rows is an echo -- and neither
    of them is, here, which is the point: the old counter said echo=1."""
    remote_post(fake_ssh, "test-e1", "bravo", "comment", "real peer")
    remote_post(fake_ssh, "test-e1", "remote~pi", "comment", "third machine")
    counts = sync.pull("test-e1")
    assert counts["inspected"] == 2
    assert counts["mirrored"] == 1
    assert counts["echo"] == 0, "a third machine's mirror row is NOT our echo"
    assert counts["skipped"] == 1


def test_pull_counts_a_real_echo_as_echo(fake_ssh):
    sync.post("test-e2", "alpha", "comment", "ours")
    remote_post(fake_ssh, "test-e2", "remote~pi", "comment", "third machine")
    counts = sync.pull("test-e2")
    assert counts["echo"] == 1
    assert counts["skipped"] == 1
    assert counts["mirrored"] == 0


def test_cli_pull_prints_both_counters(fake_ssh):
    remote_post(fake_ssh, "test-e3", "remote~pi", "comment", "third machine")
    cp = run_cli(["pull", "test-e3"])
    assert b"echo=0 skipped=1" in cp.stdout


# ---- identity comes from a shared module, not the Discord adapter --------
#
# Verifier finding 4 (LOW): cross-machine correctness must not depend on a
# display adapter for a chat service.


def test_machine_label_is_the_shared_implementation():
    """Both consumers must be the SAME function object. Two machines' worth of
    provenance tagging and the echo filter all depend on one answer; a second
    implementation would be free to drift into a second answer.

    The test adds adapters/discord to sys.path itself -- sync.py no longer
    does, which is the whole point of the fix."""
    import comms_machine

    sys.path.insert(0, os.path.join(REPO_ROOT, "adapters", "discord"))
    import mirror

    assert sync.machine_label is comms_machine.machine_label
    assert mirror.machine_label is comms_machine.machine_label


def test_sync_does_not_import_the_discord_adapter():
    source = open(os.path.join(REPO_ROOT, "adapters", "remote", "sync.py")).read()
    assert "adapters\", \"discord\"" not in source
    assert "import mirror" not in source


def test_machine_label_still_honors_the_env_override(monkeypatch):
    monkeypatch.setenv("COMMS_MACHINE_LABEL", "somewhere")
    assert sync.machine_label() == "somewhere"


# ---- the wire: what actually reaches the remote shell --------------------


def capture_ssh_command(monkeypatch, remote_argv):
    """Return the single command string sync._ssh hands to the remote shell."""
    seen = {}

    class FakeCP(object):
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return FakeCP()

    monkeypatch.setattr(sync.subprocess, "run", fake_run)
    sync._ssh(remote_argv, host="studio")
    return seen["argv"][-1]


def test_the_default_remote_bin_is_a_tilde_path(monkeypatch):
    """Guards the reason argv[0] must not be quoted. The hub's checkout is
    under a different username, so the default names it by tilde."""
    monkeypatch.delenv("COMMS_REMOTE_BIN", raising=False)
    assert sync.remote_bin().startswith("~")


def test_argv0_reaches_the_remote_shell_unquoted(monkeypatch):
    """REGRESSION: quoting argv[0] turns "~/code/comms/bin/comms" into a
    literal directory named "~" over there -- measured rc=127 against the real
    hub. Every test that sets COMMS_REMOTE_BIN to an absolute path mocks this
    failure away, which is exactly how it shipped unnoticed once."""
    cmd = capture_ssh_command(monkeypatch, ["~/code/comms/bin/comms", "status"])
    assert cmd.startswith("~/code/comms/bin/comms ")
    assert "'~/code" not in cmd


def test_every_argument_after_argv0_is_quoted(monkeypatch):
    cmd = capture_ssh_command(
        monkeypatch, ["/bin/comms", "post", "r", "s", "finding", "a b; rm -rf /"]
    )
    assert "'a b; rm -rf /'" in cmd


def test_remote_bin_is_shell_expanded_end_to_end(fake_ssh, monkeypatch):
    """Same claim as above, through the fake shell rather than by inspection:
    a remote bin naming a variable must resolve on the far side."""
    monkeypatch.setenv("COMMS_REPO_FOR_TEST", REPO_ROOT)
    monkeypatch.setenv("COMMS_REMOTE_BIN", "$COMMS_REPO_FOR_TEST/bin/comms")
    report = sync.post("test-w1", "alpha", "comment", "expanded")
    assert (report["delivered"], report["remaining"]) == (1, 0)
    assert hub_texts(fake_ssh, "test-w1") == ["expanded"]


# ---- pull ----------------------------------------------------------------


def test_pull_mirrors_remote_rows_into_the_local_mailbox(fake_ssh):
    remote_post(fake_ssh, "test-y1", "bravo", "comment", "from the studio")
    counts = sync.pull("test-y1")
    assert counts == {
        "inspected": 1, "mirrored": 1, "echo": 0, "skipped": 0, "dupes": 0,
    }
    local = swarm_mailbox.read_siblings("test-y1", "alpha")
    assert [r["text"] for r in local] == ["from the studio"]


def test_pull_qualifies_the_remote_seat(fake_ssh):
    remote_post(fake_ssh, "test-y2", "bravo", "comment", "hi")
    sync.pull("test-y2")
    assert swarm_mailbox.read_siblings("test-y2", "alpha")[0]["seat"] == "bravo~studio"


def test_pull_preserves_the_remote_timestamp(fake_ssh):
    posted = remote_post(fake_ssh, "test-y3", "bravo", "comment", "hi")
    sync.pull("test-y3")
    assert swarm_mailbox.read_siblings("test-y3", "alpha")[0]["at"] == posted["at"]


def test_pull_is_idempotent(fake_ssh):
    remote_post(fake_ssh, "test-y4", "bravo", "comment", "once")
    assert sync.pull("test-y4")["mirrored"] == 1
    assert sync.pull("test-y4")["mirrored"] == 0
    assert len(swarm_mailbox.read_siblings("test-y4", "alpha")) == 1


def test_pull_picks_up_only_rows_added_since_the_last_pass(fake_ssh):
    remote_post(fake_ssh, "test-y5", "bravo", "comment", "one")
    sync.pull("test-y5")
    remote_post(fake_ssh, "test-y5", "bravo", "comment", "two")
    counts = sync.pull("test-y5")
    assert counts["mirrored"] == 1
    assert [r["text"] for r in swarm_mailbox.read_siblings("test-y5", "alpha")] == [
        "one", "two",
    ]


def test_pull_drops_our_own_rows_coming_back(fake_ssh):
    """The echo case: we pushed it, so we must not mirror it home."""
    sync.post("test-y6", "alpha", "comment", "ours")
    remote_post(fake_ssh, "test-y6", "bravo", "comment", "theirs")
    counts = sync.pull("test-y6")
    assert counts["inspected"] == 2
    assert counts["mirrored"] == 1
    assert counts["echo"] == 1
    assert [r["text"] for r in swarm_mailbox.read_siblings("test-y6", "alpha")] == [
        "theirs",
    ]


def test_pull_advances_the_cursor_over_dropped_echoes(fake_ssh):
    """A dropped row still counts, or every pass re-scans it forever."""
    sync.post("test-y7", "alpha", "comment", "ours")
    sync.pull("test-y7")
    assert sync.pull("test-y7") == {
        "inspected": 1, "mirrored": 0, "echo": 0, "skipped": 0, "dupes": 0,
    }


def test_pull_skips_another_machines_mirror_file(fake_ssh):
    """Otherwise a third machine's rows get re-exported on every hop."""
    remote_post(fake_ssh, "test-y8", "remote~pi", "comment", "third machine")
    remote_post(fake_ssh, "test-y8", "bravo", "comment", "real seat")
    counts = sync.pull("test-y8")
    assert counts["inspected"] == 2
    assert counts["mirrored"] == 1
    assert [r["text"] for r in swarm_mailbox.read_siblings("test-y8", "alpha")] == [
        "real seat",
    ]


def test_pull_on_an_empty_remote_run_reports_zero_inspected(fake_ssh):
    assert sync.pull("test-y9") == {
        "inspected": 0, "mirrored": 0, "echo": 0, "skipped": 0, "dupes": 0,
    }


def test_pull_raises_when_the_hub_is_unreachable(fake_ssh):
    """Could-not-look must not be reported as nothing-there."""
    fake_ssh.go_down()
    with pytest.raises(sync.RemoteUnreachable):
        sync.pull("test-ya")


def test_pull_writes_no_cursor_when_it_could_not_reach_the_hub(fake_ssh):
    fake_ssh.go_down()
    with pytest.raises(sync.RemoteUnreachable):
        sync.pull("test-yb")
    assert not os.path.exists(sync._cursor_path("studio", "test-yb"))


def test_pulled_unicast_reaches_the_addressed_local_seat(fake_ssh):
    """A hub seat's `--to alpha` must land in laptop seat alpha's slice, which
    is the whole point: cross-machine agent-to-agent addressing."""
    swarm_mailbox.subscribe("test-yc", "alpha", ["ops"])
    remote_post(fake_ssh, "test-yc", "bravo", "comment", "psst alpha", to="alpha")
    remote_post(fake_ssh, "test-yc", "bravo", "comment", "unrelated", topic="other")
    sync.pull("test-yc")
    got = swarm_mailbox.read_for("test-yc", "alpha")
    assert [r["text"] for r in got] == ["psst alpha"]


def test_pull_tolerates_a_malformed_remote_line(fake_ssh):
    remote_post(fake_ssh, "test-yd", "bravo", "comment", "good")
    bad = fake_ssh.remote_root / "comms-test-yd" / "charlie.jsonl"
    bad.write_text("{ this is not json\n")
    counts = sync.pull("test-yd")
    assert counts["mirrored"] == 1


def test_pull_reads_as_an_observer_seat_so_no_real_seat_is_excluded(fake_ssh):
    remote_post(fake_ssh, "test-ye", "alpha", "comment", "same name as a local seat")
    assert sync.pull("test-ye")["mirrored"] == 1


# ---- round trip ----------------------------------------------------------


def test_round_trip_both_directions(fake_ssh):
    """The stop condition, in one test: a laptop row readable on the hub and a
    hub row readable on the laptop."""
    sync.post("test-z1", "alpha", "comment", "laptop -> studio")
    remote_post(fake_ssh, "test-z1", "bravo", "comment", "studio -> laptop")
    sync.pull("test-z1")

    on_hub = hub_texts(fake_ssh, "test-z1")
    on_laptop = [r["text"] for r in swarm_mailbox.read_siblings("test-z1", "alpha")]
    assert "laptop -> studio" in on_hub
    assert on_laptop == ["studio -> laptop"]


def test_repeated_sync_passes_do_not_multiply_rows(fake_ssh):
    """The loop check: push, pull, push, pull must not amplify."""
    for i in range(3):
        sync.post("test-z2", "alpha", "comment", "row %d" % i)
        sync.pull("test-z2")
    assert len(remote_rows(fake_ssh, "test-z2")) == 3
    assert swarm_mailbox.read_siblings("test-z2", "alpha") == []


# ---- CLI exit codes ------------------------------------------------------


def run_cli(args, extra_env=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "adapters", "remote", "sync.py")]
        + args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )


def test_cli_post_exits_0_when_delivered(fake_ssh):
    cp = run_cli(["post", "test-c1", "alpha", "comment", "hi"])
    assert cp.returncode == 0
    assert b"delivered=1 queued=0" in cp.stdout


def test_cli_post_exits_1_when_queued(fake_ssh):
    fake_ssh.go_down()
    cp = run_cli(["post", "test-c2", "alpha", "comment", "hi"])
    assert cp.returncode == 1
    assert b"queued=1" in cp.stdout


def test_cli_pull_exits_2_when_the_hub_is_unreachable(fake_ssh):
    fake_ssh.go_down()
    cp = run_cli(["pull", "test-c3"])
    assert cp.returncode == 2


def test_cli_pull_reports_counts(fake_ssh):
    remote_post(fake_ssh, "test-c4", "bravo", "comment", "hi")
    cp = run_cli(["pull", "test-c4"])
    assert cp.returncode == 0
    assert b"inspected=1 mirrored=1 echo=0" in cp.stdout


def test_cli_pull_of_an_empty_run_still_exits_0_and_says_zero(fake_ssh):
    cp = run_cli(["pull", "test-c5"])
    assert cp.returncode == 0
    assert b"inspected=0" in cp.stdout


def test_cli_sync_does_both(fake_ssh):
    remote_post(fake_ssh, "test-c6", "bravo", "comment", "down")
    cp = run_cli(["sync", "test-c6"])
    assert cp.returncode == 0
    assert b"mirrored=1" in cp.stdout


def test_cli_bad_kind_exits_2(fake_ssh):
    cp = run_cli(["post", "test-c7", "alpha", "gossip", "hi"])
    assert cp.returncode == 2


def test_cli_no_args_exits_2(fake_ssh):
    assert run_cli([]).returncode == 2


def test_cli_unknown_subcommand_exits_2(fake_ssh):
    assert run_cli(["telepathy", "test-c8"]).returncode == 2


def test_cli_wrong_arity_exits_2(fake_ssh):
    assert run_cli(["post", "test-c9", "alpha"]).returncode == 2


def test_cli_host_flag_overrides_the_default(fake_ssh):
    run_cli(["post", "test-ca", "alpha", "comment", "hi", "--host", "otherbox"])
    assert any("otherbox" in c for c in fake_ssh.calls())
