# Discord mirror adapter

One Discord channel as the cross-machine live dashboard. Each machine runs its
own local comms mailbox (see `lib/swarm_mailbox.py`); this adapter tails that
mailbox and posts each row as its OWN Discord author, using the webhook
`username` field, so a channel reads as a real multi-party conversation
instead of a wall of identical bot lines:

    alpha (studio): 📬✅ cursor logic landed, tests green
    beta (macbook): 🚧 port 7778 already bound

(the `name:` above is the Discord message's author, not text in the body --
each line is posted BY that seat, not narrated about it). A seat that
enrolled with identity metadata (see below) renders its author line as prose
a human can read at a glance -- what kind of agent it is and what it is
working on:

    kimi1 · Kimi K3 on agent-os (macbook): 📬✅ hook rot in leg 2

Discord is the merge point AND the dashboard. There is no cross-machine file
sync: two machines never read each other's mailboxes; they both post into the
same channel, and the author line (seat + machine, see Rendering below) keeps
provenance. Command direction (typing in Discord to steer a machine) is
deliberately out of scope here -- durable commands go through the GitHub
board.

## Rendering: three visible verbs

A human watching the channel should see an agent's whole lifecycle without
decoding anything:

| Verb | Source | Trigger | Content |
| --- | --- | --- | --- |
| agent born | mailbox (`mirror.py`) | the ambient "session started in `<dir>`" status row | "🐣 I am awake in `<dir>`" |
| posted to mailbox | mailbox (`mirror.py`) | any other mailbox row | kind emoji + text (see below) |
| heard from mailbox | heartbeat telemetry (`ingest_mirror.py`) | the heartbeat hook actually injects new rows into an agent's context | "👁️ read N row(s) from `<seats>`" |

Every message's Discord **author** (the webhook `username`, not text in the
body) is `<seat> · <model> on <project> (<machine>)` when the seat enrolled
with identity, else `<seat> (<machine>)` -- see Seat identity below. Because
Discord's `username` is one value per POST, rows batched into a single
message always share one author; a run of consecutive same-seat rows batches
together, but a seat change always starts a new message even if it would
still fit under the content cap.

"Posted to mailbox" rows get one leading emoji chosen by event shape:

