#!/usr/bin/env python3
"""GitHub landings watcher: poll GitHub for merged/closed PRs and closed
issues, post one Discord line per landing WITH attribution ("who merged/
closed what"), to the dedicated landings channel if one is configured
(DISCORD_COMMS_LANDINGS_WEBHOOK_URL) and otherwise to the main comms
channel (DISCORD_COMMS_WEBHOOK_URL) -- see SECRET_VAR below.

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

A discovery failure (a bad owner, a rate limit hit on the `repo list` call
itself) is NOT swallowed: it propagates out of collect_new() and is caught
by run_once() with exactly one stderr line, no traceback -- see run_once's
docstring. It is a distinct failure mode from a single repo's event
collection failing (see EVENTS below), which IS isolated per repo.

EVENTS: per repo, per pass, one or more paginated `gh api` list calls per
endpoint (closed PRs, closed issues -- see PAGINATION) plus ONE extra detail
call per FRESH merged-PR event (never per row in the list, and never for a
closed-unmerged PR or a closed issue -- see ATTRIBUTION) to learn who
actually merged it:

  merged PR       (merged_at set, >= cursor)     -> \U0001f7e3 merged
  closed-unmerged PR (closed_at set, merged_at unset, >= cursor) -> closed
  closed issue    (no "pull_request" key, closed_at >= cursor)   -> closed

ATTRIBUTION: a closed-unmerged PR renders with the PR's own `user` (author)
straight from the list row. A closed issue renders with `closed_by` --
ALSO straight from the list row: GitHub's issues list endpoint (unlike the
pulls list endpoint) already includes `closed_by` fully populated, so no
extra call is spent on it. Only "who merged" (`merged_by`) is genuinely
absent from its list endpoint (the pulls list carries `merged_at` but not
who did it), so that is the only case that pays for a detail call -- and
only for events that already passed the cursor filter, so API cost is
bounded by fresh events, not by list size.

PAGINATION: each list endpoint is fetched at `per_page=PER_PAGE` (30); if a
page comes back full AND its oldest row's `updated_at` (the field GitHub is
actually sorting by) is still newer than that endpoint's cursor, there may
be more fresh rows sitting past this page -- silently stopping there would
permanently drop them (they age out from under `sort=updated` on the very
next pass, evicted by newer traffic). So the fetch keeps requesting
`page=2, 3, ...` while that condition holds, bounded at MAX_PAGES (5); if
still truncated at the bound, ONE stderr line names the repo and endpoint
before giving up for this pass (never a silent, permanent loss).

CURSOR: cursor state is PER REPO, PER ENDPOINT (`pulls` and `issues` are
tracked independently -- a merged PR and a closed issue landing in the same
second used to share one high-water mark and could shadow each other; they
cannot now, because they are different streams). Each endpoint's state is
`{"ts": <ISO8601 UTC high-water mark>, "seen": [<event ids at that exact
ts>]}`; the filter is `event_ts >= ts`, with events exactly AT `ts` further
gated by "id not already in `seen`" (event id = `"pr:<n>"` / `"issue:<n>"`)
-- a strict `>` filter would permanently drop any second event landing in
the SAME second as the current high-water mark, because it can never be
`>` a mark equal to itself. The whole thing lives in one JSON file at
$COMMS_STATE_DIR/github-landings/cursor.json, mapping "owner/repo" ->
{"pulls": {...}, "issues": {...}}, written via tmp + os.replace with a
PID-suffixed tmp name (same shape as mirror.py's cursor). FIRST SIGHT of a
repo (no entry in the cursor file yet, either endpoint) seeds that
endpoint's `ts` at the start of TODAY, UTC (`00:00:00Z`, not the local
day -- on a machine west of UTC this reaches a few hours into what is
locally still "yesterday evening") -- the "backlog of today" backfill cap
Drake asked for: the very first pass posts today's (UTC) landings and
nothing older, instead of replaying a repo's entire closed-PR/issue history
the first time it is ever watched.

CLI:
  landings.py --once
  landings.py --follow [--interval N]      # poll loop (default 120s)
Exit: 0 delivered (or nothing new) | 1 some events skipped after retries, a
      repo failed this pass, or repo discovery itself failed this pass |
      2 usage or missing webhook secret (--once only).

LAUNCHD SAFETY: identical shape to mirror.py -- --follow catches a missing
secret (one stderr line, 60s backoff) instead of exiting, and any per-repo,
discovery, or per-pass exception is caught, named on one stderr line, and
does not stop the loop. --once keeps the loud exit 2 for a missing secret,
and now also a loud (but traceback-free) exit 1 for a discovery failure --
see run_once(). See mirror.py's own docstring, LAUNCHD SAFETY, for the
crash-loop rationale (unchanged here).
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

LANDINGS_SECRET_VAR = "DISCORD_COMMS_LANDINGS_WEBHOOK_URL"  # a channel of
# landings' OWN, checked FIRST: landings are a steady dashboard feed, and on a
# busy day they drown the main channel's human-readable traffic. Optional --
# see SECRET_VAR below for what happens when it is unset.
SECRET_VAR = "DISCORD_COMMS_WEBHOOK_URL"  # the MAIN channel -- the FALLBACK,
# used whenever LANDINGS_SECRET_VAR is set nowhere (env or secrets file), so a
# machine that never configures the split keeps its existing behavior exactly.
# Landings are dashboard/outcome news, not agent-to-agent chatter, so this
# deliberately reuses mirror.py's default-lane var, never the convo lane's.

CONTENT_CAP = 1900  # same headroom under Discord's 2000-char cap as mirror.py

DEFAULT_INTERVAL = 120  # seconds; GitHub rate budget: discovery + up to
# MAX_PAGES list calls per active repo per endpoint per pass, plus one
# detail call per FRESH merged-PR event only.
MISSING_SECRET_RETRY_SECONDS = 60  # same launchd-safety backoff as mirror.py

DEFAULT_WINDOW_HOURS = 24

PER_PAGE = 30
MAX_PAGES = 5  # small bound: a repo needing more than 150 fresh rows in one
# list endpoint in one pass is pathological, not a backlog worth chasing
# forever -- see module docstring, PAGINATION.

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
    repos and filters to ones pushed within the window. Raises on failure
    (bad owner, rate limit) -- deliberately NOT caught here; run_once()
    catches it once, at the top of a pass (see module docstring, DISCOVERY
    and LAUNCHD SAFETY) -- a discovery failure is a different, coarser
    failure mode than one repo's event collection failing, and gets its own
    single stderr line rather than being folded into the per-repo loop."""
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


