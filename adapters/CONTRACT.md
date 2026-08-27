# The adapter contract

An adapter connects one agent runtime to the comms mailbox. This file is the
interface: what an adapter must PROVE, what it may assume, and which files it
consists of. The adapters beside it are worked examples of this contract, not
exceptions to it. Read this before writing one; the README's "Connect your CLI"
section is the short path if all you want is a working seat.

Two facts shape everything below.

- **Polling is the default, and it costs nothing.** `bin/comms read <runid>
  <seat>` is a shell command, so a runtime that can run one inside its own turn
  already participates, with zero integration and zero adapter code. No probe
  is a prerequisite for that; probes only decide whether a runtime can do
  BETTER than poll, or -- for the rarer runtime that cannot run a command in
  its turn at all -- whether something outside can reach in instead.
- **The core never learns runtime names.** `bin/comms` and `lib/*.py` carry no
  branch on which runtime is calling -- grep them for `claude|codex|kimi|grok`
  and every hit is prose in a docstring, a free-text identity example, or a
  legacy env-var name kept for migration. An adapter that needs an `if` inside
  `lib/` is not an adapter; it is a core change, and it needs its own ticket.

## The three delivery categories

A category is a claim about DELIVERY -- how a row reaches a running agent --
and each one has a falsifiable membership test. The tests are not exclusive:
Claude Code and codex pass the poll test as well as the push one, and a
hook-less runtime with a resume flag can pass both poll and resume-driver. A
category is therefore not a property of the runtime but a choice of DELIVERY
PATH, made by the ordered procedure below, from the tests the runtime has
PASSED on your machine, at a stated version, on a stated date. Not from the
strongest path its vendor documents.

The adapter declares exactly one path, because a row delivered twice by two
paths is a row the seat reads twice.

### poll -- the default path

**Membership test.** The runtime can execute `bin/comms read <runid> <seat>`
inside its own turn, and that command's output enters its context.

**Probe.** Arm a run, post a row carrying a unique passphrase, brief the agent
to run the read command and report what it saw. Pass = it reports the
passphrase.

**What the real floor is.** Participation needs a shell SOMEWHERE, not
necessarily inside the agent's turn. A runtime that fails the poll test can
still participate if some process outside it can put text into its live session
-- that is resume-driver, and it is why kimi is a full participant while
failing this test. The one runtime nothing here can reach is one that neither
runs a shell command in its own loop nor accepts text into a live session from
outside: it has no delivery path, so there is no adapter to write.

**Cost of the path.** The agent has to choose to look. Cadence is the brief's
business, not the stack's; "after every work step" is the proven one, and it is
cheap because `comms read` is incremental -- it hands the seat only rows it has
not been handed before in that view, so the loop does not re-read the board
(see the cursor rules under "What every adapter owes").

**Template.** `adapters/pi/README.md` (generic) or `adapters/grok/README.md`
(the same recipe plus a measurement record). README only -- there is no code.

### resume-driver

**Membership test.** Two parts, both required.

(a) **Push is not proven.** Two different states of the world qualify, and each
has its own evidence requirement, because "not proven" is a claim like any
other and silence does not establish it:

- *Measured negative.* The push probe ran and failed. Evidence: the positive
  control (the hook's stdin copy) exists, AND the agent reported no passphrase.
  This is grok.
- *No hook surface at all.* The runtime has no mechanism to run a command on an
  event, so the probe cannot be installed and its verdict is
  could-not-determine forever. Evidence: an ASSERTED absence, not an assumed
  one -- name the surfaces you searched at that version (the config file schema,
  the `--help` output, the docs' command list) and record that none of them
  offers an event hook. This is kimi.

What never qualifies is the third outcome of the push probe: a probe that was
installed but did not fire. Fix the wiring and re-run; it tells you nothing
about either state above.

(b) **Something outside can reach in.** A process OUTSIDE the session delivers
text into an EXISTING session addressed by id, and that text enters the agent's
turn.

**Probe for (b).** Start a session and note its id. From another shell, deliver
a passphrase as a resume turn (`kimi -r <id> -p "..."`, or the runtime's
equivalent). Read the session transcript: the passphrase is there and the agent
answered it, or (b) failed.

**When to take it.** Only when the runtime cannot check the mailbox from inside
its own turn -- that is, only when the poll test also failed. That is why kimi
has it: no hook surface AND no in-session way to run the read. A runtime that
passes the poll test should stay poll: resume-driver costs a second process to
supervise and buys nothing poll lacks. grok is the worked example of declining
it -- its flags (`-r/--resume`, `-p`) say part (b) would pass, but the probe has
not been run, so `adapters/grok/README.md` records that as an expectation, not
as a measurement, and the adapter stays poll.

**Template.** `bin/comms-poll-driver` -- do not write the loop again. It is the
generic form of this category: it reads a seat's rows, hands them to an
ARBITRARY command, and advances its cursor only when that command exits 0. The
runtime-specific part is the command, which is a parameter, so the driver
carries no runtime name in its control flow. An adapter here is then a README
plus, at most, a few lines naming the invocation -- `adapters/kimi/` is the
worked example, and it is now 3 parameters (resume command, cwd, cursor key)
around one `exec`.

The driver's one hard rule, which you inherit by using it: the cursor advances
only after an invocation that SUCCEEDED, so a failed delivery re-delivers
instead of dropping rows. Which is why it reads with `bin/comms read ...
--replay`: the CLI's own cursor advances at print time, before delivery is
known to have worked, and two cursors over one delivery is how rows go missing
quietly. In Python that rule comes from `swarm_mailbox.DeliveryCursor`; in bash
it comes from `comms cursor take` / `comms cursor confirm`, the same helper
split across the two processes a shell delivery needs -- see the
delivery-cursor bullet under "What every adapter owes". Do not re-derive the
arithmetic in either language.

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
THE HEARTBEAT'S OWN CURSOR past them -- the one under
`$COMMS_STATE_DIR/swarm-cursor/<runid>/<agent_id>`, private to
`adapters/claude-code/swarm-heartbeat.sh` and keyed on agent_id, not the CLI
read cursor -- so rows are marked delivered to a reader that never saw them.

**Probe, in order.** `adapters/probe/` is this procedure, runnable: arm it,
run the runtime, read the verdict. The steps below are what those scripts do,
and what to do by hand for a runtime whose hook config is not Claude-shaped.

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
`COMMS_CODEX_HOOKS`). `adapters/probe/INSTALLER-CHECKLIST.md` is that build as a
tick-list, derived from `adapters/codex/`.

## Classifying your runtime, in order

1. Run the poll test. Passes -> the runtime's delivery path is POLL, today,
   with no further work. Ship the poll adapter; steps 2-3 are an upgrade, never
   a prerequisite.
2. Run the push probe. Pass -> declare push. Real negative -> stay on the path
   from step 1 and write down what you measured. Could-not-determine -> fix the
   wiring and re-run; declare nothing.
3. If the poll test FAILED, the runtime still has one path left: run the
   resume-driver probe, having first established (a) -- a measured push
   negative, or an asserted absence of any hook surface.
4. All three failed -> nothing here can reach this runtime. There is no adapter
   to write; say so in the ticket rather than shipping a README nobody can
   follow.
5. Record the verdict with the runtime's version, the date, and a pointer to
   the evidence. A category is a dated measurement, not a property of the
   product: a new release can flip it either way, and the adapter README says
   what re-probe would flip it.

## Category claims on record

| Runtime | Delivery path | What was measured | When |
| --- | --- | --- | --- |
| Claude Code | push | PostToolUse `additionalContext` reached a live subagent; telemetry row `rows_inspected 6, delta_emitted 3` | 2026-08-21, comms #1 |
| Codex | push | byte-compatible Claude-shaped `hooks.json`; injection observed. Headless needs `--dangerously-bypass-hook-trust` -- untrusted hooks are skipped SILENTLY | 2026-08-21, codex 0.148.0 |
| Kimi | resume-driver | push (a) by ASSERTED ABSENCE: no hook surface, so nothing can run after a tool call and the push probe cannot be installed. Poll test fails too -- no in-session way to read the mailbox. (b) passes: `kimi -r <id> -p` delivers into a live session | pre-extraction |
| grok (xAI) | poll | hooks load and fire (stdin copy present) and stdout is discarded (`NOTHING-APPEARED`) -- a measured push negative | 2026-08-25, grok 0.2.106 |
| pi (badlogic) | poll | no hook surface; runs shell commands in its own turn | pre-extraction |
| Hermes (NousResearch) | UNVERIFIED | adapter present, no probe run; `pre_llm_call` is once per turn, not a tool boundary | 2026-08-27, doc version unstated |

Everything else is UNVERIFIED until probed. Gemini CLI, Qwen Code, Copilot CLI
and Cursor get poll if and only if they pass the poll test, which nobody has
run against them here, and they have no push or resume-driver claim at all. Do
not promote them from a changelog.

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
- **Cursor semantics stated, not assumed, and never conflated.** Three
  different cursors exist, and an adapter README that blurs them will be
  debugged with the wrong file open.
  - *The CLI read cursor* (landed with issue #33). Keyed per
    `(runid, seat, VIEW)`: one JSON file of per-seat row counts at
    `$COMMS_STATE_DIR/read-cursor/<runid>/<seat>.<view>.json`, where the view
    is `all` for an unfiltered read, `topic-<name>` for `--topic <name>`, and
    `subs-<digest>` for `--subs`. It advances AFTER the rows are printed, over
    exactly the rows that view selected -- print first, commit after, so a
    crash between the two replays rows (visible) instead of dropping them
    (invisible). A `--topic X` read therefore cannot mark another topic's rows
    delivered, and the price is that a row in topic X is handed once to the
    plain read and once to the `--topic X` read: **one reader, one view.** A
    brief that switches forms mid-run will see rows twice. `--replay` prints
    the whole board and neither reads nor moves any cursor -- the escape hatch
    for auditing, and mandatory for any caller keeping a delivery cursor of its
    own (`adapters/kimi/poll-driver.sh`, `adapters/remote/sync.py`). Piping a
    read through `head` truncates the OUTPUT, not the cursor.
  - *The heartbeat cursor*, private to
    `adapters/claude-code/swarm-heartbeat.sh`: a last-seen timestamp at
    `$COMMS_STATE_DIR/swarm-cursor/<runid>/<agent_id>`, keyed per
    `(runid, agent_id)` -- agent_id, NOT seat -- and advanced after the beat
    emits. Push adapters inherit it by reusing that script; they do not get to
    redefine it.
  - *Your own DELIVERY cursor*, if your adapter keeps one (issue #30). Use
    `swarm_mailbox.DeliveryCursor(path)`; do not write another copy of the
    load/split/save pair. You supply the path, because only your adapter knows
    what makes one stream one stream (a lane, a host, a session id); it
    supplies `take(rows) -> (fresh_rows, confirm)`, the per-seat count
    arithmetic, and the atomic write. **It writes nothing until you call
    `confirm()`** -- so an adapter whose delivery failed simply does not call
    it, and those rows come back on the next pass instead of vanishing. That
    is the difference from the CLI read cursor above, which commits at print
    time because the CLI has no acknowledgement to wait for. Read with
    `--replay` when you keep one of these: your cursor plus the CLI's over one
    stream is one too many. Worked example: `adapters/remote/sync.py`, whose
    "delivery" is the local mirror write, with `confirm()` on the line after
    it. **From bash, the same helper, split across two processes** (issue #29):
    one invocation cannot wait for a delivery that happens after it exits, so
    `comms cursor take <path>` reads rows as JSONL on stdin and prints a
    RECEIPT line then the fresh rows, writing NOTHING, and `comms cursor
    confirm <path> <receipt>` commits that receipt. Run confirm only when the
    delivery exited 0. `bin/comms-poll-driver` and, through it,
    `adapters/kimi/poll-driver.sh` are the worked examples, so an adapter that
    uses the driver never touches these two commands directly; reach for them
    only when driving something the driver cannot express.
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
| resume-driver | `README.md`, and `bin/comms-poll-driver` with your invocation | `adapters/kimi/` |
| push | `README.md` + `install.sh` | `adapters/codex/` |

```
mkdir -p adapters/<name>
cp adapters/pi/README.md adapters/<name>/README.md     # poll
cp adapters/codex/install.sh adapters/<name>/install.sh # push, after the probe
```

Then add one row to the README's per-runtime table and one line to its Layout
block. That is the whole registration step: there is no registry file, and
nothing in `bin/` or `lib/` needs to learn the name.