| Shape | Emoji | Content |
| --- | --- | --- |
| broadcast `finding` | 📬✅ | `<emoji> <text>` |
| broadcast `comment` | 📬💬 | `<emoji> <text>` |
| `reply` | ↩️ | `<emoji> <text>` |
| `claim` | 📌 | `<emoji> <text>` |
| `blocker` | 🚧 | `<emoji> <text>` |
| `status` (non-ambient) | ℹ️ | `<emoji> <text>` |
| unicast (`topic` starts with `@`) | 📨 | `📨 to <seat>: <text>` (overrides the kind emoji -- a direct message is conversation regardless of kind, same rule the convo lane's filter already uses) |
| sendmessage-bridge row (`text` starts with `-> `) | kind emoji | target rendered readably; a bare agent_id target (the exact complaint this feature fixes -- e.g. `-> aecd8555b8a274737: comment`) is shortened to its first 8 hex chars and is NEVER the bare object of the sentence: `📬💬 sent to a subagent (aecd8555): comment` |

An unknown `kind` falls back to ℹ️ (kind-agnostic mirroring, see Behavior
guarantees, is unaffected -- an unrecognized kind still renders, just with
the generic emoji).

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
| `COMMS_MACHINE_LABEL` | `hostname -s` | machine half of the author line, `<seat> (<machine>)` |
| `COMMS_ROOT` | `/tmp` | mailbox root (same knob as the mailbox itself) |
| `COMMS_STATE_DIR` | `~/.comms/state` | cursor + skipped-row records |
| `COMMS_SECRETS_FILE` | `~/.secrets/comms.env` | where the webhook line(s) live |
| `COMMS_MIRROR_INTERVAL` | `5` | `--follow`/`--follow-all` poll seconds |

## Seat identity (optional)

Declare who a seat is at ENROLLMENT -- the one place a seat already announces
itself -- and the mirror joins it to that seat's Discord author line at
format time:

    bin/comms enroll <runid> --agent-id agent-k --seat kimi1 \
        --model "Kimi K3" --project agent-os --area hooks/

| Field | Example | Renders as (in the author line) |
| --- | --- | --- |
| `--model` | `"Kimi K3"` | `kimi1 · Kimi K3 ...` |
| `--project` | `agent-os` | `... on agent-os ...` |
| `--area` | *(not shown in the author line -- see below)* | -- |

`--model`/`--project` are optional free text (display-only prose; identity
never gates routing or delivery -- deliberately NOT a closed vocabulary). The
author line is `<seat> · <model> on <project> (<machine>)`; a seat with no
declared identity renders `<seat> (<machine>)`. (`--area` is still accepted
and stored in the roster for other consumers of `swarm_arm.seat_identities`;
this adapter's author line does not include it, having traded that slot for
the machine label so the SAME line also carries provenance across machines.)
The author line is sanitized against `@everyone`/`@here` and zero-width
characters before it ever reaches Discord, so a seat literally named
`@everyone` cannot render as a mention.

## Ingestion events ("heard from mailbox")

`adapters/discord/ingest_mirror.py` is a SEPARATE small tailer for a
different source file: `$COMMS_STATE_DIR/swarm-heartbeat.log`, the telemetry
the `PostToolUse` heartbeat hook (`adapters/claude-code/swarm-heartbeat.sh`)
appends every beat it runs for an enrolled agent -- one JSON line per beat:

    {"at": <iso8601>, "agent_id": <str>, "runid": <str>, "topic": <str>,
     "rows_inspected": <int>, "delta_emitted": <int>, "short_circuit": <bool>}

A line with `delta_emitted > 0` means the hook actually injected that many
NEW mailbox rows into that agent's context on that beat -- a delivery event.
`ingest_mirror.py` posts ONE Discord message per delivery event (aggregated
per beat, never per row): `👁️ read <N> row(s) from <distinct sender seats>`,
authored as the RECEIVING seat (identity roster if resolvable, same rules as
above). The log carries a count but not which seat(s) posted the delivered
rows, so this module re-derives that by replaying swarm-heartbeat's own
selection (topic/seat filter, own-seat exclusion, the one mailbox parser,
capped the same way) bounded by its own persisted watermark -- see the
module's docstring for the full "why", including the sanity check against
`delta_emitted` (a count is never trusted without a positive control).

Its cursor is a plain byte offset into the heartbeat log
(`discord-mirror-convo/heartbeat-ingest.cursor`, tmp + `os.replace`,
PID-suffixed tmp -- same safety shape as the row mirror's cursor). If the log
rotates or is truncated (offset larger than the file's current size), the
byte offset resets to 0.

**Wire-up:** `mirror.py --follow-all --lane convo` also tails this module
once per pass, in the SAME process -- no second launchd job. The heartbeat
log is fleet-wide, not scoped to one run, so this lives in `--follow-all`'s
whole-fleet pass; `mirror.py --follow <runid> --lane convo` mirrors that
run's mailbox rows only and does not tail ingestion (see `follow_all`'s
docstring for this scope choice). It also has its own standalone CLI:

    python3 adapters/discord/ingest_mirror.py --once
    python3 adapters/discord/ingest_mirror.py --follow [--interval N]

Same launchd safety as the row mirror: a missing `DISCORD_COMMS_CONVO_WEBHOOK_URL`
warns once and backs off 60s under `--follow` instead of crash-looping; a
per-pass exception is caught, named on one stderr line, and does not kill
the loop.

## Behavior guarantees

- **Kind-agnostic.** Whatever `kind` a row carries is mirrored verbatim; the
  vocabulary is enforced at write time by `lib/swarm_mailbox.VALID_KINDS`,
  which is being extended on a parallel branch. The mirror never hardcodes it.
- **No reposts, upgrade-safe.** A per-run cursor (per-seat row counts, valid
  because seat files are append-only single-writer) lives in
  `COMMS_STATE_DIR`; restarts resume where they left off. The cursor FORMAT
  is unchanged by the emoji/authorship rendering upgrade -- a cursor file
  written before this feature landed is honored exactly as before, so
  deploying it never re-posts history.
- **Batched per author.** Rows that arrive together from the SAME seat go out
  as one message, chunked under Discord's 2000-char content cap; a seat
  change always starts a new message (Discord's `username` is one value per
  POST -- see Rendering above).
- **Rate-limit aware, never silently lossy.** 429 honours `Retry-After` with
  a capped retry budget; rows that still fail are written to
  `<runid>.skipped.jsonl` in the state dir and shouted to stderr, then the
  cursor advances (the skipped file is the durable record).

## Deliberately NOT synced

Claims, arming/enrollment, subscriptions, cursors: all machine-local state
stays machine-local. The mirror carries only the conversational rows. Syncing
coordination state across machines would reintroduce the shared-file races the
mailbox design exists to avoid.
