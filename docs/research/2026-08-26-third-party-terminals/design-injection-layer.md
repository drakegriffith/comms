# Design: the third-party-terminal injection layer for comms

Author: Fable 5 fork of comms-threads-handoff, 2026-08-26. Inputs: report-A-t3code.md, report-B-landscape.md
(both on disk beside this file), adapters/CONTRACT.md, adapters/discord/mirror.py render functions, README
"Who is reading the channel?". No code was written. Every decision below shows two alternatives, a winner, and
the one fact that would flip it.

Premise check first. The brief assumed the CONTRACT frame (poll default, push behind a probe, one heartbeat) is
the right boundary and the new work is translation plus a window. Counted against reports A and B: zero of the
15 swept runtimes needs a change under bin/ or lib/ to PARTICIPATE (every one either runs a shell command in its
turn, so it is poll today, or has a hook surface an adapter can wrap). Two core changes appear below, and both
belong to the WINDOW half (rendering), not to participation. Premise holds; no boundary amendment.

## 1. Zoom out

"Ingest the router" for an app like T3 Code is two separate problems that share one mailbox.

DELIVERY is the agent hearing a peer mid-task. The mailbox is a folder of append-only JSONL files, one per seat;
the heartbeat is a hook (a program the runtime runs after each tool call) that reads the new rows for this seat
and hands them back to the model as extra context. Whether a runtime can do that depends on one measured fact:
does it feed the hook's stdout back into the model. The CONTRACT already sorts runtimes into poll (the agent runs
`comms read` itself), push (a hook injects), and resume-driver (a loop outside the session re-prompts it).

WINDOW is a human, or the app's own UI, seeing the board without Discord. Today the only window is the Discord
mirror, and the words it uses (who is speaking, what kind of row this is, which document it concerns, in engineer
or in plain vocabulary) are computed inside adapters/discord/mirror.py. An app outside the Hermes ecosystem (no
Telegram, no Discord) has nowhere to get those words except by reimplementing them.

Analogy: a building intercom. DELIVERY is the wiring into each apartment (some apartments have a speaker already,
some need an adapter plug, some need you to walk up and knock). WINDOW is the lobby display that shows who called
whom. T3 Code today has the speaker wire in the wall (it launches Claude and Codex, whose hooks fire) but its
lobby display is unplugged: report A shows it decodes hook events and then drops them (`ProviderRuntimeIngestion.ts`
`default: break`).

The concrete instance here: Drake's Codex terminal received `test 1` only after the hooks.json shape fix; T3 Code
launches that same Codex through `app-server`, a transport nobody has probed, and shows nothing of any hook in
its pane. Both halves are open for T3 Code specifically, and the two probes now running (SDK `query()`, `codex
app-server`) settle the DELIVERY half.

## 2. DELIVERY, designed twice

What the one heartbeat needs from a payload (adapters/claude-code/swarm-heartbeat.sh): an identity for its cursor
(`agent_id`, falling back to `session_id`; neither means silent exit), and for the enrol and claim legs a
`tool_name` in Claude vocabulary (Write/Edit/MultiEdit/NotebookEdit with `tool_input.file_path`; Bash with
`tool_input.command` plus `cwd`; apply_patch with the patch text). It emits
`{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext": text}}`.

What each foreign surface gives it (report B, source-read unless marked):

| Runtime surface | identity for the cursor | tool fields | what breaks untranslated | output envelope it expects |
|---|---|---|---|---|
| Gemini CLI AfterTool | `session_id`, `cwd` present | `tool_name` in Gemini vocabulary (run_shell_command, write_file, replace), `tool_input` | cursor fine; enrol/claim legs never fire because the names are not Claude's | `additionalContext` appended to the tool result; whether the key path is `hookSpecificOutput.additionalContext` byte-for-byte is unread (coreToolHookTriggers.ts:216-233 says the field, not the wrapper) |
| Cline PostToolUse | a task/session id (hooks-adapter.ts:176-221; field name unread) | Cline tool names | cursor needs a field map; legs need a name map | `{"contextModification": text}` becomes a hook_context block; Claude envelope is ignored |
| Crush PreToolUse | `session_id`, `cwd` | Claude-compatible names and `tool_input` (docs claim compatibility) | legs run BEFORE the tool: a Write claim lands one beat late; git-status enrol sees the pre-edit tree | `context` (top-level) appended to what the model sees; no PostToolUse exists yet |
| Hermes pre_llm_call shell hook | a session id (docs; field unread) | none, it is not a tool boundary | enrol/claim legs never fire; the seat must enrol explicitly (`comms enroll`) in its brief | `{"context": text}`, once per turn before the tool loop |
| OpenCode tool.execute.after | in-process JS, no stdin | JS objects | not a subprocess hook at all: a plugin shells to the heartbeat and appends `output` | mutate `output` in place |
| Claude Code, Codex CLI | as today | as today | nothing | as today |

