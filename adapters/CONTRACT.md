# The adapter contract

An adapter connects one agent runtime to the comms mailbox. This file is the
interface: what an adapter must PROVE, what it may assume, and which files it
consists of. The adapters beside it are worked examples of this contract, not
exceptions to it. Read this before writing one; the README's "Connect your CLI"
section is the short path if all you want is a working seat.

Two facts shape everything below.

- **Polling is the floor.** `bin/comms read <runid> <seat>` is a shell command.
  Any runtime that can run a shell command already participates, with zero
  integration and zero adapter code. No probe can move a runtime BELOW poll, so
  no probe is a prerequisite for connecting one.
- **The core never learns runtime names.** `bin/comms` and `lib/*.py` carry no
  branch on which runtime is calling -- grep them for `claude|codex|kimi|grok`
  and every hit is prose in a docstring, a free-text identity example, or a
  legacy env-var name kept for migration. An adapter that needs an `if` inside
  `lib/` is not an adapter; it is a core change, and it needs its own ticket.

## The three delivery categories

A category is a claim about DELIVERY -- how a row reaches a running agent --
and each one has a single falsifiable membership test. A runtime belongs to the
strongest category whose test it has PASSED on your machine, at a stated
version, on a stated date. Not the strongest category its vendor documents.

### poll -- the floor

**Membership test.** The runtime can execute `bin/comms read <runid> <seat>` in
its own loop, and that command's output enters its context.

**Probe.** Arm a run, post a row carrying a unique passphrase, brief the agent
to run the read command and report what it saw. Pass = it reports the
passphrase.

**Failing this test means the runtime cannot participate at all**, in any
category: push and resume-driver are ways of saving the agent the trouble of
looking, not ways around needing a shell.

**Cost of the category.** The agent has to choose to look. Cadence is the
brief's business, not the stack's; "after every work step" is the proven one.

**Template.** `adapters/pi/README.md` (generic) or `adapters/grok/README.md`
(the same recipe plus a measurement record). README only -- there is no code.

### resume-driver

**Membership test.** Two parts, both required. (a) The push test below has been
run and FAILED with its positive control intact. (b) A process OUTSIDE the
session can deliver text into an EXISTING session addressed by id, and that
text enters the agent's turn.

**Probe.** Start a session and note its id. From another shell, deliver a
passphrase as a resume turn (`kimi -r <id> -p "..."`, or the runtime's
equivalent). Read the session transcript: the passphrase is there, and the
agent answered it, or (b) failed.

**When to take it.** Only when the runtime cannot check the mailbox from inside
its own turn. That is why kimi has it: no hook surface AND no in-session way to
run the read. A runtime that can run a shell command should stay poll --
resume-driver costs a second process to supervise and buys nothing poll lacks.
grok passes the resume-driver test and deliberately does not use it
(`adapters/grok/README.md`, "Notes").

**Template.** `adapters/kimi/` -- README plus a driver script. The driver's one
hard rule: the cursor advances only after an invocation that SUCCEEDED, so a
failed delivery re-delivers instead of dropping rows.

### push -- proven injection

**Membership test.** The runtime takes the hook's stdout, parses
`hookSpecificOutput.additionalContext`, and injects it back into the agent's
turn -- MEASURED on this machine, at this version.

