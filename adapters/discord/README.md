# Discord mirror adapter

One Discord channel as the cross-machine live dashboard. Each machine runs its
own local comms mailbox (see `lib/swarm_mailbox.py`); this adapter tails that
mailbox and posts every row as a one-liner to a single channel via webhook:

    [studio/alpha] finding: cursor logic landed, tests green
    [macbook/beta] blocker: port 7778 already bound

A seat that enrolled with identity metadata (see below) renders as prose a
human can read at a glance -- what kind of agent it is and what it is working
on:

    [macbook] Kimi K3 on agent-os (hooks/) | seat kimi1 | finding: hook rot in leg 2

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

## Lanes: a second channel for agent-to-agent chatter

By default (`--lane` omitted, or `--lane all`) the mirror behaves exactly as
above -- every row, one channel. Pass `--lane convo` to mirror agent-to-agent
conversation to a SECOND webhook/channel instead. The predicate a row must
match, exactly:

    topic.startswith("@")          # a unicast -- a message to ONE seat,
                                    # of ANY kind (finding/status/blocker/
                                    # claim included: a direct message is
                                    # conversation regardless of what kind
                                    # carries it)
    or kind in ("comment", "reply")  # a broadcast conversational row

so the convo lane is not "findings vs. chatter" -- a `--to` unicast lands in
convo even if it's kind `finding`. `lib/swarm_mailbox.CONVO_KINDS` is the
kind-half of this predicate, defined next to `VALID_KINDS`:

    python3 adapters/discord/mirror.py --once <runid> --lane convo
    python3 adapters/discord/mirror.py --follow <runid> --lane convo

A row the convo lane skips still advances that lane's cursor -- it is never
re-scanned on the next pass, it is just never posted. The two lanes never
share a cursor, a skipped-rows log, or a secret:

| Lane | Secret var | State dir |
| --- | --- | --- |
| `all` (default) | `DISCORD_COMMS_WEBHOOK_URL` | `discord-mirror/` |
| `convo` | `DISCORD_COMMS_CONVO_WEBHOOK_URL` | `discord-mirror-convo/` |

Set up the convo lane's secret the same way as the default lane's (Setup
step 2 above), just with the `_CONVO_` var name and, in Discord, a second
webhook pointed at a second channel.

### Mirroring every run at once

    python3 adapters/discord/mirror.py --follow-all [--interval N] [--lane convo]

Each pass globs the mailbox root for every `comms-*` run directory and mirrors
each one, so a new run that gets armed while the process is running is picked
up without a restart.

### launchd safety

`--follow` and `--follow-all` are meant to run under a launchd `KeepAlive`
job, which restarts anything that exits nonzero. A job that exits every time
it polls a not-yet-configured secret would crash-loop. So in those two modes
only, a missing secret does NOT exit: one stderr line, then a 60s retry.
`--once` is unaffected -- it still exits 2 and names the exact drop-in line,
because a one-shot invocation (a human, or a script checking the result)
needs the loud failure. A per-run exception (a bad row, an unwritable state
dir) is likewise caught, named on one stderr line with the runid and
exception class, and does not stop the loop or the rest of the pass.

### Concurrency: one poller per (run, lane)

**Never run two mirror processes against the same runid AND the same lane at
once** -- neither `--follow <runid>` twice, nor `--follow <runid>` alongside
a `--follow-all` whose default `--lane all` also covers that runid. Two
pollers on one (run, lane) both read the same cursor, both post, and BOTH
advance it -- the result is double-posted rows in the channel, not a race
that merely errors. (The cursor's tmp file is PID-suffixed so the two
processes' writes cannot collide on the SAME tmp path, but that only removes
one failure mode; it does not make concurrent pollers on one (run, lane)
safe.) One lane's `all` job and another's `convo` job on the SAME runid are
fine -- they are different (run, lane) pairs with separate state dirs.

## Env knobs

| Var | Default | Meaning |
| --- | --- | --- |
| `COMMS_MACHINE_LABEL` | `hostname -s` | machine half of the `[machine/seat]` prefix |
| `COMMS_ROOT` | `/tmp` | mailbox root (same knob as the mailbox itself) |
| `COMMS_STATE_DIR` | `~/.comms/state` | cursor + skipped-row records |
| `COMMS_SECRETS_FILE` | `~/.secrets/comms.env` | where the webhook line(s) live |
| `COMMS_MIRROR_INTERVAL` | `5` | `--follow`/`--follow-all` poll seconds |

## Seat identity (optional)

Declare who a seat is at ENROLLMENT -- the one place a seat already announces
itself -- and the mirror joins it to every row by seat name at format time:

    bin/comms enroll <runid> --agent-id agent-k --seat kimi1 \
        --model "Kimi K3" --project agent-os --area hooks/

| Field | Example | Renders as |
| --- | --- | --- |
| `--model` | `"Kimi K3"` | the agent kind, verbatim |
| `--project` | `agent-os` | `on agent-os` |
| `--area` | `hooks/` | `(hooks/)` |

All three are optional free text (display-only prose; identity never gates
routing or delivery -- deliberately NOT a closed vocabulary). Any subset
renders; absent parts drop out of the line. A seat with no identity renders
in the `[machine/seat] kind: text` format, byte-identical to before, so
existing rows and enrollments are unaffected.

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
