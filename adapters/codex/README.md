# Codex adapter

Codex gets PUSH delivery by reusing the claude-code adapter's heartbeat script
through Codex's own native hook surface. There is no Codex-specific heartbeat;
`install.sh` here only wires `adapters/claude-code/swarm-heartbeat.sh` into
`~/.codex/hooks.json`.

## What is proven

Proven 2026-08-21 (recorded as claude-harness issue 129 in the origin harness):

- codex 0.148.0 runs Claude-shaped hooks.json PostToolUse hooks with a
  byte-compatible payload. The heartbeat script needs no changes to run under
  Codex.
- Codex injects `hookSpecificOutput.additionalContext` from the hook's stdout,
  the same contract Claude Code honours, so mailbox rows reach the running
  agent the same way.
- Headless runs need `--dangerously-bypass-hook-trust`, because hook trust is
  hash-pinned and untrusted hooks are skipped SILENTLY. A skipped hook looks
  identical to a quiet one: if delivery seems dead under headless codex, check
  trust before debugging the mailbox. The delivery oracle is the telemetry log
  (`swarm-heartbeat.log` in the state dir), not the agent's self-report.

## Install

```
bash adapters/codex/install.sh
```

Idempotent: re-running never duplicates the entry and never clobbers other
hooks. `COMMS_CODEX_HOOKS=<path>` overrides the target file for testing.