def _list_url(repo, endpoint, page):
    url = "repos/%s/%s?state=closed&sort=updated&direction=desc&per_page=%d" % (
        repo,
        endpoint,
        PER_PAGE,
    )
    if page > 1:
        url += "&page=%d" % page
    return url


def _fetch_paginated(repo, endpoint, cursor_ts):
    """Fetch every closed row from `endpoint` ("pulls" or "issues") for
    `repo`, following `page=2, 3, ...` while a page comes back FULL
    (== PER_PAGE rows) and its oldest row's `updated_at` is still newer than
    `cursor_ts` -- see module docstring, PAGINATION. Bounded at MAX_PAGES:
    if still truncated at the bound, logs ONE stderr line naming the repo
    and endpoint and stops (never silent, never unbounded)."""
    rows = []
    page = 1
    while True:
        raw = _gh(["api", _list_url(repo, endpoint, page)])
        page_rows = json.loads(raw)
        rows.extend(page_rows)
        if len(page_rows) < PER_PAGE:
            break  # short page: definitely the end of the list
        oldest_updated = page_rows[-1].get("updated_at")
        if not oldest_updated or not cursor_ts or oldest_updated <= cursor_ts:
            break  # oldest visible row is already old enough: nothing fresh past here
        if page >= MAX_PAGES:
            sys.stderr.write(
                "github landings: %s %s list truncated after %d page(s) "
                "(%d row(s) fetched); some fresh events may be missing this "
                "pass\n" % (repo, endpoint, page, len(rows))
            )
            break
        page += 1
    return rows


