# adapters/hermes -- Hermes Agent (NousResearch) pre_llm_call shim

Hermes runs shell commands in its turn, so it already qualifies for the
universal baseline: `bin/comms read <runid> <seat>` in its own loop. Delivery is
**POLL** here because the push surface has not been measured on this machine.

## What is verified locally

Nothing yet. This adapter ships the shim and the wiring recipe; the push probe
is owed. Hermes is not on PATH on the Mac where this was written (only a
`~/.hermes` data directory is present), so no headless run could prove that a
hook's stdout reaches the model.

What the Hermes docs claim:

- Shell hooks live in `~/.hermes/config.yaml` under a `hooks:` block and run as
  subprocesses in both CLI and gateway sessions.
- `pre_llm_call` fires **once per turn**, before the tool-calling loop begins.
- A shell hook that prints `{"context": "..."}` on stdout appends that text to
  the current turn's user message.
- `post_tool_call` is documented as observer-only; only the in-process Python
  `transform_tool_result` rewrites the tool result. There is no measured
  PostToolUse-shaped injection path.

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

To prove push before switching to it, run the probe. For non-JSON configs like
Hermes's YAML, `adapters/probe/arm-probe.sh --format none` prints the probe
hook command and probe dir so you can paste the same command into the stanza
above by hand.

Then, in the seat's brief, enroll explicitly on line one with the Hermes
session id as the agent id:

```
<repo>/bin/comms enroll RUNID --agent-id SESSION_ID --topics TOPIC --seat SEAT
```

`SESSION_ID` is the value Hermes passes as `session_id` in the `pre_llm_call`
payload. Without this explicit enrollment the shim has no run to read and emits
`{}`.

## Hazards

- **Once per turn, not once per tool.** A peer row posted while Hermes is in
  the middle of a tool loop is not injected until the *next* user turn begins.
  Do not rely on the hook for sub-turn latency.
- **Enrolment is explicit.** The shim sets `tool_name` to `Bash` with an empty
  command and `hook_event_name` to `PostToolUse` so the heartbeat's enrol and
  claim legs stay inert. The seat must enrol with `bin/comms enroll` before any
  row can reach it.
- **Never block.** The shim exits 0 and prints `{}` on malformed stdin, a
  missing heartbeat, or any runtime error. A shell hook that aborts the agent
  turn would be a bug.
- **One heartbeat.** The shim translates Hermes's envelope into the shape the
  one existing heartbeat expects and translates its stdout back. There is no
  Hermes-specific heartbeat.

## Poll fallback

If the push probe fails or you cannot enable hooks, use the poll recipe from
`adapters/pi/` -- it works from any shell-capable runtime with no further code.
