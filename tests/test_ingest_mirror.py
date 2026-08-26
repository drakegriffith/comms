#!/usr/bin/env python3
"""Tests for adapters/discord/ingest_mirror.py -- the heartbeat-telemetry
tailer that posts the "heard from mailbox" event (verb: heard) to the convo
Discord channel.

Same isolation shape as test_discord_mirror.py: COMMS_ROOT, COMMS_STATE_DIR,
COMMS_SECRETS_FILE all point into tmp_path, and the webhook is a local
HTTPServer, never the network.
"""

import datetime
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "adapters", "discord"))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import ingest_mirror as im  # noqa: E402
import mirror  # noqa: E402
import swarm_arm  # noqa: E402
import swarm_mailbox  # noqa: E402

RUNID = "ingest-test"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMS_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("COMMS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COMMS_SECRETS_FILE", str(tmp_path / "comms.env"))
    monkeypatch.delenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("COMMS_MACHINE_LABEL", "studio")
    monkeypatch.delenv("COMMS_AUDIENCE", raising=False)
    monkeypatch.setattr(mirror, "_PINNED_AUDIENCE", None)
    yield tmp_path


class _Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        self.server.requests.append(body)
        status = self.server.script.pop(0) if self.server.script else 204
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def webhook(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Hook)
    srv.requests = []
    srv.script = []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    url = "http://127.0.0.1:%d/webhook" % srv.server_address[1]
    monkeypatch.setenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", url)
    yield srv
    srv.shutdown()
    srv.server_close()


def _log_event(agent_id, runid, topic="default", rows_inspected=0,
                delta_emitted=0, short_circuit=False, at=None):
    path = im._log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    row = {
        "at": at or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "agent_id": agent_id,
        "runid": runid,
        "topic": topic,
        "rows_inspected": rows_inspected,
        "delta_emitted": delta_emitted,
        "short_circuit": short_circuit,
    }
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\n")


def _enroll(agent_id, seat, runid=RUNID, topics=None, **identity):
    swarm_arm.arm(runid)
    swarm_arm.enroll(runid, agent_id, topics=topics, seat=seat, **identity)


# ---- read_new_events: byte-offset cursor -----------------------------------


def test_read_new_events_no_log_file_yet():
    events, offset = im.read_new_events()
    assert events == []
    assert offset == 0


def test_read_new_events_reads_lines_appended_since_last_offset():
    _log_event("a1", RUNID, delta_emitted=1)
    events, offset = im.read_new_events()
    assert len(events) == 1
    im._save_offset(offset)
    # second pass with nothing new appended: no events, offset unchanged
    events2, offset2 = im.read_new_events()
    assert events2 == []
    assert offset2 == offset
    # append one more line: only the NEW line comes back
    _log_event("a2", RUNID, delta_emitted=2)
    events3, offset3 = im.read_new_events()
    assert len(events3) == 1
    assert events3[0]["agent_id"] == "a2"
    assert offset3 > offset


def test_read_new_events_skips_malformed_json_lines():
    path = im._log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("not json at all\n")
        fh.write(json.dumps({"at": "x", "agent_id": "a1", "runid": RUNID,
                              "topic": "default", "rows_inspected": 1,
                              "delta_emitted": 1, "short_circuit": False}) + "\n")
    events, _ = im.read_new_events()
    assert len(events) == 1
    assert events[0]["agent_id"] == "a1"


def test_read_new_events_resets_to_zero_when_log_is_shorter_than_offset():
    _log_event("a1", RUNID, delta_emitted=1)
    events, offset = im.read_new_events()
    im._save_offset(offset)
    # simulate rotation: log file replaced with a shorter one
    path = im._log_path()
    with open(path, "w") as fh:
        fh.write(json.dumps({"at": "y", "agent_id": "a2", "runid": RUNID,
                              "topic": "default", "rows_inspected": 1,
                              "delta_emitted": 1, "short_circuit": False}) + "\n")
    events2, offset2 = im.read_new_events()
    assert len(events2) == 1
    assert events2[0]["agent_id"] == "a2"  # re-read from 0, not skipped