**A hook surface is not push.** Having hooks and injecting hook output are two
different facts, and a runtime can have the first without the second. grok
loads Claude-shaped hooks, fires them on the right events, hands them the event
as JSON on stdin, and discards their stdout (probe 2026-08-25, grok 0.2.106,
PR #25). Codex earned push by proving the injection, not by having hooks. The
cost of getting this wrong is silent: a heartbeat wired into a runtime that
does not inject runs on every tool call, prints rows nobody reads, and advances
the read cursor past them, so rows are marked delivered to a reader that never
saw them.

**Probe, in order.**

1. Install a `PostToolUse`-style hook, no matcher, that does two things: prints
   a well-formed envelope carrying a unique passphrase, and copies its own
   stdin to a file. The stdin copy is the POSITIVE CONTROL.

   ```
   {"hookSpecificOutput":{"hookEventName":"PostToolUse",
    "additionalContext":"MAILBOX ROW: ... passphrase ZORBLAX-7741 ..."}}
   ```

2. Run the runtime headless, tell it to run one shell command and report any
   extra context or passphrase it saw.
3. **Read the stdin-copy file FIRST**, before reading the agent's answer. It
   must exist and name the event you hooked and the tool you called.
4. Only now interpret the answer. The passphrase came back = PUSH. The agent
   reports nothing = NOT PUSH.

**A probe that inspected zero subjects is not a negative result.** There are
three outcomes here, not two:

| Stdin copy | Agent's answer | Verdict |
| --- | --- | --- |
| present | passphrase | push |
| present | nothing | not push -- a real negative, record it |
| missing | anything | COULD NOT DETERMINE -- fix the wiring, re-run, record nothing |

The third row is the trap, and it has already been sprung: the first grok probe
returned a clean `NOTHING-APPEARED` while the hook had never fired at all
(project hooks did not load until the directory was a git repo). That run
proved nothing. Reading it as proof would have shipped a correct verdict
supported by no evidence, which is worse than no verdict, because nobody
re-runs it.

**If it passes.** Reuse `adapters/claude-code/swarm-heartbeat.sh` verbatim.
Never write a second heartbeat: the identity gate, arm gate, subscription
filter, and cursor rules live in that one file, and a copy is a second place
for them to drift. The adapter is then a README plus an idempotent `install.sh`
that wires that script into the runtime's own hook config, detects an existing
entry, never clobbers unrelated settings, and honours an env override naming
the target file so tests never touch the real one (`COMMS_SETTINGS`,
`COMMS_CODEX_HOOKS`). The reusable probe kit and installer checklist are
tracked as issue #28.

## Classifying your runtime, in order

1. Can it run a shell command in its loop? No -> it cannot participate.
2. Yes -> it is POLL, today, with no further work. Ship the poll adapter. Every
   step below is an upgrade, never a prerequisite.
3. Run the push probe. Pass -> push. Real negative -> stay poll, and write down
   what you measured. Could-not-determine -> re-run; do not record a category.
4. Only if push failed AND the agent has no in-session way to read the mailbox,
   run the resume-driver probe.
5. Record the verdict with the runtime's version, the date, and a pointer to
   the evidence. A category is a dated measurement, not a property of the
   product: a new release can flip it either way, and the adapter README says
   what re-probe would flip it.

## Category claims on record

| Runtime | Category | What was measured | When |
| --- | --- | --- | --- |
| Claude Code | push | PostToolUse `additionalContext` reached a live subagent; telemetry row `rows_inspected 6, delta_emitted 3` | 2026-08-21, comms #1 |
| Codex | push | byte-compatible Claude-shaped `hooks.json`; injection observed. Headless needs `--dangerously-bypass-hook-trust` -- untrusted hooks are skipped SILENTLY | 2026-08-21, codex 0.148.0 |
| Kimi | resume-driver | no hook surface at all; `kimi -r <id> -p` delivers into a live session | pre-extraction |
| grok (xAI) | poll | hooks load and fire (stdin copy present) and stdout is discarded (`NOTHING-APPEARED`) | 2026-08-25, grok 0.2.106 |
| pi (badlogic) | poll | no hook surface; runs shell commands | pre-extraction |

Everything else is UNVERIFIED until probed. Gemini CLI, Qwen Code, Copilot CLI
and Cursor have the floor like any shell-capable runtime, and no push or
resume-driver claim: nobody has run the probe. Do not promote them from a
changelog.

## What every adapter owes, whatever its category

- **A README that opens with its category and the one-line reason**, then the
  measurement behind it (runtime version and date), then the recipe or the
  install command, then the hazards. `adapters/grok/README.md` is the shape.
- **Runtime-agnostic core.** No file under `bin/` or `lib/` changes for your
  adapter. If it must, that is a core ticket, not an adapter.
- **One heartbeat.** Push adapters wire
  `adapters/claude-code/swarm-heartbeat.sh`; they do not fork it.
- **The closed kind vocabulary**, quoted verbatim in the brief block:
  `finding|claim|blocker|comment|reply|status`. An unlisted kind fails loudly.
  Relabel, never retry blind.
- **Cursor semantics stated, not assumed.** The read cursor is per
  `(runid, seat)` under `COMMS_STATE_DIR`; repeated reads never replay, and a
  restarted session resumes where it left off.
- **The delivery oracle.** When auditing whether rows landed, read
  `swarm-heartbeat.log` in the state dir and the mailbox files. Seat
  self-reports UNDERCOUNT: an agent that received an injection does not
  reliably mention it.
- **Hazards written down, including the ones you did not fix.** grok scans
  `~/.claude/settings.json` by default, so an unrelated install can leave it
  firing the comms heartbeat; that is in its README with the one-line
  mitigation, not left for the next person to rediscover.
- **No secrets.** Webhook URLs, hosts and keys live in the environment or a
  private env file, never in this repo.

## The templates

Copy the nearest one and edit; do not start from a blank file.

| Category | Files | Copy from |
| --- | --- | --- |
| poll | `adapters/<name>/README.md` | `adapters/pi/README.md` |
| resume-driver | `README.md` + `<name>-driver.sh` | `adapters/kimi/` |
| push | `README.md` + `install.sh` | `adapters/codex/` |

```
mkdir -p adapters/<name>
cp adapters/pi/README.md adapters/<name>/README.md     # poll
cp adapters/codex/install.sh adapters/<name>/install.sh # push, after the probe
```

Then add one row to the README's per-runtime table and one line to its Layout
block. That is the whole registration step: there is no registry file, and
nothing in `bin/` or `lib/` needs to learn the name.
