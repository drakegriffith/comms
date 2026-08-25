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
import swarm_threads  # noqa: E402

RUNID = "mirror-test"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every read/write knob the mirror touches points into tmp_path."""
    monkeypatch.setenv("COMMS_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("COMMS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COMMS_SECRETS_FILE", str(tmp_path / "comms.env"))
    monkeypatch.delenv("DISCORD_COMMS_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_COMMS_FORUM_WEBHOOK_URL", raising=False)
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


def _cursor_count(cursor, seat):
    """How many of `seat`'s rows this cursor has counted, across every source
    file it holds a key for. Cursor keys are "<seat>/<file>#<inode>" (see
    swarm_mailbox.cursor_key) and an inode is not knowable in advance, so a
    test asks this question instead of spelling a literal key."""
    prefix = seat + swarm_mailbox.CURSOR_KEY_SEP
    return sum(v for k, v in cursor.items() if k == seat or k.startswith(prefix))


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


# ---- forum board webhook: resolution only, NOT a posting lane -------------
#
# Slice 1 (issue #38) resolves DISCORD_COMMS_FORUM_WEBHOOK_URL through the
# same env-or-secrets-file path as the lane vars above. It is deliberately
# NOT wired into LANE_SECRET_VARS / LANE_STATE_DIRS / --lane: forum posts
# need thread_name / ?thread_id=, which slice 2 owns. These tests pin both
# halves of that contract -- resolution works, lane-folding did not happen.


def test_forum_secret_var_name():
    assert mirror.FORUM_SECRET_VAR == "DISCORD_COMMS_FORUM_WEBHOOK_URL"


def test_forum_webhook_resolved_from_env(monkeypatch):
    monkeypatch.setenv("DISCORD_COMMS_FORUM_WEBHOOK_URL", "http://example.invalid/forum")
    assert mirror.find_forum_webhook_url() == "http://example.invalid/forum"


def test_forum_webhook_resolved_from_secrets_file(tmp_path):
    (tmp_path / "comms.env").write_text(
        "DISCORD_COMMS_FORUM_WEBHOOK_URL=http://example.invalid/forum-from-file\n"
    )
    assert mirror.find_forum_webhook_url() == "http://example.invalid/forum-from-file"


def test_forum_webhook_missing_returns_none_quietly(capsys):
    assert mirror.find_forum_webhook_url() is None
    err = capsys.readouterr().err
    assert err == ""  # no side effects -- same contract as _find_webhook_url


def test_forum_webhook_missing_exits_2_naming_drop_in(capsys):
    with pytest.raises(SystemExit) as exc:
        mirror.resolve_forum_webhook_url()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "DISCORD_COMMS_FORUM_WEBHOOK_URL=" in err
    assert "~/.secrets/comms.env" in err
    assert "http" not in err  # never echoes any URL value


def test_forum_secret_is_the_board_lanes_secret_and_no_lane_is_named_forum():
    # UPDATED BY SLICE 2 (#40). Slice 1 pinned that this var was NOT wired
    # into a lane, because nothing could post a forum's payload shape yet.
    # This slice wires it -- to a lane named "board", after what it is to a
    # human, not after Discord's channel type. The half that still holds:
    # there is no lane named "forum", so `--lane forum` is still rejected.
    assert mirror.LANE_SECRET_VARS["board"] == mirror.FORUM_SECRET_VAR
    assert "forum" not in mirror.LANE_SECRET_VARS


def test_no_lane_is_named_forum_in_the_state_dirs_either():
    assert "forum" not in mirror.LANE_STATE_DIRS


def test_cli_lane_forum_is_rejected_not_a_posting_lane(capsys):
    # --lane forum must NOT work: forum is a resolved secret, not a lane.
    assert mirror.main(["mirror.py", "--once", RUNID, "--lane", "forum"]) == 2
    err = capsys.readouterr().err
    assert "--lane must be one of" in err
    assert "forum" not in err


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
    assert _cursor_count(all_cursor, "alpha") == 1
    assert _cursor_count(convo_cursor, "alpha") == 1
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
    assert _cursor_count(cursor, "alpha") == 1  # moved even though nothing posted
    # A second pass with no new rows still posts nothing and cursor unchanged.
    assert mirror.run_once(RUNID, lane="convo") == 0
    assert _cursor_count(mirror._load_cursor(RUNID, "convo"), "alpha") == 1


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
    assert _cursor_count(mirror._load_cursor(RUNID, "convo"), "alpha") == 3


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
    assert _cursor_count(mirror._load_cursor("run-ok"), "alpha") == 1
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


# ---- source-file cursor identity (#20, #23) --------------------------------
#
# The mirror reads EVERY .jsonl in the run dir, including the pull mirror
# `remote~<hub>.jsonl` that adapters/remote writes. Those rows are COPIES of
# hub rows the hub's own mirror already posted to this same channel, and one
# seat can own rows in both files at once -- the two facts behind #20 (double
# post) and #23 (an older-`at` pulled row shifting a per-seat count cursor).


def _append_to_file(filename, seat, text, at, kind="finding", runid=RUNID):
    """Write one row straight into a NAMED file, which is what tells a pulled
    copy from a first-class row -- _append_raw always writes <seat>.jsonl."""
    d = os.path.join(os.environ["COMMS_ROOT"], "comms-%s" % runid)
    os.makedirs(d, exist_ok=True)
    row = {"seat": seat, "at": at, "kind": kind, "text": text, "topic": "default"}
    with open(os.path.join(d, filename), "a") as fh:
        fh.write(json.dumps(row) + "\n")


def test_pulled_rows_are_never_posted_a_second_time(webhook):
    """#20: rows in remote~<hub>.jsonl were already posted to this channel by
    the hub's own mirror. Posting them here is the cross-machine double
    mirror that moved 167 rows on the first live pull."""
    _append_to_file("remote~studio.jsonl", "beta~studio", "pulled row",
                    "2026-08-24T10:00:00+00:00")
    assert mirror.run_once(RUNID) == 0
    assert webhook.requests == []


def test_first_class_pushed_seat_file_is_still_posted(webhook):
    """The direction control for the test above. A pushed row lands on the
    HUB as a first-class `alpha~macbook.jsonl`, whose only mirror is this
    one -- so "skip any seat containing ~" would silence exactly the rows
    that most need posting. The discriminator is the FILE."""
    _append_to_file("alpha~macbook.jsonl", "alpha~macbook", "pushed row",
                    "2026-08-24T10:00:00+00:00")
    assert mirror.run_once(RUNID) == 0
    assert len(webhook.requests) == 1
    assert "pushed row" in webhook.requests[0]["content"]


def test_pulled_rows_still_advance_the_cursor(webhook):
    """Count-but-skip, the same shape the lane filter already uses: a
    filtered row that did not advance the cursor would be re-scanned on
    every poll forever."""
    _append_to_file("remote~studio.jsonl", "beta~studio", "pulled row",
                    "2026-08-24T10:00:00+00:00")
    assert mirror.run_once(RUNID) == 0
    cursor = mirror._load_cursor(RUNID)
    assert [v for v in cursor.values()] == [1]
    assert [k for k in cursor if "remote~studio.jsonl" in k]


def test_pulled_row_with_an_older_at_never_reposts_a_delivered_row(webhook):
    """#23, the verifier's repro: seat X~studio owns rows in its own file AND
    in the pull mirror. A pull lands a row whose `at` sorts BETWEEN two rows
    already posted from the first-class file; under a per-seat count that
    shifts the merged index sequence and 'pushed-B' posts twice."""
    _append_to_file("x~studio.jsonl", "x~studio", "pushed-A",
                    "2026-08-24T10:00:00+00:00")
    _append_to_file("x~studio.jsonl", "x~studio", "pushed-B",
                    "2026-08-24T12:00:00+00:00")
    assert mirror.run_once(RUNID) == 0
    assert len(webhook.requests) == 1
    assert "pushed-A" in webhook.requests[0]["content"]
    assert "pushed-B" in webhook.requests[0]["content"]
    # The pull brings home an OLDER row for the same seat, other file.
    _append_to_file("remote~laptop.jsonl", "x~studio", "pulled-older",
                    "2026-08-24T11:00:00+00:00")
    assert mirror.run_once(RUNID) == 0
    assert len(webhook.requests) == 1  # nothing new: no repost, no duplicate


def test_normal_poll_still_advances_the_new_shape_cursor(webhook):
    """Positive control: the cursor a plain local run writes is keyed by
    seat AND source file, and it still moves."""
    swarm_mailbox.post(RUNID, "alpha", "finding", "one")
    assert mirror.run_once(RUNID) == 0
    cursor = mirror._load_cursor(RUNID)
    assert list(cursor.values()) == [1]
    key = list(cursor)[0]
    assert key.startswith("alpha/alpha.jsonl#")
    swarm_mailbox.post(RUNID, "alpha", "finding", "two")
    assert mirror.run_once(RUNID) == 0
    assert mirror._load_cursor(RUNID)[key] == 2
    assert len(webhook.requests) == 2


def test_old_shape_cursor_migrates_in_place_on_the_next_poll(webhook):
    """Back-compat on deploy: the old {seat: count} file is read, honored,
    and rewritten in the new key space -- the bare seat key retired, so the
    same budget is never spent twice."""
    swarm_mailbox.post(RUNID, "alpha", "finding", "delivered before upgrade")
    swarm_mailbox.post(RUNID, "alpha", "finding", "posted after upgrade")
    os.makedirs(os.path.dirname(mirror._cursor_path(RUNID)), exist_ok=True)
    with open(mirror._cursor_path(RUNID), "w") as fh:
        json.dump({"alpha": 1}, fh)  # pre-#39 shape: row 0 already seen
    assert mirror.run_once(RUNID) == 0
    assert len(webhook.requests) == 1
    assert "posted after upgrade" in webhook.requests[0]["content"]
    cursor = mirror._load_cursor(RUNID)
    assert "alpha" not in cursor
    assert list(cursor.values()) == [2]
    # ...and the migrated cursor is stable: a third poll posts nothing.
    assert mirror.run_once(RUNID) == 0
    assert len(webhook.requests) == 1


def test_skipped_log_records_the_row_as_authored_not_the_read_time_tag(webhook, capsys):
    """The skipped file is the durable replay record, so it must hold the
    row its author wrote -- the source tag is a fact about THIS machine's
    disk, never part of the row.

    The first assertion is the positive control that makes the second one
    mean something: prove the row the mirror actually posts from IS tagged,
    or "no _src in the log" is true of any code that never tags at all."""
    webhook.script[:] = [429] * (mirror.MAX_RETRIES + 1)
    swarm_mailbox.post(RUNID, "alpha", "finding", "undeliverable")
    fresh, _ = mirror.collect_new(RUNID)
    assert [swarm_mailbox.source_of(r) for r in fresh] != [None]  # tagged on read
    assert mirror.run_once(RUNID) == 1
    with open(mirror._skipped_path(RUNID)) as fh:
        recorded = [json.loads(l) for l in fh]
    assert recorded[0]["row"]["text"] == "undeliverable"
    assert swarm_mailbox.SOURCE_KEY not in recorded[0]["row"]


def test_skipped_log_is_fsynced_before_the_cursor_advances(webhook, monkeypatch):
    """Order is the guarantee: the skipped file is the ONLY record of a row
    the cursor is about to move past, so it has to be on the platter before
    the cursor that forgets it. Without the fsync a crash can persist the
    newer cursor and lose the recovery record -- a silent drop wearing a
    'never silently lossy' docstring."""
    events = []
    real_fsync = os.fsync
    monkeypatch.setattr(mirror.os, "fsync", lambda fd: events.append("fsync") or real_fsync(fd))
    real_save = mirror._save_cursor
    monkeypatch.setattr(
        mirror, "_save_cursor",
        lambda runid, cursor, lane=mirror.DEFAULT_LANE: events.append("save_cursor")
        or real_save(runid, cursor, lane),
    )
    webhook.script[:] = [429] * (mirror.MAX_RETRIES + 1)
    swarm_mailbox.post(RUNID, "alpha", "finding", "undeliverable")
    assert mirror.run_once(RUNID) == 1
    assert "fsync" in events
    assert events.index("fsync") < events.index("save_cursor")


# ---- one poller per (run, lane): fcntl.flock -------------------------------


def _hold_lock(path):
    """Take the same exclusive flock a running poller holds, from a second
    open file description -- flock is per-description, so this contends with
    this very process exactly as a second process would."""
    import fcntl

    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_second_poller_on_the_same_run_and_lane_no_ops(webhook, capsys):
    """Two pollers on one (run, lane) both read the cursor, both post, and
    both advance it -- double-posted rows, not an error. The lock makes the
    loser a no-op instead of a duplicate."""
    swarm_mailbox.post(RUNID, "alpha", "finding", "only once")
    held = _hold_lock(mirror._lock_path(RUNID))
    try:
        assert mirror.run_once(RUNID) == 0
        assert webhook.requests == []
        assert mirror._load_cursor(RUNID) == {}  # cursor untouched, not advanced
        err = capsys.readouterr().err
        assert "another poller" in err
        assert RUNID in err
    finally:
        os.close(held)


def test_the_rows_are_still_posted_once_the_lock_frees(webhook, capsys):
    """The other half: a skipped pass loses nothing, because the next poll
    delivers exactly what the locked-out one would have."""
    swarm_mailbox.post(RUNID, "alpha", "finding", "delayed, not dropped")
    held = _hold_lock(mirror._lock_path(RUNID))
    assert mirror.run_once(RUNID) == 0
    os.close(held)
    assert mirror.run_once(RUNID) == 0
    assert len(webhook.requests) == 1
    assert "delayed, not dropped" in webhook.requests[0]["content"]


def test_a_different_run_never_contends(webhook):
    """Per (runid, lane), not per process: --follow-all walks every run in
    one pass, so a lock that spanned runs would serialize the whole fleet
    behind one busy run (and slice 2's fleet-wide thread map needs its own
    lock precisely because this one does not span runs)."""
    other = "test-other-run"
    swarm_mailbox.post(other, "alpha", "finding", "other run's row")
    held = _hold_lock(mirror._lock_path(RUNID))
    try:
        assert mirror.run_once(other) == 0
        assert len(webhook.requests) == 1
    finally:
        os.close(held)


def test_a_different_lane_on_the_same_run_never_contends(monkeypatch):
    """One lane's `all` job and another's `convo` job on the same runid are
    a documented, supported pair -- separate state dirs, separate locks."""
    posted = []
    _fake_post(monkeypatch, posted)
    monkeypatch.setenv("DISCORD_COMMS_CONVO_WEBHOOK_URL", "http://127.0.0.1:1/convo")
    swarm_mailbox.post(RUNID, "alpha", "comment", "chatting")
    held = _hold_lock(mirror._lock_path(RUNID, "all"))
    try:
        assert mirror.run_once(RUNID, lane="convo") == 0
        assert posted and "chatting" in posted[0][1]
    finally:
        os.close(held)


def test_lock_is_released_between_passes(webhook):
    """Held for one pass, not for the process's life: back-to-back passes in
    one process must not deadlock, and an ad-hoc --once must be able to run
    between a follower's polls."""
    swarm_mailbox.post(RUNID, "alpha", "finding", "one")
    assert mirror.run_once(RUNID) == 0
    swarm_mailbox.post(RUNID, "alpha", "finding", "two")
    assert mirror.run_once(RUNID) == 0
    assert len(webhook.requests) == 2