def _fetch_prs(repo, cursor_ts):
    return _fetch_paginated(repo, "pulls", cursor_ts)


def _fetch_issues(repo, cursor_ts):
    return _fetch_paginated(repo, "issues", cursor_ts)


def _fetch_pr_detail(repo, number):
    raw = _gh(["api", "repos/%s/pulls/%d" % (repo, number)])
    return json.loads(raw)


def _is_fresh(ts, event_id, endpoint_state):
    """True if the event at `ts` (with id `event_id`, e.g. "pr:12") is new
    under `endpoint_state` ({"ts": ..., "seen": [...]}) -- newer than the
    high-water mark, or exactly AT it but not already recorded in `seen`
    (see module docstring, CURSOR, for why `>=` + a seen-set replaces a
    plain `>` filter)."""
    cursor_ts = endpoint_state.get("ts")
    if not cursor_ts or ts > cursor_ts:
        return True
    if ts == cursor_ts and event_id not in (endpoint_state.get("seen") or []):
        return True
    return False


def _advance_endpoint_state(state, considered):
    """`considered` is the list of (ts, event_id) pairs actually emitted
    this pass on this endpoint. Returns the new endpoint state: unchanged if
    nothing was considered, advanced to the highest ts seen (with `seen`
    reset to just the ids AT that new high-water mark) if that ts is newer
    than the current one, or -- the same-second case -- the SAME ts with
    `seen` extended to include the newly-emitted ids at it."""
    if not considered:
        return state
    max_ts = max(ts for ts, _ in considered)
    old_ts = state.get("ts")
    if not old_ts or max_ts > old_ts:
        seen_ids = sorted({eid for ts, eid in considered if ts == max_ts})
        return {"ts": max_ts, "seen": seen_ids}
    seen = set(state.get("seen") or [])
    seen.update(eid for ts, eid in considered if ts == max_ts)
    return {"ts": old_ts, "seen": sorted(seen)}


def collect_repo_events(repo, repo_cursor):
    """Return (events, new_repo_cursor) for one repo. `events` is a
    timestamp-sorted list of (timestamp_str, rendered_text) tuples for
    everything fresh (see _is_fresh) on either endpoint. `repo_cursor` is
    `{"pulls": {"ts", "seen"}, "issues": {"ts", "seen"}}`; either or both
    sub-states may be absent (first sight of that endpoint for this repo),
    in which case they seed to start-of-today UTC (see module docstring,
    CURSOR). `new_repo_cursor` has the SAME shape, each endpoint advanced
    independently -- a merged PR and a closed issue in the same second can
    no longer shadow each other, because they are different streams."""
    repo_cursor = repo_cursor or {}
    pulls_state = dict(repo_cursor.get("pulls") or {})
    issues_state = dict(repo_cursor.get("issues") or {})
    if not pulls_state.get("ts"):
        pulls_state["ts"] = _today_utc_start()
    if not issues_state.get("ts"):
        issues_state["ts"] = _today_utc_start()

    short = _repo_short(repo)
    events = []
    pulls_considered = []
    issues_considered = []

    for pr in _fetch_prs(repo, pulls_state["ts"]):
        number = pr.get("number")
        title = pr.get("title", "")
        author = (pr.get("user") or {}).get("login") or "someone"
        merged_at = pr.get("merged_at")
        closed_at = pr.get("closed_at")
        event_id = "pr:%d" % number
        if merged_at:
            if not _is_fresh(merged_at, event_id, pulls_state):
                continue
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
            pulls_considered.append((merged_at, event_id))
        elif closed_at:
            if not _is_fresh(closed_at, event_id, pulls_state):
                continue
            text = "%s %s closed PR #%d without merging on %s: %s" % (
                EMOJI_CLOSED_UNMERGED,
                author,
                number,
                short,
                title,
            )
            events.append((closed_at, text))
            pulls_considered.append((closed_at, event_id))

    for issue in _fetch_issues(repo, issues_state["ts"]):
        if "pull_request" in issue:
            continue  # a PR row surfaced through the issues endpoint -- the
            # PR list above already covers it under its own event shape.
        closed_at = issue.get("closed_at")
        if not closed_at:
            continue
        number = issue.get("number")
        event_id = "issue:%d" % number
        if not _is_fresh(closed_at, event_id, issues_state):
            continue
        title = issue.get("title", "")
        author = (issue.get("user") or {}).get("login") or "someone"
        # closed_by is already fully populated on the LIST row for issues
        # (unlike merged_by, which the pulls list omits) -- no detail call
        # needed; see module docstring, ATTRIBUTION.
        closed_by = (issue.get("closed_by") or {}).get("login")
        actor = closed_by or author
        text = "%s %s closed issue #%d on %s: %s" % (
            EMOJI_CLOSED_ISSUE,
            actor,
            number,
            short,
            title,
        )
        events.append((closed_at, text))
        issues_considered.append((closed_at, event_id))

    events.sort(key=lambda e: e[0])
    new_repo_cursor = {
        "pulls": _advance_endpoint_state(pulls_state, pulls_considered),
        "issues": _advance_endpoint_state(issues_state, issues_considered),
    }
    return events, new_repo_cursor


