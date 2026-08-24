#!/usr/bin/env python3
"""GitHub landings watcher: poll GitHub for merged/closed PRs and closed
issues, post one Discord line per landing WITH attribution ("who merged/
closed what"), to the main comms channel.

WHY: landing events (merge, close) come from GitHub, not this machine's
local mailbox -- adapters/discord/mirror.py tails the mailbox, which has
nothing to say about a PR someone merged from the GitHub web UI or a `gh pr
merge` on a different machine entirely. This is a SEPARATE poller with its
own source (the `gh` CLI) and its own cursor, reusing only the Discord
delivery half of mirror.py (see REUSE below).

THE SEAM: every `gh` invocation in this module goes through `_gh(args)` --
one function, so a test (or a future caller) can replace exactly one name to
fake the entire GitHub surface. `_gh` takes the argv AFTER "gh" (e.g.
["api", "repos/x/y/pulls?..."]) and returns raw stdout text; callers parse
JSON where the call is a JSON endpoint, or treat it as plain text where it
is not (`gh api user --jq .login` returns a bare username, not JSON).

DISCOVERY: which repos to watch, and it is layered so an explicit list
always wins over discovery:

  1. COMMS_GH_REPOS (comma-separated "owner/repo" list) -- if set, this IS
     the repo list, discovery never runs, `gh` is never even asked who the
     authenticated user is.
  2. Otherwise: COMMS_GH_OWNER (or, absent that, the authenticated user via
     `gh api user --jq .login`) lists that owner's repos
     (`gh repo list <owner> --limit 100 --json nameWithOwner,pushedAt`),
     filtered to `pushedAt` within COMMS_LANDINGS_WINDOW_HOURS hours of now
     (default 24) -- a repo nobody has touched recently is not worth two
     extra `gh api` calls every poll.

EVENTS: per repo, per pass, up to two `gh api` list calls (closed PRs,
closed issues) plus ONE extra detail call per FRESH event (never per row in
the list) to learn who actually merged/closed it:

  merged PR       (merged_at set, > cursor)     -> \U0001f7e3 merged
  closed-unmerged PR (closed_at set, merged_at unset, > cursor) -> ❌ closed
  closed issue    (no "pull_request" key, closed_at > cursor)   -> ✅ closed

A closed-unmerged PR renders with the PR's own `user` (author) straight from
the list row -- GitHub's list endpoint carries that already, so no detail
call is needed for that case; only "who merged" (`merged_by`, PR detail
endpoint only) and "who closed" (`closed_by`, issue detail endpoint only)
need the extra call, and only for events that already passed the cursor
filter -- API cost is bounded by fresh events, not by list size.

REUSE (Discord machinery): imports `post_content` and `machine_label` from
adapters/discord/mirror.py (sys.path pattern mirrors mirror.py's own lib/
import) instead of reimplementing HTTP delivery, retry, or the machine-label
resolution -- one webhook POST implementation for the whole repo. Author
(the webhook `username`) is always "github landings (<machine label>)" --
landings are dashboard/outcome news, not one agent's voice, so unlike
mirror.py's per-seat authorship every landings line shares one identity.
Webhook: DISCORD_COMMS_WEBHOOK_URL (the MAIN channel, same var and same
env-then-secrets-file resolution chain as mirror.py's default lane) -- NOT
the convo lane; landings are not agent-to-agent chatter.

CURSOR: single JSON file at $COMMS_STATE_DIR/github-landings/cursor.json,
mapping "owner/repo" -> ISO8601 UTC timestamp high-water mark (GitHub's own
"...Z" format, which sorts correctly as a plain string -- no need to parse
it for comparison). Written via tmp + os.replace with a PID-suffixed tmp
name, same shape as mirror.py's cursor (see that module's docstring for why
the PID suffix matters under concurrent writers). FIRST SIGHT of a repo (no
entry in the cursor file yet) seeds its cursor at the start of today, UTC --
the "backlog of today" backfill cap Drake asked for: the very first pass
posts today's landings and nothing older, instead of replaying a repo's
entire closed-PR history the first time it is ever watched.

CLI:
  landings.py --once
  landings.py --follow [--interval N]      # poll loop (default 120s)
Exit: 0 delivered (or nothing new) | 1 some rows skipped after retries, or a
      repo failed this pass | 2 usage or missing webhook secret (--once only).

LAUNCHD SAFETY: identical shape to mirror.py -- --follow catches a missing
secret (one stderr line, 60s backoff) instead of exiting, and any per-repo
or per-pass exception is caught, named on one stderr line, and does not stop
the loop. --once keeps the loud exit 2. See mirror.py's own docstring,
LAUNCHD SAFETY, for the crash-loop rationale (unchanged here).
"""

