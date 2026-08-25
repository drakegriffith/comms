# adapters/pi -- poll-loop recipe for pi (badlogic) and any hook-less runtime

pi has no PostToolUse-style hook surface, so nothing can push mailbox rows into
a running pi agent. It does not need one: polling is the universal baseline.
The whole adapter is a briefing convention -- there is no code to install.

The same recipe covers any runtime that can run a shell command in its loop:
local models (Qwen via pi), bare scripts, cron jobs.

## The recipe

1. **Enroll on line one of the brief.** The first command the agent runs names
   the run id and declares its subscription; enrollment is write-once, so it
   must happen before any other `comms` command naming that run.
2. **Read after every work step.** `bin/comms read <runid> <seat>` prints only
   the rows this seat has not been handed before -- every sibling's, including
   its unicast channel `@<seat>`. (Add `--subs` to narrow it to the seat's
   subscribed slice; that slice keeps a cursor of its own.) Empty output means
   nothing new -- carry on.
3. **Reply before finishing.** A row addressed `@<seat>` is a peer commenting
   into this agent's live run; answer it with `--to <peer>` before moving on.
4. **Post findings as they land**, not at the end. Mid-run visibility is the
   point of the mailbox.

## Brief block a dispatcher can paste

Replace `RUNID`, `SEAT`, `TOPIC`, and the repo path, then paste into the pi
agent's brief verbatim:

```
## Mailbox protocol (comms)
COMMS=$HOME/code/comms/bin/comms

Run this FIRST, before any other comms command:
  $COMMS enroll RUNID --agent-id SEAT-pi --topics TOPIC --seat SEAT

After EVERY work step (a file edited, a test run, a conclusion reached):
  $COMMS read RUNID SEAT
Empty output = nothing new. A row on topic @SEAT is a peer commenting on
your live work: answer it BEFORE your next work step:
  $COMMS post RUNID SEAT claim "<your answer>" --to <their-seat>

When you land a result worth a peer's attention:
  $COMMS post RUNID SEAT finding "<one-line result>" --topic TOPIC
If you are blocked:
  $COMMS post RUNID SEAT blocker "<what and who owns it>" --topic TOPIC
```

The dispatcher arms the run once, before any seat starts:

```
bin/comms arm RUNID --topic TOPIC
```

## Notes

- `kind` is a closed vocabulary: `finding|claim|blocker|comment|reply|status`
  (extended per issue #1). An unlisted kind fails loudly -- relabel, never
  retry blind.
- The read cursor is per `(runid, seat, filter)` and lives in
  `COMMS_STATE_DIR` (default `~/.comms/state`), so repeated reads never replay
  old rows and a restarted agent resumes where it left off. It advances after
  the rows are printed, over exactly the rows THAT filter selected: a
  `--topic X` read never marks another topic's rows delivered, and the price
  is that a row in topic X is handed once to the plain read and once to the
  `--topic X` read. Pick one form and keep the brief on it.
- `bin/comms read <runid> <seat> --replay` prints the whole board and neither
  reads nor moves any cursor -- the escape hatch for auditing a run, and what
  a caller with a delivery cursor of its own must use (see `adapters/kimi/`
  and `adapters/remote/`).
- Poll frequency is the brief's business, not the stack's. "After every work
  step" is the proven cadence: the 2026-08-21 two-seat demo ran 6 mid-run
  challenge/response exchanges between polling participants on exactly this
  loop.
- Delivery auditing: read the telemetry/mailbox files, not the seat's
  self-report (see "The delivery oracle" in the top-level README).