def collect_new():
    """Discover repos and collect every fresh event across all of them.
    Returns (event_texts, new_cursor_dict). discover_repos() failing is NOT
    caught here -- it propagates to run_once(), which gives it its own
    single stderr line (see module docstring, DISCOVERY). A single repo's
    exception during EVENT COLLECTION (a flaky `gh api` call, a rate limit
    hit mid-repo) IS caught here, named on one stderr line, and does not
    stop the rest of the pass -- so one broken repo never blocks every other
    repo's landings from reaching Discord, and that repo's cursor entry is
    left exactly as it was (untouched, not zeroed) so the next pass retries
    it from where it last succeeded."""
    repos = discover_repos()
    cursor = _load_cursor()
    new_cursor = dict(cursor)
    event_texts = []
    for repo in repos:
        try:
            repo_cursor = cursor.get(repo) or {}
            events, new_repo_cursor = collect_repo_events(repo, repo_cursor)
            event_texts.extend(text for _, text in events)
            new_cursor[repo] = new_repo_cursor
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


def _log_skipped(events, reason):
    """`events` is the list of individual EVENT lines that failed delivery
    (not the joined multi-line chunk they were batched into) -- so the
    printed count matches what actually failed, the same shape as
    mirror.py's _log_skipped(runid, rows, ...)."""
    os.makedirs(_landings_dir(), exist_ok=True)
    path = _skipped_path()
    with open(path, "a") as fh:
        for text in events:
            fh.write(json.dumps({"reason": reason, "text": text}) + "\n")
    sys.stderr.write(
        "github landings: SKIPPED %d event(s) (%s); recorded in %s\n"
        % (len(events), reason, path)
    )


# ---- delivery: chunk under the content cap, one shared author -------------


def chunk_events(events, cap=CONTENT_CAP):
    """Batch event lines into as few Discord messages as fit under the
    content cap. Unlike mirror.py's chunk_rows, every landings line shares
    ONE author ("github landings (<machine>)"), so there is no per-seat
    split -- only the size cap forces a new chunk. Returns a list of
    (content, events_in_chunk) pairs -- mirroring mirror.py's chunk_rows
    returning (author, content, rows_in_chunk) -- so a failed POST can log
    exactly which individual events it lost (see _log_skipped)."""
    chunks = []
    cur_lines, size = [], 0
    for text in events:
        over_cap = cur_lines and size + 1 + len(text) > cap
        if over_cap:
            chunks.append(("\n".join(cur_lines), list(cur_lines)))
            cur_lines, size = [], 0
        cur_lines.append(text)
        size += len(text) + (1 if size else 0)
    if cur_lines:
        chunks.append(("\n".join(cur_lines), list(cur_lines)))
    return chunks


