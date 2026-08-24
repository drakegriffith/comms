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


def _append_raw(seat, kind, text, topic=None, at=None):
    """Write a row straight to the seat's jsonl -- the on-disk contract --
    so tests can carry kinds VALID_KINDS does not (yet) allow, exactly what
    the parallel kind-vocabulary branch will produce."""
    d = os.path.join(os.environ["COMMS_ROOT"], "comms-%s" % RUNID)
    os.makedirs(d, exist_ok=True)
    row = {
        "seat": seat,
        "at": at or "2026-08-21T00:00:00+00:00",
        "kind": kind,
        "text": text,
    }
    if topic is not None:
        row["topic"] = topic
    with open(os.path.join(d, "%s.jsonl" % seat), "a") as fh:
        fh.write(json.dumps(row) + "\n")


def _fake_post(monkeypatch, posted):
    """Install a post_content stand-in that records (url, content, username)
    without touching the network. `posted` accumulates 3-tuples."""

    def fake(url, content, username=None):
        posted.append((url, content, username))
        return True

    monkeypatch.setattr(mirror, "post_content", fake)


# ---- build_content: kind -> emoji, event-shape precedence -----------------


def test_build_content_kind_emoji_prefixes():
    cases = {
        "finding": "\U0001f4ec✅",
        "comment": "\U0001f4ec\U0001f4ac",
        "reply": "↩️",
        "claim": "\U0001f4cc",
        "blocker": "\U0001f6a7",
        "status": "ℹ️",
    }
    for kind, emoji in cases.items():
        row = {"seat": "a", "kind": kind, "text": "t", "topic": "default"}
        assert mirror.build_content(row) == "%s t" % emoji


def test_build_content_unknown_kind_falls_back_to_info_emoji():
    row = {"seat": "a", "kind": "mystery", "text": "t", "topic": "default"}
    assert mirror.build_content(row) == "ℹ️ t"


def test_build_content_truncates_to_300_chars_of_text():
    row = {"seat": "a", "kind": "finding", "text": "x" * 400, "topic": "default"}
    content = mirror.build_content(row)
    assert content.endswith("x" * 300)
    assert "x" * 301 not in content


def test_build_content_session_started_renders_agent_born_verb():
    row = {
        "seat": "alpha",
        "kind": "status",
        "text": "session started in /Users/drake/code/comms",
        "topic": "default",
    }
    assert mirror.build_content(row) == "\U0001f423 I am awake in /Users/drake/code/comms"


def test_build_content_status_without_session_started_shape_uses_status_emoji():
    row = {"seat": "alpha", "kind": "status", "text": "still working", "topic": "default"}
    assert mirror.build_content(row) == "ℹ️ still working"


def test_build_content_unicast_renders_to_target_seat():
    row = {"seat": "alpha", "kind": "finding", "text": "direct msg", "topic": "@bravo"}
    assert mirror.build_content(row) == "\U0001f4e8 to bravo: direct msg"


def test_build_content_unicast_overrides_kind_emoji():
    # A unicast finding/blocker/claim renders the "to <seat>" shape, not its
    # kind's emoji -- a direct message is conversation regardless of kind
    # (same rule the convo-lane filter already applies, see swarm_mailbox).
    row = {"seat": "alpha", "kind": "blocker", "text": "stuck", "topic": "@bravo"}
    assert mirror.build_content(row) == "\U0001f4e8 to bravo: stuck"


def test_build_content_bridge_row_bare_agent_id_never_bare_object():
    # This is the exact complaint the feature exists to fix: "-> aecd8555b8a274737: comment"
    row = {
        "seat": "dispatch",
        "kind": "comment",
        "text": "-> aecd8555b8a274737: comment",
        "topic": "default",
    }
    content = mirror.build_content(row)
    assert "aecd8555b8a274737" not in content  # raw id never the bare object
    assert content == "\U0001f4ec\U0001f4ac sent to a subagent (aecd8555): comment"


def test_build_content_bridge_row_to_a_real_seat_renders_readably():
    row = {
        "seat": "dispatch",
        "kind": "comment",
        "text": "-> worker: needs review",
        "topic": "default",
    }
    assert mirror.build_content(row) == "\U0001f4ec\U0001f4ac sent to worker: needs review"