def test_lock_file_lives_in_the_lane_state_dir(webhook):
    assert mirror._lock_path(RUNID, "convo") != mirror._lock_path(RUNID, "all")
    assert "discord-mirror-convo" in mirror._lock_path(RUNID, "convo")
    assert mirror._lock_path(RUNID).endswith(".lock")


def test_a_lock_error_that_is_not_contention_is_never_swallowed(monkeypatch):
    """Contention is EAGAIN and nothing else. A permission error, a stale NFS
    handle, or an unwritable state dir must not read as 'someone else is
    polling' -- that turns a broken machine into a mirror that quietly posts
    nothing forever. Under --follow the raise is caught, named with its
    exception class, and retried by _run_once_logged; under --once it is loud."""
    real_flock = mirror.fcntl.flock

    def refuse(fd, op):
        raise PermissionError("flock not permitted here")

    monkeypatch.setattr(mirror.fcntl, "flock", refuse)
    with pytest.raises(PermissionError):
        mirror._acquire_pass_lock(RUNID)
    monkeypatch.setattr(mirror.fcntl, "flock", real_flock)


def test_follow_loop_survives_a_lock_error_instead_of_dying(webhook, monkeypatch, capsys):
    """The other half of the test above: a raising lock must not kill a
    follower's loop, because a launchd KeepAlive job would crash-loop on it."""
    monkeypatch.setattr(
        mirror.fcntl, "flock",
        lambda fd, op: (_ for _ in ()).throw(PermissionError("nope")),
    )
    assert mirror._run_once_logged(RUNID, mirror.DEFAULT_LANE) == 1
    assert "PermissionError" in capsys.readouterr().err