def test_offset_cursor_round_trips_and_uses_pid_suffixed_tmp():
    im._save_offset(42)
    assert im._load_offset() == 42
    assert not os.path.exists(im._cursor_path() + ".tmp." + str(os.getpid()))
    assert im._cursor_path().startswith(im._mirror_dir())
    assert "discord-mirror-convo" in im._cursor_path()


# ---- _reconstruct_delta: replaying swarm-heartbeat's own selection --------


def test_reconstruct_delta_excludes_own_seat_and_sorts_by_at():
    _enroll("agentA", "alpha", topics=[])
    swarm_mailbox.post(RUNID, "beta", "finding", "first", topic="default")
    swarm_mailbox.post(RUNID, "alpha", "finding", "own row never counted", topic="default")
    swarm_mailbox.post(RUNID, "gamma", "comment", "second", topic="default")
    delta, key = im._reconstruct_delta(RUNID, "agentA", {})
    assert [r["text"] for r in delta] == ["first", "second"]
    assert key == RUNID + "\x00agentA"


def test_reconstruct_delta_honors_topic_subscription():
    _enroll("agentA", "alpha", topics=["projx"])
    swarm_mailbox.post(RUNID, "beta", "finding", "in scope", topic="projx")
    swarm_mailbox.post(RUNID, "beta", "finding", "out of scope", topic="other")
    delta, _ = im._reconstruct_delta(RUNID, "agentA", {})
    assert [r["text"] for r in delta] == ["in scope"]


def test_reconstruct_delta_bounded_by_watermark():
    _enroll("agentA", "alpha", topics=[])
    swarm_mailbox.post(RUNID, "beta", "finding", "old", topic="default")
    rows = swarm_mailbox.read_siblings(RUNID, "alpha")
    watermark_at = rows[0]["at"]
    swarm_mailbox.post(RUNID, "beta", "finding", "new", topic="default")
    attrib = {RUNID + "\x00agentA": watermark_at}
    delta, _ = im._reconstruct_delta(RUNID, "agentA", attrib)
    assert [r["text"] for r in delta] == ["new"]


def test_reconstruct_delta_caps_at_CAP():
    _enroll("agentA", "alpha", topics=[])
    for i in range(im.CAP + 5):
        swarm_mailbox.post(RUNID, "beta", "finding", "row%d" % i, topic="default")
    delta, _ = im._reconstruct_delta(RUNID, "agentA", {})
    assert len(delta) == im.CAP
    assert [r["text"] for r in delta] == ["row%d" % i for i in range(im.CAP)]


# ---- process_events: per-beat aggregation, never per-row -------------------


def test_process_events_ignores_zero_delta_and_short_circuit_lines():
    _enroll("agentA", "alpha", topics=[])
    events = [
        {"agent_id": "agentA", "runid": RUNID, "delta_emitted": 0, "short_circuit": True},
        {"agent_id": "agentA", "runid": RUNID, "delta_emitted": 0, "short_circuit": False},
    ]
    assert im.process_events(events) == []


def test_process_events_aggregates_one_beat_into_one_post_not_per_row():
    _enroll("agentA", "alpha", topics=[])
    swarm_mailbox.post(RUNID, "beta", "finding", "row1", topic="default")
    swarm_mailbox.post(RUNID, "beta", "finding", "row2", topic="default")
    swarm_mailbox.post(RUNID, "gamma", "comment", "row3", topic="default")
    events = [{"agent_id": "agentA", "runid": RUNID, "delta_emitted": 3, "short_circuit": False}]
    posts = im.process_events(events)
    assert len(posts) == 1  # ONE post for a 3-row beat, not three
    author, content = posts[0]
    assert content == "\U0001f441️ read 3 row(s) from beta, gamma"


def test_process_events_everyone_audience_reads_as_a_sentence(monkeypatch):
    monkeypatch.setenv("COMMS_AUDIENCE", "everyone")
    _enroll("agentA", "alpha", topics=[], model="Opus 5", project="comms")
    swarm_mailbox.post(RUNID, "beta", "finding", "row1", topic="default")
    swarm_mailbox.post(RUNID, "gamma", "comment", "row2", topic="default")
    events = [{"agent_id": "agentA", "runid": RUNID, "delta_emitted": 2, "short_circuit": False}]
    author, content = im.process_events(events)[0]
    assert author == "alpha · Opus 5, working on comms"
    assert content == "\U0001f440 Read 2 new messages from beta and gamma"


