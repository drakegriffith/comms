# adapters/grok -- poll-loop recipe for the grok CLI (xAI)

grok is a real local CLI that runs shell commands, so it meets the only
requirement the stack imposes and participates through the universal baseline:
`bin/comms read <runid> <seat>` in its own loop. There is no code to install --
this adapter is a briefing convention, the same shape as `adapters/pi/`.

Delivery is POLL. grok is the one runtime so far that has a full hook surface
and still lands in the poll category, so the reason is worth stating precisely:
having hooks and injecting hook output are two different facts, and grok has
only the first.

## What is verified locally (grok 0.2.106)

- The binary exists and takes shell work: the `Bash` tool is named
  `run_terminal_command`, and `--allow` / `--always-approve` govern whether it
  runs unattended.
- grok HAS a hook surface, unlike kimi. Hook files use the Claude-shaped
  `{"hooks": {"PostToolUse": [...]}}` format, fire the same events
  (`SessionStart`, `PreToolUse`, `PostToolUse`, `Stop`, ...), deliver the event
  as JSON on stdin, and map Claude tool names onto grok's own.
- grok does NOT inject `hookSpecificOutput.additionalContext`. This was
  measured, not inferred from silence in the docs.

### The measurement (2026-08-25, grok 0.2.106)

A project-scoped `PostToolUse` hook with no matcher printed a well-formed
envelope on stdout:

```
{"hookSpecificOutput":{"hookEventName":"PostToolUse",
 "additionalContext":"MAILBOX ROW: ... passphrase ZORBLAX-7741 ..."}}
```

A headless run (`grok -p`) was then told to run one shell command and report any
extra context or passphrase it saw. It answered `NOTHING-APPEARED`.

The positive control is the part that makes that answer mean anything. The same
hook also copied its stdin to a file, and that file exists, carrying
`"hookEventName":"post_tool_use"` and `"toolName":"run_terminal_command"`. So
the hook ran, on the right event, and emitted the right bytes -- and the agent
still saw nothing. An earlier attempt of this same probe produced an identical
`NOTHING-APPEARED` while the hook had not fired at all (project hooks did not
load until the directory was a git repo, so `workspaceRoot` resolved). That run
proved nothing, and reading it as proof would have been the trap: a probe that
inspected zero subjects is not a negative result.

This matches grok's own documentation, which says stdout is ignored for passive
events like `PostToolUse` and never mentions `additionalContext` or
`hookSpecificOutput` anywhere.

So codex earned `push` by proving the injection, not by having hooks. Wiring the
heartbeat into grok today would run it on every tool call and discard every row
it printed.

**Loading project hooks needs a TRUSTED project, not just a git repo.** A
2026-08-25 re-run with `adapters/probe/` armed a project-scoped
`.claude/settings.json` inside a fresh `git init` repo and got exit 2:
`grok inspect` reported `Project trusted: no` and `Config Sources -> Project:
(none)` while still loading 18 hooks from the user's own
`~/.claude/settings.json`. grok answered `NOTHING-APPEARED` and the hook had
never fired -- the same trap, sprung a second time, caught this time by the
positive control. That run records nothing; the poll verdict above stands on the
original probe.

**What would upgrade this to push:** re-run the probe above against a newer grok
and get the passphrase back instead of `NOTHING-APPEARED`, with the stdin-copy
file present as the positive control. `adapters/probe/` is that probe, runnable:
`bash adapters/probe/arm-probe.sh --config <hook config> --format wrapped`, then
`bash adapters/probe/probe-verdict.sh <probe dir> --expect-event post_tool_use
--expect-tool run_terminal_command`. Trust the project first, or you will get
exit 2 again. If that flips, grok needs no new heartbeat
-- it reuses `adapters/claude-code/swarm-heartbeat.sh` exactly as
`adapters/codex/` does, and this file gains a one-screen `install.sh`. Audit
with the delivery oracle (`swarm-heartbeat.log` in the state dir) plus the
agent's transcript, never the agent's self-report.

## The hazard: grok already reads ~/.claude/settings.json