def test_build_content_bridge_row_precedence_under_unicast():
    # A bridge-shaped text on an actual unicast topic renders as the unicast
    # (bridge rows in production never carry an "@" topic, see mirror.py's
    # module docstring, but the precedence must still be well-defined).
    row = {
        "seat": "dispatch",
        "kind": "comment",
        "text": "-> aecd8555b8a274737: comment",
        "topic": "@bravo",
    }
    assert mirror.build_content(row) == "\U0001f4e8 to bravo: -> aecd8555b8a274737: comment"


# ---- build_author: identity roster, sanitization ---------------------------


def test_build_author_with_full_identity():
    identity = {"model": "Kimi K3", "project": "agent-os", "area": "hooks/"}
    assert mirror.build_author("kimi1", identity, "macbook") == "kimi1 · Kimi K3 on agent-os (macbook)"


def test_build_author_partial_identity_drops_absent_parts():
    assert mirror.build_author("kimi1", {"model": "Opus 5"}, "macbook") == "kimi1 · Opus 5 (macbook)"
    assert mirror.build_author("kimi1", {"project": "agent-os"}, "macbook") == "kimi1 · on agent-os (macbook)"


def test_build_author_without_identity_degrades_to_seat_and_machine():
    assert mirror.build_author("alpha", None, "studio") == "alpha (studio)"
    assert mirror.build_author("alpha", {}, "studio") == "alpha (studio)"


def test_build_author_strips_everyone_and_here_mentions():
    assert "@everyone" not in mirror.build_author("@everyone", None, "studio")
    assert "@here" not in mirror.build_author("@here", None, "studio")
    assert mirror.build_author("@everyone", None, "studio") == "everyone (studio)"


def test_build_author_mention_strip_is_case_insensitive():
    assert "@Everyone" not in mirror.build_author("@Everyone", None, "studio")


def test_build_author_strips_zero_width_characters():
    smuggled = "alpha​‌‍﻿"
    author = mirror.build_author(smuggled, None, "studio")
    for ch in "​‌‍﻿":
        assert ch not in author


def test_format_row_returns_author_content_tuple():
    row = {"seat": "alpha", "kind": "finding", "text": "cursor landed", "topic": "default"}
    author, content = mirror.format_row(row, "studio")
    assert author == "alpha (studio)"
    assert content == "\U0001f4ec✅ cursor landed"


def test_format_row_joins_identity_by_seat():
    row = {"seat": "kimi1", "kind": "finding", "text": "hook rot in leg 2", "topic": "default"}
    identity = {"model": "Kimi K3", "project": "agent-os", "area": "hooks/"}
    author, content = mirror.format_row(row, "macbook", identity)
    assert author == "kimi1 · Kimi K3 on agent-os (macbook)"
    assert content == "\U0001f4ec✅ hook rot in leg 2"


# ---- enrollment identity roster (unchanged surface) ------------------------


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
    assert swarm_arm.seat_identities("never-armed") == {}


def test_machine_label_env_overrides_hostname():
    assert mirror.machine_label() == "studio"


def test_machine_label_falls_back_to_short_hostname(monkeypatch):
    monkeypatch.delenv("COMMS_MACHINE_LABEL", raising=False)
    label = mirror.machine_label()
    assert label and "." not in label


# ---- chunk_rows: per-seat authorship, batching, cap ------------------------


def test_chunk_rows_never_mixes_two_seats_into_one_message():
    rows = [
        {"seat": "alpha", "kind": "finding", "text": "first", "topic": "default"},
        {"seat": "beta", "kind": "blocker", "text": "second", "topic": "default"},
    ]
    chunks = mirror.chunk_rows(rows, "studio")
    assert len(chunks) == 2  # one POST per seat, even though both fit under the cap
    authors = [author for author, _, _ in chunks]
    assert authors == ["alpha (studio)", "beta (studio)"]
    assert chunks[0][2] == [rows[0]]
    assert chunks[1][2] == [rows[1]]


def test_chunk_rows_groups_consecutive_same_seat_rows_into_one_message():
    rows = [
        {"seat": "alpha", "kind": "finding", "text": "first", "topic": "default"},
        {"seat": "alpha", "kind": "comment", "text": "second", "topic": "default"},
    ]
    chunks = mirror.chunk_rows(rows, "studio")
    assert len(chunks) == 1
    author, content, chunk_rows_in = chunks[0]
    assert author == "alpha (studio)"
    assert "first" in content and "second" in content
    assert chunk_rows_in == rows