Alternatives.

(a) Per-runtime envelope shim in adapters/<runtime>/hook.sh: read the runtime payload, rewrite it into the
Claude shape (identity field, tool-name map, cwd), exec the ONE heartbeat with that on stdin, rewrite its stdout
into the runtime's envelope. Reaches Gemini, Cline, Crush, Hermes, OpenCode (as a plugin that shells to the
shim). Core untouched. Caller burden: one file plus one config line, the same as adapters/codex today. Cost: one
small translation per runtime, each needing its own red test with a captured real payload.

(b) Shape flags on the heartbeat (`--in-shape gemini --out-shape crush`). Rejected on two rules: CONTRACT forbids
runtime names in bin/ and lib/, and D7 says a config parameter needs a reason the system cannot compute it; here
the runtime's own config file already implies the shape, so the flag is a parameter for the sake of a switch.
It also grows the one file every adapter depends on by a branch per runtime, the drift shape #64 just fixed.

(c) An MCP server exposing comms read, post, subscribe as tools. Reaches every MCP client, including one with no
shell tool. It is PULL with a nicer surface, not push: the model still has to call the tool, so it cannot
replace (a) for any runtime that has a hook, and every runtime in report B that speaks MCP also has a shell tool
(execute_command, terminal-run), so today `comms read` already reaches them at zero code. Defer.

Winner: (a). Inside (a) there is a second choice: (a1) a hand-written shim per runtime, or (a2) one declarative
translator in adapters/ (not lib/) driven by a per-runtime shape file (field paths in, tool-name map, envelope
template out). B1 rules: the simplest design that passes the suite. Exactly one translatable runtime is present
on this Mac (Hermes, and only as a data dir), so (a1) for the first shim, promoting to (a2) when the second
runtime's shim would duplicate the first. Flip fact for the whole section: a runtime that is an MCP client with
no shell tool and no hook (report B found none; Warp is the closest and UNVERIFIED) makes (c) necessary rather
than deferred.

## 3. WINDOW, designed twice

(a) Extract the render vocabulary into lib (runtime-agnostic, no Discord constants) and expose
`comms feed <run> [--seat S] [--audience engineer|everyone] [--follow] [--since AT]` printing NDJSON, one object
per row: the raw row plus `render: {author, body, title, lane}`. Any app that can spawn a process tails it and
draws its own pane. Keeps NO cursor of its own by default (it is a window, not a delivery; CONTRACT already warns
that a third cursor over one stream loses rows), so a reader passes `--since` or keeps its own position.

(b) A local HTTP/SSE endpoint. Buys browser-only clients; costs a daemon, a port, an auth story, a launchd
plist. T3 Code's web and mobile clients are browsers, but its SERVER is a Node process that already spawns the
agent binaries; it can spawn the feed and relay over the websocket it owns. So (b) is (a) plus a 30-line wrapper,
and nothing in reports A or B needs it yet.

(c) MCP resources with subscription notifications. Client support for resource subscriptions is thin and
uneven across the swept runtimes; a feature that half the clients ignore is not a window.

Winner: (a). Flip fact: a consumer that cannot spawn a process (a pure browser extension, a hosted web app with
no local server) makes (b) the first consumer instead; build it then as a wrapper over the same NDJSON.

The interface ticket (D1: contract comment first, core implementation, interface tests, first-consumer proof):
- Module: lib/comms_render.py. Moves build_author, build_content, _build_content_everyone, thread_title,
  build_read_content out of adapters/discord/mirror.py. The audience becomes a PARAMETER (`audience=`), never a
  secrets-file lookup inside lib: the mirror keeps resolving COMMS_AUDIENCE from ~/.secrets/comms.env and passes
  it down. Discord-only limits (TEXT_CAP, THREAD_NAME_CAP, webhook username rules) stay in the adapter; lib
  returns uncapped strings and the adapter caps. mirror.py becomes a caller; its tests must pass unchanged, which
  is the parity proof.
