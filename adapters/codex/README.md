# Codex adapter

Codex gets PUSH delivery by reusing the claude-code adapter's heartbeat script
through Codex's own native hook surface. There is no Codex-specific heartbeat;
`install.sh` here only wires `adapters/claude-code/swarm-heartbeat.sh` into
`~/.codex/hooks.json`.

## What is proven

Measured 2026-08-26 on codex-cli 0.148.0:

- Codex loads `~/.codex/hooks.json` only in the WRAPPED shape
  `{"hooks": {"PostToolUse": [...]}}`. A flat top-level event map
  `{"PostToolUse": [...]}` is rejected and the hook never fires (probe1 and
  probe5: 0 fires, with matcher `"*"` and with matcher `".*"`).
- The wrapped shape fires on every tool call (probe4: 3 fires with matcher
  `".*"`; probe6: 1 fire with matcher `"*"`).
- A file holding both keys fires nothing (probe3), so the installed config must
  hold exactly one shape.
- The 2026-08-21 proof (claude-harness issue 129, comment 2) used the wrapped
  shape. The installer diverged from that evidence and wrote the flat shape,
  so the wiring it installed on 2026-08-25 delivered zero rows.
- Headless runs need `--dangerously-bypass-hook-trust`, because hook trust is
  hash-pinned and untrusted hooks are skipped SILENTLY. A skipped hook looks
  identical to a quiet one: if delivery seems dead under headless codex, check
  trust before debugging the mailbox.

Failure signature: a flat `hooks.json` is skipped with no warning at all, so
 the delivery oracle is the telemetry log (`swarm-heartbeat.log` in the state
 dir), never the absence of an error.

## Install

```
bash adapters/codex/install.sh
```

Idempotent: re-running never duplicates the entry and never clobbers other
hooks. `COMMS_CODEX_HOOKS=<path>` overrides the target file for testing.