grok scans Claude and Cursor hook sources BY DEFAULT, including
`~/.claude/settings.json`. This is not hypothetical: on the machine where this
adapter was written, `grok inspect` reported 19 loaded hooks, 17 of them tagged
`user [claude]`. So on any machine where `adapters/claude-code/install.sh` has
run, grok is already loading the comms heartbeat -- firing it on every tool call
while discarding the rows it prints. The heartbeat advances its read cursor
after emitting, so rows can be marked delivered to a reader that never saw them.

Two details make this less likely to bite than it reads, and both are worth
knowing before debugging a quiet mailbox:

- A grok session that never runs an enroll command naming an armed run is a
  bystander: it emits zero rows and zero telemetry and exits early. The hazard
  needs an enrolled seat, not merely an installed hook.
- The heartbeat keys its cursor on `agent_id`, falling back to the payload's
  `session_id`. grok's captured payload spells it `sessionId` in camelCase,
  which that exact-key lookup misses, so grok sessions collapse onto the shared
  `unknown` cursor key rather than taking a real seat's cursor.

If a grok session on this machine is enrolled in a run, close the hole rather
than reasoning about it: set `[compat.claude] hooks = false` in
`~/.grok/config.toml` so grok stops scanning the Claude hook source, and let
the poll loop below be the only delivery path.

### Compat opt-out status (this machine, 2026-08-25, grok 0.2.106)

The opt-out is now IN PLACE on this machine. `~/.grok/config.toml` carries:

```toml
[compat.claude]
hooks = false
```

Verified with `grok inspect` run from a scratch cwd after the edit:

```
Harness Compatibility
└ claude
  └ hooks      OFF  (config)
```

