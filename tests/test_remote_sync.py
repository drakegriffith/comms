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
    delivered, remaining = sync.post("test-x1", "alpha", "comment", "hello hub")
    assert (delivered, remaining) == (1, 0)
    rows = remote_rows(fake_ssh, "test-x1")
    assert len(rows) == 1
    assert rows[0]["text"] == "hello hub"


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
    assert remote_rows(fake_ssh, "test-x6")[0]["text"] == nasty


def test_post_rejects_an_invalid_kind_before_queueing(fake_ssh):
    with pytest.raises(ValueError):
        sync.post("test-x7", "alpha", "gossip", "hi")
    assert sync.load_outbox() == []


def test_post_rejects_topic_and_to_together(fake_ssh):
    with pytest.raises(ValueError):
        sync.post("test-x8", "alpha", "comment", "hi", topic="ops", to="bravo")


def test_post_queues_when_the_hub_is_offline(fake_ssh):
    fake_ssh.go_down()
    delivered, remaining = sync.post("test-x9", "alpha", "comment", "later")
    assert (delivered, remaining) == (0, 1)
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
    delivered, remaining = sync.flush()
    assert (delivered, remaining) == (2, 0)
    assert [r["text"] for r in remote_rows(fake_ssh, "test-xa")] == ["one", "two"]
    assert sync.load_outbox() == []


def test_a_fresh_post_never_overtakes_a_queued_one(fake_ssh):
    """post() enqueues BEFORE it flushes, which is what makes ordering free."""
    fake_ssh.go_down()
    sync.post("test-xb", "alpha", "comment", "first")
    fake_ssh.come_up()
    sync.post("test-xb", "alpha", "comment", "second")
    assert [r["text"] for r in remote_rows(fake_ssh, "test-xb")] == ["first", "second"]


def test_flush_stops_at_the_first_failure_and_keeps_the_remainder(fake_ssh):
    fake_ssh.go_down()
    sync.post("test-xc", "alpha", "comment", "aaa")
    sync.post("test-xc", "alpha", "comment", "bbb")
    sync.post("test-xc", "alpha", "comment", "ccc")
    fake_ssh.come_up()
    fake_ssh.reject("bbb")
    delivered, remaining = sync.flush()
    assert (delivered, remaining) == (1, 2)
    assert [r["text"] for r in remote_rows(fake_ssh, "test-xc")] == ["aaa"]
    assert [r["text"] for r in sync.load_outbox()] == ["bbb", "ccc"]


def test_a_rejected_row_is_never_dropped(fake_ssh):
    """A row the hub refuses is a bug to be seen, not a message this adapter
    may discard on the author's behalf."""
    fake_ssh.reject("bad")
    sync.post("test-xd", "alpha", "comment", "bad row")
    assert [r["text"] for r in sync.load_outbox()] == ["bad row"]


def test_flush_on_an_empty_outbox_makes_no_ssh_call(fake_ssh):
    assert sync.flush() == (0, 0)
    assert fake_ssh.calls() == []


def test_outbox_skips_a_corrupt_line_instead_of_wedging(fake_ssh):
    fake_ssh.go_down()
    sync.post("test-xe", "alpha", "comment", "good")
    path = sync._outbox_path("studio")
    with open(path, "a") as fh:
        fh.write("{not json\n")
    assert [r["text"] for r in sync.load_outbox()] == ["good"]


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
    delivered, remaining = sync.post("test-w1", "alpha", "comment", "expanded")
    assert (delivered, remaining) == (1, 0)
    assert [r["text"] for r in remote_rows(fake_ssh, "test-w1")] == ["expanded"]


# ---- pull ----------------------------------------------------------------


def test_pull_mirrors_remote_rows_into_the_local_mailbox(fake_ssh):
    remote_post(fake_ssh, "test-y1", "bravo", "comment", "from the studio")
    counts = sync.pull("test-y1")
    assert counts == {"inspected": 1, "mirrored": 1, "echo": 0}
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
    assert sync.pull("test-y7") == {"inspected": 1, "mirrored": 0, "echo": 0}


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
    assert sync.pull("test-y9") == {"inspected": 0, "mirrored": 0, "echo": 0}


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

    on_hub = [r["text"] for r in remote_rows(fake_ssh, "test-z1")]
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