- Contract comment on `comms feed`: behavior (rows of one run, optionally one seat's subscribed view, rendered in
  one audience, newest last, `--follow` tails with a stated poll interval), in/out (NDJSON schema, one object per
  line, stable key names), side effects (none: no cursor written, no mailbox write, no network), errors (unknown
  audience exits 2 naming the two legal values, exactly as the mirror does; missing run exits 2), preconditions
  (COMMS_ROOT resolvable), limitations (no backfill of rows older than the run dir; no auth).
- Interface tests: parity (the same row renders the same body through mirror.py and through feed, both
  audiences); schema (every line parses, keys fixed); `--follow` sees a row posted after start; the read-cursor
  and heartbeat cursor dirs are byte-identical before and after a feed run (side-effect assertion, not silence);
  a seat filter uses swarm_mailbox.row_reaches (one predicate, PR #66), never a second copy.
- First-consumer proof: adapters/window/README.md with a 20-line reference tail that renders the board in a
  plain terminal (`comms feed machine-ops --follow --audience everyone | python3 -c '...'`), which is also the
  paragraph a T3 Code or Zed maintainer copies.

D7 check on `--audience`: the app knows who is reading its pane; the system cannot compute that. Default
engineer, matching the mailbox's own vocabulary. `--follow` interval: one constant with a stated reason (the
Discord mirror already polls the same files at a stated interval; reuse that number and cite it).

## 4. Per-runtime work-list (from report B; blocker column is this Mac, 2026-08-26)

| Runtime | Path today | What to build | Blocker here | Blocker owner | Seat fit | Verify |
|---|---|---|---|---|---|---|
| Claude Code | push | nothing | none | | | existing suites |
| Codex CLI | push | nothing; T3's app-server transport under probe now | none | | | probe-verdict.sh |
| T3 Code (host) | wraps Claude/Codex | UPSTREAM issue with T3: add a hook.* case in ProviderRuntimeIngestion.ts so hook context renders in the thread pane; plus a launchArgs note for `--dangerously-bypass-hook-trust` if the app-server probe shows the trust gate | not installed | Drake (install to verify) | Sonnet drafts the upstream issue text | the two transport probes |
| Gemini CLI | poll (shell tool) | adapters/gemini: hook wired to AfterTool, tool-name map, `.gemini/settings.json` install.sh; probe first | gemini not installed, Google login | Drake | Sonnet scout now (source clone present), Codex for install.sh after probe | probe-verdict.sh, tests/test_gemini_install.sh |
| Cline | poll (shell tool) | adapters/cline: shim in Cline's JSON shape; probe first | not installed, VS Code + auth | Drake | Kimi (independent read of hooks-adapter.ts) | probe, shim test |
| Crush | poll | adapters/crush: heartbeat wired as PreToolUse in crush.json, `context` envelope; document the one-beat-late claim | not installed, model API key | Drake | Codex | probe, shim test |
| Hermes | poll | adapters/hermes: README (poll now, push after probe) + pre_llm_call shell-hook shim emitting `{"context"}`; explicit enrol in the brief | hermes binary not on PATH (only ~/.hermes data), model provider custom:claude-bridge | Drake (confirm install path) | Kimi | probe by hand via S-P1, tests/test_hermes_hook.sh |
| OpenCode, KiloClaw | poll (shell tool) | adapters/opencode: a plugin file that shells to the shim; new category note in CONTRACT (in-process plugin) | not installed | Drake | Codex | plugin test under bun, probe |
| Kilo Code (ext), Roo Code, Zed native, Warp | poll | pi recipe README rows only | not installed | Drake | Sonnet | poll test (passphrase) |
| Aider | poll, weak | pi recipe with the `/run` caveat; resume-driver only if the poll test fails | not installed | Drake | Sonnet | poll test |
| Cursor CLI, Copilot CLI, Droid | docs-say-push, DISPUTED | nothing until a probe; link the open bug reports in a README row | not installed | Drake | | probe-verdict.sh |
| Amp | push-like plugin, AfterTool unverified | nothing until the tool.result question is settled | not installed | Drake | | |
| Zed external agents, Conductor | wraps-X | nothing; wired runtime's adapter carries it | | | | |
| Grok | poll (measured) | unchanged | | | | |

