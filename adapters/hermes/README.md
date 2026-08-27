# adapters/hermes -- Hermes Agent (NousResearch) pre_llm_call shim

Hermes runs shell commands in its turn, so it already qualifies for the
universal baseline: `bin/comms read <runid> <seat>` in its own loop.

**NONE YET: adapter ready; poll test and push probe both owed (hermes not on
PATH on this Mac).** A passing poll test would flip the category to **POLL**; a
passing push probe showing hook context reaches the live model would flip it to
**PUSH**.

## What is verified locally

Nothing yet. This adapter ships the shim and the wiring recipe; both the poll
test and push probe are owed. Hermes is not on PATH on the Mac where this was
written, so no live run could prove either category.

Documentation measurement: **doc read 2026-08-27, version unstated** (hermes-agent.nousresearch.com/docs/user-guide/features/hooks).

What the Hermes docs claim:

- Shell hooks live in `~/.hermes/config.yaml` under a `hooks:` block and run as
  subprocesses in both CLI and gateway sessions.
- `pre_llm_call` fires **once per turn**, before the tool-calling loop begins.
- A shell hook that prints `{"context": "..."}` on stdout appends that text to
  the current turn's user message.
- `post_tool_call` is documented as observer-only; only the in-process Python
  `transform_tool_result` rewrites the tool result. There is no measured
  PostToolUse-shaped injection path.
- Shell-hook `timeout` defaults to 60 seconds. The shim uses 45 seconds so its
  child heartbeat resolves before Hermes's outer timeout; if the stanza sets a
  lower timeout, keep the shim timeout lower still.

Because `pre_llm_call` is once per turn, not once per tool, rows posted after
the last tool of a turn arrive before the model's next LLM call -- not
immediately after each sibling post.

## Hand-wiring recipe

Add this block to `~/.hermes/config.yaml` (replace `<repo>` with the absolute
path to this checkout):

```yaml
hooks:
  pre_llm_call:
    - command: "<repo>/adapters/hermes/hook.sh"
```

To prove push before switching to it, run the probe, with one translation.
`adapters/probe/arm-probe.sh --format none --dir <d>` (on master since PR #68)
writes `<d>/hand-wiring.txt` with the probe hook command and the envelope it
prints. That envelope is the Claude shape (`hookSpecificOutput.additionalContext`),
which Hermes ignores on a shell hook: it reads stdout as JSON and injects only
`{"context": ...}`, so pasting the probe command unmodified yields a hook that
fires (stdin copy present) and an agent that sees nothing, and
`probe-verdict.sh` would record NOT-PUSH for the wrong reason. Wire the probe
through this two-line translation instead:

```yaml
  pre_llm_call:
    - command: "sh -c 'bash <repo>/adapters/probe/push-probe-hook.sh <d> | python3 -c \"import json,sys; o=json.load(sys.stdin); print(json.dumps({\\\"context\\\": o[\\\"hookSpecificOutput\\\"][\\\"additionalContext\\\"]}))\"'"
```

Read the stdin copy first (positive control), then the answer, then run
`probe-verdict.sh <d>` as the kit documents. Doc: the Hermes hooks page,
`hermes-agent.nousresearch.com/docs/user-guide/features/hooks`, read 2026-08-27.

Then, in the seat's brief, enroll explicitly on line one with the Hermes
session id as the agent id:

```
<repo>/bin/comms enroll RUNID --agent-id SESSION_ID --topics TOPIC --seat SEAT
```

`SESSION_ID` is the value Hermes passes as `session_id` in the `pre_llm_call`
payload. Without this explicit enrollment the shim has no run to read and emits
`{}`.

## Hazards

- **Consent can silently suppress a new hook in headless use.** The docs state:
  “Non-TTY runs (gateway, cron, CI) need one of these three — otherwise any
  newly-added hook silently stays un-registered and logs a warning.” Each new
  `(event, command)` pair normally prompts once. Headless runs must bypass that
  prompt with one of the three documented escape hatches: `--accept-hooks`,
  `HERMES_ACCEPT_HOOKS=1`, or `hooks_auto_accept: true` in
  `~/.hermes/config.yaml`. Without one, a push probe can produce a false
  negative because the hook never registered.
- **Once per turn, not once per tool.** A peer row posted while Hermes is in
  the middle of a tool loop is not injected until the *next* user turn begins.
  Do not rely on the hook for sub-turn latency.
- **Enrolment is explicit.** The shim sets `tool_name` to `Bash` with an empty
  command and `hook_event_name` to `PostToolUse` so the heartbeat's enrol and
  claim legs stay inert. The seat must enrol with `bin/comms enroll` before any
  row can reach it.
- **Never block.** The shim exits 0 and prints `{}` on malformed stdin, a
  missing heartbeat, or any runtime error. A missing heartbeat also emits one
  diagnostic line on stderr. A shell hook that aborts the agent turn would be a
  bug.
- **One heartbeat.** The shim translates Hermes's envelope into the shape the
  one existing heartbeat expects and translates its stdout back. There is no
  Hermes-specific heartbeat.

## Poll fallback

If the push probe fails or you cannot enable hooks, use the poll recipe from
`adapters/pi/` -- it works from any shell-capable runtime with no further code.