def test_missing_secret_still_exits_2_when_another_poller_holds_the_lock(capsys):
    """Order matters: the missing-secret exit is --once's contract and must
    not be masked into a quiet 0 by a lock another poller happens to hold."""
    held = _hold_lock(mirror._lock_path(RUNID))
    try:
        os.environ.pop("DISCORD_COMMS_WEBHOOK_URL", None)
        with pytest.raises(SystemExit) as exc:
            mirror.run_once(RUNID)
        assert exc.value.code == 2
    finally:
        os.close(held)


# ---- the board lane: threads, held rows, and the drain (issue #40) ---------
#
# The board lane is the only lane whose delivery is DEFERRED: a row about a
# document is not posted when it arrives, it is posted when the document's
# conversation goes alive (two seats, close together -- lib/swarm_threads).
# That means a second piece of state besides the cursor: the HELD file, which
# answers "what have I not yet posted" while the cursor keeps answering "what
# have I read". These tests pin the per-pass order those two impose on each
# other, and the drain, which is the thing the design note says an
# implementer gets wrong.

BOARD = "board"
DOC = "doc:comms/docs/plan.md"
T0 = "2026-08-21T00:00:00+00:00"


def _at(seconds):
    return "2026-08-21T00:%02d:%02d+00:00" % (seconds // 60, seconds % 60)


def _append_thread_row(seat, text, thread=DOC, at=T0, kind="comment"):
    """A row carrying a `thread` field, written straight to the seat's jsonl."""
    d = os.path.join(os.environ["COMMS_ROOT"], "comms-%s" % RUNID)
    os.makedirs(d, exist_ok=True)
    row = {"seat": seat, "at": at, "kind": kind, "text": text, "topic": "default"}
    if thread is not None:
        row["thread"] = thread
    with open(os.path.join(d, "%s.jsonl" % seat), "a") as fh:
        fh.write(json.dumps(row) + "\n")


@pytest.fixture()
def board(monkeypatch):
    """The board lane with its secret set, a FAKE thread-creating poster, and
    post_content captured. The real threads.thread_for runs (map file, lock,
    atomic persist and all) -- only the network is faked, at the composition
    root, which is the seam the design note put there for exactly this."""
    monkeypatch.setenv("DISCORD_COMMS_FORUM_WEBHOOK_URL", "http://127.0.0.1:1/forum")
    created = []

    def fake_webhook_poster(url):
        def poster(name, content):
            created.append((url, name, content))
            return "T%d" % len(created)

        return poster

    monkeypatch.setattr(mirror.threads, "webhook_poster", fake_webhook_poster)

    posted = []
    script = []

    def fake_post(url, content, username=None, allowed_mentions=None):
        posted.append(
            {
                "url": url,
                "content": content,
                "username": username,
                "allowed_mentions": allowed_mentions,
            }
        )
        return script.pop(0) if script else True

    monkeypatch.setattr(mirror, "post_content", fake_post)

    class _Board:
        pass

    b = _Board()
    b.created, b.posted, b.script = created, posted, script
    return b


def _held(lane=BOARD):
    with open(mirror._held_path(RUNID, lane)) as fh:
        return json.load(fh)


def _texts(posted):
    """Every row text that reached Discord, in POST order then line order."""
    out = []
    for p in posted:
        out.extend(p["content"].split("\n"))
    return out


# ---- lane registration ----------------------------------------------------


def test_board_lane_uses_the_forum_secret():
    # Slice 1 kept DISCORD_COMMS_FORUM_WEBHOOK_URL out of LANE_SECRET_VARS
    # because nothing could post to a forum yet. This slice is what changes
    # that: the var is now the "board" lane's secret. The lane is named for
    # what it is to a human (a board), not for Discord's channel type.
    assert mirror.LANE_SECRET_VARS[BOARD] == mirror.FORUM_SECRET_VAR
    assert "forum" not in mirror.LANE_SECRET_VARS


def test_board_lane_state_dir_is_its_own_and_spelled_once():
    assert mirror.LANE_STATE_DIRS[BOARD] == "discord-mirror-board"
    # One spelling: threads.py owns the name (the thread map is its file) and
    # the mirror imports it, so a cursor and a thread map can never end up in
    # two different directories.
    assert mirror.LANE_STATE_DIRS[BOARD] == mirror.threads.STATE_DIRS[BOARD]


def test_every_lane_state_dir_is_disjoint():
    dirs = list(mirror.LANE_STATE_DIRS.values())
    assert len(set(dirs)) == len(dirs)


def test_cli_lane_board_validates_and_names_the_forum_secret(monkeypatch, capsys):
    monkeypatch.delenv("DISCORD_COMMS_FORUM_WEBHOOK_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        mirror.main(["mirror.py", "--once", RUNID, "--lane", BOARD])
    assert exc.value.code == 2
    assert "DISCORD_COMMS_FORUM_WEBHOOK_URL=" in capsys.readouterr().err


def test_board_state_files_all_live_in_the_board_dir():
    for path in (
        mirror._cursor_path(RUNID, BOARD),
        mirror._held_path(RUNID, BOARD),
        mirror._skipped_path(RUNID, BOARD),
        mirror._lock_path(RUNID, BOARD),
        mirror.threads.map_path(BOARD),
    ):
        assert os.path.basename(os.path.dirname(path)) == "discord-mirror-board"


# ---- a thread that is not alive: held, and the cursor still advances -------


def test_board_not_alive_row_is_held_and_the_cursor_advances(board):
    _append_thread_row("alpha", "one seat talking", at=_at(0))
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert board.posted == []          # nothing rendered
    assert board.created == []         # and no thread created for a dead key
    assert [r["text"] for r in _held()[DOC]] == ["one seat talking"]
    cursor = mirror._load_cursor(RUNID, BOARD)
    assert _cursor_count(cursor, "alpha") == 1


def test_board_a_held_row_is_not_held_twice_across_passes(board):
    _append_thread_row("alpha", "one", at=_at(0))
    mirror.run_once(RUNID, lane=BOARD)
    mirror.run_once(RUNID, lane=BOARD)
    # The cursor is what stops the re-read; without it the row would be
    # appended to its bucket again on every single poll, forever.
    assert [r["text"] for r in _held()[DOC]] == ["one"]


def test_board_held_rows_are_stored_as_their_author_wrote_them(board):
    # The read-time source tag is a fact about THIS machine's disk. Persisting
    # it would leak it back out when the row is finally posted or replayed.
    _append_thread_row("alpha", "one", at=_at(0))
    mirror.run_once(RUNID, lane=BOARD)
    assert swarm_mailbox.SOURCE_KEY not in _held()[DOC][0]


def test_board_rows_without_a_thread_are_counted_but_never_posted(board):
    # Deviation from D1's literal "rows without thread take today's path": a
    # forum webhook REJECTS a post carrying neither thread_name nor
    # thread_id, so this lane has no un-threaded path to take. They are
    # already mirrored by the `all` lane; here they are lane-filtered, which
    # is the same count-but-skip the convo lane does.
    _append_raw("alpha", "finding", "no thread on me")
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert board.posted == []
    assert not os.path.exists(mirror._held_path(RUNID, BOARD))
    assert _cursor_count(mirror._load_cursor(RUNID, BOARD), "alpha") == 1


# ---- alive: the drain ------------------------------------------------------


def test_board_second_seat_drains_the_WHOLE_backlog_in_at_order(board):
    # THE bug the design note names: posting only the row that tripped
    # `alive` and leaving the backlog behind. Three earlier rows from alpha
    # are held; bravo answers; every held row must land, oldest first.
    for i in range(3):
        _append_thread_row("alpha", "old %d" % i, at=_at(i * 30))
    mirror.run_once(RUNID, lane=BOARD)
    assert board.posted == []

    _append_thread_row("bravo", "answering", at=_at(90))
    _append_thread_row("alpha", "and again", at=_at(120))
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert _texts(board.posted) == [
        "\U0001f4ec\U0001f4ac old 0",
        "\U0001f4ec\U0001f4ac old 1",
        "\U0001f4ec\U0001f4ac old 2",
        "\U0001f4ec\U0001f4ac answering",
        "\U0001f4ec\U0001f4ac and again",
    ]


def test_board_drain_spans_multiple_chunks_because_a_seat_change_forces_one(board):
    # The backlog spans seats and Discord's webhook `username` is per-POST,
    # so the drain is necessarily several POSTs, not one.
    for i in range(3):
        _append_thread_row("alpha", "old %d" % i, at=_at(i * 30))
    mirror.run_once(RUNID, lane=BOARD)
    _append_thread_row("bravo", "answering", at=_at(90))
    _append_thread_row("alpha", "and again", at=_at(120))
    mirror.run_once(RUNID, lane=BOARD)
    assert [p["username"].split(" ")[0] for p in board.posted] == [
        "alpha",
        "bravo",
        "alpha",
    ]


def test_board_a_drained_thread_leaves_an_empty_held_file(board):
    _append_thread_row("alpha", "a", at=_at(0))
    mirror.run_once(RUNID, lane=BOARD)
    _append_thread_row("bravo", "b", at=_at(30))
    mirror.run_once(RUNID, lane=BOARD)
    assert _held() == {}


def test_board_posts_into_the_thread_not_the_channel(board):
    _append_thread_row("alpha", "a", at=_at(0))
    _append_thread_row("bravo", "b", at=_at(30))
    mirror.run_once(RUNID, lane=BOARD)
    assert board.created and board.created[0][1] == "comms/docs/plan.md"
    assert all("thread_id=T1" in p["url"] for p in board.posted)


def test_board_every_post_suppresses_mentions(board):
    # A constant, not a knob: a mailbox row is prose written by an agent, and
    # a row containing @everyone must never ring a phone.
    _append_thread_row("alpha", "hey @everyone", at=_at(0))
    _append_thread_row("bravo", "b", at=_at(30))
    mirror.run_once(RUNID, lane=BOARD)
    assert board.posted
    assert all(p["allowed_mentions"] == {"parse": []} for p in board.posted)


def test_board_thread_title_drops_the_doc_prefix(board):
    _append_thread_row("alpha", "a", at=_at(0))
    _append_thread_row("bravo", "b", at=_at(30))
    mirror.run_once(RUNID, lane=BOARD)
    assert board.created[0][1] == "comms/docs/plan.md"


def test_board_reuses_the_thread_across_passes_creating_it_once(board):
    # FIXED (PR #51 review): this used to hand the second pass a fresh
    # two-seat exchange, so it proved only that the map is read -- it would
    # have passed even if alive() were re-required every pass. The single
    # later row below is what actually tests reuse.
    _append_thread_row("alpha", "a", at=_at(0))
    _append_thread_row("bravo", "b", at=_at(30))
    mirror.run_once(RUNID, lane=BOARD)
    _append_thread_row("alpha", "c", at=_at(60))
    mirror.run_once(RUNID, lane=BOARD)
    assert len(board.created) == 1


# ---- alive is a ONE-WAY transition; the map is its record -----------------
#
# Once a document has a thread, it HAS one. The alive predicate decides
# whether to OPEN a thread, never whether to deliver into one that exists.
# Re-asking it every pass was the bug: a drained thread's liveness history is
# gone (the rows left held), so the next single row from one seat would sit
# in held forever -- breaking README rehearsal step 13 and, worse, doing it
# silently, since a held row looks exactly like a row that is merely waiting.


def test_board_a_single_later_row_lands_in_the_EXISTING_thread(board):
    # Rehearsal step 13, as a test. One seat, no second speaker, no window:
    # none of that matters once the thread exists.
    _append_thread_row("alpha", "a", at=_at(0))
    _append_thread_row("bravo", "b", at=_at(30))
    mirror.run_once(RUNID, lane=BOARD)
    del board.posted[:]

    _append_thread_row("alpha", "a lone follow-up", at=_at(600))
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert _texts(board.posted) == ["\U0001f4ec\U0001f4ac a lone follow-up"]
    assert all("thread_id=T1" in p["url"] for p in board.posted)
    assert _held() == {}


def test_board_a_late_row_long_past_the_window_still_lands(board):
    # The window is about when a conversation STARTS being one, not about
    # expiring a thread. A day later is still that document's thread.
    _append_thread_row("alpha", "a", at=_at(0))
    _append_thread_row("bravo", "b", at=_at(30))
    mirror.run_once(RUNID, lane=BOARD)
    del board.posted[:]
    _append_thread_row("carol", "much later", at="2026-08-29T00:00:00+00:00")
    mirror.run_once(RUNID, lane=BOARD)
    assert _texts(board.posted) == ["\U0001f4ec\U0001f4ac much later"]


def test_board_an_existing_thread_needs_no_create_call(board):
    _append_thread_row("alpha", "a", at=_at(0))
    _append_thread_row("bravo", "b", at=_at(30))
    mirror.run_once(RUNID, lane=BOARD)
    _append_thread_row("alpha", "c", at=_at(60))
    mirror.run_once(RUNID, lane=BOARD)
    assert len(board.created) == 1  # no second create, and no second HTTP call


def test_board_a_key_ALREADY_in_the_map_posts_without_ever_going_alive(board):
    # The map is fleet-wide: another RUN (or another machine's operator) may
    # have opened this thread. A single row from a single seat in THIS run
    # then has a destination, and holding it would be holding a row whose
    # thread is sitting right there.
    os.makedirs(os.path.dirname(mirror.threads.map_path(BOARD)), exist_ok=True)
    with open(mirror.threads.map_path(BOARD), "w") as fh:
        json.dump({DOC: "T-preexisting"}, fh)
    _append_thread_row("alpha", "solo", at=_at(0))
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert board.created == []
    assert _texts(board.posted) == ["\U0001f4ec\U0001f4ac solo"]
    assert all("thread_id=T-preexisting" in p["url"] for p in board.posted)


def test_board_a_key_with_NO_thread_yet_still_has_to_earn_one(board):
    # The other half: the transition is one-way, not absent. A document
    # nobody has answered still does not open a thread.
    _append_thread_row("alpha", "lonely", at=_at(0))
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert board.created == []
    assert board.posted == []
    assert [r["text"] for r in _held()[DOC]] == ["lonely"]


def test_board_two_documents_get_two_threads(board):
    other = "doc:comms/README.md"
    _append_thread_row("alpha", "a", at=_at(0))
    _append_thread_row("bravo", "b", at=_at(30))
    _append_thread_row("alpha", "x", thread=other, at=_at(0))
    _append_thread_row("bravo", "y", thread=other, at=_at(30))
    mirror.run_once(RUNID, lane=BOARD)
    assert sorted(n for _, n, _ in board.created) == [
        "comms/README.md",
        "comms/docs/plan.md",
    ]


def test_board_a_dead_thread_is_untouched_while_a_live_one_drains(board):
    dead = "doc:comms/dead.md"
    _append_thread_row("alpha", "lonely", thread=dead, at=_at(0))
    _append_thread_row("alpha", "a", at=_at(0))
    _append_thread_row("bravo", "b", at=_at(30))
    mirror.run_once(RUNID, lane=BOARD)
    assert _texts(board.posted) == [
        "\U0001f4ec\U0001f4ac a",
        "\U0001f4ec\U0001f4ac b",
    ]
    assert list(_held()) == [dead]


# ---- the per-pass order: held is durable BEFORE the cursor moves -----------


def test_board_a_crash_during_the_drain_leaves_held_intact(board, monkeypatch):
    # The order that defines "held row lost" out of existence: held is
    # written (step 4) and the cursor saved (step 5) BEFORE any posting, so a
    # process that dies mid-drain has already recorded what it owes. It
    # re-posts, at worst; it never forgets.
    _append_thread_row("alpha", "a", at=_at(0))
    _append_thread_row("bravo", "b", at=_at(30))

    def boom(*args, **kwargs):
        raise RuntimeError("killed mid-drain")

    monkeypatch.setattr(mirror.threads, "thread_for", boom)
    with pytest.raises(RuntimeError):
        mirror.run_once(RUNID, lane=BOARD)
    assert [r["text"] for r in _held()[DOC]] == ["a", "b"]
    assert _cursor_count(mirror._load_cursor(RUNID, BOARD), "alpha") == 1


def test_board_a_thread_that_cannot_be_created_keeps_its_rows_held(board, monkeypatch):
    # D6: thread_for degrades to None on every failure. None means "not
    # renderable yet", so the rows wait rather than being dropped or posted
    # somewhere else.
    monkeypatch.setattr(mirror.threads, "thread_for", lambda *a, **k: None)
    _append_thread_row("alpha", "a", at=_at(0))
    _append_thread_row("bravo", "b", at=_at(30))
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert board.posted == []
    assert [r["text"] for r in _held()[DOC]] == ["a", "b"]


def test_board_corrupt_held_file_reads_as_empty_and_says_so(board, capsys):
    os.makedirs(mirror._mirror_dir(BOARD), exist_ok=True)
    with open(mirror._held_path(RUNID, BOARD), "w") as fh:
        fh.write("{this is not json")
    _append_thread_row("alpha", "a", at=_at(0))
    _append_thread_row("bravo", "b", at=_at(30))
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert _texts(board.posted) == [
        "\U0001f4ec\U0001f4ac a",
        "\U0001f4ec\U0001f4ac b",
    ]
    assert "held" in capsys.readouterr().err.lower()  # never silent


def test_board_held_file_holding_a_non_dict_reads_as_empty(board):
    os.makedirs(mirror._mirror_dir(BOARD), exist_ok=True)
    with open(mirror._held_path(RUNID, BOARD), "w") as fh:
        fh.write(json.dumps(["not", "a", "map"]))
    _append_thread_row("alpha", "a", at=_at(0))
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert [r["text"] for r in _held()[DOC]] == ["a"]


# ---- the drain rewrites held PER CHUNK ------------------------------------


def test_board_a_failed_middle_chunk_reposts_only_its_remainder(board):
    # The second bug the design note names. Four chunks (a, b, a, b); the
    # SECOND POST fails. Held must keep only what was never delivered --
    # chunks 3 and 4 -- so the next pass posts two lines, not six. Dropping
    # from held after the LAST chunk instead of each one re-posts the whole
    # backlog; that is a duplicate storm every time one POST 500s.
    for i, seat in enumerate(("alpha", "bravo", "alpha", "bravo")):
        _append_thread_row(seat, "row %d" % i, at=_at(i * 30))
    board.script.extend([True, False])
    assert mirror.run_once(RUNID, lane=BOARD) == 1  # 1 = something was skipped
    assert _texts(board.posted) == [
        "\U0001f4ec\U0001f4ac row 0",
        "\U0001f4ec\U0001f4ac row 1",
    ]
    assert [r["text"] for r in _held()[DOC]] == ["row 2", "row 3"]

    del board.posted[:]
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert _texts(board.posted) == [
        "\U0001f4ec\U0001f4ac row 2",
        "\U0001f4ec\U0001f4ac row 3",
    ]
    assert _held() == {}


def test_board_an_undeliverable_chunk_is_recorded_and_never_wedges(board):
    for i, seat in enumerate(("alpha", "bravo")):
        _append_thread_row(seat, "row %d" % i, at=_at(i * 30))
    board.script.append(False)
    assert mirror.run_once(RUNID, lane=BOARD) == 1
    with open(mirror._skipped_path(RUNID, BOARD)) as fh:
        recorded = [json.loads(line) for line in fh if line.strip()]
    assert [r["row"]["text"] for r in recorded] == ["row 0"]
    # ...and it is gone from held, so one bad batch cannot block the rest
    # forever. The skipped file is its durable record.
    assert [r["text"] for r in _held()[DOC]] == ["row 1"]


# ---- the held merge is idempotent ----------------------------------------
#
# Kimi's finding on PR #51, and the window nothing else covers: held is
# written (step 4) BEFORE the cursor is saved (step 5). If the process dies
# in between -- or _save_cursor itself raises -- the next pass loads the
# already-held rows AND re-reads the same fresh rows against the OLD cursor,
# appending a second copy of each. Nothing is lost, but the backlog grows a
# duplicate per crash and every one of them posts when the document goes
# alive. The fix is that merging the same row twice is a no-op.


def _breakable_cursor_save(monkeypatch):
    """Make _save_cursor raise while `flag["broken"]` is True, and work
    normally otherwise. A toggle rather than monkeypatch.undo(): the `board`
    fixture shares this same monkeypatch instance, so undo() would also rip
    out the fake poster and let a test reach for the network."""
    flag = {"broken": True}
    real = mirror._save_cursor

    def maybe(*args, **kwargs):
        if flag["broken"]:
            raise OSError("cursor save failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(mirror, "_save_cursor", maybe)
    return flag


def test_board_a_cursor_save_failure_does_not_duplicate_held_rows(board, monkeypatch):
    _append_thread_row("alpha", "one", at=_at(0))
    flag = _breakable_cursor_save(monkeypatch)
    with pytest.raises(OSError):
        mirror.run_once(RUNID, lane=BOARD)
    assert [r["text"] for r in _held()[DOC]] == ["one"]  # held survived

    flag["broken"] = False
    # The cursor never advanced, so this pass re-reads the very same row.
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert [r["text"] for r in _held()[DOC]] == ["one"]


def test_board_a_row_re_read_after_a_cursor_failure_posts_ONCE(board, monkeypatch):
    # The consequence a human would actually see: the duplicate is not just
    # a bigger file, it is a doubled message in the thread.
    _append_thread_row("alpha", "one", at=_at(0))
    flag = _breakable_cursor_save(monkeypatch)
    with pytest.raises(OSError):
        mirror.run_once(RUNID, lane=BOARD)
    flag["broken"] = False

    _append_thread_row("bravo", "two", at=_at(30))
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert _texts(board.posted) == [
        "\U0001f4ec\U0001f4ac one",
        "\U0001f4ec\U0001f4ac two",
    ]


def test_board_repeated_cursor_failures_do_not_compound(board, monkeypatch):
    _append_thread_row("alpha", "one", at=_at(0))
    _breakable_cursor_save(monkeypatch)
    for _ in range(3):
        with pytest.raises(OSError):
            mirror.run_once(RUNID, lane=BOARD)
    assert [r["text"] for r in _held()[DOC]] == ["one"]


def test_board_two_genuinely_distinct_rows_are_both_kept(board):
    # The other direction, and the reason the identity includes `at`: dedupe
    # must not swallow a real second row. Two seats, same text, same instant.
    _append_thread_row("alpha", "same words", at=_at(0))
    _append_thread_row("bravo", "same words", at=_at(0))
    mirror.run_once(RUNID, lane=BOARD)
    assert len(_held()[DOC]) == 2


def test_board_one_seat_repeating_itself_later_keeps_both_rows(board):
    _append_thread_row("alpha", "ping", at=_at(0))
    _append_thread_row("alpha", "ping", at=_at(30))
    mirror.run_once(RUNID, lane=BOARD)
    assert len(_held()[DOC]) == 2


# ---- durable ordering: fsync before replace, for both state files --------
#
# The whole crash-safety argument is "held is durable BEFORE the cursor
# moves". Ordering two Python writes does not order two DISK writes: after
# os.replace returns, both files can still be dirty page cache, and a power
# loss is free to keep the newer cursor while losing the held file it depends
# on -- which loses rows permanently, the one failure this lane refuses.


class _SyncSpy:
    """Records the ORDER of os.fsync and os.replace calls, delegating to the
    real ones so the writes still happen."""

    def __init__(self, monkeypatch):
        self.calls = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            self.calls.append("fsync")
            return real_fsync(fd)

        def replace(src, dst):
            self.calls.append("replace:" + os.path.basename(dst))
            return real_replace(src, dst)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "replace", replace)

    def fsynced_before(self, name):
        """True if at least one fsync happened before the replace of `name`."""
        idx = next(
            i for i, c in enumerate(self.calls) if c.startswith("replace:") and name in c
        )
        return "fsync" in self.calls[:idx]


def test_held_file_is_fsynced_before_it_is_renamed_into_place(board, monkeypatch):
    spy = _SyncSpy(monkeypatch)
    _append_thread_row("alpha", "held row", at=_at(0))
    mirror.run_once(RUNID, lane=BOARD)
    assert spy.fsynced_before(".held.json")


def test_cursor_file_is_fsynced_before_it_is_renamed_into_place(board, monkeypatch):
    spy = _SyncSpy(monkeypatch)
    _append_thread_row("alpha", "held row", at=_at(0))
    mirror.run_once(RUNID, lane=BOARD)
    assert spy.fsynced_before(".cursor.json")


def test_the_default_lanes_cursor_is_fsynced_too(webhook, monkeypatch):
    # Same argument, older file: the skipped log already fsyncs before the
    # cursor moves past a row, which only helps if the cursor write itself
    # is durable in the same sense.
    posted = []
    _fake_post(monkeypatch, posted)
    spy = _SyncSpy(monkeypatch)
    swarm_mailbox.post(RUNID, "alpha", "finding", "a row")
    mirror.run_once(RUNID)
    assert spy.fsynced_before(".cursor.json")


def test_thread_map_is_fsynced_before_it_is_renamed_into_place(board, monkeypatch):
    spy = _SyncSpy(monkeypatch)
    _append_thread_row("alpha", "a", at=_at(0))
    _append_thread_row("bravo", "b", at=_at(30))
    mirror.run_once(RUNID, lane=BOARD)
    assert spy.fsynced_before("threads.json")


def test_held_write_survives_a_directory_that_cannot_be_fsynced(board, monkeypatch):
    # Some filesystems refuse an fsync on a directory fd. That must degrade
    # to "no directory fsync", never to a lost write: the file rename has
    # already happened by then.
    real_fsync = os.fsync

    def picky(fd):
        if os.fstat(fd).st_mode & 0o040000:  # a directory
            raise OSError("no fsync on directories here")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", picky)
    _append_thread_row("alpha", "held row", at=_at(0))
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert [r["text"] for r in _held()[DOC]] == ["held row"]


# ---- the hold cap ---------------------------------------------------------


def test_board_hold_cap_drops_the_oldest_and_records_them(board, monkeypatch, capsys):
    monkeypatch.setenv("COMMS_THREAD_HOLD_MAX", "2")
    for i in range(4):
        _append_thread_row("alpha", "row %d" % i, at=_at(i * 30))
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert [r["text"] for r in _held()[DOC]] == ["row 2", "row 3"]
    with open(mirror._skipped_path(RUNID, BOARD)) as fh:
        recorded = [json.loads(line) for line in fh if line.strip()]
    assert [r["row"]["text"] for r in recorded] == ["row 0", "row 1"]
    assert capsys.readouterr().err  # loud, never a silent truncation


def test_board_hold_cap_default_is_500():
    assert mirror.HOLD_MAX_DEFAULT == 500


# ---- the alive knobs pass through -----------------------------------------


def test_board_alive_seats_knob_can_demand_three_speakers(board, monkeypatch):
    monkeypatch.setenv("COMMS_THREAD_ALIVE_SEATS", "3")
    _append_thread_row("alpha", "a", at=_at(0))
    _append_thread_row("bravo", "b", at=_at(30))
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert board.posted == []
    _append_thread_row("carol", "c", at=_at(60))
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert len(_texts(board.posted)) == 3


def test_board_alive_seconds_knob_narrows_the_window(board, monkeypatch):
    monkeypatch.setenv("COMMS_THREAD_ALIVE_SECONDS", "10")
    _append_thread_row("alpha", "a", at=_at(0))
    _append_thread_row("bravo", "b", at=_at(30))  # 30s apart, window is 10s
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert board.posted == []


def test_board_alive_defaults_match_the_config_table():
    assert mirror.ALIVE_SECONDS_DEFAULT == 1800
    assert mirror.ALIVE_SEATS_DEFAULT == 2
    assert swarm_threads.DEFAULT_WINDOW_S == mirror.ALIVE_SECONDS_DEFAULT
    assert swarm_threads.DEFAULT_MIN_SEATS == mirror.ALIVE_SEATS_DEFAULT


def test_board_a_junk_knob_value_falls_back_to_the_default(board, monkeypatch):
    # A typo in a launchd plist must not take the lane down.
    monkeypatch.setenv("COMMS_THREAD_ALIVE_SEATS", "two")
    _append_thread_row("alpha", "a", at=_at(0))
    _append_thread_row("bravo", "b", at=_at(30))
    assert mirror.run_once(RUNID, lane=BOARD) == 0
    assert len(_texts(board.posted)) == 2


# ---- seat collisions are named, once per pass -----------------------------


def test_board_pass_names_a_seat_collision_once(board, capsys):
    swarm_arm.arm(RUNID)
    swarm_arm.enroll(RUNID, "agent-a", seat="alpha")
    swarm_arm.enroll(RUNID, "agent-b", seat="alpha")
    _append_thread_row("alpha", "a", at=_at(0))
    mirror.run_once(RUNID, lane=BOARD)
    err = capsys.readouterr().err
    assert err.count("alpha") >= 1
    assert len([ln for ln in err.splitlines() if "seat" in ln.lower()]) == 1


def test_a_clean_roster_says_nothing(board, capsys):
    swarm_arm.arm(RUNID)
    swarm_arm.enroll(RUNID, "agent-a", seat="alpha")
    _append_thread_row("alpha", "a", at=_at(0))
    mirror.run_once(RUNID, lane=BOARD)
    assert "collision" not in capsys.readouterr().err.lower()