import datetime
import json
import os
import subprocess
import sys
import time

SELF_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SELF_DIR))
sys.path.insert(0, os.path.join(REPO_ROOT, "adapters", "discord"))
import mirror  # noqa: E402  (reused Discord machinery: post_content, machine_label)

SECRET_VAR = "DISCORD_COMMS_WEBHOOK_URL"  # the MAIN channel -- landings are
# dashboard/outcome news, not agent-to-agent chatter, so this deliberately
# reuses mirror.py's default-lane var, never the convo lane's.

CONTENT_CAP = 1900  # same headroom under Discord's 2000-char cap as mirror.py
MAX_RETRIES = 3

DEFAULT_INTERVAL = 120  # seconds; GitHub rate budget: discovery + 2 list
# calls per active repo per pass, plus one detail call per FRESH event only.
MISSING_SECRET_RETRY_SECONDS = 60  # same launchd-safety backoff as mirror.py

DEFAULT_WINDOW_HOURS = 24

EMOJI_MERGED = "\U0001f7e3"
EMOJI_CLOSED_UNMERGED = "❌"
EMOJI_CLOSED_ISSUE = "✅"


# ---- the seam: every gh call goes through this one function ----------------


def _gh(args):
    """Run `gh <args>`, return raw stdout text. THE ONE SEAM every gh call in
    this module goes through -- tests replace this single name to fake the
    entire GitHub surface (see module docstring, THE SEAM). Raises
    subprocess.CalledProcessError on a nonzero exit; callers that must
    isolate one repo's failure from the rest catch that (or any Exception)
    themselves -- this function never swallows anything."""
    result = subprocess.run(
        ["gh"] + list(args), capture_output=True, text=True, check=True
    )
    return result.stdout


# ---- time helpers ------------------------------------------------------


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _today_utc_start():
    """ISO8601 UTC start-of-today in GitHub's own "...Z" shape, so it
    string-compares correctly against GitHub's own timestamps without ever
    parsing either side (see module docstring, CURSOR)."""
    now = _utcnow()
    return now.strftime("%Y-%m-%dT00:00:00Z")


