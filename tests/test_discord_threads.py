#!/usr/bin/env python3
"""Tests for adapters/discord/threads.py -- the thread map and thread_for.

NO NETWORK: the creating POST is injected as a `poster` callable at the
composition root (mirror's board branch builds the real one from the forum
webhook URL), so every test here hands in a fake and asserts on what the map
file holds afterward. The one test that exercises the REAL wire shape fakes
urlopen instead, and asserts the payload, never a live Discord.
"""

import errno
import fcntl
import io
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "adapters", "discord"))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import threads  # noqa: E402

LANE = "board"
KEY = "doc:comms/docs/plan.md"
NAME = "comms/docs/plan.md"


def _map_contents():
    with open(threads.map_path(LANE)) as fh:
        return json.load(fh)


def _write_map(text):
    path = threads.map_path(LANE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


class _Poster:
    """A fake creating-POST. Records every call, returns the scripted id."""

    def __init__(self, result="777"):
        self.calls = []
        self.result = result

    def __call__(self, name, content):
        self.calls.append((name, content))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


# ---- the map is a cache: a hit costs no HTTP -----------------------------


def test_a_map_hit_returns_the_id_without_calling_the_poster():
    _write_map(json.dumps({KEY: "123"}))
    poster = _Poster()
    assert threads.thread_for(KEY, NAME, LANE, poster) == "123"
    assert poster.calls == []


def test_a_miss_creates_and_returns_the_new_id():
    poster = _Poster("777")
    assert threads.thread_for(KEY, NAME, LANE, poster) == "777"
    assert len(poster.calls) == 1
    assert poster.calls[0][0] == NAME  # the thread's human-visible title


def test_the_new_id_is_PERSISTED_BEFORE_thread_for_returns():
    # The whole point of the map: the NEXT pass (or the next process) must
    # find this thread instead of creating a second one for the same
    # document. A persist deferred to "later" is a duplicate thread every
    # time the process dies between the create and the write.
    threads.thread_for(KEY, NAME, LANE, _Poster("777"))
    assert _map_contents() == {KEY: "777"}


def test_a_second_call_for_the_same_key_is_served_from_the_map():
    poster = _Poster("777")
    threads.thread_for(KEY, NAME, LANE, poster)
    threads.thread_for(KEY, NAME, LANE, poster)
    assert len(poster.calls) == 1


def test_a_second_key_does_not_clobber_the_first():
    threads.thread_for(KEY, NAME, LANE, _Poster("777"))
    threads.thread_for("doc:comms/b.md", "comms/b.md", LANE, _Poster("888"))
    assert _map_contents() == {KEY: "777", "doc:comms/b.md": "888"}


def test_the_map_is_fleet_wide_per_lane_not_per_run():
    # D3: the key spans runs, so a per-runid map would let two runs
    # discussing one document open two threads. The path carries the lane
    # and nothing else.
    path = threads.map_path(LANE)
    assert os.path.basename(path) == "threads.json"
    assert os.path.basename(os.path.dirname(path)) == "discord-mirror-board"


def test_the_map_path_follows_COMMS_STATE_DIR(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMS_STATE_DIR", str(tmp_path / "elsewhere"))
    assert str(tmp_path / "elsewhere") in threads.map_path(LANE)


# ---- every failure is None, never an exception ---------------------------


def test_a_poster_that_fails_yields_None_and_writes_no_map_entry():
    assert threads.thread_for(KEY, NAME, LANE, _Poster(None)) is None
    assert not os.path.exists(threads.map_path(LANE))


def test_a_poster_that_RAISES_yields_None(capsys):
    # D6: any failure of the create degrades to None. A raise here would
    # travel up into the mirror pass and stop every other thread's delivery.
    boom = _Poster(RuntimeError("network on fire"))
    assert threads.thread_for(KEY, NAME, LANE, boom) is None
    assert "thread" in capsys.readouterr().err.lower()


def test_an_empty_id_from_the_poster_is_treated_as_a_failure():
    assert threads.thread_for(KEY, NAME, LANE, _Poster("")) is None


def test_a_corrupt_map_reads_as_empty_and_the_thread_is_recreated(capsys):
    _write_map("{not json at all")
    assert threads.thread_for(KEY, NAME, LANE, _Poster("777")) == "777"
    assert _map_contents() == {KEY: "777"}
    assert capsys.readouterr().err  # never silent


def test_a_map_holding_a_non_dict_reads_as_empty():
    _write_map(json.dumps(["a", "list"]))
    assert threads.thread_for(KEY, NAME, LANE, _Poster("777")) == "777"
    assert _map_contents() == {KEY: "777"}


def test_a_persist_failure_after_a_successful_create_returns_None(monkeypatch, capsys):
    # D6, the ugly row: the thread EXISTS in Discord but this machine could
    # not record it. Returning the id anyway would post rows into a thread
    # nothing remembers, so next pass opens another one AND the rows are
    # already gone from held. Returning None keeps the rows in held; the cost
    # is one leaked empty thread, which auto-archives.
    def no_replace(src, dst):
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(threads.os, "replace", no_replace)
    assert threads.thread_for(KEY, NAME, LANE, _Poster("777")) is None
    err = capsys.readouterr().err
    assert "map" in err.lower()


def test_a_persist_failure_leaves_no_half_written_map(monkeypatch):
    def no_replace(src, dst):
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(threads.os, "replace", no_replace)
    threads.thread_for(KEY, NAME, LANE, _Poster("777"))
    assert not os.path.exists(threads.map_path(LANE))


def test_a_successful_persist_leaves_no_tmp_file_behind():
    threads.thread_for(KEY, NAME, LANE, _Poster("777"))
    d = os.path.dirname(threads.map_path(LANE))
    assert [n for n in os.listdir(d) if ".tmp." in n] == []


# ---- the lock is held ACROSS the create ----------------------------------


def test_the_map_lock_is_held_across_the_create_not_just_the_write():
    # D3: two processes racing on one key both read an empty map, both
    # create, and the second's persist wins -- one orphan thread per race,
    # forever. Holding the lock across read->create->persist is what makes
    # the check-then-act atomic. This test asserts from INSIDE the create
    # that the lock is already taken.
    observed = {}

    def poster(name, content):
        fd = os.open(threads.lock_path(LANE), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed["held"] = False
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            observed["held"] = True
        finally:
            os.close(fd)
        return "777"

    assert threads.thread_for(KEY, NAME, LANE, poster) == "777"
    assert observed["held"] is True


def test_the_lock_is_released_when_the_poster_raises():
    # A lock leaked on the error path wedges every later pass on this
    # machine -- a worse outage than the failure that caused it.
    threads.thread_for(KEY, NAME, LANE, _Poster(RuntimeError("x")))
    fd = os.open(threads.lock_path(LANE), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_the_lock_file_is_not_the_map_file():
    # Deliberate deviation from the design note, which said to flock the map
    # itself: the map is written with tmp + os.replace, so the locked fd
    # names an inode that is no longer the map the moment it is saved --
    # a second process opening the path gets the NEW inode and its flock
    # succeeds against nothing.
    assert threads.lock_path(LANE) != threads.map_path(LANE)


# ---- the real wire shape (no network: urlopen is faked) ------------------


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(monkeypatch, body, seen):
    def fake(req, timeout=None):
        seen.append(
            {
                "url": req.full_url,
                "payload": json.loads(req.data.decode("utf-8")),
                "headers": dict(req.headers),
            }
        )
        return _FakeResponse(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(threads.urllib.request, "urlopen", fake)


def test_webhook_poster_sends_thread_name_and_asks_for_the_body_back(monkeypatch):
    seen = []
    _fake_urlopen(monkeypatch, {"id": "555", "channel_id": "999"}, seen)
    poster = threads.webhook_poster("https://discord.example/webhook")
    assert poster("comms/a.md", "seed") == "999"
    assert seen[0]["payload"]["thread_name"] == "comms/a.md"
    assert "wait=true" in seen[0]["url"]


def test_webhook_poster_never_lets_a_mention_out(monkeypatch):
    seen = []
    _fake_urlopen(monkeypatch, {"channel_id": "999"}, seen)
    threads.webhook_poster("https://discord.example/webhook")("@everyone", "x")
    assert seen[0]["payload"]["allowed_mentions"] == {"parse": []}


def test_webhook_poster_falls_back_to_the_message_id(monkeypatch):
    # A forum-webhook create returns the starter MESSAGE; its channel_id is
    # the new thread. If a future response shape omits it, the message id is
    # the same value for a thread's starter post -- better than None.
    seen = []
    _fake_urlopen(monkeypatch, {"id": "555"}, seen)
    assert threads.webhook_poster("https://x/w")("n", "c") == "555"


def test_webhook_poster_returns_None_on_a_response_with_no_id(monkeypatch):
    seen = []
    _fake_urlopen(monkeypatch, {"nothing": "useful"}, seen)
    assert threads.webhook_poster("https://x/w")("n", "c") is None


def test_webhook_poster_returns_None_on_a_non_json_body(monkeypatch):
    def fake(req, timeout=None):
        return _FakeResponse(b"<html>rate limited</html>")

    monkeypatch.setattr(threads.urllib.request, "urlopen", fake)
    assert threads.webhook_poster("https://x/w")("n", "c") is None


def test_webhook_poster_returns_None_on_an_http_error(monkeypatch):
    import urllib.error

    def fake(req, timeout=None):
        raise urllib.error.HTTPError("https://x/w", 403, "Forbidden", {}, None)

    monkeypatch.setattr(threads.urllib.request, "urlopen", fake)
    assert threads.webhook_poster("https://x/w")("n", "c") is None


def test_webhook_poster_never_prints_the_url(monkeypatch, capsys):
    import urllib.error

    secret = "https://discord.example/api/webhooks/1/SUPERSECRETTOKEN"

    def fake(req, timeout=None):
        raise urllib.error.HTTPError(secret, 500, "boom", {}, None)

    monkeypatch.setattr(threads.urllib.request, "urlopen", fake)
    threads.webhook_poster(secret)("n", "c")
    out = capsys.readouterr()
    assert "SUPERSECRETTOKEN" not in (out.err + out.out)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
