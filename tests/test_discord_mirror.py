#!/usr/bin/env python3
"""Tests for adapters/discord/mirror.py.

Webhook is a LOCAL HTTPServer (scriptable status codes), never the network.
ALL writes are isolated to tmp dirs: COMMS_ROOT, COMMS_STATE_DIR, and
COMMS_SECRETS_FILE are pointed at tmp_path in every test (autouse fixture) --
half-isolation reads as isolation, so the fixture covers every knob the
mirror writes or reads through.
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "adapters", "discord"))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import mirror  # noqa: E402
import swarm_arm  # noqa: E402
import swarm_mailbox  # noqa: E402

RUNID = "mirror-test"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every read/write knob the mirror touches points into tmp_path."""
    monkeypatch.setenv("COMMS_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("COMMS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COMMS_SECRETS_FILE", str(tmp_path / "comms.env"))
    monkeypatch.delenv("DISCORD_COMMS_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("COMMS_MACHINE_LABEL", "studio")
    yield tmp_path


class _Hook(BaseHTTPRequestHandler):
    """Fake webhook endpoint. Pops the next scripted status (default 204);
    a 429 response carries Retry-After: 0 so retry tests run fast."""

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        self.server.requests.append(body)
        status = self.server.script.pop(0) if self.server.script else 204
        self.send_response(status)
        if status == 429:
            self.send_header("Retry-After", "0")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture()
def webhook(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Hook)
    srv.requests = []
    srv.script = []  # per-test list of status codes to serve, in order
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    url = "http://127.0.0.1:%d/webhook" % srv.server_address[1]
    monkeypatch.setenv("DISCORD_COMMS_WEBHOOK_URL", url)
    yield srv
    srv.shutdown()
    srv.server_close()


def _append_raw(seat, kind, text):
    """Write a row straight to the seat's jsonl -- the on-disk contract --
    so tests can carry kinds VALID_KINDS does not (yet) allow, exactly what
    the parallel kind-vocabulary branch will produce."""
    d = os.path.join(os.environ["COMMS_ROOT"], "comms-%s" % RUNID)
    os.makedirs(d, exist_ok=True)
    row = {"seat": seat, "at": "2026-08-21T00:00:00+00:00", "kind": kind, "text": text}
    with open(os.path.join(d, "%s.jsonl" % seat), "a") as fh:
        fh.write(json.dumps(row) + "\n")


# ---- formatting ----------------------------------------------------------


def test_format_row_prefix_and_kind():
    row = {"seat": "alpha", "kind": "finding", "text": "cursor landed"}
    assert mirror.format_row(row, "studio") == "[studio/alpha] finding: cursor landed"


def test_format_row_truncates_to_300():
    row = {"seat": "a", "kind": "finding", "text": "x" * 400}
    line = mirror.format_row(row, "m")
    assert line.endswith("x" * 300)
    assert "x" * 301 not in line


def test_machine_label_env_overrides_hostname():
    assert mirror.machine_label() == "studio"


def test_machine_label_falls_back_to_short_hostname(monkeypatch):
    monkeypatch.delenv("COMMS_MACHINE_LABEL", raising=False)
    label = mirror.machine_label()
    assert label and "." not in label


# ---- enrollment identity (display-only; joined by seat at format time) ----


def test_format_row_with_full_identity():
    row = {"seat": "kimi1", "kind": "finding", "text": "hook rot in leg 2"}
    identity = {"model": "Kimi K3", "project": "agent-os", "area": "hooks/"}
    assert (
        mirror.format_row(row, "macbook", identity)
        == "[macbook] Kimi K3 on agent-os (hooks/) | seat kimi1 | finding: hook rot in leg 2"
    )


def test_format_row_with_partial_identity_drops_absent_parts():
    row = {"seat": "kimi1", "kind": "finding", "text": "t"}
    assert (
        mirror.format_row(row, "macbook", {"model": "Opus 5"})
        == "[macbook] Opus 5 | seat kimi1 | finding: t"
    )
    assert (
        mirror.format_row(row, "macbook", {"project": "agent-os"})
        == "[macbook] on agent-os | seat kimi1 | finding: t"
    )


def test_format_row_without_identity_is_byte_identical_to_old_format():
    row = {"seat": "alpha", "kind": "finding", "text": "cursor landed"}
    old = "[studio/alpha] finding: cursor landed"
    assert mirror.format_row(row, "studio") == old
    assert mirror.format_row(row, "studio", None) == old
    assert mirror.format_row(row, "studio", {}) == old


def test_enroll_identity_roundtrips_through_roster():
    swarm_arm.arm(RUNID)
    assert swarm_arm.enroll(
        RUNID, "agent-k", seat="kimi1",
        model="Kimi K3", project="agent-os", area="hooks/",
    )
    assert swarm_arm.seat_identities(RUNID) == {
        "kimi1": {"model": "Kimi K3", "project": "agent-os", "area": "hooks/"}
    }


def test_enroll_without_identity_yields_empty_roster_map():
    swarm_arm.arm(RUNID)
    assert swarm_arm.enroll(RUNID, "agent-a", seat="alpha", topics="t1")
    assert swarm_arm.seat_identities(RUNID) == {}
    # And with no arm state at all (the common pre-identity case): still {}.
    assert swarm_arm.seat_identities("never-armed") == {}


def test_once_joins_identity_from_enrollment(webhook):
    swarm_arm.arm(RUNID)
    swarm_arm.enroll(
        RUNID, "agent-k", seat="kimi1",
        model="Kimi K3", project="agent-os", area="hooks/",
    )
    swarm_mailbox.post(RUNID, "kimi1", "finding", "identity rendered")
    swarm_mailbox.post(RUNID, "alpha", "finding", "no identity here")
    assert mirror.run_once(RUNID) == 0
    content = webhook.requests[0]["content"]
    # Enrolled seat: rich line. Un-enrolled sibling in the SAME batch: old line.
    assert (
        "[studio] Kimi K3 on agent-os (hooks/) | seat kimi1 | finding: identity rendered"
        in content
    )
    assert "[studio/alpha] finding: no identity here" in content


# ---- mirroring, batching, cursor -----------------------------------------


def test_once_posts_rows_batched_into_one_message(webhook):
    swarm_mailbox.post(RUNID, "alpha", "finding", "first")
    swarm_mailbox.post(RUNID, "beta", "blocker", "second")
    assert mirror.run_once(RUNID) == 0
    assert len(webhook.requests) == 1  # batched, not one POST per row
    content = webhook.requests[0]["content"]
    assert "[studio/alpha] finding: first" in content
    assert "[studio/beta] blocker: second" in content


def test_kind_agnostic_mirrors_unknown_kinds(webhook):
    _append_raw("alpha", "comment", "a new kind from the parallel branch")
    assert mirror.run_once(RUNID) == 0
    assert "[studio/alpha] comment: a new kind" in webhook.requests[0]["content"]


def test_cursor_never_reposts_across_restarts(webhook):
    swarm_mailbox.post(RUNID, "alpha", "finding", "one")
    assert mirror.run_once(RUNID) == 0
    # "restart": run_once reloads the cursor from disk every call
    assert mirror.run_once(RUNID) == 0
    assert len(webhook.requests) == 1
    swarm_mailbox.post(RUNID, "alpha", "finding", "two")
    assert mirror.run_once(RUNID) == 0
    assert len(webhook.requests) == 2
    assert "two" in webhook.requests[1]["content"]
    assert "one" not in webhook.requests[1]["content"]


def test_long_batch_chunks_under_discord_cap(webhook):
    for i in range(8):
        swarm_mailbox.post(RUNID, "alpha", "finding", ("row%d " % i) * 60)
    assert mirror.run_once(RUNID) == 0
    assert len(webhook.requests) > 1  # would blow the 2000-char cap in one
    for req in webhook.requests:
        assert len(req["content"]) <= 2000
    joined = "\n".join(r["content"] for r in webhook.requests)
    for i in range(8):
        assert "row%d" % i in joined  # nothing lost to chunking


# ---- 429 handling ---------------------------------------------------------


def test_429_retries_then_delivers(webhook):
    webhook.script[:] = [429, 204]
    swarm_mailbox.post(RUNID, "alpha", "finding", "rate limited once")
    assert mirror.run_once(RUNID) == 0
    assert len(webhook.requests) == 2  # first 429ed, retry delivered


def test_429_exhaustion_logs_skipped_never_silent(webhook, capsys):
    webhook.script[:] = [429] * (mirror.MAX_RETRIES + 1)
    swarm_mailbox.post(RUNID, "alpha", "finding", "undeliverable")
    assert mirror.run_once(RUNID) == 1  # loud exit code
    err = capsys.readouterr().err
    assert "SKIPPED" in err
    skipped_path = mirror._skipped_path(RUNID)
    with open(skipped_path) as fh:
        recorded = [json.loads(l) for l in fh]
    assert len(recorded) == 1
    assert recorded[0]["row"]["text"] == "undeliverable"
    # cursor advanced past the skipped row: no wedge, no repost storm
    webhook.script[:] = []
    webhook.requests[:] = []
    assert mirror.run_once(RUNID) == 0
    assert webhook.requests == []


# ---- secret handling ------------------------------------------------------


def test_missing_secret_exits_2_naming_drop_in(capsys):
    with pytest.raises(SystemExit) as exc:
        mirror.run_once(RUNID)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "DISCORD_COMMS_WEBHOOK_URL=" in err
    assert "~/.secrets/comms.env" in err
    assert "http" not in err  # never echoes any URL value


def test_secret_read_from_secrets_file(webhook, tmp_path, monkeypatch):
    # Move the URL out of env and into the file: the fallback path must work.
    url = os.environ["DISCORD_COMMS_WEBHOOK_URL"]
    monkeypatch.delenv("DISCORD_COMMS_WEBHOOK_URL")
    (tmp_path / "comms.env").write_text(
        "# comms secrets\nDISCORD_COMMS_WEBHOOK_URL=%s\n" % url
    )
    swarm_mailbox.post(RUNID, "alpha", "finding", "via file")
    assert mirror.run_once(RUNID) == 0
    assert "via file" in webhook.requests[0]["content"]


def test_state_writes_stay_in_tmp(webhook, tmp_path):
    """Half-isolation reads as isolation: prove the cursor landed under the
    tmp state dir, not the real ~/.comms/state."""
    swarm_mailbox.post(RUNID, "alpha", "finding", "where does state go")
    mirror.run_once(RUNID)
    cursor = mirror._cursor_path(RUNID)
    assert cursor.startswith(str(tmp_path))
    assert os.path.isfile(cursor)


# ---- CLI ------------------------------------------------------------------


def test_main_usage_exit_2(capsys):
    assert mirror.main(["mirror.py"]) == 2
    assert mirror.main(["mirror.py", "--bogus", "x"]) == 2
    assert "usage" in capsys.readouterr().err


def test_main_once_returns_run_once_result(webhook):
    swarm_mailbox.post(RUNID, "alpha", "finding", "via main")
    assert mirror.main(["mirror.py", "--once", RUNID]) == 0
    assert "via main" in webhook.requests[0]["content"]


# ---- default lane: byte-identical when --lane is not given ----------------


def test_default_lane_secret_var_unchanged():
    assert mirror.SECRET_VAR == "DISCORD_COMMS_WEBHOOK_URL"
    assert mirror.resolve_webhook_url is not None  # still the one entrypoint


def test_default_lane_format_and_secret_var_unchanged(webhook):
    # No --lane anywhere: run_once() with no lane arg, and the CLI with no
    # --lane flag, must both still speak DISCORD_COMMS_WEBHOOK_URL and emit
    # the pre-lane line format, exactly as before this feature existed.
    swarm_mailbox.post(RUNID, "alpha", "finding", "first")
    assert mirror.run_once(RUNID) == 0
    content = webhook.requests[0]["content"]
    assert "[studio/alpha] finding: first" in content


def test_default_lane_missing_secret_names_all_lane_var(capsys):
    with pytest.raises(SystemExit) as exc:
        mirror.run_once(RUNID)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "DISCORD_COMMS_WEBHOOK_URL=" in err
    assert "DISCORD_COMMS_CONVO_WEBHOOK_URL" not in err


def test_main_without_lane_flag_behaves_as_default_lane(webhook):
    swarm_mailbox.post(RUNID, "alpha", "finding", "via main default lane")
    assert mirror.main(["mirror.py", "--once", RUNID]) == 0
    assert "via main default lane" in webhook.requests[0]["content"]
    # cursor landed under the default-lane state dir, not a convo one
    assert os.path.isfile(mirror._cursor_path(RUNID))
    assert os.path.isfile(mirror._cursor_path(RUNID, "all"))


# ---- convo lane: secret var --------------------------------------------


def test_convo_lane_missing_secret_names_convo_var(capsys):
    with pytest.raises(SystemExit) as exc:
        mirror.run_once(RUNID, lane="convo")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "DISCORD_COMMS_CONVO_WEBHOOK_URL=" in err
    assert "http" not in err


def test_convo_lane_reads_its_own_env_var(monkeypatch):
    monkeypatch.setenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", "http://example.invalid/convo")
    assert mirror.resolve_webhook_url("convo") == "http://example.invalid/convo"


def test_convo_lane_secret_read_from_secrets_file(tmp_path, monkeypatch):
    (tmp_path / "comms.env").write_text(
        "DISCORD_COMMS_CONVO_WEBHOOK_URL=http://example.invalid/from-file\n"
    )
    assert mirror.resolve_webhook_url("convo") == "http://example.invalid/from-file"


# ---- convo lane: separate state dir / cursors ------------------------------


def test_convo_lane_cursor_path_differs_from_default_lane():
    default_path = mirror._cursor_path(RUNID)
    convo_path = mirror._cursor_path(RUNID, "convo")
    assert default_path != convo_path
    assert "discord-mirror-convo" in convo_path
    assert "discord-mirror-convo" not in default_path


def test_convo_lane_cursor_independent_of_default_lane_cursor(monkeypatch):
    convo_url = "http://127.0.0.1:1/convo"
    all_url = "http://127.0.0.1:1/all"
    monkeypatch.setenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", convo_url)
    monkeypatch.setenv("DISCORD_COMMS_WEBHOOK_URL", all_url)

    def fake_post(url, content):
        return True

    monkeypatch.setattr(mirror, "post_content", fake_post)
    swarm_mailbox.post(RUNID, "alpha", "comment", "hello there")
    assert mirror.run_once(RUNID, lane="all") == 0
    assert mirror.run_once(RUNID, lane="convo") == 0
    all_cursor = mirror._load_cursor(RUNID, "all")
    convo_cursor = mirror._load_cursor(RUNID, "convo")
    assert all_cursor == {"alpha": 1}
    assert convo_cursor == {"alpha": 1}
    # Two separate files on disk, not one shared cursor.
    assert mirror._cursor_path(RUNID, "all") != mirror._cursor_path(RUNID, "convo")


# ---- convo lane: row filter -------------------------------------------


def test_convo_lane_mirrors_unicast_topic_rows(monkeypatch):
    posted = []
    monkeypatch.setattr(mirror, "post_content", lambda url, content: posted.append(content) or True)
    monkeypatch.setenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", "http://127.0.0.1:1/convo")
    swarm_mailbox.post(RUNID, "alpha", "finding", "direct msg", to="bravo")
    assert mirror.run_once(RUNID, lane="convo") == 0
    assert posted and "direct msg" in posted[0]


def test_convo_lane_mirrors_comment_and_reply_kinds(monkeypatch):
    posted = []
    monkeypatch.setattr(mirror, "post_content", lambda url, content: posted.append(content) or True)
    monkeypatch.setenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", "http://127.0.0.1:1/convo")
    swarm_mailbox.post(RUNID, "alpha", "comment", "chatting")
    swarm_mailbox.post(RUNID, "beta", "reply", "replying")
    assert mirror.run_once(RUNID, lane="convo") == 0
    joined = "\n".join(posted)
    assert "chatting" in joined
    assert "replying" in joined


def test_convo_lane_filters_out_plain_finding_status(monkeypatch):
    posted = []
    monkeypatch.setattr(mirror, "post_content", lambda url, content: posted.append(content) or True)
    monkeypatch.setenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", "http://127.0.0.1:1/convo")
    swarm_mailbox.post(RUNID, "alpha", "finding", "not conversation")
    swarm_mailbox.post(RUNID, "alpha", "status", "still not conversation")
    assert mirror.run_once(RUNID, lane="convo") == 0
    assert posted == []  # nothing to post: no message sent at all


def test_convo_lane_filtered_rows_still_advance_cursor(monkeypatch):
    monkeypatch.setenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", "http://127.0.0.1:1/convo")
    swarm_mailbox.post(RUNID, "alpha", "finding", "not conversation")
    assert mirror.run_once(RUNID, lane="convo") == 0
    cursor = mirror._load_cursor(RUNID, "convo")
    assert cursor.get("alpha") == 1  # cursor moved even though nothing posted
    # A second pass with no new rows still posts nothing and cursor unchanged.
    assert mirror.run_once(RUNID, lane="convo") == 0
    assert mirror._load_cursor(RUNID, "convo").get("alpha") == 1


def test_convo_lane_mixed_batch_only_posts_convo_rows(monkeypatch):
    posted = []
    monkeypatch.setattr(mirror, "post_content", lambda url, content: posted.append(content) or True)
    monkeypatch.setenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", "http://127.0.0.1:1/convo")
    swarm_mailbox.post(RUNID, "alpha", "finding", "skip me")
    swarm_mailbox.post(RUNID, "alpha", "comment", "keep me")
    swarm_mailbox.post(RUNID, "alpha", "status", "skip me too")
    assert mirror.run_once(RUNID, lane="convo") == 0
    joined = "\n".join(posted)
    assert "keep me" in joined
    assert "skip me" not in joined
    assert "skip me too" not in joined
    # cursor advanced past ALL three rows for alpha, not just the posted one
    assert mirror._load_cursor(RUNID, "convo") == {"alpha": 3}


def test_all_lane_is_unfiltered_mirrors_everything(monkeypatch):
    posted = []
    monkeypatch.setattr(mirror, "post_content", lambda url, content: posted.append(content) or True)
    monkeypatch.setenv("DISCORD_COMMS_WEBHOOK_URL", "http://127.0.0.1:1/all")
    swarm_mailbox.post(RUNID, "alpha", "finding", "plain finding")
    swarm_mailbox.post(RUNID, "alpha", "comment", "a comment too")
    assert mirror.run_once(RUNID, lane="all") == 0
    joined = "\n".join(posted)
    assert "plain finding" in joined
    assert "a comment too" in joined


# ---- CLI: --lane flag -------------------------------------------------


def test_cli_lane_convo_uses_convo_secret(monkeypatch, capsys):
    monkeypatch.delenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        mirror.main(["mirror.py", "--once", RUNID, "--lane", "convo"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "DISCORD_COMMS_CONVO_WEBHOOK_URL=" in err


def test_cli_lane_bogus_rejected():
    assert mirror.main(["mirror.py", "--once", RUNID, "--lane", "nonsense"]) == 2


def test_cli_lane_convo_posts_to_convo_webhook(monkeypatch):
    posted = []
    monkeypatch.setattr(mirror, "post_content", lambda url, content: posted.append((url, content)) or True)
    monkeypatch.setenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", "http://127.0.0.1:1/convo")
    swarm_mailbox.post(RUNID, "alpha", "comment", "cli convo lane")
    assert mirror.main(["mirror.py", "--once", RUNID, "--lane", "convo"]) == 0
    assert posted and posted[0][0] == "http://127.0.0.1:1/convo"
    assert "cli convo lane" in posted[0][1]


# ---- launchd safety: missing secret must not crash --follow / --follow-all


def test_follow_missing_secret_does_not_raise_and_retries_60s(monkeypatch, capsys):
    monkeypatch.delenv("DISCORD_COMMS_WEBHOOK_URL", raising=False)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt()

    monkeypatch.setattr(mirror.time, "sleep", fake_sleep)
    rc = mirror.follow(RUNID, 5)
    assert rc == 0  # never raised SystemExit out of follow()
    assert sleeps == [60]
    err = capsys.readouterr().err
    assert err.count("\n") == 1  # exactly one stderr line, not the multi-line drop-in
    assert "60" in err


def test_follow_all_missing_secret_does_not_raise_and_retries_60s(monkeypatch, capsys):
    monkeypatch.delenv("DISCORD_COMMS_WEBHOOK_URL", raising=False)
    swarm_mailbox.init(RUNID)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt()

    monkeypatch.setattr(mirror.time, "sleep", fake_sleep)
    rc = mirror.follow_all(5)
    assert rc == 0
    assert sleeps == [60]
    err = capsys.readouterr().err
    assert err.count("\n") == 1


def test_follow_resumes_normal_interval_once_secret_present(webhook):
    swarm_mailbox.post(RUNID, "alpha", "finding", "will mirror")
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt()

    import mirror as _m
    orig_sleep = _m.time.sleep
    _m.time.sleep = fake_sleep
    try:
        rc = mirror.follow(RUNID, 5)
    finally:
        _m.time.sleep = orig_sleep
    assert rc == 0
    assert sleeps == [5]  # normal interval, not the 60s fallback
    assert "will mirror" in webhook.requests[0]["content"]


def test_once_still_exits_2_on_missing_secret_no_retry():
    # --once must NOT swallow a missing secret: launchd safety is a
    # --follow/--follow-all concern only.
    with pytest.raises(SystemExit) as exc:
        mirror.main(["mirror.py", "--once", RUNID])
    assert exc.value.code == 2


# ---- --follow-all: discover every run and mirror each ---------------------


def test_follow_all_discovers_and_mirrors_every_run(monkeypatch):
    posted = []
    monkeypatch.setattr(mirror, "post_content", lambda url, content: posted.append(content) or True)
    monkeypatch.setenv("DISCORD_COMMS_WEBHOOK_URL", "http://127.0.0.1:1/all")
    swarm_mailbox.post("run-a", "alpha", "finding", "in run a")
    swarm_mailbox.post("run-b", "beta", "finding", "in run b")

    def fake_sleep(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(mirror.time, "sleep", fake_sleep)
    rc = mirror.follow_all(5)
    assert rc == 0
    joined = "\n".join(posted)
    assert "in run a" in joined
    assert "in run b" in joined


def test_follow_all_honors_lane_argument(monkeypatch):
    posted = []
    monkeypatch.setattr(mirror, "post_content", lambda url, content: posted.append(content) or True)
    monkeypatch.setenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", "http://127.0.0.1:1/convo")
    swarm_mailbox.post("run-c", "alpha", "comment", "chit chat")
    swarm_mailbox.post("run-c", "alpha", "finding", "not chat")

    def fake_sleep(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(mirror.time, "sleep", fake_sleep)
    rc = mirror.follow_all(5, lane="convo")
    assert rc == 0
    joined = "\n".join(posted)
    assert "chit chat" in joined
    assert "not chat" not in joined


def test_main_follow_all_dispatches(monkeypatch):
    posted = []
    monkeypatch.setattr(mirror, "post_content", lambda url, content: posted.append(content) or True)
    monkeypatch.setenv("DISCORD_COMMS_WEBHOOK_URL", "http://127.0.0.1:1/all")
    swarm_mailbox.post("run-d", "alpha", "finding", "via follow-all cli")

    def fake_sleep(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(mirror.time, "sleep", fake_sleep)
    rc = mirror.main(["mirror.py", "--follow-all"])
    assert rc == 0
    assert "via follow-all cli" in "\n".join(posted)


# ---- S1: a per-run exception must not kill the follow loop ----------------


def test_follow_survives_run_once_exception(webhook, capsys, monkeypatch):
    swarm_mailbox.post(RUNID, "alpha", "finding", "will error on save")

    def raise_permission(*a, **k):
        raise PermissionError("state dir not writable")

    monkeypatch.setattr(mirror, "_save_cursor", raise_permission)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt()

    monkeypatch.setattr(mirror.time, "sleep", fake_sleep)
    rc = mirror.follow(RUNID, 5)  # must not raise PermissionError out
    assert rc == 1  # loud (a run failed) but the loop survived, never raised
    assert sleeps == [5]  # normal interval -- this is not the missing-secret case
    err = capsys.readouterr().err
    assert RUNID in err
    assert "PermissionError" in err


def test_follow_all_survives_one_run_failing(webhook, capsys, monkeypatch):
    swarm_mailbox.post("run-ok", "alpha", "finding", "fine")
    swarm_mailbox.post("run-bad", "alpha", "finding", "boom")
    orig_save = mirror._save_cursor

    def flaky_save(runid, cursor, lane=mirror.DEFAULT_LANE):
        if runid == "run-bad":
            raise PermissionError("state dir not writable")
        return orig_save(runid, cursor, lane)

    monkeypatch.setattr(mirror, "_save_cursor", flaky_save)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt()

    monkeypatch.setattr(mirror.time, "sleep", fake_sleep)
    rc = mirror.follow_all(5)  # must not raise out of the loop
    assert rc == 1  # loud (one run failed) but the loop survived, never raised
    assert sleeps == [5]
    err = capsys.readouterr().err
    assert "run-bad" in err
    assert "PermissionError" in err
    # the good run still got delivered despite the bad one failing
    joined = "\n".join(r["content"] for r in webhook.requests)
    assert "fine" in joined
    assert "boom" in joined  # run-bad's row still POSTed; only its cursor save failed


# ---- S6/S7: no idle cursor rewrites, no orphan cursors for empty runs -----


def test_run_once_no_new_rows_skips_cursor_rewrite(webhook, monkeypatch):
    swarm_mailbox.post(RUNID, "alpha", "finding", "one")
    assert mirror.run_once(RUNID) == 0
    calls = []
    orig_save = mirror._save_cursor
    monkeypatch.setattr(
        mirror, "_save_cursor",
        lambda *a, **k: (calls.append(1), orig_save(*a, **k))[1],
    )
    assert mirror.run_once(RUNID) == 0  # nothing new this pass
    assert calls == []  # _save_cursor never called: no idle rewrite


def test_run_once_second_pass_no_new_rows_leaves_cursor_mtime_untouched(webhook):
    swarm_mailbox.post(RUNID, "alpha", "finding", "one")
    assert mirror.run_once(RUNID) == 0
    cursor_path = mirror._cursor_path(RUNID)
    old_mtime = os.path.getmtime(cursor_path) - 100  # detectable if rewritten
    os.utime(cursor_path, (old_mtime, old_mtime))
    assert mirror.run_once(RUNID) == 0
    assert os.path.getmtime(cursor_path) == old_mtime


def test_run_once_empty_run_creates_no_orphan_cursor_file(webhook):
    # A runid nothing has ever posted to: no mailbox dir, no rows, ever.
    assert mirror.run_once("never-posted-run") == 0
    assert not os.path.exists(mirror._cursor_path("never-posted-run"))


# ---- S3 (cheap half): pid-suffixed cursor tmp name -------------------------


def test_cursor_tmp_path_includes_pid():
    tmp = mirror._cursor_tmp_path(RUNID, "all")
    assert tmp == mirror._cursor_path(RUNID, "all") + ".tmp." + str(os.getpid())


def test_save_cursor_uses_pid_suffixed_tmp_name(webhook):
    swarm_mailbox.post(RUNID, "alpha", "finding", "one")
    assert mirror.run_once(RUNID) == 0
    # the pid-suffixed tmp file was cleaned up by os.replace, only the real
    # cursor file remains
    assert os.path.isfile(mirror._cursor_path(RUNID))
    assert not os.path.isfile(mirror._cursor_tmp_path(RUNID))
