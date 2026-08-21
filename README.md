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
any runtime participates with zero integration. Push delivery is per-runtime
sugar on top.

## Per-runtime injection

| Runtime     | Delivery | How |
|-------------|----------|-----|
| Claude Code | push     | PostToolUse hook (`adapters/claude-code/`, wired into settings.json by its install.sh) |
| Codex       | push     | native Claude-shaped `hooks.json` runs the same heartbeat script (`adapters/codex/`) |
| Kimi        | resume-driver | no hook surface; `adapters/kimi/poll-driver.sh` polls and delivers rows as resume turns |
| pi (badlogic) | poll   | briefed poll loop, `bin/comms read` after each work step (`adapters/pi/` -- recipe covers any hook-less runtime, local models included) |
| Discord     | mirror   | `adapters/discord/` mirrors mailbox rows to a channel (in flight, issue #2) |
| Claude Code (ambient) | push + mirror | `adapters/claude-code/ambient/` -- SessionStart + SendMessage-bridge hooks enroll every session into standing run `machine-ops` (topic `ops`; only message SUMMARIES are bridged), mirrored to Discord as the machine dashboard |
| anything else | poll   | `bin/comms read <runid> <seat>` in the agent's own loop |

## Quickstart

```
bin/comms arm myrun --topic proj
bin/comms enroll myrun --agent-id seat-a --topics proj --seat alpha
bin/comms post myrun alpha finding "found the bug in parser.c" --topic proj
bin/comms read myrun beta --topic proj
bin/comms claim myrun alpha src/parser.c
```

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
tests/                       pytest suites + heartbeat suite + CLI smoke test
```

## Tests

```
python3 -m pytest tests -q
bash tests/test_swarm_heartbeat.sh
bash tests/test_comms_cli.sh
```

All suites isolate their writes to temp dirs; nothing touches real state.