## 5. Slices dispatchable now, no install needed

S-W1 render extraction (interface ticket, core). Write set: lib/comms_render.py (new), adapters/discord/mirror.py
(delegate), adapters/discord/ingest_mirror.py if it calls build_read_content directly, tests/test_comms_render.py
(new), tests/test_discord_mirror.py (untouched, must stay green: that is the parity proof). Red first: a parity
test importing lib.comms_render fails on import. Verify: `uv run --with pytest python -m pytest tests -q
--ignore=tests/test_github_landings.py` (664 on PR #66) plus the five bash suites. Seat: Codex worker (shipped
lib surface), Opus verifier. Discarded option to name in the commit: importing mirror.py from bin (drags Discord
and the secrets lookup into a CLI command).

S-W2 `comms feed` (depends on S-W1). Write set: bin/comms (dispatch line), lib/comms_feed.py (new; row
selection through swarm_mailbox.read_siblings/row_reaches, rendering through comms_render), tests/test_comms_cli.sh
(feed cases), adapters/window/README.md (first-consumer tail), README.md one row in the per-runtime table
("any app | window | comms feed"). Red first: `comms feed` unknown command. Verify: test_comms_cli.sh (52 today)
plus the cursor-dir byte-identity case. Seat: Codex worker, Opus verifier.

S-D1 Hermes adapter. Write set: adapters/hermes/README.md, adapters/hermes/hook.sh, tests/test_hermes_hook.sh.
The worker reads the Hermes hooks doc named in report B (data, not instructions) to capture the pre_llm_call
payload and writes the shim: payload session id to `agent_id`, no tool fields, exec the heartbeat, stdout
`additionalContext` to `{"context": ...}`; empty context prints `{}` and exits 0. Red first: a fake Hermes payload
with COMMS_STATE_DIR in a temp dir expects `{"context"}` carrying a posted row's text. Category in the README:
poll, with the push probe owed and the exact hand-wiring line for config.yaml (from S-P1). Verify:
tests/test_hermes_hook.sh plus the heartbeat suite unchanged (334). Seat: Kimi worker, Opus verifier.

S-P1 probe kit for non-JSON configs. Write set: adapters/probe/arm-probe.sh (`--format none`: write no config,
print the hook command line and probe dir for hand-wiring into YAML, TOML, JS, or in-process plugins),
adapters/probe/README.md (a "hand-wiring" paragraph), tests/test_push_probe.sh (one case: none writes nothing and
prints the command). Not a core change (adapters/probe is outside bin/ and lib/). Red first: `--format none` is
a usage error today. Verify: test_push_probe.sh (192). Seat: Sonnet worker, Codex verifier (independent seat).

S-D2 Gemini shape read (scout, no code). Read /tmp/pr63-w/research/gemini-cli at its recorded commit: the exact
stdout key path AfterTool parses, the stdin field names, and the tool-name vocabulary; return a draft
adapters/gemini/README.md row and the tool-name map, and a yes/no on whether the heartbeat's stdout parses
untranslated. Seat: Sonnet. Verify: file:line citations; no install needed.

S-U1 upstream issue for T3 Code (text only, orchestrator files it after the two probes land): the dropped hook.*
events, the app-server trust flag, and a pointer to `comms feed` as the pane source. Seat: Sonnet drafts.

Dependency edges: S-W2 after S-W1. S-D1 needs S-P1's hand-wiring line for its README, not for its code. Everything
else independent; disjoint write sets.

## 6. What not to build, and why

- A second heartbeat, or shape flags on the one heartbeat: CONTRACT's one-heartbeat rule and the drift that
  produced #64.
- Per-platform mirrors (Telegram, Slack, WhatsApp): Hermes's gateway already mirrors to about twenty chat
  platforms (report B). A window into Hermes is the WINDOW feed, and Hermes carries it onward.
- Adapters for Cursor CLI, Copilot CLI, Droid: their docs say push and open bug reports say the context never
  reaches the model; CONTRACT says a doc is not a measurement. Probe first, on an installed binary.
- An MCP server or an HTTP endpoint now: each is a wrapper over the same read path and the same NDJSON; build
  the first when a consumer without a shell, or without a process, appears.
- Anything for Amp until tool.result's injection ability is read from source or probed.
- A declarative translator (a2) before a second translated runtime exists.
