# T3 Code: can the comms router deliver into it, and how would a row render

## Premise check

The brief's premise is TRUE with one important qualifier. T3 Code (canonical repo
confirmed at `github.com/pingdotgg/t3code`, MIT, ~200k users per its own AGENTS.md)
is a server that spawns provider agent runtimes as subprocesses -- for two of its
five backends (Claude, Codex) those subprocesses ARE the same vendor CLI/SDK the
comms adapters already target, and by default T3 Code neither overrides HOME nor
passes a programmatic `hooks:` option, so the on-disk hook config
(`~/.claude/settings.json`, `~/.codex/hooks.json`) is read the normal way. But T3
Code does not run "the Claude Code CLI" or "the Codex CLI" as a bare subprocess --
it runs the **Claude Agent SDK's `query()`** (which itself spawns the `claude`
binary) and **Codex's own `app-server` JSON-RPC mode** (via a wrapper package,
`effect-codex-app-server`) -- two different wire protocols than the ones the
existing comms push probes were measured against (raw CLI, `-p`/headless).
Nobody has re-run the push probe against either of those specific transports, so
"push works inside T3 Code" is inherited by strong inference, not re-measured.
Its other three backends (Cursor, Grok, OpenCode) do not go through Claude- or
Codex-shaped hooks at all inside T3 Code: Cursor and Grok run over ACP (Agent
Client Protocol), a different wire protocol with no `PostToolUse`/`additionalContext`
concept, and OpenCode talks to its own server. Comms' existing "grok" row (poll,
measured against the bare `grok` CLI) does not describe T3's Grok backend.

Clone: `git clone --depth 50 https://github.com/pingdotgg/t3code.git` into
`/tmp/pr63-w/research/t3code`. HEAD `33b650a5b3b27382b35d2182dec6b22438c3da56`,
authored 2026-08-26 18:35:18 -0700 (today), confirmed as `origin` HEAD by the
clone itself (a fresh depth-50 clone's HEAD is origin's HEAD at fetch time).

## Table