def test_chunk_rows_seat_change_starts_a_new_chunk_even_under_the_cap():
    rows = [
        {"seat": "alpha", "kind": "finding", "text": "a", "topic": "default"},
        {"seat": "beta", "kind": "finding", "text": "b", "topic": "default"},
        {"seat": "alpha", "kind": "finding", "text": "c", "topic": "default"},
    ]
    chunks = mirror.chunk_rows(rows, "studio", cap=1900)
    # three seat-runs (alpha, beta, alpha), never re-merged even though the
    # same seat reappears later -- ordering must be preserved.
    assert len(chunks) == 3
    assert [a for a, _, _ in chunks] == ["alpha (studio)", "beta (studio)", "alpha (studio)"]


def test_chunk_rows_never_exceeds_cap_even_at_a_tight_boundary():
    """Same-seat rows still respect the content cap: a size tracker off by
    even 2 chars either overflows the cap or splits one message too many."""
    rows = [{"seat": "s", "kind": "finding", "text": "a" * 100, "topic": "default"} for _ in range(3)]
    line_len = len(mirror.build_content(rows[0]))
    assert line_len == 103
    joined_len = line_len * 3 + 2  # two "\n" separators between three lines
    tight_cap = joined_len - 1  # one char too small to hold all three
    chunks = mirror.chunk_rows(rows, "m", cap=tight_cap)
    for _author, content, _rows_in in chunks:
        assert len(content) <= tight_cap
    assert sum(len(rows_in) for _, _, rows_in in chunks) == 3  # no row lost
    exact_chunks = mirror.chunk_rows(rows, "m", cap=joined_len)
    assert len(exact_chunks) == 1


# ---- post_content: username field -----------------------------------------


def test_post_content_includes_username_when_given(webhook):
    assert mirror.post_content(os.environ["DISCORD_COMMS_WEBHOOK_URL"], "hi", username="alpha (studio)") is True
    assert webhook.requests[0] == {"content": "hi", "username": "alpha (studio)"}


def test_post_content_omits_username_key_when_not_given(webhook):
    assert mirror.post_content(os.environ["DISCORD_COMMS_WEBHOOK_URL"], "hi") is True
    assert "username" not in webhook.requests[0]
    assert webhook.requests[0] == {"content": "hi"}


# ---- mirroring, batching, cursor -----------------------------------------


def test_once_posts_two_seats_as_two_separate_authored_messages(webhook):
    swarm_mailbox.post(RUNID, "alpha", "finding", "first")
    swarm_mailbox.post(RUNID, "beta", "blocker", "second")
    assert mirror.run_once(RUNID) == 0
    assert len(webhook.requests) == 2  # one POST per seat, not batched across seats
    by_username = {r["username"]: r["content"] for r in webhook.requests}
    assert by_username["alpha (studio)"] == "\U0001f4ec✅ first"
    assert by_username["beta (studio)"] == "\U0001f6a7 second"


def test_kind_agnostic_mirrors_unknown_kinds(webhook):
    _append_raw("alpha", "comment", "a new kind from the parallel branch")
    assert mirror.run_once(RUNID) == 0
    req = webhook.requests[0]
    assert req["username"] == "alpha (studio)"
    assert req["content"] == "\U0001f4ec\U0001f4ac a new kind from the parallel branch"


def test_once_joins_identity_from_enrollment(webhook):
    swarm_arm.arm(RUNID)
    swarm_arm.enroll(
        RUNID, "agent-k", seat="kimi1",
        model="Kimi K3", project="agent-os", area="hooks/",
    )
    swarm_mailbox.post(RUNID, "kimi1", "finding", "identity rendered")
    swarm_mailbox.post(RUNID, "alpha", "finding", "no identity here")
    assert mirror.run_once(RUNID) == 0
    by_username = {r["username"]: r["content"] for r in webhook.requests}
    assert by_username["kimi1 · Kimi K3 on agent-os (studio)"] == "\U0001f4ec✅ identity rendered"
    assert by_username["alpha (studio)"] == "\U0001f4ec✅ no identity here"


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


