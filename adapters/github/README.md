# GitHub landings watcher

Posts merged/closed PRs and closed issues to the main comms Discord channel,
WITH attribution -- "who actually merged this, who actually closed that" --
so a human watching the channel sees the whole fleet's outcomes, not just its
chatter:

    github landings (studio): 🟣 alice merged PR #12 on comms: feat(github): landings watcher
    github landings (studio): ❌ dave closed PR #14 without merging on comms: Abandoned idea
    github landings (studio): ✅ frank closed issue #7 on comms: Crash on startup

Landing events come from GitHub itself (`gh api`), not the local comms
mailbox -- `adapters/discord/mirror.py` tails the mailbox and has nothing to
say about a PR merged from the GitHub web UI, or from a machine that never
ran a comms agent at all. This is a separate poller with its own source and
its own cursor.

## Rendering

| Event | Emoji | Source |
| --- | --- | --- |
| PR merged | 🟣 | closed-PRs list, `merged_at` set |
| PR closed without merging | ❌ | closed-PRs list, `merged_at` unset |
| Issue closed | ✅ | closed-issues list, no `pull_request` key |

Line shape: `<emoji> <actor> <verb> #<n> on <repo-short>: <title>`. `<actor>`
is, in order of preference: the merge/close actor (`merged_by`/`closed_by`,
fetched with one extra `gh api` call per FRESH event only -- never per row
in the list, to keep API cost bounded) if GitHub reports one, else the
PR/issue author. `<repo-short>` is the repo name without the owner
(`comms`, not `drakegriffith/comms`) -- provenance across repos without the
noise of repeating the owner on every line.

Every landings line shares ONE Discord author (the webhook `username`):
`github landings (<machine label>)` -- unlike `adapters/discord/mirror.py`'s
per-seat authorship, landings are dashboard/outcome news, not one agent's
voice, so there is no seat to attribute the POST to.

## Discovery: which repos to watch

1. `COMMS_GH_REPOS` (comma-separated `owner/repo` list) -- if set, this IS
   the repo list. Discovery never runs; `gh` is never even asked who the
   authenticated user is.
2. Otherwise: `COMMS_GH_OWNER` (or, absent that, the authenticated user via
   `gh api user --jq .login`) lists that owner's repos
   (`gh repo list <owner> --limit 100 --json nameWithOwner,pushedAt`),
   filtered to ones pushed within `COMMS_LANDINGS_WINDOW_HOURS` hours of now
   (default 24) -- a repo nobody has touched recently costs two `gh api`
   calls every poll for nothing.

## Setup

1. Requires the `gh` CLI, authenticated (`gh auth status`).

2. Drop the webhook secret in (same var, same channel, as
   `adapters/discord/mirror.py`'s default lane -- landings post to the MAIN
   channel, not a conversation lane):
   1. `open -e ~/.secrets/comms.env`
   2. add line: `DISCORD_COMMS_WEBHOOK_URL=<paste webhook URL from the main
      channel's settings>`
   3. `chmod 600 ~/.secrets/comms.env`

   The URL is a credential: never commit it, never echo it. Read from the
   environment first, then that file; only the drop-in instructions print
   on failure (exit 2), never any value.

3. Run:

       python3 adapters/github/landings.py --once
       python3 adapters/github/landings.py --follow                    # poll loop, default 120s
       python3 adapters/github/landings.py --follow --interval 60      # tighter poll

## Cursor and the "backlog of today" cap

One JSON file, `$COMMS_STATE_DIR/github-landings/cursor.json`, mapping
`owner/repo` -> ISO8601 UTC timestamp high-water mark (GitHub's own `...Z`
format, string-compared, never parsed). Written via tmp + `os.replace` with
a PID-suffixed tmp name (same shape as `mirror.py`'s cursor).

The FIRST time a repo is seen (no entry yet), its cursor seeds at the start
of today, UTC -- so the first pass over a newly-watched repo posts today's
landings and nothing older, instead of replaying its entire closed-PR/issue
history on day one.

## Env knobs

| Var | Default | Meaning |
| --- | --- | --- |
| `COMMS_GH_REPOS` | *(unset)* | explicit comma-separated `owner/repo` list; overrides discovery |
| `COMMS_GH_OWNER` | authenticated user (`gh api user --jq .login`) | discovery owner |
| `COMMS_LANDINGS_WINDOW_HOURS` | `24` | discovery: only repos pushed within this many hours |
| `COMMS_LANDINGS_INTERVAL` | `120` | `--follow` poll seconds (overridden by `--interval`) |
| `COMMS_STATE_DIR` | `~/.comms/state` | cursor + skipped-event records |
| `COMMS_SECRETS_FILE` | `~/.secrets/comms.env` | where the webhook line lives |
| `DISCORD_COMMS_WEBHOOK_URL` | *(required)* | the main channel's webhook (same var as `mirror.py`'s default lane) |
| `COMMS_MACHINE_LABEL` | `hostname -s` | machine half of the author line |

## launchd safety

`--follow` is meant to run under a launchd `KeepAlive` job, which restarts
anything that exits nonzero. A job that exits every time it polls a
not-yet-configured secret would crash-loop, so under `--follow` a missing
secret does NOT exit: one stderr line, then a 60s retry. `--once` is
unaffected -- it still exits 2 and names the exact drop-in line, because a
one-shot invocation (a human, or a script checking the result) needs the
loud failure. A single repo's exception (a flaky `gh api` call, a rate
limit) is caught, named on one stderr line with the repo and exception
class, and does not stop the rest of that pass or the loop -- see
`landings.py`'s module docstring for the full rationale (same shape as
`adapters/discord/mirror.py`'s per-run exception isolation).

Example plist (save as
`~/Library/LaunchAgents/com.comms.github-landings.plist`, then
`launchctl load` it):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.comms.github-landings</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>/absolute/path/to/comms/adapters/github/landings.py</string>
    <string>--follow</string>
  </array>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/github-landings.err.log</string>
</dict></plist>
```

## Behavior guarantees

- **Bounded API cost.** Two list calls per active repo per pass (closed PRs,
  closed issues), plus exactly one detail call per FRESH event -- never per
  row in a list, and never for closed-unmerged PRs at all (the list row
  already carries the author GitHub reports for that case).
- **No reposts, idempotent.** The per-repo cursor advances to the latest
  event timestamp considered each pass; a second pass over unchanged data
  emits nothing.
- **Per-repo failure isolation.** One repo's `gh` failure is caught, logged,
  and skipped -- every other repo's landings still deliver that pass.
- **Rate-limit aware, never silently lossy.** A failed webhook POST is
  written to `skipped.jsonl` in the state dir and shouted to stderr, then
  the cursor advances (the skipped file is the durable record).

## Deliberately NOT covered here

Reviews, comments, force-pushes, draft PRs, or anything short of a terminal
merge/close -- this watcher is specifically the "did it land" signal Drake
asked for, not a general GitHub activity firehose.