and all 17 hooks tagged `user [claude]` in the `Hooks (18)` block show
`[disabled]` (the 18th entry, `plugin: codex`, is unrelated -- it is the codex
plugin's own hook, not sourced from `~/.claude/settings.json`, and is still
active). Before this edit, `grok inspect` reported those same 17 hooks loaded
and enabled, sourced from `~/.claude/settings.json` (PR #45 / issue #28's
comment on issue #31, 2026-08-25). This is a per-machine setting, not a repo
file -- a fresh machine still needs this write before a grok seat is enrolled
in any run, and `grok inspect` is how to confirm it took.

## The recipe

1. **Enroll on line one of the brief.** The first command grok runs names the
   run id and declares its subscription; enrollment is write-once, so it must
   happen before any other `comms` command naming that run.
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

Replace `RUNID`, `SEAT`, `TOPIC`, and the repo path, then paste into the grok
agent's prompt verbatim:

```
## Mailbox protocol (comms)
COMMS=$HOME/code/comms/bin/comms

Run this FIRST, before any other comms command:
  $COMMS enroll RUNID --agent-id SEAT-grok --topics TOPIC --seat SEAT

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

For an unattended seat, launch grok so the poll commands do not stop for
approval:

```
grok --allow "Bash(*/bin/comms *)" "<brief>"
```

## Notes

- The resume-driver route is available but not taken. grok has `-r/--resume
  [SESSION_ID]`, `-p/--single`, and `--output-format plain|json`, so the kimi
  pattern (an outside loop delivering rows as resume turns) would work. It is
  not the default here because it buys nothing a poll loop lacks while adding a
  second process to supervise, and because kimi needed it only for having no
  in-session way to check the mailbox at all. grok can just run the command.
- `kind` is a closed vocabulary: `finding|claim|blocker|comment|reply|status`.
  An unlisted kind fails loudly -- relabel, never retry blind.
- The read cursor is per `(runid, seat, filter)` and lives in
  `COMMS_STATE_DIR` (default `~/.comms/state`), so repeated reads never replay
  old rows and a restarted grok session resumes where it left off. It advances
  after the rows are printed, over exactly the rows THAT filter selected: a
  `--topic X` read never marks another topic's rows delivered, and the price is
  that a row in topic X is handed once to the plain read and once to the
  `--topic X` read. Pick one form and keep the brief on it.
- `bin/comms read <runid> <seat> --replay` prints the whole board and neither
  reads nor moves any cursor -- the escape hatch for auditing a run, and what a
  caller with a delivery cursor of its own must use (see `adapters/kimi/` and
  `adapters/remote/`).
- grok sets `GROK_SESSION_ID` on every hook process and accepts `--session-id`
  for new conversations, so a stable seat identity is available if this adapter
  ever grows push delivery.
- Delivery auditing: read the telemetry/mailbox files, not the seat's
  self-report (see "The delivery oracle" in the top-level README).
- **The `--allow` pattern above can silently never match.** `Bash(...)` allow
  rules are checked against the WHOLE command string as grok received it, with
  no special treatment for a leading environment assignment (per grok's own
  docs, `22-permissions-and-safety.md`). A brief that tells the agent to set
  `COMMS=$HOME/code/comms/bin/comms` and then run `$COMMS enroll ...` produces
  a tool call whose literal text starts with `COMMS=`, not the binary path, so
  `--allow "Bash(*/bin/comms *)"` never matches it, the call sits waiting on a
  prompt headless mode cannot answer, and the turn is silently cancelled with
  no enrollment, no error, and a self-report that still sounds like it worked.
  The recipe that was actually observed to work (2026-08-25 verification,
  below) skips the shell variable: the brief names the full literal path in
  each command line, one command per tool call, and the `--allow` rule matches
  that literal path as a bare prefix, e.g.
  `--allow "Bash(/full/path/to/bin/comms)"`.

## Live poll-seat verification (receiver-verified, 2026-08-25, grok 0.2.106)

Issue #31's acceptance criteria: a grok seat completes an enroll/read/reply
round trip, and receipt is proven from MAILBOX STATE, not the agent's
self-report. Run against the real `bin/comms` defaults (`~/.comms/state`), a
scratch runid keeping it namespaced. Ran after the compat opt-out above was
confirmed in place.

Setup:

```
runid=grok-verify-20260825   seat=g1   agent-id=g1-grok   topic=verify
passphrase=COMMS-GROK-VERIFY-1F70CF3C
```

Commands, in order:

```
bin/comms arm grok-verify-20260825 --topic verify
bin/comms post grok-verify-20260825 coordinator claim \
  "MAILBOX ROW for g1: reply with this exact passphrase to prove you read it: COMMS-GROK-VERIFY-1F70CF3C" \
  --to g1
grok --allow "Bash(/Users/drakegriffith8/code/comms/bin/comms)" \
  --output-format plain --max-turns 12 -p "<brief: enroll as g1-grok/g1 on
  topic verify, read the mailbox, find the passphrase in the row addressed to
  g1, then post a reply to coordinator quoting it, one literal command per
  tool call, no shell variables>"
```

grok's self-report (not trusted as the verdict, recorded for comparison only):
"Passphrase: COMMS-GROK-VERIFY-1F70CF3C. Step 3 succeeded (exit 0). Posted a
reply from seat g1 to coordinator: `received: COMMS-GROK-VERIFY-1F70CF3C`."

The oracle -- mailbox state, read after the run:

```
$ bin/comms status grok-verify-20260825
{"runid": "grok-verify-20260825", "armed": true, ...,
 "participants": ["g1-grok"]}

$ bin/comms read grok-verify-20260825 g1 --replay
{"seat": "coordinator", "at": "2026-08-25T21:27:37...Z", "kind": "claim",
 "text": "MAILBOX ROW for g1: reply with this exact passphrase to prove you
 read it: COMMS-GROK-VERIFY-1F70CF3C", "topic": "@g1", "to": "g1"}

$ bin/comms read grok-verify-20260825 coordinator --replay
{"seat": "g1", "at": "2026-08-25T21:32:33...Z", "kind": "reply",
 "text": "received: COMMS-GROK-VERIFY-1F70CF3C", "topic": "@coordinator",
 "to": "coordinator"}
```

`status` shows `g1-grok` enrolled (the enroll ran). `read ... coordinator`
shows a `reply` row from seat `g1`, addressed `@coordinator`, quoting the
planted passphrase back verbatim, timestamped after the planted row -- receipt
proven from the mailbox grok's own commands wrote, not from grok's answer
text. Round trip closed: enroll, read, reply, all confirmed from state.