def _parse_iso(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---- discovery ---------------------------------------------------------


def _default_owner():
    owner = os.environ.get("COMMS_GH_OWNER")
    if owner:
        return owner
    return _gh(["api", "user", "--jq", ".login"]).strip()


def discover_repos():
    """Return the list of "owner/repo" strings to watch this pass. Explicit
    COMMS_GH_REPOS always wins (see module docstring, DISCOVERY) and skips
    `gh` entirely for the owner/list calls. Otherwise lists the owner's
    repos and filters to ones pushed within the window."""
    explicit = os.environ.get("COMMS_GH_REPOS")
    if explicit:
        return [r.strip() for r in explicit.split(",") if r.strip()]

    owner = _default_owner()
    window_hours = float(
        os.environ.get("COMMS_LANDINGS_WINDOW_HOURS", str(DEFAULT_WINDOW_HOURS))
    )
    cutoff = _utcnow() - datetime.timedelta(hours=window_hours)
    raw = _gh(
        ["repo", "list", owner, "--limit", "100", "--json", "nameWithOwner,pushedAt"]
    )
    repos = json.loads(raw)
    result = []
    for r in repos:
        pushed_at = r.get("pushedAt")
        if not pushed_at:
            continue
        if _parse_iso(pushed_at) >= cutoff:
            result.append(r["nameWithOwner"])
    return result


# ---- per-repo event collection ------------------------------------------


def _repo_short(repo):
    return repo.rsplit("/", 1)[-1]


def _fetch_prs(repo):
    raw = _gh(
        [
            "api",
            "repos/%s/pulls?state=closed&sort=updated&direction=desc&per_page=30"
            % repo,
        ]
    )
    return json.loads(raw)


def _fetch_issues(repo):
    raw = _gh(
        [
            "api",
            "repos/%s/issues?state=closed&sort=updated&direction=desc&per_page=30"
            % repo,
        ]
    )
    return json.loads(raw)


def _fetch_pr_detail(repo, number):
    raw = _gh(["api", "repos/%s/pulls/%d" % (repo, number)])
    return json.loads(raw)


def _fetch_issue_detail(repo, number):
    raw = _gh(["api", "repos/%s/issues/%d" % (repo, number)])
    return json.loads(raw)


def collect_repo_events(repo, cursor_ts):
    """Return (events, new_cursor_ts) for one repo. `events` is a list of
    (timestamp_str, rendered_text) tuples, timestamp-sorted, for everything
    newer than `cursor_ts`. `new_cursor_ts` is the highest timestamp seen
    across ALL considered events (or `cursor_ts` unchanged if none), so a
    repo with nothing new this pass gets its cursor left exactly alone.

    Detail calls (merged_by / closed_by) are made ONLY for events that
    already passed the cursor filter -- see module docstring, EVENTS."""
    short = _repo_short(repo)
    events = []
    latest = cursor_ts

    for pr in _fetch_prs(repo):
        number = pr.get("number")
        title = pr.get("title", "")
        author = (pr.get("user") or {}).get("login") or "someone"
        merged_at = pr.get("merged_at")
        closed_at = pr.get("closed_at")
        if merged_at and merged_at > cursor_ts:
            detail = _fetch_pr_detail(repo, number)
            merged_by = (detail.get("merged_by") or {}).get("login")
            actor = merged_by or author
            text = "%s %s merged PR #%d on %s: %s" % (
                EMOJI_MERGED,
                actor,
                number,
                short,
                title,
            )
            events.append((merged_at, text))
            if merged_at > latest:
                latest = merged_at
        elif closed_at and not merged_at and closed_at > cursor_ts:
            text = "%s %s closed PR #%d without merging on %s: %s" % (
                EMOJI_CLOSED_UNMERGED,
                author,
                number,
                short,
                title,
            )
            events.append((closed_at, text))
            if closed_at > latest:
                latest = closed_at

    for issue in _fetch_issues(repo):
        if "pull_request" in issue:
            continue  # a PR row surfaced through the issues endpoint -- the
            # PR list above already covers it under its own event shape.
        closed_at = issue.get("closed_at")
        if not closed_at or closed_at <= cursor_ts:
            continue
        number = issue.get("number")
        title = issue.get("title", "")
        author = (issue.get("user") or {}).get("login") or "someone"
        detail = _fetch_issue_detail(repo, number)
        closed_by = (detail.get("closed_by") or {}).get("login")
        actor = closed_by or author
        text = "%s %s closed issue #%d on %s: %s" % (
            EMOJI_CLOSED_ISSUE,
            actor,
            number,
            short,
            title,
        )
        events.append((closed_at, text))
        if closed_at > latest:
            latest = closed_at

    events.sort(key=lambda e: e[0])
    return events, latest


def collect_new():
    """Discover repos and collect every fresh event across all of them.
    Returns (event_texts, new_cursor_dict). A single repo's exception (a
    flaky `gh api` call, a rate limit) is caught here, named on one stderr
    line, and does NOT stop the rest of the pass -- see module docstring,
    LAUNCHD SAFETY -- so one broken repo never blocks every other repo's
    landings from reaching Discord."""
    repos = discover_repos()
    cursor = _load_cursor()
    new_cursor = dict(cursor)
    event_texts = []
    for repo in repos:
        try:
            repo_cursor = cursor.get(repo) or _today_utc_start()
            events, latest = collect_repo_events(repo, repo_cursor)
            event_texts.extend(text for _, text in events)
            new_cursor[repo] = latest
        except Exception as exc:
            sys.stderr.write(
                "github landings: repo %r failed (%s); continuing\n"
                % (repo, exc.__class__.__name__)
            )
    return event_texts, new_cursor


# ---- cursor persistence --------------------------------------------------


def _state_dir():
    return os.environ.get("COMMS_STATE_DIR") or os.path.expanduser("~/.comms/state")


def _landings_dir():
    return os.path.join(_state_dir(), "github-landings")


def _cursor_path():
    return os.path.join(_landings_dir(), "cursor.json")


def _cursor_tmp_path():
    # PID-suffixed tmp name -- same rationale as mirror.py's cursor: two
    # pollers racing on this one cursor file each get their own tmp path, so
    # one process's partial write is never clobbered by the other's
    # os.replace.
    return _cursor_path() + ".tmp." + str(os.getpid())


def _load_cursor():
    try:
        with open(_cursor_path()) as fh:
            cur = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return cur if isinstance(cur, dict) else {}


def _save_cursor(cursor):
    os.makedirs(_landings_dir(), exist_ok=True)
    tmp = _cursor_tmp_path()
    with open(tmp, "w") as fh:
        json.dump(cursor, fh)
    os.replace(tmp, _cursor_path())


def _skipped_path():
    return os.path.join(_landings_dir(), "skipped.jsonl")


def _log_skipped(texts, reason):
    os.makedirs(_landings_dir(), exist_ok=True)
    path = _skipped_path()
    with open(path, "a") as fh:
        for text in texts:
            fh.write(json.dumps({"reason": reason, "text": text}) + "\n")
    sys.stderr.write(
        "github landings: SKIPPED %d event(s) (%s); recorded in %s\n"
        % (len(texts), reason, path)
    )


# ---- delivery: chunk under the content cap, one shared author -------------


def chunk_events(events, cap=CONTENT_CAP):
    """Batch event lines into as few Discord messages as fit under the
    content cap. Unlike mirror.py's chunk_rows, every landings line shares
    ONE author ("github landings (<machine>)"), so there is no per-seat
    split -- only the size cap forces a new chunk."""
    chunks = []
    cur_lines, size = [], 0
    for text in events:
        over_cap = cur_lines and size + 1 + len(text) > cap
        if over_cap:
            chunks.append("\n".join(cur_lines))
            cur_lines, size = [], 0
        cur_lines.append(text)
        size += len(text) + (1 if size else 0)
    if cur_lines:
        chunks.append("\n".join(cur_lines))
    return chunks


def _find_webhook_url():
    """No side effects -- see mirror.py's _find_webhook_url for why this is
    factored apart from resolve_webhook_url (the launchd-safety quiet-check
    path)."""
    url = os.environ.get(SECRET_VAR)
    if url:
        return url
    path = os.environ.get("COMMS_SECRETS_FILE") or os.path.expanduser(
        "~/.secrets/comms.env"
    )
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(SECRET_VAR + "="):
                    val = line.split("=", 1)[1].strip().strip("'\"")
                    if val:
                        return val
    except OSError:
        pass
    return None


def resolve_webhook_url():
    url = _find_webhook_url()
    if url:
        return url
    sys.stderr.write(
        "github landings: no webhook configured.\n"
        "  1. open -e ~/.secrets/comms.env\n"
        "  2. add line: %s=<paste webhook URL from the MAIN comms Discord "
        "channel settings>\n"
        "  3. chmod 600 ~/.secrets/comms.env\n" % SECRET_VAR
    )
    sys.exit(2)


def run_once():
    """Poll every discovered repo once, deliver every fresh event, advance
    the cursor. Exit-code semantics of main(). Raises SystemExit(2) via
    resolve_webhook_url if the secret is missing -- follow() catches that
    itself before ever calling this (see module docstring, LAUNCHD
    SAFETY)."""
    url = resolve_webhook_url()  # before any gh call: missing secret = 2 always
    machine = mirror.machine_label()
    old_cursor = _load_cursor()
    events, new_cursor = collect_new()
    skipped = False
    if events:
        author = "github landings (%s)" % machine
        for content in chunk_events(events):
            if not mirror.post_content(url, content, username=author):
                _log_skipped([content], "webhook delivery failed")
                skipped = True
    if new_cursor != old_cursor:
        _save_cursor(new_cursor)
    return 1 if skipped else 0


def _warn_missing_secret():
    sys.stderr.write(
        "github landings: webhook secret missing; retrying in %ds\n"
        % MISSING_SECRET_RETRY_SECONDS
    )


def _run_once_logged():
    """run_once, but ANY exception is caught, named on one stderr line, and
    swallowed -- same S1 shape as mirror.py's _run_once_logged, so one
    broken pass (a bad discover_repos call, an unwritable state dir) never
    kills the --follow loop under launchd. SystemExit is not caught here."""
    try:
        return run_once()
    except Exception as exc:
        sys.stderr.write(
            "github landings: run_once failed (%s); continuing\n"
            % exc.__class__.__name__
        )
        return 1


def follow(interval):
    """Poll forever. LAUNCHD SAFETY: checks for the secret BEFORE calling
    run_once, so a missing secret never reaches resolve_webhook_url's
    multi-line drop-in on every poll -- one stderr line, then a 60s backoff.
    See mirror.py's follow() and module docstring, LAUNCHD SAFETY."""
    rc = 0
    while True:
        if _find_webhook_url() is None:
            _warn_missing_secret()
            sleep_for = MISSING_SECRET_RETRY_SECONDS
        else:
            rc = max(rc, _run_once_logged())
            sleep_for = interval
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            return rc


def main(argv):
    args = list(argv[1:])
    interval = float(os.environ.get("COMMS_LANDINGS_INTERVAL", str(DEFAULT_INTERVAL)))
    if "--interval" in args:
        i = args.index("--interval")
        try:
            interval = float(args[i + 1])
        except (IndexError, ValueError):
            sys.stderr.write("--interval needs a number\n")
            return 2
        del args[i : i + 2]
    if len(args) == 1 and args[0] == "--once":
        return run_once()
    if len(args) == 1 and args[0] == "--follow":
        try:
            return follow(interval)
        except KeyboardInterrupt:
            return 0
    sys.stderr.write(
        "usage: landings.py --once\n"
        "       landings.py --follow [--interval <seconds>]\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
