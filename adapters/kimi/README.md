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

## The driver is now three parameters, not a loop

`poll-driver.sh` used to contain the read/format/invoke/remember loop. That
loop had nothing kimi-specific in it except the invocation, so it lives once in
`bin/comms-poll-driver` (issue #29) and this script is the kimi-shaped part:
the resume command, the directory-bound cwd, and the cursor key. It expands to

```
bin/comms-poll-driver <runid> <seat> --subs --cursor <cursor> --cwd <cwd> \
    -- kimi -r <session-id> -p '{}' --output-format text
```

where the generic driver substitutes the formatted rows for `{}` on the argv
array (never through a shell) and confirms the cursor only when `kimi` exits 0
-- the same rule as before, now enforced by the shared `comms cursor
take`/`confirm` pair rather than by a private copy of it here (issue #30).

The adapter always selects the generic driver's `--subs` view: rows reach the
session when their topic or thread is in the seat's subscription set, including
the implicit `@<seat>` unicast topic. A seat with no subscription file still
sees the whole board, preserving the mailbox's backward-compatible contract.

**Cursor format and key changed.** The old cursor was the `at` of the last row
delivered, at `$COMMS_STATE_DIR/kimi-cursor/<runid>-<seat>`; the current
subscription-view cursor is per-poster counts at
`$COMMS_STATE_DIR/kimi-cursor/<runid>-<seat>.subs-<digest>.json`, where the
digest names the effective sorted subscription set by the same rule as the CLI
read cursor. The shared helper stores the counts in that file. Both formats mean "everything
up to here is delivered", so the driver TRANSLATES an existing timestamp cursor
on its first run -- a seat's count is how many of its rows are at or before
that timestamp -- and leaves the old file as `<runid>-<seat>.pre-counts`. A
live seat with the former view-less `.subs.json` count cursor replays its
subscription board once on upgrade, and again whenever its subscription set
changes; that old `.json` is left behind. If the timestamp
translation cannot be done the driver says so on stderr and starts from zero,
which costs one replay and loses nothing.

## Usage

```
bash adapters/kimi/poll-driver.sh <runid> <seat> <kimi-session-id> <cwd> [--interval <seconds>]
```

`--once` polls a single time and prints what would be delivered (no kimi
invocation, cursor untouched) -- use it to test wiring without a live session.
The preview banner now names the delivery command rather than the session id
(`comms-poll-driver: would deliver 2 row(s) (cwd ...): kimi -r ...`); the
delivered text itself is unchanged, header included.
