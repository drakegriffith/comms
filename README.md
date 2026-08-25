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
  SET and reads only its slice plus its own unicast channel `@<seat>`. This is
  what keeps per-reader context bounded as the swarm grows.
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
| Claude Code | push     | PostToolUse hook (`adapters/claude-code/`, wired into settings.json by its install.sh) |
| Codex       | push     | native Claude-shaped `hooks.json` runs the same heartbeat script (`adapters/codex/`) |
| Kimi        | resume-driver | no hook surface; `adapters/kimi/poll-driver.sh` polls and delivers rows as resume turns |
| pi (badlogic) | poll   | briefed poll loop, `bin/comms read` after each work step (`adapters/pi/` -- recipe covers any hook-less runtime, local models included) |
| Grok (xAI)  | poll     | runs Claude-shaped hooks but was measured NOT to inject `additionalContext`, so a hook would drop every row; briefed poll loop instead (`adapters/grok/` -- carries the probe that would upgrade it to push) |
| Discord     | mirror   | `adapters/discord/` mirrors mailbox rows to a channel (in flight, issue #2) |
| Claude Code (ambient) | push + mirror | `adapters/claude-code/ambient/` -- SessionStart + SendMessage-bridge hooks enroll every session into standing run `machine-ops` (topic `ops`; only message SUMMARIES are bridged), mirrored to Discord as the machine dashboard |
| GitHub (landings) | poll + mirror | `adapters/github/` polls `gh api` for merged/closed PRs and closed issues, posts each to Discord with attribution ("who merged/closed what") -- source is GitHub itself, not the comms mailbox |
| another machine | ssh push + poll | `adapters/remote/` -- one machine's mailbox is the hub; the other pushes rows into it and pulls its slice back, over plain ssh. The hub runs no new code (only `bin/comms post`/`read`), and outbound rows queue locally while it is unreachable |
| anything else | poll   | `bin/comms read <runid> <seat>` in the agent's own loop |

## Installing

```
git clone https://github.com/drakegriffith/comms
cd comms
bin/comms            # prints usage; you are installed
python3 -m pytest tests -q   # optional: prove it on your machine
```

There is nothing else: the core is `bin/comms` plus three stdlib-only Python
files. No pip installs, no config files, no daemon. Two optional env knobs are
listed under Configuration below.

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

## Configuration

Two environment knobs, both optional:

- `COMMS_ROOT` -- mailbox root (default `/tmp`).
- `COMMS_STATE_DIR` -- arming, claims, cursors, telemetry (default
  `~/.comms/state`).

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
lib/swarm_mailbox.py         mailbox: post/read/subscribe, topics, unicast
lib/swarm_arm.py             per-participant arming and enrollment
lib/swarm_claims.py          run-scoped write-set claims arbiter
adapters/claude-code/        push adapter: PostToolUse heartbeat + installer
adapters/codex/              wires the same heartbeat into ~/.codex/hooks.json
adapters/kimi/               resume-driver for a runtime with no hook surface
adapters/pi/                 poll-loop recipe for pi and any hook-less runtime
adapters/grok/               poll-loop recipe for the grok CLI; records why its hooks cannot push
adapters/discord/            mirrors mailbox rows to a Discord channel
adapters/github/             polls gh api for merged/closed PRs and issues, posts landings to Discord
adapters/remote/             carries rows between two machines over ssh, hub-and-spoke
tests/                       pytest suites + heartbeat suite + CLI smoke test
```

## Tests

```
python3 -m pytest tests -q
bash tests/test_swarm_heartbeat.sh
bash tests/test_comms_cli.sh
```

All suites isolate their writes to temp dirs; nothing touches real state.