def test_existing_cursor_file_is_honored_across_the_rendering_upgrade(webhook):
    """The cursor FORMAT (per-seat row counts) is unchanged by this feature
    -- prove an old-shape cursor file, dropped in before this test ever
    calls run_once, is read correctly and nothing it already counted is
    re-posted on deploy."""
    swarm_mailbox.post(RUNID, "alpha", "finding", "already delivered before upgrade")
    os.makedirs(os.path.dirname(mirror._cursor_path(RUNID)), exist_ok=True)
    with open(mirror._cursor_path(RUNID), "w") as fh:
        json.dump({"alpha": 1}, fh)  # pre-existing cursor: row 0 already seen
    swarm_mailbox.post(RUNID, "alpha", "finding", "posted after upgrade")
    assert mirror.run_once(RUNID) == 0
    assert len(webhook.requests) == 1
    assert "posted after upgrade" in webhook.requests[0]["content"]
    assert "already delivered before upgrade" not in webhook.requests[0]["content"]


def test_long_batch_chunks_under_discord_cap(webhook):
    for i in range(8):
        swarm_mailbox.post(RUNID, "alpha", "finding", ("row%d " % i) * 60)
    assert mirror.run_once(RUNID) == 0
    assert len(webhook.requests) > 1  # would blow the 2000-char cap in one
    for req in webhook.requests:
        assert len(req["content"]) <= 2000
        assert req["username"] == "alpha (studio)"  # same seat -> same author throughout
    joined = "\n".join(r["content"] for r in webhook.requests)
    for i in range(8):
        assert "row%d" % i in joined  # nothing lost to chunking


# ---- 429 handling ---------------------------------------------------------


def test_429_retries_then_delivers(webhook):
    webhook.script[:] = [429, 204]
    swarm_mailbox.post(RUNID, "alpha", "finding", "rate limited once")
    assert mirror.run_once(RUNID) == 0
    assert len(webhook.requests) == 2  # first 429ed, retry delivered


def test_429_response_honors_scripted_retry_after_header_value(webhook, monkeypatch):
    """The fake webhook's do_POST answers every 429 with header
    Retry-After: 0 (see _Hook.do_POST). Prove post_content actually reads
    and uses that header's value instead of its 1s no-header fallback --
    the only thing that makes every other 429 test fast rather than
    flaky-slow. Fixes the test double gap where do_POST could stop sending
    the header entirely and no test would notice."""
    webhook.script[:] = [429, 204]
    sleeps = []
    monkeypatch.setattr(mirror.time, "sleep", lambda s: sleeps.append(s))
    url = os.environ["DISCORD_COMMS_WEBHOOK_URL"]
    assert mirror.post_content(url, "x") is True
    assert sleeps == [0.0]  # not [1.0] -- the header's value was honored


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


# ---- default lane: same rendering upgrade as convo -------------------------


def test_default_lane_secret_var_unchanged():
    assert mirror.SECRET_VAR == "DISCORD_COMMS_WEBHOOK_URL"
    assert mirror.resolve_webhook_url is not None  # still the one entrypoint


def test_default_lane_renders_with_the_same_emoji_content_as_convo(webhook):
    # No --lane anywhere: run_once() with no lane arg, and the CLI with no
    # --lane flag, must both still speak DISCORD_COMMS_WEBHOOK_URL. Content
    # rendering (emoji, per-seat authorship) applies to BOTH lanes now.
    swarm_mailbox.post(RUNID, "alpha", "finding", "first")
    assert mirror.run_once(RUNID) == 0
    req = webhook.requests[0]
    assert req["username"] == "alpha (studio)"
    assert req["content"] == "\U0001f4ec✅ first"


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
    monkeypatch.setattr(mirror, "post_content", lambda url, content, username=None: True)
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
    _fake_post(monkeypatch, posted)
    monkeypatch.setenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", "http://127.0.0.1:1/convo")
    swarm_mailbox.post(RUNID, "alpha", "finding", "direct msg", to="bravo")
    assert mirror.run_once(RUNID, lane="convo") == 0
    assert posted and "direct msg" in posted[0][1]


def test_convo_lane_mirrors_comment_and_reply_kinds(monkeypatch):
    posted = []
    _fake_post(monkeypatch, posted)
    monkeypatch.setenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", "http://127.0.0.1:1/convo")
    swarm_mailbox.post(RUNID, "alpha", "comment", "chatting")
    swarm_mailbox.post(RUNID, "beta", "reply", "replying")
    assert mirror.run_once(RUNID, lane="convo") == 0
    joined = "\n".join(c for _, c, _ in posted)
    assert "chatting" in joined
    assert "replying" in joined


