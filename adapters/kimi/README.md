# Kimi adapter

Kimi has NO hook surface: nothing can run after a tool call, so push injection
(the claude-code and codex adapters) is impossible. Delivery is a resume
driver instead: a shell loop OUTSIDE the session polls the mailbox and, when
new rows appear, delivers them as a resume turn:

```
kimi -r <session-id> -p "<rows>" --output-format text
```

run from the session's recorded cwd, because kimi sessions are
directory-bound.

Constraints worth knowing before debugging:

- `-p` combines with neither `-y` nor `--auto`. A resume delivery is a plain
  prompt turn; it cannot also grant autonomy flags.
- The driver's cursor advances only after a successful `kimi` invocation, so a
  failed delivery re-delivers on the next poll rather than dropping rows.

## Usage

```
bash adapters/kimi/poll-driver.sh <runid> <seat> <kimi-session-id> <cwd> [--interval <seconds>]
```

`--once` polls a single time and prints what would be delivered (no kimi
invocation, cursor untouched) -- use it to test wiring without a live session.
