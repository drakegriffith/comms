# Discord mirror adapter

One Discord channel as the cross-machine live dashboard. Each machine runs its
own local comms mailbox (see `lib/swarm_mailbox.py`); this adapter tails that
mailbox and posts every row as a one-liner to a single channel via webhook:

    [studio/alpha] finding: cursor logic landed, tests green
    [macbook/beta] blocker: port 7778 already bound

Discord is the merge point AND the dashboard. There is no cross-machine file
sync: two machines never read each other's mailboxes; they both post into the
same channel, and the `[machine/seat]` prefix keeps provenance. Command
direction (typing in Discord to steer a machine) is deliberately out of scope
here -- durable commands go through the GitHub board.

## Setup

1. Create the webhook (one time, in Discord):
   Server Settings -> Integrations -> Webhooks -> New Webhook -> pick the
   channel -> Copy Webhook URL.

2. Drop the secret in (3 steps, for Drake):
   1. `open -e ~/.secrets/comms.env`
   2. add line: `DISCORD_COMMS_WEBHOOK_URL=<paste webhook URL from Discord channel settings>`
   3. `chmod 600 ~/.secrets/comms.env`

   The URL is a credential: never commit it, never echo it. The mirror reads
   it from the environment first, then from that file; it prints only the
   drop-in instructions when it is missing (exit 2), never any value.

3. Preflight + run:

       bash adapters/discord/install.sh          # checks wiring, prints run cmds
       python3 adapters/discord/mirror.py --once <runid>
       python3 adapters/discord/mirror.py --follow <runid>   # poll loop

   Run one mirror per machine per run. `install.sh` also prints a launchd
   plist template if you want it supervised.

## Env knobs

| Var | Default | Meaning |
| --- | --- | --- |
| `COMMS_MACHINE_LABEL` | `hostname -s` | machine half of the `[machine/seat]` prefix |
| `COMMS_ROOT` | `/tmp` | mailbox root (same knob as the mailbox itself) |
| `COMMS_STATE_DIR` | `~/.comms/state` | cursor + skipped-row records |
| `COMMS_SECRETS_FILE` | `~/.secrets/comms.env` | where the webhook line lives |
| `COMMS_MIRROR_INTERVAL` | `5` | `--follow` poll seconds |

## Behavior guarantees

- **Kind-agnostic.** Whatever `kind` a row carries is mirrored verbatim; the
  vocabulary is enforced at write time by `lib/swarm_mailbox.VALID_KINDS`,
  which is being extended on a parallel branch. The mirror never hardcodes it.
- **No reposts.** A per-run cursor (per-seat row counts, valid because seat
  files are append-only single-writer) lives in `COMMS_STATE_DIR`; restarts
  resume where they left off.
- **Batched.** Rows that arrive together go out as one message, chunked under
  Discord's 2000-char content cap.
- **Rate-limit aware, never silently lossy.** 429 honours `Retry-After` with
  a capped retry budget; rows that still fail are written to
  `<runid>.skipped.jsonl` in the state dir and shouted to stderr, then the
  cursor advances (the skipped file is the durable record).

## Deliberately NOT synced

Claims, arming/enrollment, subscriptions, cursors: all machine-local state
stays machine-local. The mirror carries only the conversational rows. Syncing
coordination state across machines would reintroduce the shared-file races the
mailbox design exists to avoid.
