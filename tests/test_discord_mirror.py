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
