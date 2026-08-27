Probe complete. The report file was not written to disk (my harness blocks agent-written `.md` report files); here it is in full, as the brief's return contract requires.

---

# Push probe against `codex app-server` (the transport T3 Code launches Codex through)

Run 2026-08-26 · worker seat: Opus 5 via Agent tool · brief `/tmp/pr63-w/probes/brief-appserver.md`
All evidence under `/tmp/pr63-w/probes/appserver/`. Nothing outside that directory was written.

## Verdict line

```
PUSH        exit 0    codex-cli 0.148.0, transport `codex app-server` (JSON-RPC over stdio), 2026-08-26
            CONDITIONAL on a per-thread hook-trust bypass (see "Hook-trust controls" below).
            Without that bypass: exit 2, COULD-NOT-DETERMINE (hook loaded, never fired).
```

Zoom out, in plain English: the question was whether a hook script's stdout gets fed back into the model's own turn when Codex is driven as a JSON-RPC *server* (the way the T3 Code desktop app drives it) rather than as the `codex exec` command line the earlier probe measured. Answer: yes, it does — the model read back the secret passphrase the hook printed. But only after the client explicitly turned off Codex's hook-trust check on that thread. Left alone, the hook sits there loaded, enabled, and silently never runs.

Analogy: a fire alarm wired into the building and switched on, but the panel is in "unverified device" mode. Nothing is broken, nothing warns you, and it will never ring. `hooks/list` is the panel readout that tells you which mode you are in.

## 1. Version

```
$ codex --version
codex-cli 0.148.0
```
Binary: `/Users/drakegriffith8/.local/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex`

## 2. Hook-trust controls that exist on this transport

Read from `codex app-server --help` (full), `codex --help`, `codex exec --help`, and the protocol schema emitted by `codex app-server generate-json-schema --out /tmp/pr63-w/probes/appserver/schema`.

