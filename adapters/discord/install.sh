#!/bin/bash
# adapters/discord/install.sh -- preflight + wiring instructions for the
# Discord mirror. Deliberately writes NOTHING (trivially idempotent): v1 runs
# the mirror by hand or under launchd from the instructions this prints.
#
# Checks (existence only -- the webhook VALUE is never read into output):
#   * python3 present
#   * mirror.py present beside this script
#   * DISCORD_COMMS_WEBHOOK_URL configured (env, or a line in the secrets file)
#   * DISCORD_COMMS_FORUM_WEBHOOK_URL configured -- the BOARD lane's secret
#     (--lane board: one Discord forum thread per document, see README.md).
#     Reported, never blocking: the board lane is opt-in, and gating install
#     on it would fail every existing install whose human has not yet created
#     the forum channel and its webhook -- a Discord-UI step nothing here can
#     script. Without it, --lane board exits 2 naming the drop-in line; the
#     other two lanes are unaffected.
#
# Exit: 0 ready | 2 secret missing (prints the exact drop-in) | 1 broken.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
MIRROR="$SELF_DIR/mirror.py"
SECRETS="${COMMS_SECRETS_FILE:-$HOME/.secrets/comms.env}"

command -v python3 >/dev/null || { echo "install: FAILED: python3 not found" >&2; exit 1; }
[ -f "$MIRROR" ] || { echo "install: FAILED: missing $MIRROR" >&2; exit 1; }

# Existence check ONLY: grep -c counts matching lines, never prints the value.
have_secret=0
if [ -n "${DISCORD_COMMS_WEBHOOK_URL:-}" ]; then
  have_secret=1
elif [ -f "$SECRETS" ] && [ "$(grep -c '^DISCORD_COMMS_WEBHOOK_URL=' "$SECRETS")" -ge 1 ]; then
  have_secret=1
fi

if [ "$have_secret" -ne 1 ]; then
  cat >&2 <<EOF
install: webhook secret not configured. Drop-in (3 steps):
  1. open -e ~/.secrets/comms.env
  2. add line: DISCORD_COMMS_WEBHOOK_URL=<paste webhook URL from Discord channel settings>
  3. chmod 600 ~/.secrets/comms.env
Then re-run this script.
EOF
  exit 2
fi

# Existence check ONLY, and non-blocking: the forum board webhook is
# optional in this slice (see header comment). Only affects the status
# line printed below.
have_forum_secret=0
if [ -n "${DISCORD_COMMS_FORUM_WEBHOOK_URL:-}" ]; then
  have_forum_secret=1
elif [ -f "$SECRETS" ] && [ "$(grep -c '^DISCORD_COMMS_FORUM_WEBHOOK_URL=' "$SECRETS")" -ge 1 ]; then
  have_forum_secret=1
fi
if [ "$have_forum_secret" -eq 1 ]; then
  forum_status="configured -- 'mirror.py --follow-all --lane board' can run"
else
  forum_status="NOT configured -- the board lane cannot run (the other lanes are unaffected)"
fi

# Audience switch: value check only, mirrored from mirror.py's closed set.
# A typo here would make every follower exit 2 under KeepAlive, so name it
# now while a human is watching.
audience="${COMMS_AUDIENCE:-}"
if [ -z "$audience" ] && [ -f "$SECRETS" ]; then
  audience="$(grep '^COMMS_AUDIENCE=' "$SECRETS" | tail -1 | cut -d= -f2- | tr -d "\"' ")"
fi
audience="$(printf '%s' "${audience:-engineer}" | tr '[:upper:]' '[:lower:]' | tr -d ' ')"
case "$audience" in
  engineer|everyone) ;;
  *) echo "install: FAILED: COMMS_AUDIENCE=$audience; must be engineer or everyone" >&2; exit 2 ;;
esac

cat <<EOF
discord mirror: ready. Audience: $audience (COMMS_AUDIENCE=engineer|everyone;
see README.md, The everyone audience).

Run it manually (per run, per machine):
  python3 $MIRROR --once <runid>              # mirror new rows, exit
  python3 $MIRROR --follow <runid>            # poll loop (5s; --interval N)