def test_process_events_distinct_seats_preserve_first_seen_order():
    _enroll("agentA", "alpha", topics=[])
    swarm_mailbox.post(RUNID, "gamma", "finding", "row1", topic="default")
    swarm_mailbox.post(RUNID, "beta", "finding", "row2", topic="default")
    swarm_mailbox.post(RUNID, "gamma", "finding", "row3", topic="default")
    events = [{"agent_id": "agentA", "runid": RUNID, "delta_emitted": 3, "short_circuit": False}]
    _, content = im.process_events(events)[0]
    assert content == "\U0001f441️ read 3 row(s) from gamma, beta"


def test_process_events_second_beat_only_attributes_new_rows():
    _enroll("agentA", "alpha", topics=[])
    swarm_mailbox.post(RUNID, "beta", "finding", "row1", topic="default")
    events1 = [{"agent_id": "agentA", "runid": RUNID, "delta_emitted": 1, "short_circuit": False}]
    im.process_events(events1)  # persists the watermark past row1
    swarm_mailbox.post(RUNID, "gamma", "finding", "row2", topic="default")
    events2 = [{"agent_id": "agentA", "runid": RUNID, "delta_emitted": 1, "short_circuit": False}]
    posts = im.process_events(events2)
    assert posts[0][1] == "\U0001f441️ read 1 row(s) from gamma"  # not beta again


def test_process_events_mismatch_between_reconstruction_and_telemetry_is_logged(capsys):
    _enroll("agentA", "alpha", topics=[])
    # No mailbox rows exist at all: reconstruction finds 0, telemetry says 3.
    events = [{"agent_id": "agentA", "runid": RUNID, "delta_emitted": 3, "short_circuit": False}]
    posts = im.process_events(events)
    err = capsys.readouterr().err
    assert "reconstructed 0 row(s)" in err
    assert "telemetry said 3" in err
    # never fabricates sender seats it could not find
    assert posts[0][1] == "\U0001f441️ read 3 row(s) from unknown sender(s)"


def test_process_events_author_uses_identity_roster_when_resolvable():
    _enroll("agentA", "alpha", topics=[], model="Sonnet 5", project="comms", area="adapters/discord")
    swarm_mailbox.post(RUNID, "beta", "finding", "row1", topic="default")
    events = [{"agent_id": "agentA", "runid": RUNID, "delta_emitted": 1, "short_circuit": False}]
    author, _ = im.process_events(events)[0]
    assert author == "alpha · Sonnet 5 on comms (studio)"


def test_process_events_author_degrades_without_identity():
    _enroll("agentA", "alpha", topics=[])
    swarm_mailbox.post(RUNID, "beta", "finding", "row1", topic="default")
    events = [{"agent_id": "agentA", "runid": RUNID, "delta_emitted": 1, "short_circuit": False}]
    author, _ = im.process_events(events)[0]
    assert author == "alpha (studio)"


def test_process_events_author_falls_back_to_agent_id_when_no_seat():
    swarm_arm.arm(RUNID)
    swarm_arm.enroll(RUNID, "agentNoSeat9", topics=[])  # no --seat
    swarm_mailbox.post(RUNID, "beta", "finding", "row1", topic="default")
    events = [{"agent_id": "agentNoSeat9", "runid": RUNID, "delta_emitted": 1, "short_circuit": False}]
    author, _ = im.process_events(events)[0]
    assert author == "agent agentNoS (studio)"


def test_process_events_ignores_events_missing_runid_or_agent_id():
    assert im.process_events([{"delta_emitted": 1}]) == []
    assert im.process_events([{"agent_id": "a", "delta_emitted": 1}]) == []
    assert im.process_events([{"runid": RUNID, "delta_emitted": 1}]) == []
    assert im.process_events(["not a dict"]) == []


# ---- tail_once: end-to-end, one process per pass ---------------------------


def test_tail_once_posts_and_advances_offset(webhook):
    _enroll("agentA", "alpha", topics=[])
    swarm_mailbox.post(RUNID, "beta", "finding", "row1", topic="default")
    _log_event("agentA", RUNID, delta_emitted=1)
    url = os.environ["DISCORD_COMMS_CONVO_WEBHOOK_URL"]
    assert im.tail_once(url) == 0
    assert len(webhook.requests) == 1
    assert webhook.requests[0]["username"] == "alpha (studio)"
    assert webhook.requests[0]["content"] == "\U0001f441️ read 1 row(s) from beta"
    # a second pass with nothing new appended posts nothing again
    assert im.tail_once(url) == 0
    assert len(webhook.requests) == 1