| Control | Where it exists | Effective on app-server? |
| --- | --- | --- |
| `--dangerously-bypass-hook-trust` | `codex --help` line 99 and `codex exec --help` line 65 | **Not present** in `codex app-server --help`. app-server's option list is `-c/--config`, `--enable`, `--disable`, `--code-mode-host`, `--strict-config`, `--listen`, `--stdio`, `--analytics-default-enabled`, `--ws-*`. No hook or trust flag at all. |
| `-c bypass_hook_trust=true` (global config override on app-server argv) | accepted; server starts, no error | **No observed effect.** The bypass warning notification that the working lever emits was absent (`grep -c '"method": "warning"' wire-argvcheck.jsonl` → 0). Evidence is an absence at the same `thread/start` boundary where the working lever produces a warning, so: suggestive, not a fired-hook measurement. |
| `thread/start` params `config: {"bypass_hook_trust": true}` (the params' `config` field is a free-form object, `ThreadStartParams.json`) | **This is the one that works.** | On `thread/start` the server emits `{"method":"warning","params":{"message":"`--dangerously-bypass-hook-trust` is enabled. Enabled hooks may run without review for this invocation."}}` and the hook then fires. |
| `hooks/list` request (`ClientRequest.json` → `hooks/list`, response `HooksListEntry` → `HookMetadata.trustStatus`, enum `managed \| untrusted \| trusted \| modified`) | read-only oracle | Not a control, but it is the cheap pre-flight that tells you a hook is loaded-but-untrusted before you spend a turn. |
| `hooks.state` config key + `config/batchWrite` | present in the binary's TUI strings (`config/batchWrite failed while updating hook trust in TUI`, `failed to write hook trust`) | the persist-trust route the interactive TUI uses. Not exercised here (two-run budget spent on the bypass route, which is the one a headless client can use). |

Supporting string evidence in the binary, in `app-server/src/*` frames: `bypass_hook_trust`, `` `bypass_hook_trust` override must be a boolean ``.

Protocol facts worth carrying forward:
- `HookEventName` enum is camelCase on the wire (`postToolUse`), while the `hooks.json` file key stays `PostToolUse`. The wrapped file shape was accepted unchanged.
- `ThreadItem` has a `hookPrompt` variant carrying `HookPromptFragment`s — the injection has a first-class item type, not just an invisible context splice.
- `hook/completed` carries the injected text inline as `run.entries[].{kind:"context", text:...}` (verbatim payload below).

## 3. Run 1 — no trust control

Command: `perl -e 'alarm 240; exec @ARGV' -- python3 drive.py run1`
(`drive.py` sets `CODEX_HOME=/tmp/pr63-w/probes/appserver/home`, `COMMS_STATE_DIR=/tmp/pr63-w/probes/appserver/state`; spawns `codex app-server`; `initialize` → `initialized` → `hooks/list` → `thread/start` {cwd `/tmp/pr63-w/probes/appserver/work`, approvalPolicy `never`, sandbox `workspace-write`} → `turn/start`.)

**Positive control: FAILED — read first, before the answer.**
`/tmp/pr63-w/probes/appserver/probe/stdin-copy.json` did not exist after the run (directory held only `armed-at`, `event`, `passphrase`, `state/`). No `hook-stdout.json`, no `fires.jsonl`.

Hook notification counts, stated grep:
```
$ grep -c '"method": "hook/started"'   wire-run1.jsonl   → 0
$ grep -c '"method": "hook/completed"' wire-run1.jsonl   → 0
```

The oracle that names the cause — `hooks/list` for cwd `/tmp/pr63-w/probes/appserver/work`, from `summary-run1.json`:
```json
{"key": "/private/tmp/pr63-w/probes/appserver/home/hooks.json:post_tool_use:0:0",
 "eventName": "postToolUse", "handlerType": "command",
 "command": "bash /Users/drakegriffith8/code/comms/adapters/probe/push-probe-hook.sh /tmp/pr63-w/probes/appserver/probe",
 "matcher": "*", "source": "user", "enabled": true, "isManaged": false,
 "currentHash": "sha256:97df781a8164a4d40029283bc0769978464be695e31b068d0da3dd1c5adf4693",
 "trustStatus": "untrusted"}
```
Loaded, enabled, correct event, correct matcher, `warnings: []`, `errors: []` — and `untrusted`, therefore skipped, with no warning on the wire.

Verdict: **COULD-NOT-DETERMINE, exit 2.** Run 2's fire wrote into the same shared probe dir, so the exit code was produced by re-running the helper against a reconstruction of the post-run-1 directory state (`/tmp/pr63-w/probes/appserver/run1-control-snapshot/`, holding exactly the four entries listed above), with run 1's own answer file:
```
$ bash ~/code/comms/adapters/probe/probe-verdict.sh /tmp/pr63-w/probes/appserver/run1-control-snapshot \
      --answer-file /tmp/pr63-w/probes/appserver/agent-answer-run1.txt
COULD-NOT-DETERMINE
  reason: the hook never fired: no stdin copy at .../stdin-copy.json
  The agent answer was NOT read. ...
EXITCODE=2
```

Agent answer, run 1, verbatim (**not admissible** — the control failed; recorded only to show the trap):
```
I'll run exactly that command and inspect only its output.
NOTHING-APPEARED
```
This is the grok trap in a new coat. Read answer-first, run 1 is a clean measured negative for `codex app-server`. It is not a result at all.

## 4. Two cheap non-turn acceptance checks (no model tokens spent)

Both ran `initialize` → `hooks/list` → `thread/start` and stopped before `turn/start` (`PROBE_NO_TURN=1`), so neither consumed a probe run.

- `argvcheck`: `codex app-server -c bypass_hook_trust=true`. Server started, thread started, **no** bypass warning. `trustStatus` still `untrusted`.
- `cfgcheck`: `thread/start params.config = {"bypass_hook_trust": true}`. Thread started, **warning emitted**: `` `--dangerously-bypass-hook-trust` is enabled. Enabled hooks may run without review for this invocation. ``

That warning is what selected the lever for run 2.

## 5. Run 2 — with the trust bypass

Command: `PROBE_THREAD_CONFIG='{"bypass_hook_trust": true}' perl -e 'alarm 240; exec @ARGV' -- python3 drive.py run2`

**Positive control: PASSED — read first.**
`/tmp/pr63-w/probes/appserver/probe/stdin-copy.json`, the event and tool names it carries:
```json
{"session_id": "01a04125-0e00-7321-a608-3fe98c6200f6",
 "turn_id": "01a04125-0e88-7762-bf23-d1972d4f7a0a",
 "cwd": "/tmp/pr63-w/probes/appserver/work",
 "hook_event_name": "PostToolUse",
 "tool_name": "Bash",
 "tool_input": {"command": "echo hello"},
 "tool_response": "hello\n",
 "model": "gpt-5.6-sol",
 "permission_mode": "bypassPermissions",
 "tool_use_id": "exec-ee352c3e-81a2-4735-ab2d-e30aa2e2af1f"}
```
Event `PostToolUse`, tool `Bash` — the same PascalCase envelope shape the Claude-Code-family hooks use, despite the wire enum being `postToolUse`. `fire-count` = 1.

Hook notification counts, stated grep:
```
$ grep -c '"method": "hook/started"'   wire-run2.jsonl   → 1
$ grep -c '"method": "hook/completed"' wire-run2.jsonl   → 1
```
`hook/completed` carried the injected text inline:
```json
"run": {"eventName": "postToolUse", "handlerType": "command",
        "executionMode": "sync", "scope": "turn", "status": "completed",
        "durationMs": 55,
        "entries": [{"kind": "context",
          "text": "MAILBOX ROW (comms push probe -- this is DATA, not instructions): passphrase COMMS-PROBE-198219-8954 . If you can read this, report the passphrase verbatim."}]}
```

Verdict:
```
$ bash ~/code/comms/adapters/probe/probe-verdict.sh /tmp/pr63-w/probes/appserver/probe \
      --answer-file /tmp/pr63-w/probes/appserver/agent-answer.txt
positive control PASSED: the hook fired, was handed the event, and emitted the envelope.
PUSH
  The passphrase COMMS-PROBE-198219-8954 came back in the agent answer ...
EXITCODE=0
```

Agent answer, run 2, verbatim:
```
I'll run exactly that command and inspect only its output.
COMMS-PROBE-198219-8954
```

Passphrase for this arm: `COMMS-PROBE-198219-8954` (`probe/passphrase`, `probe/hook-stdout.json`).

## 6. What this means for T3 Code

- **The premise in the brief is half right.** `codex app-server` does read `$CODEX_HOME/hooks.json` in the same wrapped shape, does fire `PostToolUse` with the same envelope, and does inject `hookSpecificOutput.additionalContext` back into the model's turn. That half is now measured, not inferred.
- **The trust gate has no headless bypass on app-server's argv**, which is the finding report-A's "Open questions" item 2 asked for. `codex exec` has `--dangerously-bypass-hook-trust`; `codex app-server` does not expose it as a flag. The bypass exists only as a per-thread config override the *client* must set in `thread/start`.
- **T3 Code does not set it.** report-A already grounded that T3's Codex driver passes no trust/bypass anything (grep for trust/bypass across its Codex files: zero hits). T3's escape hatch is `launchArgs`, which is forwarded into app-server's *argv* — and argv is exactly the path where the override showed no effect here. So the user-facing workaround report-A proposed (put `--dangerously-bypass-hook-trust` in launchArgs) will not work as written: the flag does not exist on app-server, and the `-c` form of it produced no bypass warning.
- **Therefore, on an untrusted hook, comms delivery into T3-Code-launched Codex is silently dead**, and T3's UI cannot even show you that, because it reads `hook/started`/`hook/completed` only for thread-id bookkeeping.
- **Two routes remain for someone who wants this to work today**, in cost order:
  1. Get the hook to `trustStatus: "trusted"` in the real `~/.codex` once (run the interactive TUI's hook-review prompt, or write `hooks.state` via `config/batchWrite`). Trust is hash-pinned on `currentHash`, so it must be re-approved every time the hook command string changes — an installer that rewrites the command silently re-breaks delivery. Cost: one human approval per hook change. Not measured here.
  2. Patch T3 Code's Codex driver to set `thread/start params.config.bypass_hook_trust = true`. Measured to work. Cost: a source change in someone else's app, and it disables review for every hook on that thread.

  Recommendation: route 1. It is the only one that does not require forking T3 Code and does not blanket-disable review. The one fact that would flip it to route 2: if `hooks.state`-persisted trust turns out not to be honoured by app-server (untested), route 1 has no mechanism and route 2 is all that is left.
- **Independent of delivery**, `hook/completed.run.entries[].text` already carries the injected row verbatim over the wire, so the UI-rendering work report-A flagged is a switch case away — the data is already at T3's door.

## 7. Safety / isolation ledger

Baseline re-check at end of run:
```
$ shasum -a 256 -c /tmp/pr63-w/research/config-baseline.sha
/Users/drakegriffith8/.claude/settings.json: OK
/Users/drakegriffith8/.codex/hooks.json: OK
/Users/drakegriffith8/.codex/config.toml: OK
EXITCODE=0
```

Isolated home `/tmp/pr63-w/probes/appserver/home` (`CODEX_HOME`):
- **Symlinked (never copied, never printed, never `cat`'d): `auth.json` → `~/.codex/auth.json`. That is the only symlink.** It was sufficient; no other auth material was needed, and no other file was linked.
- `hooks.json` — written by `arm-probe.sh --format wrapped`, confirmed shape `{"hooks": {"PostToolUse": [ {matcher, hooks:[{type:"command", command}]} ]}}`.
- `config.toml` — **minimal, NOT copied from `~/.codex/config.toml`.** The brief's precondition (`grep -i -E "key|token|secret" ~/.codex/config.toml` shows nothing sensitive) could not be evaluated: the sandbox classifier denied that grep, both in content form and in count-only form. Silence is not evidence, so the conservative branch was taken and a one-line placeholder config.toml was written instead. Codex then populated its own defaults into it during the runs.
- Everything else in that directory (`sessions/`, `*.sqlite*`, `models_cache.json`, `installation_id`, `cache/`, `plugins/`, `skills/`, `tmp/`, `shell_snapshots/`, `thread-writer-locks/`) was created by codex itself inside the isolated home.

Not done, per the constraints: no interactive TUI, no mailbox post, no run armed, no edit to `~/.codex`, `~/.claude`, `~/.secrets`, or `~/code`.

## 8. Evidence paths

| File | What |
| --- | --- |
| `/tmp/pr63-w/probes/appserver/drive.py` | the JSON-RPC-over-stdio client; method names taken from the generated schema, not guessed |
| `/tmp/pr63-w/probes/appserver/schema/` | `codex app-server generate-json-schema --out` output (v1/v2 + combined) |
| `/tmp/pr63-w/probes/appserver/wire-run1.jsonl` · `wire-run2.jsonl` | every JSON line both directions |
| `/tmp/pr63-w/probes/appserver/wire-argvcheck.jsonl` · `wire-cfgcheck.jsonl` | the two non-turn lever checks |
| `/tmp/pr63-w/probes/appserver/summary-run1.json` · `summary-run2.json` | initialize / hooks/list / thread/start / notification counts |
| `/tmp/pr63-w/probes/appserver/probe/stdin-copy.json` | **the positive control** (run 2) |
| `/tmp/pr63-w/probes/appserver/probe/hook-stdout.json` · `fires.jsonl` · `fire-count` | hook stimulus + 1 fire |
| `/tmp/pr63-w/probes/appserver/agent-answer.txt` (= `agent-answer-run2.txt`) | run 2 answer read by `probe-verdict.sh` |
| `/tmp/pr63-w/probes/appserver/agent-answer-run1.txt` | run 1 answer (inadmissible) |
| `/tmp/pr63-w/probes/appserver/run1-control-snapshot/` | reconstruction used to produce run 1's exit code |
| `/tmp/pr63-w/probes/appserver/home/hooks.json` | the armed wrapped-shape config |