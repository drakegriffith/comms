#!/bin/bash
# adapters/discord/install.sh -- preflight + wiring instructions for the
# Discord mirror. Deliberately writes NOTHING (trivially idempotent): v1 runs
# the mirror by hand or under launchd from the instructions this prints.
#
# Checks (existence only -- the webhook VALUE is never read into output):
#   * python3 present
#   * mirror.py present beside this script
#   * DISCORD_COMMS_WEBHOOK_URL configured (env, or a line in the secrets file)
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

cat <<EOF
discord mirror: ready.

Run it manually (per run, per machine):
  python3 $MIRROR --once <runid>              # mirror new rows, exit
  python3 $MIRROR --follow <runid>            # poll loop (5s; --interval N)

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

Env knobs: COMMS_MACHINE_LABEL (prefix; default hostname -s),
COMMS_ROOT (mailbox root), COMMS_STATE_DIR (cursor/skipped state),
COMMS_SECRETS_FILE (default ~/.secrets/comms.env), COMMS_MIRROR_INTERVAL.
EOF
