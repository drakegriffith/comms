---
name: comms-say
description: Send a 1-1 message to another live agent terminal (Codex, Claude, Kimi) over the comms mailbox. Use when the user says communicate with / message / tell the codex terminal (or another named terminal or seat) something.
---

# comms-say

The user says "communicate with the codex terminal: <text>" (or names another
terminal/seat). Send ONE mailbox row, confirm it landed, report in one line.
No exploration, no reading the comms repo first.

<!-- Canonical source: adapters/claude-code/skills/comms-say/SKILL.md in the
     comms repo. install.sh renders __COMMS_ROOT__ to the checkout path and
     copies this file into ~/.claude/skills/; edit it there, not here. -->

## Send

    __COMMS_ROOT__/bin/comms post comment "<text>" --to <seat>

If that errors with "needs COMMS_SEAT, or an enrolled CLAUDE_SESSION_ID", use
the explicit form; your own seat is `<cwd basename>-<first 4 hex of your
session id>` (a `session started` row on the board names it):

    __COMMS_ROOT__/bin/comms post machine-ops <your-seat> comment "<text>" --to <seat>

## Target seats

- "the codex terminal" -> the seat named `codex-<name>` in recent beats
  (convention: one interactive Codex seat per machine).
- Anything else: live seats are the recent `topic` fields in
  `tail -20 ~/.comms/state/swarm-heartbeat.log` (strip the `@`). A seat with
  no recent beat has no living session; say so instead of sending into a void.

## Confirm and report

    __COMMS_ROOT__/bin/comms feed machine-ops | tail -1

Report: seat it went to, board timestamp, and the delivery mechanic in one
sentence: the message renders in the receiver's terminal on its NEXT tool
call (pull-on-heartbeat, not interrupt).

## Failure modes

- Receiver enrolled but silent: it has not made a tool call yet; have it run
  anything (`ls`).
- Codex seat dead (no beat since its session ended): a fresh Codex session
  must re-enroll by running
  `python3 __COMMS_ROOT__/lib/swarm_arm.py enroll machine-ops --seat <codex-seat> --topics ops`
  inside the Codex terminal (the command text is the handshake; the heartbeat
  enrolls the session that ran it). Enrollment is per session UUID
  (2026-08-27 incident: messages to a dead seat queue silently).
- Replying into a thread a peer named: add `--thread <key>` so both sides
  render under the same thread.
