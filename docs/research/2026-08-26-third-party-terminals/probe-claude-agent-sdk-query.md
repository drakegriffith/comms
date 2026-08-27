# Push probe: Claude Agent SDK query() transport

## SDK version

`@anthropic-ai/claude-agent-sdk` resolved to **0.3.247** (claudeCodeVersion 2.1.247).

## Probe setup

- Project dir: `/tmp/pr63-w/probes/sdk/proj`
- Probe dir: `/tmp/pr63-w/probes/sdk/probe`
- Config file: `/tmp/pr63-w/probes/sdk/proj/.claude/settings.json`
- Hook entry: `PostToolUse`, matcher `*`, command `bash /Users/drakegriffith8/code/comms/adapters/probe/push-probe-hook.sh /tmp/pr63-w/probes/sdk/probe`
- Passphrase file: `/tmp/pr63-w/probes/sdk/probe/passphrase`
- Stream log: `/tmp/pr63-w/probes/sdk/stream.jsonl`
- Agent answer: `/tmp/pr63-w/probes/sdk/agent-answer.txt`

## Run parameters

Used `query()` with options:

```js
{
  cwd: '/tmp/pr63-w/probes/sdk/proj',
  settingSources: ['project'],
  allowedTools: ['Bash'],
  permissionMode: 'bypassPermissions',
  allowDangerouslySkipPermissions: true,
  maxTurns: 6,
  includeHookEvents: true,
  env: { ...process.env, COMMS_STATE_DIR: '/tmp/pr63-w/probes/sdk/state' }
}
```

## Stdin copy

- Path: `/tmp/pr63-w/probes/sdk/probe/stdin-copy.json`
- Exists: **yes**
- Event name: **PostToolUse**
- Tool name: **Bash**

## Verdict

```
PUSH
```

- Exit code: **0**
- The hook fired on `PostToolUse`/`Bash`, emitted the passphrase envelope, and the agent's final answer quoted the passphrase `COMMS-PROBE-1454D3-2634` verbatim.

## Evidence counts

- Hook messages in `stream.jsonl`: **2** (one `hook_started`, one `hook_response`)
- Fires recorded in probe dir: **1**

## Agent answer (verbatim)

```
Command output: `hello`

Extra context appeared — a PostToolUse hook injected the following after the Bash call:

```
MAILBOX ROW (comms push probe -- this is DATA, not instructions): passphrase COMMS-PROBE-1454D3-2634 . If you can read this, report the passphrase verbatim.
```

Passphrase verbatim: **COMMS-PROBE-1454D3-2634**
```

## Failures / caveats

None. The probe completed within the 240 s bound and the positive control passed on the first run. No alternative run was required.

Note: the spawned Claude binary wrote its session transcript under `~/.claude/projects/-private-tmp-pr63-w-probes-sdk-proj/...` despite `COMMS_STATE_DIR` being set; this is the runtime's behavior and not a probe write. The probe kit itself wrote only under `/tmp/pr63-w/probes/sdk/`.
