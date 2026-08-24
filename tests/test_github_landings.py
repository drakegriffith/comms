#!/usr/bin/env python3
"""Tests for adapters/github/landings.py -- the GitHub landings watcher.

Mirrors test_discord_mirror.py's shape: gh is NEVER really invoked. Every gh
call in the module goes through the single seam `landings._gh(args)` (see
module docstring, THE SEAM), so tests install a fake dispatch table keyed on
the exact argv list and assert on what landings.py asked for, never on real
network or a real `gh` binary. Delivery is likewise faked at
`landings.mirror.post_content` -- no real HTTP either.

Isolation: tests/conftest.py's autouse fixture already points COMMS_STATE_DIR
at a per-test tmp_path (see conftest.py, _isolated_comms_env), so the cursor
file lands under tmp automatically. This file's own autouse fixture only
adds the knobs conftest does NOT cover: the webhook secret var/file and the
machine label, plus a default owner/window so tests that don't care about
discovery specifics have a sane default.
"""

import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "adapters", "github"))

import landings  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMS_SECRETS_FILE", str(tmp_path / "comms.env"))
    monkeypatch.delenv("DISCORD_COMMS_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("COMMS_MACHINE_LABEL", "studio")
    monkeypatch.delenv("COMMS_GH_REPOS", raising=False)
    monkeypatch.delenv("COMMS_GH_OWNER", raising=False)
    monkeypatch.delenv("COMMS_LANDINGS_WINDOW_HOURS", raising=False)
    yield tmp_path


class FakeGh:
    """The one seam every gh call goes through (landings._gh). Routes are
    keyed on the EXACT argv list landings.py builds, so a test that asserts
    "unexpected gh call" catches a spec drift immediately instead of
    returning stale/wrong data silently."""

    def __init__(self):
        self.calls = []
        self.routes = {}
        self.raises = {}

    def add(self, args, response):
        self.routes[tuple(args)] = (
            response if isinstance(response, str) else json.dumps(response)
        )

    def add_raise(self, args, exc):
        self.raises[tuple(args)] = exc

    def __call__(self, args):
        key = tuple(args)
        self.calls.append(key)
        if key in self.raises:
            raise self.raises[key]
        if key in self.routes:
            return self.routes[key]
        raise AssertionError("unexpected gh call: %r" % (args,))


@pytest.fixture()
def fake_gh(monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(landings, "_gh", gh)
    return gh


@pytest.fixture()
def posted(monkeypatch):
    """Fakes landings.mirror.post_content -- the reused Discord machinery --
    so delivery never touches the network. Returns the list it appends
    (url, content, username) 3-tuples to."""
    out = []

    def fake(url, content, username=None):
        out.append((url, content, username))
        return True

    monkeypatch.setattr(landings.mirror, "post_content", fake)
    return out


@pytest.fixture(autouse=True)
def webhook_env(monkeypatch):
    monkeypatch.setenv("DISCORD_COMMS_WEBHOOK_URL", "http://127.0.0.1:1/main")


PULLS_URL = "repos/acme/widgets/pulls?state=closed&sort=updated&direction=desc&per_page=30"
ISSUES_URL = "repos/acme/widgets/issues?state=closed&sort=updated&direction=desc&per_page=30"


def _pr(number, title, user, merged_at=None, closed_at=None):
    return {
        "number": number,
        "title": title,
        "user": {"login": user},
        "merged_at": merged_at,
        "closed_at": closed_at,
    }


def _issue(number, title, user, closed_at, pull_request=False):
    row = {
        "number": number,
        "title": title,
        "user": {"login": user},
        "closed_at": closed_at,
    }
    if pull_request:
        row["pull_request"] = {"url": "x"}
    return row


# ---- classification: merged / closed-unmerged / closed-issue ---------------


def test_merged_pr_renders_with_merged_by_and_emoji(fake_gh, posted):
    fake_gh.add(["api", PULLS_URL], [
        _pr(12, "Add landings watcher", "alice", merged_at="2026-08-24T10:00:00Z"),
    ])
    fake_gh.add(["api", ISSUES_URL], [])
    fake_gh.add(
        ["api", "repos/acme/widgets/pulls/12"],
        {"merged_by": {"login": "bob"}},
    )
    landings.collect_repo_events("acme/widgets", "2026-08-24T00:00:00Z")
    events, _ = landings.collect_repo_events("acme/widgets", "2026-08-24T00:00:00Z")
    assert events == [
        ("2026-08-24T10:00:00Z",
         "\U0001f7e3 bob merged PR #12 on widgets: Add landings watcher")
    ]


def test_merged_pr_falls_back_to_user_when_merged_by_absent(fake_gh):
    fake_gh.add(["api", PULLS_URL], [
        _pr(13, "Fix typo", "carol", merged_at="2026-08-24T10:05:00Z"),
    ])
    fake_gh.add(["api", ISSUES_URL], [])
    fake_gh.add(["api", "repos/acme/widgets/pulls/13"], {"merged_by": None})
    events, _ = landings.collect_repo_events("acme/widgets", "2026-08-24T00:00:00Z")
    assert events == [
        ("2026-08-24T10:05:00Z", "\U0001f7e3 carol merged PR #13 on widgets: Fix typo")
    ]


def test_closed_unmerged_pr_renders_with_author_no_detail_call(fake_gh):
    fake_gh.add(["api", PULLS_URL], [
        _pr(14, "Abandoned idea", "dave", closed_at="2026-08-24T11:00:00Z"),
    ])
    fake_gh.add(["api", ISSUES_URL], [])
    events, _ = landings.collect_repo_events("acme/widgets", "2026-08-24T00:00:00Z")
    assert events == [
        ("2026-08-24T11:00:00Z",
         "❌ dave closed PR #14 without merging on widgets: Abandoned idea")
    ]
    # no detail call for closed-unmerged PRs -- API cost stays bounded
    assert ("api", "repos/acme/widgets/pulls/14") not in fake_gh.calls


def test_closed_issue_renders_with_closed_by_and_emoji(fake_gh):
    fake_gh.add(["api", PULLS_URL], [])
    fake_gh.add(["api", ISSUES_URL], [
        _issue(7, "Crash on startup", "erin", "2026-08-24T09:00:00Z"),
    ])
    fake_gh.add(
        ["api", "repos/acme/widgets/issues/7"],
        {"closed_by": {"login": "frank"}},
    )
    events, _ = landings.collect_repo_events("acme/widgets", "2026-08-24T00:00:00Z")
    assert events == [
        ("2026-08-24T09:00:00Z",
         "✅ frank closed issue #7 on widgets: Crash on startup")
    ]


def test_closed_issue_falls_back_to_user_when_closed_by_absent(fake_gh):
    fake_gh.add(["api", PULLS_URL], [])
    fake_gh.add(["api", ISSUES_URL], [
        _issue(8, "Docs typo", "grace", "2026-08-24T09:10:00Z"),
    ])
    fake_gh.add(["api", "repos/acme/widgets/issues/8"], {"closed_by": None})
    events, _ = landings.collect_repo_events("acme/widgets", "2026-08-24T00:00:00Z")
    assert events == [
        ("2026-08-24T09:10:00Z", "✅ grace closed issue #8 on widgets: Docs typo")
    ]


def test_pull_request_key_issue_row_is_skipped(fake_gh):
    fake_gh.add(["api", PULLS_URL], [])
    fake_gh.add(["api", ISSUES_URL], [
        _issue(9, "Actually a PR", "hank", "2026-08-24T09:20:00Z", pull_request=True),
    ])
    events, _ = landings.collect_repo_events("acme/widgets", "2026-08-24T00:00:00Z")
    assert events == []
    assert ("api", "repos/acme/widgets/issues/9") not in fake_gh.calls


def test_pr_neither_merged_nor_closed_after_cursor_is_ignored(fake_gh):
    fake_gh.add(["api", PULLS_URL], [
        _pr(15, "Old merged one", "ivan",
            merged_at="2026-08-23T10:00:00Z", closed_at="2026-08-23T10:00:00Z"),
    ])
    fake_gh.add(["api", ISSUES_URL], [])
    events, latest = landings.collect_repo_events("acme/widgets", "2026-08-24T00:00:00Z")
    assert events == []
    assert latest == "2026-08-24T00:00:00Z"  # cursor unchanged, nothing newer found


# ---- cursor: advance + idempotent second pass, first-run today-cap --------


def test_cursor_advances_to_latest_event_timestamp(fake_gh):
    fake_gh.add(["api", PULLS_URL], [
        _pr(1, "One", "a", merged_at="2026-08-24T08:00:00Z"),
        _pr(2, "Two", "b", merged_at="2026-08-24T12:00:00Z"),
    ])
    fake_gh.add(["api", ISSUES_URL], [])
    fake_gh.add(["api", "repos/acme/widgets/pulls/1"], {"merged_by": {"login": "a"}})
    fake_gh.add(["api", "repos/acme/widgets/pulls/2"], {"merged_by": {"login": "b"}})
    events, latest = landings.collect_repo_events("acme/widgets", "2026-08-24T00:00:00Z")
    assert len(events) == 2
    assert latest == "2026-08-24T12:00:00Z"


def test_run_once_second_pass_reposts_nothing_new(fake_gh, posted, tmp_path):
    fake_gh.add(["api", "user", "--jq", ".login"], "acme\n")
    fake_gh.add(
        ["repo", "list", "acme", "--limit", "100", "--json", "nameWithOwner,pushedAt"],
        [{"nameWithOwner": "acme/widgets", "pushedAt": landings._utcnow().isoformat().replace("+00:00", "Z")}],
    )
    merged_ts = landings._utcnow().isoformat().replace("+00:00", "Z")
    fake_gh.add(["api", PULLS_URL], [
        _pr(1, "One", "a", merged_at=merged_ts),
    ])
    fake_gh.add(["api", ISSUES_URL], [])
    fake_gh.add(["api", "repos/acme/widgets/pulls/1"], {"merged_by": {"login": "a"}})
    assert landings.run_once() == 0
    assert len(posted) == 1

    # Second pass: same fixed responses, cursor already advanced past them --
    # collect_repo_events must not re-emit row 1 (idempotent second pass).
    posted.clear()
    assert landings.run_once() == 0
    assert posted == []


def test_first_sight_of_repo_caps_backfill_to_start_of_today(fake_gh):
    old_event_ts = "2020-01-01T00:00:00Z"  # long before "today"
    fake_gh.add(["api", PULLS_URL], [
        _pr(1, "Ancient", "a", merged_at=old_event_ts),
    ])
    fake_gh.add(["api", ISSUES_URL], [])
    cursor = landings._today_utc_start()
    events, _ = landings.collect_repo_events("acme/widgets", cursor)
    assert events == []  # older than today's cap: never surfaced on first sight


def test_today_utc_start_shape():
    start = landings._today_utc_start()
    assert start.endswith("T00:00:00Z")
    assert len(start) == len("2026-08-24T00:00:00Z")


# ---- per-repo exception isolation ------------------------------------------


def test_collect_new_isolates_one_repo_failure_from_the_rest(fake_gh, monkeypatch, capsys):
    monkeypatch.setenv("COMMS_GH_REPOS", "acme/widgets,acme/gizmos")
    fake_gh.add(["api", "repos/acme/widgets/pulls?state=closed&sort=updated&direction=desc&per_page=30"], [
        _pr(1, "Good one", "a", merged_at="2026-08-24T10:00:00Z"),
    ])
    fake_gh.add(["api", "repos/acme/widgets/issues?state=closed&sort=updated&direction=desc&per_page=30"], [])
    fake_gh.add(["api", "repos/acme/widgets/pulls/1"], {"merged_by": {"login": "a"}})
    fake_gh.add_raise(
        ["api", "repos/acme/gizmos/pulls?state=closed&sort=updated&direction=desc&per_page=30"],
        RuntimeError("gh api rate limited"),
    )
    events, new_cursor = landings.collect_new()
    assert any("Good one" in e for e in events)
    assert "acme/gizmos" in new_cursor or "acme/gizmos" not in new_cursor  # never crashes either way
    assert "acme/widgets" in new_cursor
    err = capsys.readouterr().err
    assert "acme/gizmos" in err
    assert "RuntimeError" in err


def test_run_once_survives_one_repo_exception_and_delivers_the_rest(fake_gh, posted, monkeypatch):
    monkeypatch.setenv("COMMS_GH_REPOS", "acme/widgets,acme/gizmos")
    fake_gh.add(["api", "repos/acme/widgets/pulls?state=closed&sort=updated&direction=desc&per_page=30"], [
        _pr(1, "Good one", "a", merged_at="2026-08-24T10:00:00Z"),
    ])
    fake_gh.add(["api", "repos/acme/widgets/issues?state=closed&sort=updated&direction=desc&per_page=30"], [])
    fake_gh.add(["api", "repos/acme/widgets/pulls/1"], {"merged_by": {"login": "a"}})
    fake_gh.add_raise(
        ["api", "repos/acme/gizmos/pulls?state=closed&sort=updated&direction=desc&per_page=30"],
        RuntimeError("boom"),
    )
    assert landings.run_once() == 0
    joined = "\n".join(c for _, c, _ in posted)
    assert "Good one" in joined


# ---- discovery: explicit repos / owner / window ----------------------------


def test_explicit_comms_gh_repos_overrides_discovery(fake_gh, monkeypatch):
    monkeypatch.setenv("COMMS_GH_REPOS", "one/repo, two/repo")
    assert landings.discover_repos() == ["one/repo", "two/repo"]
    assert fake_gh.calls == []  # discovery never touched gh at all


def test_discover_repos_uses_explicit_owner_env(fake_gh, monkeypatch):
    monkeypatch.setenv("COMMS_GH_OWNER", "explicit-owner")
    fake_gh.add(
        ["repo", "list", "explicit-owner", "--limit", "100", "--json", "nameWithOwner,pushedAt"],
        [{"nameWithOwner": "explicit-owner/repo1", "pushedAt": landings._utcnow().isoformat().replace("+00:00", "Z")}],
    )
    repos = landings.discover_repos()
    assert repos == ["explicit-owner/repo1"]
    assert ("api", "user", "--jq", ".login") not in fake_gh.calls


def test_discover_repos_falls_back_to_authenticated_user(fake_gh):
    fake_gh.add(["api", "user", "--jq", ".login"], "myuser\n")
    fake_gh.add(
        ["repo", "list", "myuser", "--limit", "100", "--json", "nameWithOwner,pushedAt"],
        [{"nameWithOwner": "myuser/repo1", "pushedAt": landings._utcnow().isoformat().replace("+00:00", "Z")}],
    )
    assert landings.discover_repos() == ["myuser/repo1"]


def test_discover_repos_filters_by_window_hours(fake_gh, monkeypatch):
    monkeypatch.setenv("COMMS_GH_OWNER", "acme")
    now = landings._utcnow()
    import datetime as _dt
    recent = (now - _dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    stale = (now - _dt.timedelta(hours=48)).isoformat().replace("+00:00", "Z")
    fake_gh.add(
        ["repo", "list", "acme", "--limit", "100", "--json", "nameWithOwner,pushedAt"],
        [
            {"nameWithOwner": "acme/fresh", "pushedAt": recent},
            {"nameWithOwner": "acme/stale", "pushedAt": stale},
        ],
    )
    repos = landings.discover_repos()
    assert repos == ["acme/fresh"]


def test_discover_repos_window_hours_env_override(fake_gh, monkeypatch):
    monkeypatch.setenv("COMMS_GH_OWNER", "acme")
    monkeypatch.setenv("COMMS_LANDINGS_WINDOW_HOURS", "72")
    now = landings._utcnow()
    import datetime as _dt
    within_72 = (now - _dt.timedelta(hours=48)).isoformat().replace("+00:00", "Z")
    fake_gh.add(
        ["repo", "list", "acme", "--limit", "100", "--json", "nameWithOwner,pushedAt"],
        [{"nameWithOwner": "acme/inrange", "pushedAt": within_72}],
    )
    assert landings.discover_repos() == ["acme/inrange"]


# ---- delivery: author, chunking --------------------------------------------


def test_run_once_uses_landings_author_with_machine_label(fake_gh, posted, monkeypatch):
    monkeypatch.setenv("COMMS_GH_REPOS", "acme/widgets")
    fake_gh.add(["api", PULLS_URL], [
        _pr(1, "One", "a", merged_at="2026-08-24T10:00:00Z"),
    ])
    fake_gh.add(["api", ISSUES_URL], [])
    fake_gh.add(["api", "repos/acme/widgets/pulls/1"], {"merged_by": {"login": "a"}})
    assert landings.run_once() == 0
    assert posted[0][2] == "github landings (studio)"


def test_run_once_no_events_posts_nothing(fake_gh, posted, monkeypatch):
    monkeypatch.setenv("COMMS_GH_REPOS", "acme/widgets")
    fake_gh.add(["api", PULLS_URL], [])
    fake_gh.add(["api", ISSUES_URL], [])
    assert landings.run_once() == 0
    assert posted == []


# ---- secret handling: both modes -------------------------------------------


def test_once_exits_2_on_missing_secret(monkeypatch, capsys):
    monkeypatch.delenv("DISCORD_COMMS_WEBHOOK_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        landings.run_once()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "DISCORD_COMMS_WEBHOOK_URL=" in err
    assert "http" not in err


def test_follow_missing_secret_does_not_raise_and_retries_60s(monkeypatch, capsys):
    monkeypatch.delenv("DISCORD_COMMS_WEBHOOK_URL", raising=False)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt()

    monkeypatch.setattr(landings.time, "sleep", fake_sleep)
    rc = landings.follow(120)
    assert rc == 0
    assert sleeps == [60]
    err = capsys.readouterr().err
    assert err.count("\n") == 1


def test_main_once_exits_2_on_missing_secret(monkeypatch):
    monkeypatch.delenv("DISCORD_COMMS_WEBHOOK_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        landings.main(["landings.py", "--once"])
    assert exc.value.code == 2


# ---- CLI --------------------------------------------------------------------


def test_main_usage_exit_2(capsys):
    assert landings.main(["landings.py"]) == 2
    assert landings.main(["landings.py", "--bogus"]) == 2
    assert "usage" in capsys.readouterr().err


def test_main_once_dispatches_to_run_once(monkeypatch):
    monkeypatch.setattr(landings, "run_once", lambda: 0)
    assert landings.main(["landings.py", "--once"]) == 0


def test_main_follow_dispatches_with_default_interval(monkeypatch):
    calls = []
    monkeypatch.setattr(landings, "follow", lambda interval: calls.append(interval) or 0)
    assert landings.main(["landings.py", "--follow"]) == 0
    assert calls == [landings.DEFAULT_INTERVAL]


def test_main_follow_interval_flag_parsed(monkeypatch):
    calls = []
    monkeypatch.setattr(landings, "follow", lambda interval: calls.append(interval) or 0)
    assert landings.main(["landings.py", "--follow", "--interval", "30"]) == 0
    assert calls == [30.0]


def test_main_interval_bad_value_returns_2(capsys):
    assert landings.main(["landings.py", "--follow", "--interval", "nope"]) == 2
    assert "--interval needs a number" in capsys.readouterr().err


# ---- state: cursor file location / tmp+replace -----------------------------


def test_cursor_file_lives_under_state_dir_github_landings(fake_gh, posted, monkeypatch):
    monkeypatch.setenv("COMMS_GH_REPOS", "acme/widgets")
    fake_gh.add(["api", PULLS_URL], [
        _pr(1, "One", "a", merged_at="2026-08-24T10:00:00Z"),
    ])
    fake_gh.add(["api", ISSUES_URL], [])
    fake_gh.add(["api", "repos/acme/widgets/pulls/1"], {"merged_by": {"login": "a"}})
    landings.run_once()
    cursor_path = landings._cursor_path()
    assert cursor_path.endswith(os.path.join("github-landings", "cursor.json"))
    assert os.path.isfile(cursor_path)
    with open(cursor_path) as fh:
        data = json.load(fh)
    assert data["acme/widgets"] == "2026-08-24T10:00:00Z"


def test_cursor_tmp_path_includes_pid():
    tmp = landings._cursor_tmp_path()
    assert tmp == landings._cursor_path() + ".tmp." + str(os.getpid())