def test_convo_lane_filters_out_plain_finding_status(monkeypatch):
    posted = []
    _fake_post(monkeypatch, posted)
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
    _fake_post(monkeypatch, posted)
    monkeypatch.setenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", "http://127.0.0.1:1/convo")
    swarm_mailbox.post(RUNID, "alpha", "finding", "skip me")
    swarm_mailbox.post(RUNID, "alpha", "comment", "keep me")
    swarm_mailbox.post(RUNID, "alpha", "status", "skip me too")
    assert mirror.run_once(RUNID, lane="convo") == 0
    joined = "\n".join(c for _, c, _ in posted)
    assert "keep me" in joined
    assert "skip me" not in joined
    assert "skip me too" not in joined
    # cursor advanced past ALL three rows for alpha, not just the posted one
    assert mirror._load_cursor(RUNID, "convo") == {"alpha": 3}


def test_all_lane_is_unfiltered_mirrors_everything(monkeypatch):
    posted = []
    _fake_post(monkeypatch, posted)
    monkeypatch.setenv("DISCORD_COMMS_WEBHOOK_URL", "http://127.0.0.1:1/all")
    swarm_mailbox.post(RUNID, "alpha", "finding", "plain finding")
    swarm_mailbox.post(RUNID, "alpha", "comment", "a comment too")
    assert mirror.run_once(RUNID, lane="all") == 0
    joined = "\n".join(c for _, c, _ in posted)
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

    def fake(url, content, username=None):
        posted.append((url, content, username))
        return True

    monkeypatch.setattr(mirror, "post_content", fake)
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
    _fake_post(monkeypatch, posted)
    monkeypatch.setenv("DISCORD_COMMS_WEBHOOK_URL", "http://127.0.0.1:1/all")
    swarm_mailbox.post("run-a", "alpha", "finding", "in run a")
    swarm_mailbox.post("run-b", "beta", "finding", "in run b")

    def fake_sleep(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(mirror.time, "sleep", fake_sleep)
    rc = mirror.follow_all(5)
    assert rc == 0
    joined = "\n".join(c for _, c, _ in posted)
    assert "in run a" in joined
    assert "in run b" in joined


def test_follow_all_honors_lane_argument(monkeypatch):
    posted = []
    _fake_post(monkeypatch, posted)
    monkeypatch.setenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", "http://127.0.0.1:1/convo")
    swarm_mailbox.post("run-c", "alpha", "comment", "chit chat")
    swarm_mailbox.post("run-c", "alpha", "finding", "not chat")

    def fake_sleep(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(mirror.time, "sleep", fake_sleep)
    rc = mirror.follow_all(5, lane="convo")
    assert rc == 0
    joined = "\n".join(c for _, c, _ in posted)
    assert "chit chat" in joined
    assert "not chat" not in joined


def test_main_follow_all_dispatches(monkeypatch):
    posted = []
    _fake_post(monkeypatch, posted)
    monkeypatch.setenv("DISCORD_COMMS_WEBHOOK_URL", "http://127.0.0.1:1/all")
    swarm_mailbox.post("run-d", "alpha", "finding", "via follow-all cli")

    def fake_sleep(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(mirror.time, "sleep", fake_sleep)
    rc = mirror.main(["mirror.py", "--follow-all"])
    assert rc == 0
    assert "via follow-all cli" in "\n".join(c for _, c, _ in posted)


# ---- follow_all --lane convo: also tails the ingest log --------------------


def test_follow_all_convo_lane_also_tails_ingest_each_pass(monkeypatch):
    calls = []
    monkeypatch.setenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", "http://127.0.0.1:1/convo")

    def fake_tail_once(url):
        calls.append(url)
        return 0

    import ingest_mirror
    monkeypatch.setattr(ingest_mirror, "tail_once", fake_tail_once)

    def fake_sleep(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(mirror.time, "sleep", fake_sleep)
    rc = mirror.follow_all(5, lane="convo")
    assert rc == 0
    assert calls == ["http://127.0.0.1:1/convo"]


def test_follow_all_default_lane_never_tails_ingest(monkeypatch):
    calls = []
    monkeypatch.setenv("DISCORD_COMMS_WEBHOOK_URL", "http://127.0.0.1:1/all")

    def fake_tail_once(url):
        calls.append(url)
        return 0

    import ingest_mirror
    monkeypatch.setattr(ingest_mirror, "tail_once", fake_tail_once)

    def fake_sleep(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(mirror.time, "sleep", fake_sleep)
    rc = mirror.follow_all(5, lane="all")
    assert rc == 0
    assert calls == []  # the "all" lane never touches the heartbeat log


def test_follow_all_convo_lane_survives_ingest_tail_exception(monkeypatch, capsys):
    monkeypatch.setenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", "http://127.0.0.1:1/convo")

    import ingest_mirror

    def boom(url):
        raise PermissionError("state dir not writable")

    monkeypatch.setattr(ingest_mirror, "tail_once", boom)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt()

    monkeypatch.setattr(mirror.time, "sleep", fake_sleep)
    rc = mirror.follow_all(5, lane="convo")  # must not raise PermissionError out
    assert rc == 1
    assert sleeps == [5]
    err = capsys.readouterr().err
    assert "PermissionError" in err


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
    # the good run's cursor was actually persisted through to the real
    # _save_cursor (flaky_save must delegate, not swallow the good case)
    assert mirror._load_cursor("run-ok") == {"alpha": 1}
    # the bad run's cursor never landed: its save raised
    assert mirror._load_cursor("run-bad") == {}


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


# ---- post_content: the OSError/URLError retry path (never previously
# exercised -- only the 429/HTTPError path had tests) ------------------------
# (mutation gate: 6 of 12 survived, all in or around this branch)


class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b""


def test_post_content_retries_transient_oserror_then_delivers(monkeypatch):
    calls = []

    def flaky_urlopen(req, timeout=15):
        calls.append(1)
        if len(calls) < 3:
            raise OSError("connection reset")
        return _FakeResp()

    monkeypatch.setattr(mirror.time, "sleep", lambda s: None)
    monkeypatch.setattr(mirror.urllib.request, "urlopen", flaky_urlopen)
    assert mirror.post_content("http://x.invalid/hook", "hello") is True
    assert len(calls) == 3  # two failures retried, third attempt delivered


def test_post_content_gives_up_after_max_retries_oserror(monkeypatch, capsys):
    calls = []

    def always_fail(req, timeout=15):
        calls.append(1)
        raise OSError("still down")

    monkeypatch.setattr(mirror.time, "sleep", lambda s: None)
    monkeypatch.setattr(mirror.urllib.request, "urlopen", always_fail)
    assert mirror.post_content("http://x.invalid/hook", "hello") is False
    # first attempt + MAX_RETRIES retries, then stop -- never fewer, never more
    assert len(calls) == mirror.MAX_RETRIES + 1
    err = capsys.readouterr().err
    assert ("after %d attempt(s)" % (mirror.MAX_RETRIES + 1)) in err


def test_post_content_non_429_http_error_gives_up_immediately_with_correct_count(monkeypatch, capsys):
    def raise_500(req, timeout=15):
        raise mirror.urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, None)

    monkeypatch.setattr(mirror.urllib.request, "urlopen", raise_500)
    assert mirror.post_content("http://x.invalid/hook", "hello") is False
    err = capsys.readouterr().err
    assert "HTTP 500" in err
    # a single attempt was made -- the message must report exactly that
    assert "after 1 attempt(s)" in err


# ---- main(): --interval and a truncated --lane must be parsed correctly --
# (mutation gate: 7 of 12 survived -- --interval had NO direct test at all,
# and --lane's missing-value guard was only ever exercised with a full,
# valid flag set)


def test_main_interval_flag_is_parsed_and_stripped_before_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mirror, "follow",
        lambda runid, interval, lane=mirror.DEFAULT_LANE: calls.append((runid, interval, lane)) or 0,
    )
    rc = mirror.main(["mirror.py", "--follow", RUNID, "--interval", "7"])
    assert rc == 0
    assert calls == [(RUNID, 7.0, mirror.DEFAULT_LANE)]


def test_main_interval_flag_bad_value_returns_2(capsys):
    assert mirror.main(["mirror.py", "--follow", RUNID, "--interval", "notanumber"]) == 2
    assert "--interval needs a number" in capsys.readouterr().err


def test_main_lane_flag_missing_value_returns_2(capsys):
    assert mirror.main(["mirror.py", "--once", RUNID, "--lane"]) == 2
    assert "--lane needs a value" in capsys.readouterr().err
