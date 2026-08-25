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
  failed delivery re-delivers on the next poll rather than dropping rows. That
  is why it reads with `bin/comms read ... --replay`: `comms read`'s own cursor
  advances as soon as rows are printed, which here is before delivery is known
  to have worked, so letting it advance would convert a failed kimi invocation
  into a silent drop. One cursor owns delivery, and it is the driver's.

## Usage

```
bash adapters/kimi/poll-driver.sh <runid> <seat> <kimi-session-id> <cwd> [--interval <seconds>]
```

`--once` polls a single time and prints what would be delivered (no kimi
invocation, cursor untouched) -- use it to test wiring without a live session.