| Runtime / surface | Version or commit read (origin HEAD, dated) | How it runs the model | Tool-boundary hook surface | stdout injected into model's next turn? | MCP client? | UI surface for an inbound row | Verdict | Evidence |
|---|---|---|---|---|---|---|---|---|
| T3 Code (host app, all backends) | pingdotgg/t3code@33b650a5b3b27382b35d2182dec6b22438c3da56, 2026-08-26 | Node/Effect-TS server (apps/server); 5 built-in "provider drivers," one per backend, each wrapping that vendor's own CLI/SDK/server as a child process -- GROUNDED | None of its own outside the backend it wraps -- "Adding a driver means writing the driver plus adapter... No orchestration, contract, or client change is required," i.e. new integrations are a source change, not a runtime plugin API -- GROUNDED | n/a (see per-backend rows) | Yes, but only as MCP host: it hands its own "t3-code" HTTP MCP server to the agent for tool access into T3's environment (ClaudeAdapter.ts:4335-4342); found no config surface for a user-supplied external MCP server, and no MCP-client role for T3 itself -- GROUNDED for what exists, UNVERIFIED-by-absence for "no user MCP config" | Transcript/timeline pane per thread (web/desktop/mobile all render the same canonical item.*/tool.* event stream) is the only rendering surface found; a defined-but-unwired hook.* event type exists (see Claude row) that could become a dedicated lane -- GROUNDED | n/a | docs/internals/overview.md, docs/internals/providers.md, AGENTS.md |
| -> Claude backend (claudeAgent driver) | @anthropic-ai/claude-agent-sdk ^0.3.170 pinned in apps/server/package.json:25, read at the same HEAD | Wraps the Claude Agent SDK's query(), which itself spawns the real claude binary (pathToClaudeCodeExecutable, resolved in Drivers/ClaudeExecutable.ts) -- not the bare Claude Code CLI invoked directly -- GROUNDED (ClaudeAdapter.ts:9-24 imports query, Options; :4276-4278 pathToClaudeCodeExecutable: claudeBinaryPath) | settingSources: ["user","project","local"] is passed explicitly to query() (ClaudeAdapter.ts:4312, constant at :1244-1248), and no programmatic hooks: option is set anywhere in the built queryOptions object (ClaudeAdapter.ts:4276-4320, read in full) -- so hook config is whatever ~/.claude/settings.json (user), .claude/settings.json (project), .claude/settings.local.json (local) contain, same schema/path as the standalone CLI. HOME is not overridden; per-account isolation uses CLAUDE_CONFIG_DIR instead, and it is empty ("uses Claude Code's normal config directory") unless the user opts into a second account -- GROUNDED (Drivers/ClaudeHome.ts:17-35, docs/user/providers-claude.md:16-33) | UNVERIFIED (inferred, not re-measured). Two supporting facts, neither is the measurement itself: (1) the SDK's message stream carries first-class hook_started/hook_progress/hook_response messages that T3 Code decodes into its own hook.started/hook.progress/hook.completed runtime-event type (ClaudeAdapter.ts:3196-3229, payload includes stdout/output), proving hooks DO fire and their stdout DOES reach the SDK consumer; (2) comms' own push verdict for Claude Code (exit 0, PUSH, 2026-08-21/2026-08-25) was measured against the bare claude CLI ("Claude Code 2.1.246" in adapters/probe/README.md), not against @anthropic-ai/claude-agent-sdk query(). Whether the SDK's internal additionalContext injection into the model's next turn behaves identically when driven through query() instead of the interactive CLI has not been probed here. Separately, and grounded either way: the hook.* events T3 Code DOES decode are then dropped -- ProviderRuntimeIngestion.ts's event-type switch has no "hook.started"/"hook.progress"/"hook.completed" case and falls through default: break; return [] (:883-887), so today nothing downstream of the adapter turns a hook firing into a persisted item or a UI-visible row, even though the adapter already parsed it | Yes, T3-owned "t3-code" HTTP server only (see host row) | Same per-thread transcript pane as everything else -- IF hook.* were wired into the projector, which it currently is not (see previous column) | wraps-Claude Code, with the caveats above | apps/server/src/provider/Layers/ClaudeAdapter.ts:9-24,1244-1248,3196-3229,4276-4320; apps/server/src/provider/Drivers/ClaudeHome.ts:17-35; apps/server/src/orchestration/Layers/ProviderRuntimeIngestion.ts:855-887; docs/user/providers-claude.md:16-33; comms adapters/probe/README.md |
| -> Codex backend (codex driver) | effect-codex-app-server (workspace package, version not independently pinned in apps/server/package.json:47 -- workspace:*); underlying codex binary version is whatever the user's codex install reports, T3 Code does not vendor it | Spawns the user's own codex binary in app-server mode (codexAppServerArgs = ["app-server", ...], codexLaunchArgs.ts:13-16; spawn command logged as ${options.binaryPath} app-server, CodexSessionRuntime.ts:1163) -- a JSON-RPC server mode, not codex exec and not the interactive TUI comms' probe used | CODEX_HOME env is only set when the user configures a non-default homePath/shadowHomePath (CodexSessionRuntime.ts:1140, CodexProvider.ts:332); default is unset, so codex reads its normal ~/.codex/hooks.json -- the exact file adapters/codex/install.sh writes. When a "shadow home" (multi-account overlay) IS configured, materializeCodexShadowHome symlinks every top-level entry from the real home into the shadow home EXCEPT auth.json/models_cache.json (private) -- hooks.json is not in that private set, so it is symlinked through and still visible (CodexHomeLayout.ts:32-33,373-408). Codex's own app-server wire protocol has native hook/started/hook/completed notification methods (CodexSessionRuntime.ts:736,738), confirming the underlying mechanism exists in this transport too | UNVERIFIED, with a specific named risk. hook/started/hook/completed notifications are read ONLY for thread-id bookkeeping in two switch statements (CodexSessionRuntime.ts:725-750 and :819-866, both falling to a default that just extracts an id or nothing) -- they are never turned into any ProviderRuntimeEvent, unlike Claude's adapter, so T3 Code's UI has zero visibility into a codex hook firing even if the underlying process does the injection. Named risk: comms' own Codex adapter README states "Headless runs need --dangerously-bypass-hook-trust, because hook trust is hash-pinned and untrusted hooks are skipped SILENTLY." grep for trust/bypass across T3's Codex driver/adapter/session-runtime files returned zero hits -- T3 Code does not pass that flag itself. A user COULD add it via the per-instance launchArgs setting (codexLaunchArgs.ts tokenizes and forwards arbitrary launchArgs into app-server's argv), but nothing in T3 Code does this automatically, and whether app-server mode's trust gate behaves like headless codex exec -p at all is itself unverified | Not found in files read | No rendering path at all currently, since the event never becomes a canonical item (see previous column) | wraps-Codex, unverified push, with a real risk that hook trust is silently ungated in this transport | apps/server/src/provider/Layers/CodexSessionRuntime.ts:725-750,819-866,1120-1163; apps/server/src/provider/Drivers/CodexHomeLayout.ts:19-34,320-415; apps/server/src/provider/Layers/codexLaunchArgs.ts; docs/user/providers-codex.md:1-25; comms adapters/codex/README.md |
| -> Cursor backend (cursor driver) | cursor-agent CLI, version not vendored/pinned by T3 Code | ACP (Agent Client Protocol) client against the cursor-agent CLI's own ACP server mode (CursorDriver.ts:1-11 doc comment: "Cursor exposes an ACP-based CLI"; acp/CursorAcpSupport.ts, acp/CursorAcpExtension.ts, acp/CursorAcpCliProbe.test.ts) | None found. grep -n hook across CursorDriver.ts and every acp/*.ts file: zero hits. ACP is a JSON-RPC protocol between an editor/host and an agent; it has no PostToolUse-shaped hook concept in what was read here | No (asserted absence per the same standard comms' CONTRACT.md applies to kimi: no mechanism to run a command on a tool-call boundary was found in the driver or the ACP support files) | Not determined from files read | Not determined (same transcript pane presumably, unconfirmed for Cursor specifically) | poll or no-adapter (asserted absence of a hook surface; would need the same kind of probe kimi got, run against cursor-agent's ACP mode specifically, not against a bare cursor-agent CLI) | apps/server/src/provider/Drivers/CursorDriver.ts:1-11; apps/server/src/provider/acp/CursorAcpSupport.ts, CursorAcpExtension.ts |
| -> Grok backend (grok driver) | grok/Grok Build CLI, version not vendored/pinned by T3 Code | Also ACP, against xAI's Grok Build CLI (acp/GrokAcpSupport.ts, acp/GrokAcpCliProbe.test.ts, acp/XAiAcpExtension.ts) -- a different transport than the bare grok -p ... headless CLI comms already measured (2026-08-25, grok 0.2.106, poll/NOT-PUSH) | None found (same grep -n hook sweep across GrokDriver.ts and acp/*.ts: zero hits) | No (asserted absence, same standard as Cursor above -- and separately, comms' own existing grok row is a measured negative for a DIFFERENT transport, so it cannot be reused as evidence for T3's ACP-mode Grok without a fresh probe) | Not determined | Not determined | poll or no-adapter; comms' existing "grok: poll, NOT-PUSH" row does not transfer to this transport as-is | apps/server/src/provider/Drivers/GrokDriver.ts; apps/server/src/provider/acp/GrokAcpSupport.ts, XAiAcpExtension.ts; comms adapters/grok/README.md |
| -> OpenCode backend (opencode driver) | OpenCode, version not vendored/pinned by T3 Code | Talks to an OpenCode server process (own or a configured serverUrl) -- own protocol, not ACP, not Claude/Codex-shaped (OpenCodeDriver.ts:1-13 doc comment) | None found (grep -n hook OpenCodeDriver.ts: zero hits) | No (asserted absence, not measured) | Not determined | Not determined | poll or no-adapter (asserted absence); comms has no existing OpenCode row to compare against -- prior-art gate flagged "OpenCode is documented as CLAUDE.md-compatible" as an unchecked lead, and that claim is about markdown-file conventions, not about a hook/event mechanism, so it does not resolve this row | apps/server/src/provider/Drivers/OpenCodeDriver.ts:1-13 |

## Where per-session identity lives (for an adapter's enrol step)

GROUNDED. Orchestration contracts define ThreadId (durable conversation,
survives resume) and TurnId/ProviderInstanceId (packages/contracts/src/providerRuntime.ts,
imported throughout ClaudeAdapter.ts/CodexSessionRuntime.ts). cwd is
tracked per project as workspaceRoot (apps/server/src/project/ProjectSetupScriptRunner.ts:135-181,
RepositoryIdentityResolver.ts) and is passed into both the Claude query()
options (cwd: input.cwd, ClaudeAdapter.ts:4277) and Codex's turn start. The
vendor-native session id is also captured: Claude's resumeSessionId/sessionId
(ClaudeAdapter.ts ClaudeSessionContext.resumeSessionId) and Codex's
app-server thread.id (CodexSessionRuntime.ts readNotificationThreadId,
case "thread/started"). Any of (threadId, cwd) or the vendor session id
would work as an enrol key; the durable state.sqlite these project onto lives
under ~/.t3/userdata by default, or <worktree>/.t3/userdata in a T3 Code dev
worktree (AGENTS.md, "Test data" section) -- GROUNDED for the dev-worktree
path, UNVERIFIED for the exact production DB path/schema (did not open the
sqlite file or its migration DDL).

## What would have to be built

- Claude backend: nothing new to reach the model, if the existing
  adapters/claude-code/install.sh target (~/.claude/settings.json, or the
  per-instance CLAUDE_CONFIG_DIR if the user runs a second account) is where
  the heartbeat gets wired -- but re-run adapters/probe/ against a Claude
  session started through query() in T3 Code specifically before declaring
  push, since the only PUSH measurement on record used the bare CLI. To render
  an inbound row in T3's own UI (as opposed to just reaching the model), the
  hook.* case needs a real branch in ProviderRuntimeIngestion.ts's switch
  (today: default: break) that turns a hook firing into a persisted item the
  projector and client will show.
- Codex backend: same "nothing new to reach the model" claim, weaker --
  first confirm codex app-server mode honors hook trust the same way headless
  codex exec -p does (comms' own README already flags trust as silently
  fail-open), and if not, get T3's per-instance launchArgs field to carry
  --dangerously-bypass-hook-trust (or whatever app-server's equivalent is).
  Then, same as Claude, wire hook/started/hook/completed into a real
  runtime-event case (today they are read only for id bookkeeping) before any
  row can render.
- Cursor / Grok / OpenCode backends: no hook surface was found in T3
  Code's own driver/adapter code for any of the three. This is the
  adapters/pi/-style default: poll, i.e. brief the agent to run
  bin/comms read <runid> <seat> itself -- IF the underlying ACP/OpenCode
  session can run an arbitrary shell command in its own turn (not verified
  here; would need the same poll-test probe CONTRACT.md describes, run against
  each CLI's ACP/server mode specifically). If it cannot, this is
  resume-driver territory or no adapter at all, same as kimi.
- Rendering, host-app-wide: T3 Code's per-thread transcript pane is the
  only inbound-row surface found across every backend; there is no
  toast/notification/side-panel system identified for provider events, and no
  user-facing MCP-server registration surface a comms adapter could piggyback
  on (T3 only offers its own MCP server outward, to the agent).

## Open questions

- Does @anthropic-ai/claude-agent-sdk query()'s hook execution path
  actually call the injection probe's positive-control mechanism
  (hookSpecificOutput.additionalContext) the same way the bare CLI does?
  Not settled here -- would need adapters/probe/arm-probe.sh run against a
  T3-Code-launched Claude session (or, more cheaply, against a standalone
  script that calls query() directly with the same settingSources).
- Does codex app-server apply the same hash-pinned hook-trust gate as
  headless codex exec? If yes and unbypassed, Codex-inside-T3-Code hooks may
  be silently skipped even though ~/.codex/hooks.json is visible and the
  wire protocol has hook/started/hook/completed notifications.
- Whether cursor-agent, Grok Build, and OpenCode's ACP/server modes can run
  an arbitrary shell command inside their own turn (the poll-test membership
  check) was not determined -- no evidence either way was found in the files
  read, only the absence of a hook mechanism.
- Whether T3 Code's .mcp.json at the repo root (present in the clone, not
  read) or its Claude mcpServers merge behavior (ClaudeAdapter.ts:4335-4342
  supplies exactly one server, "t3-code", when mcpSession is set) fully
  replaces or adds to any user-configured .mcp.json/settings-level MCP
  servers was not resolved -- tangential to delivery but relevant if a comms
  adapter were ever built as an MCP server instead of a hook.
- The production ~/.t3/userdata/state.sqlite schema (exact column names for
  thread/session identity) was not opened; the dev-worktree path and the
  contract-level ThreadId/TurnId types were used instead, which should be
  sufficient for an adapter's enrol key but were not cross-checked against the
  DB.