def test_tail_once_no_new_log_lines_posts_nothing(webhook):
    url = os.environ["DISCORD_COMMS_CONVO_WEBHOOK_URL"]
    assert im.tail_once(url) == 0
    assert webhook.requests == []


def test_tail_once_returns_1_when_post_fails(webhook):
    _enroll("agentA", "alpha", topics=[])
    swarm_mailbox.post(RUNID, "beta", "finding", "row1", topic="default")
    _log_event("agentA", RUNID, delta_emitted=1)
    webhook.script[:] = [500]
    url = os.environ["DISCORD_COMMS_CONVO_WEBHOOK_URL"]
    assert im.tail_once(url) == 1


def test_tail_once_offset_persists_across_calls_no_repost(webhook):
    _enroll("agentA", "alpha", topics=[])
    swarm_mailbox.post(RUNID, "beta", "finding", "row1", topic="default")
    _log_event("agentA", RUNID, delta_emitted=1)
    url = os.environ["DISCORD_COMMS_CONVO_WEBHOOK_URL"]
    assert im.tail_once(url) == 0
    swarm_mailbox.post(RUNID, "beta", "finding", "row2", topic="default")
    _log_event("agentA", RUNID, delta_emitted=1)
    assert im.tail_once(url) == 0
    assert len(webhook.requests) == 2
    assert "row1" not in json.dumps(webhook.requests[1])


# ---- CLI ---------------------------------------------------------------


def test_main_usage_exit_2(capsys):
    assert im.main(["ingest_mirror.py"]) == 2
    assert im.main(["ingest_mirror.py", "--bogus"]) == 2
    assert "usage" in capsys.readouterr().err


def test_main_once_missing_secret_exits_2_naming_convo_var(capsys):
    with pytest.raises(SystemExit) as exc:
        im.main(["ingest_mirror.py", "--once"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "DISCORD_COMMS_CONVO_WEBHOOK_URL=" in err


def test_main_once_dispatches_tail_once(webhook, monkeypatch):
    calls = []
    monkeypatch.setattr(im, "tail_once", lambda url: calls.append(url) or 0)
    assert im.main(["ingest_mirror.py", "--once"]) == 0
    assert calls == [os.environ["DISCORD_COMMS_CONVO_WEBHOOK_URL"]]


def test_main_interval_bad_value_returns_2(capsys):
    assert im.main(["ingest_mirror.py", "--follow", "--interval", "nope"]) == 2
    assert "--interval needs a number" in capsys.readouterr().err


def test_follow_missing_secret_does_not_raise_and_retries_60s(monkeypatch, capsys):
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt()

    monkeypatch.setattr(im.time, "sleep", fake_sleep)
    rc = im.follow(5)
    assert rc == 0
    assert sleeps == [mirror.MISSING_SECRET_RETRY_SECONDS]
    err = capsys.readouterr().err
    assert err.count("\n") == 1


def test_follow_resumes_normal_interval_once_secret_present(webhook):
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt()

    orig_sleep = im.time.sleep
    im.time.sleep = fake_sleep
    try:
        rc = im.follow(5)
    finally:
        im.time.sleep = orig_sleep
    assert rc == 0
    assert sleeps == [5]


def test_follow_survives_tail_once_exception(webhook, capsys, monkeypatch):
    def boom(url):
        raise PermissionError("state dir not writable")

    monkeypatch.setattr(im, "tail_once", boom)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt()

    monkeypatch.setattr(im.time, "sleep", fake_sleep)
    rc = im.follow(5)
    assert rc == 1
    assert sleeps == [5]
    err = capsys.readouterr().err
    assert "PermissionError" in err


def test_main_follow_dispatches(monkeypatch):
    calls = []
    monkeypatch.setattr(im, "follow", lambda interval: calls.append(interval) or 0)
    assert im.main(["ingest_mirror.py", "--follow", "--interval", "9"]) == 0
    assert calls == [9.0]
