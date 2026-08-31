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

CURSOR SHAPE (post verifier3 fix round): a repo's cursor entry is now
`{"pulls": {"ts": <iso>, "seen": [<event ids at ts>]}, "issues": {...}}` --
NOT a flat ISO string -- so PR and issue events landing in the same second
no longer share (and cannot shadow each other on) one high-water mark. The
`_cursor()` helper below builds that shape for tests that need to seed a
specific starting point.
"""

import datetime
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "adapters", "github"))

import landings  # noqa: E402


# The whole file's fixtures are stamped on this day (see _pr/_issue call
# sites: "2026-08-24T10:00:00Z"). landings.py seeds a first-sight cursor from
# start-of-today UTC (_today_utc_start -> _utcnow), so a real clock makes the
# floor march past the fixtures and the suite goes red on a calendar boundary
# rather than on a code change. Freeze the module's ONE clock seam instead of
# bumping the fixture dates, which would only re-arm the same bomb.
FROZEN_NOW = datetime.datetime(2026, 8, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(landings, "_utcnow", lambda: FROZEN_NOW)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMS_SECRETS_FILE", str(tmp_path / "comms.env"))
    monkeypatch.delenv("DISCORD_COMMS_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_COMMS_LANDINGS_WEBHOOK_URL", raising=False)
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

GIZMOS_PULLS_URL = "repos/acme/gizmos/pulls?state=closed&sort=updated&direction=desc&per_page=30"
GIZMOS_ISSUES_URL = "repos/acme/gizmos/issues?state=closed&sort=updated&direction=desc&per_page=30"


def _pr(number, title, user, merged_at=None, closed_at=None, updated_at=None):
    return {
        "number": number,
        "title": title,
        "user": {"login": user},
        "merged_at": merged_at,
        "closed_at": closed_at,
        "updated_at": updated_at or merged_at or closed_at,
    }


def _issue(number, title, user, closed_at, pull_request=False, closed_by=None, updated_at=None):
    row = {
        "number": number,
        "title": title,
        "user": {"login": user},
        "closed_at": closed_at,
        "updated_at": updated_at or closed_at,
        "closed_by": {"login": closed_by} if closed_by else None,
    }
    if pull_request:
        row["pull_request"] = {"url": "x"}
    return row


def _cursor(pulls_ts="2000-01-01T00:00:00Z", pulls_seen=None,
            issues_ts="2000-01-01T00:00:00Z", issues_seen=None):
    """Build a repo_cursor dict with an old-enough ts that any realistic
    test timestamp reads as fresh, unless the test overrides it."""
    return {
        "pulls": {"ts": pulls_ts, "seen": pulls_seen or []},
        "issues": {"ts": issues_ts, "seen": issues_seen or []},
    }


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
    events, new_cursor = landings.collect_repo_events("acme/widgets", _cursor())
    assert events == [
        ("2026-08-24T10:00:00Z",
         "\U0001f7e3 bob merged PR #12 on widgets: Add landings watcher")
    ]
    assert new_cursor["pulls"]["ts"] == "2026-08-24T10:00:00Z"
    assert new_cursor["pulls"]["seen"] == ["pr:12"]


def test_merged_pr_falls_back_to_user_when_merged_by_absent(fake_gh):
    fake_gh.add(["api", PULLS_URL], [
        _pr(13, "Fix typo", "carol", merged_at="2026-08-24T10:05:00Z"),
    ])
    fake_gh.add(["api", ISSUES_URL], [])
    fake_gh.add(["api", "repos/acme/widgets/pulls/13"], {"merged_by": None})
    events, _ = landings.collect_repo_events("acme/widgets", _cursor())
    assert events == [
        ("2026-08-24T10:05:00Z", "\U0001f7e3 carol merged PR #13 on widgets: Fix typo")
    ]


def test_closed_unmerged_pr_renders_with_author_no_detail_call(fake_gh):
    fake_gh.add(["api", PULLS_URL], [
        _pr(14, "Abandoned idea", "dave", closed_at="2026-08-24T11:00:00Z"),
    ])
    fake_gh.add(["api", ISSUES_URL], [])
    events, _ = landings.collect_repo_events("acme/widgets", _cursor())
    assert events == [
        ("2026-08-24T11:00:00Z",
         "❌ dave closed PR #14 without merging on widgets: Abandoned idea")
    ]
    # no detail call for closed-unmerged PRs -- API cost stays bounded
    assert ("api", "repos/acme/widgets/pulls/14") not in fake_gh.calls


def test_closed_issue_renders_with_closed_by_from_list_row_no_detail_call(fake_gh):
    # closed_by comes straight off the issues LIST row now (fix: it was
    # already fully populated there; a detail call for it was wasted cost).
    fake_gh.add(["api", PULLS_URL], [])
    fake_gh.add(["api", ISSUES_URL], [
        _issue(7, "Crash on startup", "erin", "2026-08-24T09:00:00Z", closed_by="frank"),
    ])
    events, _ = landings.collect_repo_events("acme/widgets", _cursor())
    assert events == [
        ("2026-08-24T09:00:00Z",
         "✅ frank closed issue #7 on widgets: Crash on startup")
    ]
    assert ("api", "repos/acme/widgets/issues/7") not in fake_gh.calls
    assert not any(c[:2] == ("api", "repos/acme/widgets/issues/7") for c in fake_gh.calls)


def test_closed_issue_falls_back_to_user_when_closed_by_absent(fake_gh):
    fake_gh.add(["api", PULLS_URL], [])
    fake_gh.add(["api", ISSUES_URL], [
        _issue(8, "Docs typo", "grace", "2026-08-24T09:10:00Z"),  # no closed_by
    ])
    events, _ = landings.collect_repo_events("acme/widgets", _cursor())
    assert events == [
        ("2026-08-24T09:10:00Z", "✅ grace closed issue #8 on widgets: Docs typo")
    ]


def test_pull_request_key_issue_row_is_skipped(fake_gh):
    fake_gh.add(["api", PULLS_URL], [])
    fake_gh.add(["api", ISSUES_URL], [
        _issue(9, "Actually a PR", "hank", "2026-08-24T09:20:00Z", pull_request=True),
    ])
    events, _ = landings.collect_repo_events("acme/widgets", _cursor())
    assert events == []
    assert ("api", "repos/acme/widgets/issues/9") not in fake_gh.calls


def test_pr_neither_merged_nor_closed_after_cursor_is_ignored(fake_gh):
    fake_gh.add(["api", PULLS_URL], [
        _pr(15, "Old merged one", "ivan",
            merged_at="2026-08-23T10:00:00Z", closed_at="2026-08-23T10:00:00Z"),
    ])
    fake_gh.add(["api", ISSUES_URL], [])
    cursor = _cursor(pulls_ts="2026-08-24T00:00:00Z", issues_ts="2026-08-24T00:00:00Z")
    events, new_cursor = landings.collect_repo_events("acme/widgets", cursor)
    assert events == []
    assert new_cursor["pulls"]["ts"] == "2026-08-24T00:00:00Z"  # unchanged, nothing newer found


# ---- pagination: truncation detection + bounded fetch (verifier3 #1) -------


def test_fetch_paginated_follows_truncated_pages_until_a_short_page(fake_gh):
    # 31 rows total: a full page1 (30) whose oldest row is still newer than
    # the cursor must trigger page2; page2 is short (1 row) so it stops
    # there -- all 31 rows recovered, no bound-hit warning.
    page1 = [
        {"number": n, "updated_at": "2026-08-24T10:%02d:00Z" % n}
        for n in range(30, 0, -1)
    ]  # minutes 30..1, newest first
    page2 = [{"number": 0, "updated_at": "2026-08-24T10:00:00Z"}]  # minute 0, oldest
    url_page1 = PULLS_URL
    url_page2 = PULLS_URL + "&page=2"
    fake_gh.add(["api", url_page1], page1)
    fake_gh.add(["api", url_page2], page2)
    rows = landings._fetch_paginated("acme/widgets", "pulls", "2026-08-24T09:00:00Z")
    assert len(rows) == 31
    assert {r["number"] for r in rows} == set(range(31))
    assert ("api", url_page2) in fake_gh.calls


def test_fetch_paginated_stops_and_logs_one_line_at_the_page_bound(fake_gh, capsys):
    # Every page comes back FULL and still fresh -- pagination could run
    # forever; it must stop at MAX_PAGES and log exactly one stderr line
    # naming the repo and endpoint (never silent, never unbounded).
    for page in range(1, landings.MAX_PAGES + 1):
        rows = [
            {"number": page * 1000 + i, "updated_at": "2026-08-24T10:00:00Z"}
            for i in range(landings.PER_PAGE)
        ]
        url = PULLS_URL if page == 1 else PULLS_URL + "&page=%d" % page
        fake_gh.add(["api", url], rows)
    rows = landings._fetch_paginated("acme/widgets", "pulls", "2026-08-24T00:00:00Z")
    assert len(rows) == landings.MAX_PAGES * landings.PER_PAGE
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "acme/widgets" in err
    assert "pulls" in err
    assert "truncated" in err.lower()


def test_fetch_paginated_short_first_page_never_requests_page_two(fake_gh):
    fake_gh.add(["api", PULLS_URL], [{"number": 1, "updated_at": "2026-08-24T10:00:00Z"}])
    rows = landings._fetch_paginated("acme/widgets", "pulls", "2000-01-01T00:00:00Z")
    assert len(rows) == 1
    assert (PULLS_URL + "&page=2") not in [c[1] for c in fake_gh.calls]


def test_collect_repo_events_recovers_all_31_fresh_events_across_pagination(fake_gh):
    # End-to-end: without the pagination fix, PR #0 (the oldest, evicted off
    # page1) would be silently and permanently dropped. Closed-unmerged PRs
    # need no detail call, keeping this test's route table small.
    page1 = [
        _pr(n, "T%d" % n, "user%d" % n,
            closed_at="2026-08-24T10:%02d:00Z" % n,
            updated_at="2026-08-24T10:%02d:00Z" % n)
        for n in range(30, 0, -1)
    ]
    page2 = [
        _pr(0, "T0", "user0",
            closed_at="2026-08-24T10:00:00Z", updated_at="2026-08-24T10:00:00Z")
    ]
    fake_gh.add(["api", PULLS_URL], page1)
    fake_gh.add(["api", PULLS_URL + "&page=2"], page2)
    fake_gh.add(["api", ISSUES_URL], [])
    cursor = _cursor(pulls_ts="2026-08-24T09:00:00Z", issues_ts="2026-08-24T09:00:00Z")
    events, _ = landings.collect_repo_events("acme/widgets", cursor)
    assert len(events) == 31
    assert any("T0" in text for _, text in events)  # the row that pagination rescues


# ---- cursor: >= filter + seen-id dedupe, per-endpoint (verifier3 #2) -------


def test_cursor_advances_to_latest_event_timestamp(fake_gh):
    fake_gh.add(["api", PULLS_URL], [
        _pr(1, "One", "a", merged_at="2026-08-24T08:00:00Z"),
        _pr(2, "Two", "b", merged_at="2026-08-24T12:00:00Z"),
    ])
    fake_gh.add(["api", ISSUES_URL], [])
    fake_gh.add(["api", "repos/acme/widgets/pulls/1"], {"merged_by": {"login": "a"}})
    fake_gh.add(["api", "repos/acme/widgets/pulls/2"], {"merged_by": {"login": "b"}})
    events, new_cursor = landings.collect_repo_events("acme/widgets", _cursor())
    assert len(events) == 2
    assert new_cursor["pulls"]["ts"] == "2026-08-24T12:00:00Z"


def test_pr_merged_and_issue_closed_same_second_across_three_passes(fake_gh, posted, monkeypatch):
    """The exact scenario verifier3 reproduced: a PR merged at T posts;
    an issue closed at the SAME second T, becoming visible to the API only
    on the next pass, still posts (no longer shadowed by a shared
    high-water mark); a third pass -- nothing changed -- posts nothing."""
    monkeypatch.setenv("COMMS_GH_REPOS", "acme/widgets")
    t = "2026-08-24T18:39:21Z"

    # Pass 1: only the merged PR is visible.
    fake_gh.add(["api", PULLS_URL], [_pr(1, "Merged one", "a", merged_at=t)])
    fake_gh.add(["api", ISSUES_URL], [])
    fake_gh.add(["api", "repos/acme/widgets/pulls/1"], {"merged_by": {"login": "a"}})
    assert landings.run_once() == 0
    assert len(posted) == 1
    assert "Merged one" in posted[0][1]

    # Pass 2: the issue, closed at the exact same second, becomes visible.
    posted.clear()
    fake_gh.add(["api", ISSUES_URL], [
        _issue(9, "Closed one", "b", t, closed_by="b"),
    ])
    assert landings.run_once() == 0
    assert len(posted) == 1
    assert "Closed one" in posted[0][1]

    # Pass 3: nothing changed -- idempotent, nothing re-posted.
    posted.clear()
    assert landings.run_once() == 0
    assert posted == []


def test_two_prs_merged_same_second_then_a_late_third_still_posts(fake_gh):
    """Within-endpoint version of the same-second fix: two PRs merged in the
    SAME second both post once and never repost; a third PR that lands at
    that identical second on a later pass (eventual consistency) still
    posts, because the seen-id set -- not just the timestamp -- gates it."""
    t = "2026-08-24T12:00:00Z"
    fake_gh.add(["api", PULLS_URL], [
        _pr(1, "One", "a", merged_at=t),
        _pr(2, "Two", "b", merged_at=t),
    ])
    fake_gh.add(["api", ISSUES_URL], [])
    fake_gh.add(["api", "repos/acme/widgets/pulls/1"], {"merged_by": {"login": "a"}})
    fake_gh.add(["api", "repos/acme/widgets/pulls/2"], {"merged_by": {"login": "b"}})
    events1, cursor1 = landings.collect_repo_events("acme/widgets", _cursor())
    assert len(events1) == 2
    assert cursor1["pulls"]["ts"] == t
    assert sorted(cursor1["pulls"]["seen"]) == ["pr:1", "pr:2"]

    # Second pass, same two PRs still in the list window: both already seen.
    events2, cursor2 = landings.collect_repo_events("acme/widgets", cursor1)
    assert events2 == []
    assert cursor2 == cursor1

    # A third PR merged at the identical second appears (late arrival).
    fake_gh.add(["api", PULLS_URL], [
        _pr(1, "One", "a", merged_at=t),
        _pr(2, "Two", "b", merged_at=t),
        _pr(3, "Three", "c", merged_at=t),
    ])
    fake_gh.add(["api", "repos/acme/widgets/pulls/3"], {"merged_by": {"login": "c"}})
    events3, cursor3 = landings.collect_repo_events("acme/widgets", cursor2)
    assert len(events3) == 1
    assert "Three" in events3[0][1]
    assert sorted(cursor3["pulls"]["seen"]) == ["pr:1", "pr:2", "pr:3"]


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
    events, _ = landings.collect_repo_events("acme/widgets", {})  # no cursor at all: first sight
    assert events == []  # older than today's cap: never surfaced on first sight


def test_today_utc_start_shape():
    start = landings._today_utc_start()
    assert start.endswith("T00:00:00Z")
    assert len(start) == len("2026-08-24T00:00:00Z")


# ---- per-repo exception isolation (verifier3 #3: real isolation claim) ----


def test_collect_new_isolates_one_repo_failure_the_healthy_repo_advances_the_failing_one_does_not(
    fake_gh, monkeypatch, capsys
):
    monkeypatch.setenv("COMMS_GH_REPOS", "acme/widgets,acme/gizmos")
    # Seed a pre-existing cursor for gizmos, as if an earlier pass had
    # already succeeded there -- so "unchanged" is a real, checkable claim,
    # not just "the key happens to be absent".
    landings._save_cursor({
        "acme/gizmos": _cursor(pulls_ts="2026-08-20T00:00:00Z", issues_ts="2026-08-20T00:00:00Z"),
    })
    fake_gh.add(["api", PULLS_URL], [_pr(1, "Good one", "a", merged_at="2026-08-24T10:00:00Z")])
    fake_gh.add(["api", ISSUES_URL], [])
    fake_gh.add(["api", "repos/acme/widgets/pulls/1"], {"merged_by": {"login": "a"}})
    fake_gh.add_raise(["api", GIZMOS_PULLS_URL], RuntimeError("gh api rate limited"))

    events, new_cursor = landings.collect_new()

    assert any("Good one" in e for e in events)
    # The real isolation claim: the healthy repo's cursor ADVANCED past the
    # seeded far-past default...
    assert new_cursor["acme/widgets"]["pulls"]["ts"] == "2026-08-24T10:00:00Z"
    # ...and the failing repo's cursor is UNCHANGED from what it was before
    # this pass ran -- not zeroed, not reseeded, exactly what it already was.
    assert new_cursor["acme/gizmos"]["pulls"]["ts"] == "2026-08-20T00:00:00Z"
    assert new_cursor["acme/gizmos"]["issues"]["ts"] == "2026-08-20T00:00:00Z"

    err = capsys.readouterr().err
    assert "acme/gizmos" in err
    assert "RuntimeError" in err


def test_run_once_survives_one_repo_exception_and_delivers_the_rest(fake_gh, posted, monkeypatch):
    monkeypatch.setenv("COMMS_GH_REPOS", "acme/widgets,acme/gizmos")
    fake_gh.add(["api", PULLS_URL], [_pr(1, "Good one", "a", merged_at="2026-08-24T10:00:00Z")])
    fake_gh.add(["api", ISSUES_URL], [])
    fake_gh.add(["api", "repos/acme/widgets/pulls/1"], {"merged_by": {"login": "a"}})
    fake_gh.add_raise(["api", GIZMOS_PULLS_URL], RuntimeError("boom"))
    assert landings.run_once() == 0
    joined = "\n".join(c for _, c, _ in posted)
    assert "Good one" in joined


# ---- discover_repos() failure under --once (verifier3 #4) ------------------


def test_run_once_discover_repos_failure_exits_1_with_one_stderr_line_no_traceback(
    fake_gh, capsys, monkeypatch
):
    monkeypatch.setenv("COMMS_GH_OWNER", "acme")  # skip the api-user call
    fake_gh.add_raise(
        ["repo", "list", "acme", "--limit", "100", "--json", "nameWithOwner,pushedAt"],
        RuntimeError("gh: rate limit exceeded"),
    )
    assert landings.run_once() == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "Traceback" not in err
    assert "RuntimeError" in err


def test_main_once_discover_repos_failure_returns_1_not_2(fake_gh, monkeypatch):
    monkeypatch.setenv("COMMS_GH_OWNER", "acme")
    fake_gh.add_raise(
        ["repo", "list", "acme", "--limit", "100", "--json", "nameWithOwner,pushedAt"],
        RuntimeError("boom"),
    )
    assert landings.main(["landings.py", "--once"]) == 1


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


# ---- delivery: author, chunking, skipped-count (verifier3 #6) -------------


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


def test_chunk_events_returns_content_and_events_in_chunk():
    events = ["a" * 10, "b" * 10]
    chunks = landings.chunk_events(events, cap=1900)
    assert len(chunks) == 1
    content, events_in_chunk = chunks[0]
    assert content == "\n".join(events)
    assert events_in_chunk == events


def test_log_skipped_counts_individual_events_not_the_joined_chunk(fake_gh, monkeypatch, capsys):
    # verifier3's finding: _log_skipped used to receive [joined_chunk_string],
    # so a chunk of 4 events logged "SKIPPED 1 event(s)". It must now log the
    # real count -- same shape as mirror.py's _log_skipped(runid, rows, ...).
    monkeypatch.setenv("COMMS_GH_REPOS", "acme/widgets")

    def failing_post(url, content, username=None):
        return False

    monkeypatch.setattr(landings.mirror, "post_content", failing_post)
    fake_gh.add(["api", PULLS_URL], [
        _pr(1, "One", "a", merged_at="2026-08-24T10:00:00Z"),
        _pr(2, "Two", "b", merged_at="2026-08-24T10:01:00Z"),
    ])
    fake_gh.add(["api", ISSUES_URL], [
        _issue(3, "Three", "c", "2026-08-24T10:02:00Z", closed_by="c"),
        _issue(4, "Four", "d", "2026-08-24T10:03:00Z", closed_by="d"),
    ])
    fake_gh.add(["api", "repos/acme/widgets/pulls/1"], {"merged_by": {"login": "a"}})
    fake_gh.add(["api", "repos/acme/widgets/pulls/2"], {"merged_by": {"login": "b"}})
    assert landings.run_once() == 1
    err = capsys.readouterr().err
    assert "SKIPPED 4 event(s)" in err
    with open(landings._skipped_path()) as fh:
        recorded = [json.loads(line) for line in fh]
    assert len(recorded) == 4


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


# ---- landings-channel split: DISCORD_COMMS_LANDINGS_WEBHOOK_URL first ------
#
# The dedicated-channel var wins wherever it is set (env or the secrets file);
# the MAIN-channel var is only consulted when BOTH of those miss. The
# fallback cases below are the regression guard for machines that never
# configure the split -- their behavior must not change at all.


def test_landings_var_wins_over_main_var_when_both_set_in_env(monkeypatch):
    monkeypatch.setenv("DISCORD_COMMS_WEBHOOK_URL", "http://127.0.0.1:1/main")
    monkeypatch.setenv(
        "DISCORD_COMMS_LANDINGS_WEBHOOK_URL", "http://127.0.0.1:1/landings"
    )
    assert landings.resolve_webhook_url() == "http://127.0.0.1:1/landings"


def test_landings_var_absent_falls_back_to_main_var(monkeypatch):
    monkeypatch.delenv("DISCORD_COMMS_LANDINGS_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("DISCORD_COMMS_WEBHOOK_URL", "http://127.0.0.1:1/main")
    assert landings.resolve_webhook_url() == "http://127.0.0.1:1/main"


def test_landings_var_read_from_secrets_file_beats_main_var_in_env(
    tmp_path, monkeypatch
):
    """The landings var's SECOND lookup step (the secrets-file line scan)
    still outranks the main var -- precedence is per-var, not per-source."""
    secrets = tmp_path / "comms.env"
    secrets.write_text(
        "DISCORD_COMMS_LANDINGS_WEBHOOK_URL=http://127.0.0.1:1/from-file\n"
    )
    monkeypatch.setenv("COMMS_SECRETS_FILE", str(secrets))
    monkeypatch.setenv("DISCORD_COMMS_WEBHOOK_URL", "http://127.0.0.1:1/main")
    monkeypatch.delenv("DISCORD_COMMS_LANDINGS_WEBHOOK_URL", raising=False)
    assert landings.resolve_webhook_url() == "http://127.0.0.1:1/from-file"


def test_main_var_in_secrets_file_still_resolves_with_no_landings_var(
    tmp_path, monkeypatch
):
    secrets = tmp_path / "comms.env"
    secrets.write_text("DISCORD_COMMS_WEBHOOK_URL=http://127.0.0.1:1/main-file\n")
    monkeypatch.setenv("COMMS_SECRETS_FILE", str(secrets))
    monkeypatch.delenv("DISCORD_COMMS_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_COMMS_LANDINGS_WEBHOOK_URL", raising=False)
    assert landings.resolve_webhook_url() == "http://127.0.0.1:1/main-file"


def test_run_once_delivers_to_the_landings_channel_when_configured(
    fake_gh, posted, monkeypatch
):
    """End to end: the split reaches the actual POST, not just the resolver."""
    monkeypatch.setenv(
        "DISCORD_COMMS_LANDINGS_WEBHOOK_URL", "http://127.0.0.1:1/landings"
    )
    monkeypatch.setenv("COMMS_GH_REPOS", "acme/widgets")
    fake_gh.add(["api", PULLS_URL], [
        _pr(1, "One", "a", merged_at="2026-08-24T10:00:00Z"),
    ])
    fake_gh.add(["api", ISSUES_URL], [])
    fake_gh.add(["api", "repos/acme/widgets/pulls/1"], {"merged_by": {"login": "a"}})
    assert landings.run_once() == 0
    assert posted[0][0] == "http://127.0.0.1:1/landings"


def test_missing_secret_message_names_both_vars(monkeypatch, capsys):
    monkeypatch.delenv("DISCORD_COMMS_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_COMMS_LANDINGS_WEBHOOK_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        landings.resolve_webhook_url()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "DISCORD_COMMS_LANDINGS_WEBHOOK_URL=" in err
    assert "DISCORD_COMMS_WEBHOOK_URL=" in err
    assert "http" not in err  # never echo a URL into the transcript


def test_follow_missing_secret_ignores_an_unset_landings_var(monkeypatch, capsys):
    """--follow's quiet check must not gain a second noisy failure mode from
    the split: one stderr line, one 60s backoff, exactly as before."""
    monkeypatch.delenv("DISCORD_COMMS_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_COMMS_LANDINGS_WEBHOOK_URL", raising=False)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt()

    monkeypatch.setattr(landings.time, "sleep", fake_sleep)
    assert landings.follow(120) == 0
    assert sleeps == [60]
    assert capsys.readouterr().err.count("\n") == 1


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
    assert data["acme/widgets"]["pulls"]["ts"] == "2026-08-24T10:00:00Z"


def test_cursor_tmp_path_includes_pid():
    tmp = landings._cursor_tmp_path()
    assert tmp == landings._cursor_path() + ".tmp." + str(os.getpid())
