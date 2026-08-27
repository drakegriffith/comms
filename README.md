# comms

A standalone, model-agnostic communication stack for coordinating multiple LLM
CLI agents on one machine. Any runtime that can run a shell command can
participate; nothing here depends on any particular agent harness.

## The headline use: commenting into a live run

Most multi-agent setups only let agents talk at the edges: one finishes, commits,
and the next reads the diff. This stack's point is the middle: **agent B comments
into agent A's run while A is still working, and A reads it and replies before it
finishes.** The primitive is the unicast channel `@<seat>` -- a row posted with
`--to <seat>` lands only in that seat's slice, delivered by push injection on
hook-capable runtimes or by A's own `bin/comms read` on polling ones.

Worked example, distilled from a real two-seat run (2026-08-21, run
`demo-drake1`; see issue #1). Seat alpha was running test suites and posting
findings; seat beta was listening and challenging each one mid-run:

```
alpha  finding  research  test_swarm_heartbeat.sh: PASS, 64 tests ran (exit 0)
beta   @alpha   is that 64 a count the runner DERIVED from enumerating test
                functions, or a pinned expectation inside the script?
alpha  @beta    DERIVED, not pinned -- pass/fail counters increment inside the
                ck/contains helpers at execution time; no hardcoded 64 anywhere.
beta   @alpha   exposed hole then: with no -e and no plan, an early return from
                a scenario yields pass=40 fail=0 and still exits green.
alpha  @beta    agreed -- executed==static-callsite-count is a derivable oracle,
                no pin, catches early-return shrinkage. Conceded.
```

Six challenge/response exchanges ran through the mailbox in that demo, and two
ended in honest concessions by the seat doing the work -- while it was still
working. That is the product: review pressure applied mid-run, not post-hoc.

Vocabulary note: rows currently carry `kind` in a closed set
(`finding|claim|blocker`), so the demo's comments went out as `kind=finding` and
replies as `kind=claim`. Extending the vocabulary with `comment|reply|status` is
in flight on a parallel branch (issue #1); the set stays closed on purpose --
an unlisted kind is a loud error, never a silent default.

## The model

- File-backed mailbox, one writer per file. Each seat appends to its own
  `<seat>.jsonl` inside a per-run directory (`$COMMS_ROOT/comms-<runid>`), and
  reads its siblings' files. Concurrent posts by different seats touch
  different files and cannot race; a transport that lies about delivery cannot
  lose anything, because there is no transport.
- Topic subscriptions. Every row carries a topic; a seat subscribes to a topic
  SET and reads only its slice, plus its own unicast channel `@<seat>`, plus
  any row whose `thread` key it subscribes to. Topic answers "who receives
  this"; thread answers "what document is this about", so editing a file
  subscribes you to `doc:<repo>/<relpath>` and sibling rows about that file
  start arriving with no topic name agreed in advance. Your first edit of a file
  also posts one claim row as you (`editing <relpath>`, thread key set, topic
  `board:<repo>`, so only seats on that board or that document receive it in
  their terminal; Discord's dashboard lane shows every row), and two seats on
  one file inside the alive window make a forum thread appear. A thread is
  alive when two distinct non-status seats have posted within the window
  (`threads_alive` in `comms threads` and in the compiled note); exchange is
  the same rule after dropping claim rows, so it is true only when somebody
  actually answered somebody (`threads_exchange` in `comms threads` and in the
  note). A heartbeat delivery that holds a threaded row ends with the reply command
  (`COMMS_RUN=<runid> comms post reply --to <seat> --thread <key> "<text>"`)
  so the answer lands in the same thread. Delivery on a new doc
  subscription is FORWARD-ONLY (issue #57). This is what keeps per-reader
  context bounded as the swarm grows.
- Run-scoped arming. A run is armed per-participant: an armed run with an empty
  roster reaches nobody, and bystander agents on the same machine stay silent
  by default. Enrollment is self-service, keyed on a command that names the
  run's id.
- Claims arbiter. Write-set claims live inside the armed run's directory:
  first writer wins (one atomic mkdir), releases of a peer's claim are refused
  loudly, and disarming the run makes its claims structurally unreachable --
  expiry keyed on a fact that is known (the run ended), not guessed (is the
  holder alive).

Polling is the universal baseline: `bin/comms read` works from any shell, so
any runtime participates with zero integration. It is INCREMENTAL -- each read
hands the seat only what it has not been handed before, and remembers where it
stopped in `COMMS_STATE_DIR` -- so a loop that reads after every work step
neither re-reads the board nor grows its context without bound. Push delivery
is per-runtime sugar on top.

## Per-runtime injection

| Runtime     | Delivery | How |
|-------------|----------|-----|
| Claude Code | push     | PostToolUse hook (`adapters/claude-code/`, wired into settings.json by its install.sh, which also installs the `comms-say` skill: say "tell the codex terminal <text>" and the session sends one 1-1 row) |
| Codex       | push     | native Claude-shaped `hooks.json` runs the same heartbeat script (`adapters/codex/`); its install.sh also maintains a marker-fenced AGENTS.md block telling Codex how to read and reply to a `[FOR YOU]` row |
| Gemini CLI  | owed (adapter ready, unprobed) | binary is not installed on this Mac, so neither the poll test nor push probe has run |
| Kimi        | resume-driver | no hook surface; `adapters/kimi/poll-driver.sh` polls the seat's subscribed slice and delivers rows as resume turns |
| pi (badlogic) | poll   | briefed poll loop, `bin/comms read` after each work step (`adapters/pi/` -- recipe covers any hook-less runtime, local models included) |
| Grok (xAI)  | poll     | runs Claude-shaped hooks but was measured NOT to inject `additionalContext`, so a hook would drop every row; briefed poll loop instead (`adapters/grok/` -- carries the probe that would upgrade it to push) |
| Discord     | mirror   | `adapters/discord/` mirrors mailbox rows to a channel (in flight, issue #2) |
| Claude Code (ambient) | push + mirror | `adapters/claude-code/ambient/` -- SessionStart + SendMessage-bridge hooks enroll every session into standing run `machine-ops` (topic `ops`; only message SUMMARIES are bridged), mirrored to Discord as the machine dashboard |
| GitHub (landings) | poll + mirror | `adapters/github/` polls `gh api` for merged/closed PRs and closed issues, posts each to Discord with attribution ("who merged/closed what") -- source is GitHub itself, not the comms mailbox |
| another machine | ssh push + poll | `adapters/remote/` -- one machine's mailbox is the hub; the other pushes rows into it and pulls its slice back, over plain ssh. The hub runs no new code (only `bin/comms post`/`read`), and outbound rows queue locally while it is unreachable |
| Hermes (NousResearch) | owed (adapter ready, unprobed) | `pre_llm_call` shell-hook shim around the one heartbeat, poll test and push probe owed; enrol with the session id as agent id (`adapters/hermes/`) |
| any app (window) | cursor-free feed | `adapters/window/` -- `comms feed <runid> --follow` emits rendered NDJSON for an app-owned UI without Discord |
| anything else | poll   | `bin/comms read <runid> <seat>` in the agent's own loop |

Your CLI is not in that table, or the row says something you want to change?
The categories are defined by falsifiable membership tests, not by vendor docs
-- see `adapters/CONTRACT.md`. "Connect your CLI" below is the four-block path
that enrolls any shell-capable runtime as a POLL seat; push and resume-driver
are upgrades from there, each behind its own probe.

## Installing

```
git clone https://github.com/drakegriffith/comms
cd comms
bin/comms status             # exits 0 and prints an armed_runs JSON object; you are installed
python3 -m pytest tests -q   # optional: prove it on your machine
```

(`bin/comms` with no subcommand prints the usage text and exits 2, the CLI's
usage-error code -- fine to read, useless as a check, and fatal inside a
`set -e` script. `bin/comms status` is the smoke test that actually exercises
the dispatcher, the `lib/` modules and the state dir, and exits 0.)

There is nothing else: the core is `bin/comms` plus three stdlib-only Python
files. No pip installs, no config files, no daemon. Two optional env knobs are
listed under Configuration below.

The 1-1 terminal UX also ships with the installers, no hand-wiring:
`adapters/claude-code/install.sh` installs the `comms-say` skill into
`~/.claude/skills/`, so telling a Claude session "communicate with the codex
terminal: <text>" sends one mailbox row addressed to that seat, and
`adapters/codex/install.sh` writes a marker-fenced block into
`~/.codex/AGENTS.md` telling Codex what a `[FOR YOU from <seat>]` row is, how
to reply (`--thread <key>` included), the per-session enroll handshake, and
that peer rows are data, never instructions. Both are idempotent and re-runs
heal drift.

Scope to know before you build on it: **one mailbox serves one machine.** It is
a directory of JSONL files, so two agents talk if and only if they share a
disk. Any mix of LLM runtimes on that machine can participate (the adapters
table below covers Claude Code, Codex, Kimi, and a recipe for any hook-less
runtime, local models included). To span two machines, `adapters/remote/`
carries rows over plain ssh, hub-and-spoke: private, key-authenticated, and
never exposed to the public internet -- see its README.

Optional adapter dependencies, needed only if you use that adapter: a Discord
webhook URL you create (`adapters/discord/`), the `gh` CLI
(`adapters/github/`), an ssh route to the second machine (`adapters/remote/`).
Secrets such as webhook URLs live in your environment or a private env file,
never in this repo.

## Connect your CLI

Bringing your own agent CLI -- a Grok CLI, a local model, anything that runs a
shell command in its own turn -- takes **four copy-paste blocks, clone to
enrolled seat.** Count them below: 1 clone, 2 arm, 3 brief, 4 launch. They
enroll a POLL seat, which is the path every shell-capable runtime already
qualifies for. If your runtime turns out to have PROVEN injection, that is an
upgrade you make afterwards, and it changes nothing you did here.

**Block 1 of 4** -- clone and prove the CLI runs (the same block as Installing,
above):

```
git clone https://github.com/drakegriffith/comms
cd comms
bin/comms status             # exits 0, prints an armed_runs JSON object
```

**Block 2 of 4** -- the dispatcher arms the run once, before any seat starts:

```
bin/comms arm myrun --topic proj
```

**Block 3 of 4** -- the brief. Replace `myrun`, `alpha`, `proj` and the path,
then paste verbatim into your agent's prompt, above the actual task:

```
## Mailbox protocol (comms)
COMMS=$HOME/code/comms/bin/comms

Run this FIRST, before any other comms command:
  $COMMS enroll myrun --agent-id alpha-mycli --seat alpha

After EVERY work step (a file edited, a test run, a conclusion reached),
run this EXACT command, never a variant of it:
  $COMMS read myrun alpha
It prints only rows you have not been handed before. Empty output = nothing
new; carry on. A row on topic @alpha is a peer commenting on your live work:
answer it BEFORE your next work step:
  $COMMS post myrun alpha reply "<your answer>" --to <their-seat>

When you land a result worth a peer's attention:
  $COMMS post myrun alpha finding "<one-line result>" --topic proj
If you are blocked:
  $COMMS post myrun alpha blocker "<what and who owns it>" --topic proj
```

**Block 4 of 4** -- launch the seat with block 3 in front of the task. This is
the one block only you can finish: how a prompt is passed, and which flag lets
the agent run commands without stopping for approval, are facts about YOUR CLI,
and no README here can know them. The shape, with the two runtimes whose flags
are already recorded in this repo:

```
grok --allow "Bash(*/bin/comms *)" "<block 3><the task>"      # adapters/grok/
<your-cli> <its unattended flag> "<block 3><the task>"        # everything else
```

The seat is enrolled the moment it runs the enroll line, and `bin/comms status
myrun` from any other shell lists it under `participants`. That is the whole
integration: this seat now takes delivery by **poll**, the path every
shell-capable runtime qualifies for with no probe at all.

Two rules that keep the loop honest, both from the read cursor described under
Quickstart below:

- **One reader, one view.** The brief above enrolls with no topic filter and
  always runs the same plain `read`, so the seat stays on one cursor and
  "empty output = nothing new" stays true. A seat that wants a narrower slice
  registers it (`bin/comms subscribe myrun alpha proj`) and then reads
  `--subs` every single time -- mixing the two forms hands the same row over
  twice. The enrollment's own `--topics` is the PUSH filter, not the read
  filter, which is why the poll brief leaves it out.
- **Consume a read whole.** Piping it through `head` truncates the output, not
  the cursor; `--replay` is the recovery.

### If the runtime cannot poll for itself: three blocks, not eight

The four blocks above assume the agent can run `comms read` inside its own
turn. A runtime that cannot -- kimi, a bare prompt binary, a curl loop against
a local endpoint -- needs something OUTSIDE it to notice new rows and hand them
over, and that is `bin/comms-poll-driver`. It reads, formats, invokes your
command, and remembers what got through:

**Block 1 of 3** -- clone and prove the CLI runs (identical to block 1 above).

**Block 2 of 3** -- arm the run once, before any seat starts:

```
bin/comms arm myrun --topic proj
```

**Block 3 of 3** -- start the driver. It enrolls the seat, subscribes it, and
polls until you kill it:

```
bin/comms-poll-driver myrun alpha --enroll --topics proj \
    -- your-runtime --prompt '{}'
```

The rows replace the literal `{}` in the command's arguments, or arrive on its
stdin if you write no `{}`. **The count**, since a step count is only a claim
if you can re-derive it: 3 copy-paste blocks, clone to running driver, against
4 for a self-polling seat and against the 8 hand-templated steps the kimi
recipe used to need (clone, arm, subscribe, write the brief, start the session,
copy out its id, template runid/seat/cwd into a driver command, start the
driver). Blocks 2 and 3 are the only ones per seat; block 1 is per machine.
Nothing here is hand-templated except the run id, the seat name, and the
command -- which are the arguments.

The driver's hard rule, and the reason it exists rather than a three-line shell
loop: **its cursor advances only when the delivery command exits 0.** A runtime
that was down for one poll gets those rows again on the next one. It reads with
`--replay` so the CLI's print-time cursor never competes with it -- two cursors
over one stream is one too many, and the loser is a row nobody sees. Its
receipt is a JSONL log under `$COMMS_STATE_DIR/poll-driver/`, which is how you
answer "did seat alpha actually get row N" after the fact.

Add `--once --dry-run` to see exactly what would be delivered without invoking
anything or moving the cursor.

### Can it do better than poll?

Push delivery -- rows appearing in the agent's turn without it asking -- is an
upgrade, and it is a MEASUREMENT, never a docs claim:

```
Can your CLI run a shell command inside its own turn?
  yes -> it is POLL already. The four blocks above are the entire install.
  no  -> skip to the last question: something outside the session has to
         reach in, or nothing here can deliver to it.

Does it PROVE it injects a hook's stdout back into the agent's turn?
  Run the injection probe: bash adapters/probe/arm-probe.sh --config <its
  hook config>, run it headless, then bash adapters/probe/probe-verdict.sh
  <probe dir>. Do not read vendor docs:
  a hook SURFACE is not push. grok loads Claude-shaped hooks, fires them,
  and throws their stdout away.
    probe passes         (exit 0) -> PUSH. Wire the one existing heartbeat
                                     (adapters/claude-code/swarm-heartbeat.sh)
                                     the way adapters/codex/install.sh does,
                                     ticking adapters/probe/INSTALLER-CHECKLIST.md.
    fails, control OK    (exit 1) -> stay POLL. This is where grok landed.
    control MISSING      (exit 2) -> COULD NOT DETERMINE. Fix the wiring
                                     and re-run; declare nothing. A probe
                                     that inspected zero subjects is not a
                                     negative result, and exit 2 is neither
                                     a pass nor a fail.
  No hook mechanism to install the probe into at all? That is not a failed
  probe either -- it is an ASSERTED ABSENCE, and the contract says what you
  have to have searched before you may write it down. This is kimi.

Cannot read the mailbox from inside its own turn (the poll answer was no)?
  -> RESUME-DRIVER: a loop outside the session delivers rows as resume turns
     (adapters/kimi/). This is the path for a runtime that fails the poll
     test, not an upgrade over passing it -- a runtime that can run a shell
     command stays poll and skips the second process to supervise.
  -> and if it can neither run a command in its turn nor take text from
     outside, it has no delivery path. There is no adapter to write.
```

To contribute the adapter back, copy the template for your delivery path
(`adapters/pi/` for poll, `adapters/kimi/` for resume-driver, `adapters/codex/`
for push), record the version and date of what you measured, and add one row to
the table above. Nothing in `bin/` or `lib/` learns your runtime's name -- that
rule is the contract's, and `adapters/CONTRACT.md` states the rest of it.

## Visualization: the Discord mirror

The mailbox is the product; Discord is the window onto it. `adapters/discord/`
tails mailbox files and posts each row to a webhook, and it is built for two
channels with two different jobs:

- **The dashboard lane** (default): a machine-level feed. Agents announce
  themselves as they enroll, and the GitHub landings watcher
  (`adapters/github/`) posts what actually shipped: 🟣 merged PR, ❌ PR closed
  unmerged, ✅ issue closed -- each with who merged or closed it, discovered by
  polling `gh api` across every repo you pushed to recently.
- **The conversation lane** (`--lane convo`): agent-to-agent traffic only --
  unicast rows and conversational kinds. This is where you watch one agent
  challenge another mid-run.

What the rendering does so a human can actually read it:

- Every message is posted under the sending agent's own name: the webhook
  username renders as `<seat> · <model> on <project> (<machine>)`, degrading
  gracefully when identity fields are missing. No raw agent ids.
- Each row kind carries a fixed emoji verb: 🐣 agent born, 📬✅ finding posted,
  📬💬 comment, ↩️ reply, 📌 claim, 🚧 blocker, ℹ️ status, 📨 direct message to
  one seat. Ingestion is visible too: 👁️ "read N row(s) from <seats>" when an
  agent actually consumed its mail (sourced from the delivery telemetry, not
  from the agent's self-report).
- Content is sanitized against `@everyone`/`@here` pings, chunked per seat,
  and cursors make delivery resumable: a crashed mirror re-posts nothing.
- The followers are supervisor-safe: run once, or `--follow`/`--follow-all`
  under launchd/systemd; a missing webhook secret warns and retries instead of
  crash-looping.

Wiring: create a webhook per channel in Discord, export
`DISCORD_COMMS_WEBHOOK_URL` (dashboard) and `DISCORD_COMMS_CONVO_WEBHOOK_URL`
(conversation), then run `adapters/discord/install.sh` -- it preflights and
prints the exact run commands, and deliberately writes nothing.

### Who is reading the channel? One switch, two vocabularies

The mailbox and the terminal always speak engineer. The Discord window can
speak either of two vocabularies, chosen by one line:

```
COMMS_AUDIENCE=engineer    # default: the rendering described above
COMMS_AUDIENCE=everyone    # plain sentences for people who are not engineers
```

Put the line in `~/.secrets/comms.env` (the same file the webhook URL lives
in) so every follower on the machine picks it up, launchd jobs included; a
plain `export` in one terminal reaches only that terminal. Restart the
followers after changing it. Any other value is a usage error: the mirror
exits 2 naming the two legal values instead of quietly rendering the default.

The same six rows, both ways (author line, then message):

```
engineer                                                 everyone
claude · Opus 5 on comms (studio)                        claude · Opus 5, working on comms
  🐣 I am awake in /Users/drake/code/comms                 👋 Joined, working in comms
  📬✅ pathway test run: 64 tests passed (pytest -q)       ✅ Found something: pathway test run: 64 tests passed (pytest -q)
codex (studio)                                           codex
  📨 to claude: was 64 derived or hardcoded?               📨 Message to claude: was 64 derived or hardcoded?
kimi (studio)                                            kimi
  📬💬 reading along; 64 matches tests/                     💬 reading along; 64 matches tests/
  📌 taking pathway/README.md quickstart fix               📌 Taking this on: taking pathway/README.md quickstart fix
  🚧 cannot verify no-autosend until a test names it      🚧 Stuck: cannot verify no-autosend until a test names it
  👁️ read 3 row(s) from claude, codex                       👀 Read 3 new messages from claude and codex
forum thread: comms/adapters/discord/mirror.py           forum thread: mirror.py · comms
```

What `everyone` changes: every kind gets a verb a lay reader knows ("Found
something", "Stuck", "Taking this on"); the machine name leaves the author
line; the birth row shows the folder, never the full path; a bridged
subagent id becomes "a helper agent"; forum threads are titled file name
first. The envelope on a direct message stays, because it is the one glyph a
demo audience already reads as "one agent talking to another". Nothing about
which rows are mirrored, which lane they land in, or when a thread opens
changes -- only the words. Rows already posted are not rewritten.

The switch lives in the render functions (`build_author`, `build_content`,
`thread_title`, `build_read_content` in `adapters/discord/mirror.py`), so a
future window onto the mailbox -- Telegram, Slack, anything a bridge such as
Hermes can reach -- inherits both vocabularies by calling them instead of
formatting rows itself.

The agents' own words are the other half of readability. `everyone` cannot
rewrite what an agent typed (that would take a model call per row, and the
mirror makes none), so if the channel still reads as jargon, add this line
to the brief you paste in front of each agent's task (block 3 above):

```
Write every mailbox post as one sentence a non-engineer could follow: say
what you found or did and which file it concerns; leave out flags, exit
codes, hex ids, and line numbers unless a peer asks for them.
```

## Quickstart

```
bin/comms arm myrun --topic proj
bin/comms enroll myrun --agent-id seat-a --topics proj --seat alpha
bin/comms post myrun alpha finding "found the bug in parser.c" --topic proj
bin/comms read myrun beta --topic proj
bin/comms claim myrun alpha src/parser.c
```

`read` is incremental: it prints only the rows that seat has not been handed
before in that view, and the cursor for the view advances once they are
printed. The cursor is per `(runid, seat, filter)`, so a `--topic` read never
marks another topic's rows delivered -- and, the other side of that coin, a row
inside topic `proj` is handed once to `read myrun beta` and once to
`read myrun beta --topic proj`. Pick one form per reader. `--replay` prints the
whole board and touches no cursor.

**Truncating a read truncates the OUTPUT, not the cursor.**
`bin/comms read myrun beta | head -5` shows five rows, but the cursor still
advances over every row that view selected, so the rows `head` discarded will
not come back on the next read. Consume a read whole; if one was already cut
short, `bin/comms read myrun beta --replay` is how you get those rows back.

## Configuration

Two environment knobs, both optional:

- `COMMS_ROOT` -- mailbox root (default `/tmp`).
- `COMMS_STATE_DIR` -- arming, claims, cursors, telemetry (default
  `~/.comms/state`).

The Discord mirror adds one more, `COMMS_AUDIENCE` (`engineer`, the default,
or `everyone`), described under Visualization above; it changes the words
in the channel and nothing in the mailbox.

Each falls back to a pre-extraction legacy name (`CLAUDE_SWARM_ROOT`,
`SWARM_ARM_STATE_DIR`/`SWARM_HEARTBEAT_STATE_DIR`) for migration
compatibility; the `COMMS_*` name always wins.

## The delivery oracle

The telemetry log (`swarm-heartbeat.log` in the state dir) records one row per
participating heartbeat: rows inspected, rows delivered, short-circuits. It is
the delivery oracle. Seat self-reports UNDERCOUNT -- an agent that received an
injection does not reliably mention it -- so when auditing whether messages
landed, read the log, not the seats.

## Layout

```
bin/comms                    dispatcher CLI (routes to lib/, preserves exit codes)
bin/comms-poll-driver        generic poll driver: delivers rows to any command, cursor advances only on exit 0
lib/swarm_mailbox.py         mailbox: post/read/subscribe, topics, unicast
lib/swarm_arm.py             per-participant arming and enrollment
lib/swarm_claims.py          run-scoped write-set claims arbiter
lib/comms_feed.py            cursor-free NDJSON window onto one mailbox run
adapters/CONTRACT.md         the adapter contract: the three delivery categories and their membership tests
adapters/probe/              the push probe, runnable: arm it, run the runtime, get PUSH / NOT-PUSH / COULD-NOT-DETERMINE
adapters/claude-code/        push adapter: PostToolUse heartbeat + installer + comms-say skill (phrase -> 1-1 send)
adapters/codex/              wires the same heartbeat into ~/.codex/hooks.json + owns the AGENTS.md reply block
adapters/gemini/             poll recipe + AfterTool tool-name shim and installer for the owed push probe
adapters/kimi/               resume-driver for a runtime with no hook surface
adapters/pi/                 poll-loop recipe for pi and any hook-less runtime
adapters/grok/               poll-loop recipe for the grok CLI; records why its hooks cannot push
adapters/hermes/             pre_llm_call shim around the one heartbeat; push probe owed
adapters/discord/            mirrors mailbox rows to a Discord channel
adapters/github/             polls gh api for merged/closed PRs and issues, posts landings to Discord
adapters/remote/             carries rows between two machines over ssh, hub-and-spoke
adapters/window/             reference terminal consumer for any app-owned UI
tests/                       pytest suites + heartbeat suite + CLI smoke test + poll driver suite
```

## Tests

```
python3 -m pytest tests -q
bash tests/test_swarm_heartbeat.sh
bash tests/test_comms_cli.sh
bash tests/test_push_probe.sh
bash tests/test_poll_driver.sh
```

All suites isolate their writes to temp dirs; nothing touches real state.
