# Hook events are decoded but dropped before the UI; Codex hooks are untrusted by default

## Summary
T3 Code's Claude and Codex drivers already decode `hook.*`/`hook/*` events off
the wire, but the ingestion layer discards them before they reach a UI row,
and the Codex `app-server` transport runs with hook trust unset, so a
configured hook silently never fires with no warning anywhere. Two small,
additive changes fix both.

## What we measured
- Claude Agent SDK `query()`: **PUSH**, positive control passed. A
  `PostToolUse` hook fired, its `additionalContext` reached the model's next
  turn, model echoed a planted passphrase verbatim
  (`@anthropic-ai/claude-agent-sdk` 0.3.247).
- Codex `app-server`: **PUSH, conditional.** Fires only when the client sets
  `thread/start` params `config: {"bypass_hook_trust": true}`. Without it:
  `hooks/list` shows the hook loaded/enabled/matched with `trustStatus:
  "untrusted"`, it never fires, no warning on the wire (codex-cli 0.148.0).
  `--dangerously-bypass-hook-trust` (works on `codex exec`) does not exist on
  `app-server`'s argv; `-c bypass_hook_trust=true` on argv has no effect.
- T3 Code sets neither. `buildThreadStartParams`
  (`apps/server/src/provider/Layers/CodexSessionRuntime.ts:523-538`) builds
  `thread/start` with `cwd`/`approvalPolicy`/`sandbox`/`approvalsReviewer`/
  `model`/`serviceTier` only — no `config` key, so no path sets it today.

## Two small changes
1. Wire `hook.*`/`hook/*` into the ingestion switch. Claude's adapter already
   decodes SDK hook messages into `hook.started`/`hook.progress`/
   `hook.completed` (`ClaudeAdapter.ts:3196-3229`); Codex's
   `readNotificationThreadId` switch already sees `hook/started`/
   `hook/completed` (`CodexSessionRuntime.ts:724-750`) but only extracts a
   thread id. Either way the ingestion switch has no `hook.*` case and falls
   to `default: break; return [];` (`ProviderRuntimeIngestion.ts:883-887`),
   so nothing persists. Mapping `hook.completed`/`hook/completed`
   (`run.entries[].{kind:"context", text}`, confirmed on the wire) to a
   transcript item is additive: the data is already parsed, just discarded.
2. Make hook trust visible or settable per thread: let a user opt a thread
   into `config.bypass_hook_trust: true` in `buildThreadStartParams` (the
   confirmed-working lever), or surface `hooks/list` `trustStatus` (`managed
   | untrusted | trusted | modified`) so a dead hook isn't silent.

## Why it matters
Not specific to us. Any tool shipping a `PostToolUse`-style hook (ours
injects `additionalContext` via one) hits this in T3 Code: works but
invisible on Claude, invisible and silently dead on Codex unless trusted
outside T3 Code first. A user can't tell "not wired to the UI" from "not
running" from "misconfigured" — a support cost for every hook author.

## Repro
`CODEX_HOME=<home w/ hooks.json>` `codex app-server` → `initialize` →
`hooks/list` (`trustStatus: "untrusted"`) → `thread/start {cwd,
approvalPolicy, sandbox}` (no `config.bypass_hook_trust`) → `turn/start`
(hook never fires, no warning). Repeat with `thread/start` params
`config: {"bypass_hook_trust": true}`: hook fires,
`hook/completed.run.entries[].text` carries the injected content.

## Attachments (available on request)
Codex `app-server` push-probe report (wire logs, both arms); Claude Agent SDK
`query()` push-probe report (positive control).