def _find_config_var(var):
    """Two-step lookup for ONE var name: the process environment first, then a
    line scan of the secrets file ($COMMS_SECRETS_FILE, else
    ~/.secrets/comms.env). Returns None when both miss. No side effects -- both
    webhook vars share this one code path so they can never drift apart in how
    they are read. Env values are stripped, matching the file scan below: a
    whitespace-only value would otherwise count as configured, block the
    fallback var, and hand delivery an unpostable URL."""
    val = (os.environ.get(var) or "").strip()
    if val:
        return val
    path = os.environ.get("COMMS_SECRETS_FILE") or os.path.expanduser(
        "~/.secrets/comms.env"
    )
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(var + "="):
                    val = line.split("=", 1)[1].strip().strip("'\"")
                    if val:
                        return val
    except OSError:
        pass
    return None


def _find_webhook_url():
    """The ONE resolution site for this module's webhook. Precedence:
    LANDINGS_SECRET_VAR (env, then secrets file) wins; only when BOTH of its
    steps miss does SECRET_VAR get looked up the same way. With the landings
    var configured nowhere this is byte-identical to the pre-split behavior.
    No side effects -- see mirror.py's _find_webhook_url for why this is
    factored apart from resolve_webhook_url (the launchd-safety quiet-check
    path)."""
    return _find_config_var(LANDINGS_SECRET_VAR) or _find_config_var(SECRET_VAR)


def resolve_webhook_url():
    url = _find_webhook_url()
    if url:
        return url
    sys.stderr.write(
        "github landings: no webhook configured.\n"
        "  1. open -e ~/.secrets/comms.env\n"
        "  2. add line: %s=<paste webhook URL from the DEDICATED landings "
        "Discord channel settings> -- recommended, it keeps the landings feed "
        "out of the main channel\n"
        "     (or, to keep posting into the main channel, add line: %s=<its "
        "webhook URL> instead -- still supported as the fallback)\n"
        "  3. chmod 600 ~/.secrets/comms.env\n"
        % (LANDINGS_SECRET_VAR, SECRET_VAR)
    )
    sys.exit(2)


def run_once():
    """Poll every discovered repo once, deliver every fresh event, advance
    the cursor. Exit-code semantics of main(). Raises SystemExit(2) via
    resolve_webhook_url if the secret is missing -- follow() catches that
    itself before ever calling this (see module docstring, LAUNCHD SAFETY).

    A discover_repos() failure (bad owner, rate limit on the `repo list`
    call) is caught HERE -- not inside collect_new()'s per-repo loop, which
    only isolates ONE repo's event-collection failure from the rest -- and
    turned into exactly one stderr line and return 1, no traceback. This is
    a coarser, pass-wide failure (nothing was discovered, so nothing else
    ran either), distinct from a single repo failing mid-pass."""
    url = resolve_webhook_url()  # before any gh call: missing secret = 2 always
    machine = mirror.machine_label()
    old_cursor = _load_cursor()
    try:
        events, new_cursor = collect_new()
    except Exception as exc:
        sys.stderr.write(
            "github landings: repo discovery failed (%s); skipping this "
            "pass\n" % exc.__class__.__name__
        )
        return 1
    skipped = False
    if events:
        author = "github landings (%s)" % machine
        for content, events_in_chunk in chunk_events(events):
            if not mirror.post_content(url, content, username=author):
                _log_skipped(events_in_chunk, "webhook delivery failed")
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
    broken pass (an unwritable state dir, anything run_once itself does not
    already handle) never kills the --follow loop under launchd. SystemExit
    is not caught here."""
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