Or mirror EVERY run under the mailbox root in one process (picks up newly
armed runs without a restart):
  python3 $MIRROR --follow-all                        # lane "all" (default)
  python3 $MIRROR --follow-all --lane convo           # conversation-only, 2nd webhook
  python3 $MIRROR --follow-all --lane board           # one forum thread per document

--lane convo on ANY of the above sends to DISCORD_COMMS_CONVO_WEBHOOK_URL
(same drop-in steps as above, different var) instead of
DISCORD_COMMS_WEBHOOK_URL. Never run two of these against the same
(runid or "every run") AND the same lane at once -- see README.md,
Concurrency.

NOTE: the GitHub landings watcher (adapters/github/landings.py) is not one of
these lanes and needs no secret of its own -- it posts to
DISCORD_COMMS_LANDINGS_WEBHOOK_URL when that OPTIONAL var is set (env or the
secrets file, same drop-in steps as above) and otherwise falls back to
DISCORD_COMMS_WEBHOOK_URL, the channel above. Not checked here, and never a
reason this script fails: see adapters/github/README.md.

"--follow-all --lane convo" ALSO tails the heartbeat-telemetry ingestion
log each pass (adapters/discord/ingest_mirror.py), posting a "heard from
mailbox" event when the heartbeat hook delivers new rows to an agent -- one
process, no second launchd job. It has its own standalone CLI too, if you
want it separate:
  python3 $SELF_DIR/ingest_mirror.py --once
  python3 $SELF_DIR/ingest_mirror.py --follow            # poll loop
Set DISCORD_COMMS_CONVO_INGEST=0 (default 1) to stop posting those "read N
row(s)" lines; the ingest cursor keeps advancing while it is off, so turning
it back on does not replay the backlog. Mailbox rows in convo are unaffected.
See README.md, Ingestion events.

Keep it alive under launchd (optional): save as
~/Library/LaunchAgents/com.comms.discord-mirror.<runid>.plist, then
'launchctl load' it:

  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0"><dict>
    <key>Label</key><string>com.comms.discord-mirror.RUNID</string>
    <key>ProgramArguments</key><array>
      <string>/usr/bin/python3</string>
      <string>$MIRROR</string>
      <string>--follow</string><string>RUNID</string>
    </array>
    <key>KeepAlive</key><true/>
  </dict></plist>

A --follow-all --lane convo variant of the same plist (mirrors every run's
conversation to the second channel; swap the Label so it does not collide
with a plain --follow-all job):

  <key>Label</key><string>com.comms.discord-mirror.follow-all-convo</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>$MIRROR</string>
    <string>--follow-all</string><string>--lane</string><string>convo</string>
  </array>
  <key>KeepAlive</key><true/>

Env knobs: COMMS_AUDIENCE (engineer|everyone, the channel's vocabulary),
COMMS_MACHINE_LABEL (prefix; default hostname -s),
COMMS_ROOT (mailbox root), COMMS_STATE_DIR (cursor/skipped/held state),
COMMS_SECRETS_FILE (default ~/.secrets/comms.env), COMMS_MIRROR_INTERVAL.
Convo lane only: DISCORD_COMMS_CONVO_INGEST (0|1, default 1).
Board lane only: COMMS_THREAD_ALIVE_SECONDS (default 1800),
COMMS_THREAD_ALIVE_SEATS (2), COMMS_THREAD_HOLD_MAX (500).

Board lane webhook (DISCORD_COMMS_FORUM_WEBHOOK_URL): $forum_status.
Setting it up is a HUMAN step in the Discord UI, in this order:
  1. in Discord, create a FORUM channel (not a text channel -- the board
     posts one thread per document, which only a forum can hold)
  2. that channel's Settings -> Integrations -> Webhooks -> New Webhook,
     then Copy Webhook URL
  3. open -e ~/.secrets/comms.env
  4. add line: DISCORD_COMMS_FORUM_WEBHOOK_URL=<paste that URL>
  5. chmod 600 ~/.secrets/comms.env
Then: python3 $MIRROR --once <runid> --lane board
A row only reaches the board once its document's conversation is alive (two
seats within 30 minutes, by default); until then it waits in the lane's
held file. See README.md, The board lane.
EOF
