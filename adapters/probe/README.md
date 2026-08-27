# adapters/probe -- the push probe, runnable

This is not an adapter. It is the tool that decides whether a runtime GETS a
push adapter: the procedure in `adapters/CONTRACT.md` ("push -- proven
injection") turned into three scripts you can run.

The question it answers is narrow. A runtime is PUSH when it takes a hook's
stdout, parses `hookSpecificOutput.additionalContext`, and injects it back into
the agent's turn. Having hooks is a different fact -- grok loads Claude-shaped
hooks, fires them on the right events, and throws their stdout away. Vendor docs
do not settle this; a measurement on your machine at a stated version does.

Nothing here changes `bin/` or `lib/`, and no kit script BRANCHES on a runtime
name -- what differs between runtimes arrives as flags, never as an `if`. Every
byte a probe writes lands in one isolated probe dir, so a probe run cannot touch
the mailbox, the state dir, or any cursor.

## The three files

| File | Job |
| --- | --- |
| `push-probe-hook.sh` | the hook. Copies its stdin (the POSITIVE CONTROL), prints an envelope carrying a unique passphrase, saves those bytes too. |
| `arm-probe.sh` | mints the passphrase, makes the probe dir, wires the hook into one config file you name, prints the next two commands. |
| `probe-verdict.sh` | reads the positive control FIRST, then the answer. Prints PUSH / NOT-PUSH / COULD-NOT-DETERMINE with the evidence paths. |

## Usage

```
bash adapters/probe/arm-probe.sh --config <runtime hook config>   # prints the probe dir
COMMS_STATE_DIR=<dir>/state <runtime> -p "<the brief below>" | tee <dir>/agent-answer.txt
bash adapters/probe/probe-verdict.sh <dir>
```

The brief for step 2, near enough verbatim:

```
Run this one shell command: echo hello
Then report any extra context or passphrase that appeared in your turn,
verbatim. If none appeared, answer exactly NOTHING-APPEARED.
```

`arm-probe.sh --help` lists the flags. The two that matter for an unfamiliar
runtime: `--event` (which event to hook, default `PostToolUse`) and `--format`
(`flat` for a `hooks.json`-shaped file, `wrapped` for a `settings.json`-shaped
one, `auto` to guess from the file). Codex reads `hooks.json` only in the
wrapped shape, so pass `--format wrapped` for Codex. With no `--config`, it
writes an isolated `hooks.json` inside the probe dir that reaches no runtime --
useful for inspecting the shape before you point it at anything real.

## Reading the result

`probe-verdict.sh` prints one word and exits on a code you can branch on:

| Exit | Verdict | What to do |
| --- | --- | --- |
| 0 | PUSH | declare push; wire `adapters/claude-code/swarm-heartbeat.sh` (never fork it) via an `install.sh` like `adapters/codex/`; record version + date. |
| 1 | NOT-PUSH | a REAL negative. Record it with version and date; the runtime keeps whatever path it already passed. This is grok. |
| 2 | COULD-NOT-DETERMINE | not a pass and not a fail. Fix the wiring, re-run, **record nothing**. |

Exit 2 is the whole reason this kit exists. The first grok probe returned a
clean `NOTHING-APPEARED` while the hook had never fired at all -- project hooks
did not load until the directory was a git repo. That run inspected zero
subjects and proved nothing, and writing it down as a negative would have
shipped a correct verdict supported by no evidence, which is worse than no
verdict, because nobody re-runs it. So the helper refuses to read the answer at
all until the control passes, and a caller that collapses exit 1 and exit 2 into
"not push" has put that bug straight back.

The positive control is four checks, in this order:

1. the probe dir has a passphrase (it was armed at all);
2. `stdin-copy.json` exists and is non-empty (the hook LOADED and FIRED, and was
   handed the event);
3. the payload names the event and tool you expected, if you passed
   `--expect-event` / `--expect-tool` (the hook fired on the RIGHT thing);
4. `hook-stdout.json` exists and carries THIS run's passphrase (the stimulus was
   emitted, and the evidence is not left over from an earlier arm).

## Known-answer cases

All three are replayed as fixtures by `tests/test_push_probe.sh`, so a change to
the kit that flips any of them fails the suite.

- **NOT-PUSH -- grok 0.2.106, 2026-08-25.** Payload naming
  `"hookEventName":"post_tool_use"` and `"toolName":"run_terminal_command"`,
  answer `NOTHING-APPEARED`. Control present, nothing came back. See
  `adapters/grok/README.md`.
- **PUSH -- Claude Code / codex 0.148.0, 2026-08-21.** Same shape of control,
  answer quotes the passphrase. See `adapters/codex/README.md`.
- **COULD-NOT-DETERMINE -- the first grok attempt.** Armed, hook never fired, an
  answer of `NOTHING-APPEARED` sitting there unread.

## Runs of this kit on record

Both live runs below were made with the scripts in this directory, on
2026-08-25, into scratch dirs under `/tmp` -- no real config, no real state.

| Runtime | Exit | Verdict | Evidence |
| --- | --- | --- | --- |
| Claude Code 2.1.246 | 0 | PUSH | control passed (1 fire, `PostToolUse`/`Bash`); the agent quoted `COMMS-PROBE-BC3CFA-9403` back verbatim |
| grok 0.2.106 | 2 | COULD-NOT-DETERMINE | the agent answered `NOTHING-APPEARED` and the hook had never fired -- no stdin copy |

The grok row is the point of the whole kit, and it sprang the trap a second
time. Read answer-first, that run is a clean measured negative. It is not a
result at all: the probe was armed into a project-scoped
`.claude/settings.json` inside a fresh git repo, and grok 0.2.106 loaded no
project config because the project was UNTRUSTED (`grok inspect` reports
`Project trusted: no` and `Config Sources -> Project: (none)`, while still
loading 18 hooks from the user's own `~/.claude/settings.json`). A git repo is
necessary and not sufficient. So this run records NOTHING about grok's push
status; grok's NOT-PUSH stands on the 2026-08-25 probe already in
`adapters/grok/README.md`, not on this.

## Hazards

- **Capture the AGENT'S ANSWER, not the whole transcript.** Some runtimes echo a
  hook's stdout into their own logs without ever injecting it. Tee the answer
  channel (`-p` / `--output-format` stdout), or the passphrase can appear in the
  file without ever having reached the agent, and the probe scores itself PUSH.
- **Trust gates fail silently.** Headless codex skips untrusted hooks with no
  message (`--dangerously-bypass-hook-trust`). A skipped hook and a quiet hook
  look identical from outside -- which the positive control catches, as exit 2.
- **Un-wire when you are done.** The entry fires on every tool call for as long
  as it is in the config. `arm-probe.sh` prints the file it edited.
- **Re-arming into a new dir repoints the existing entry** rather than adding a
  second one, and clears the old evidence, because stale evidence read as fresh
  is the same trap in a different coat.
- **grok scans `~/.claude/settings.json` by default.** Arming a probe there arms
  it for more runtimes than you think. Prefer a project-scoped config in a
  scratch git repo -- but check the runtime actually LOADED it (`grok inspect`
  and its equivalents list the hooks in force). Exit 2 is what you get when it
  did not, which is the kit working.

## After a PUSH verdict

`INSTALLER-CHECKLIST.md` beside this file takes a runtime from a passing probe
to an adapter at parity with `adapters/codex/`, without touching `bin/` or
`lib/`.
