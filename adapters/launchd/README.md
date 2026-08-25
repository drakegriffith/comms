# adapters/launchd -- nightly thread-compile job

Keeps `scripts/comms_compile_threads.py` running on a schedule so the vault
notes under `research/agent-threads/<board>/<date>.md` (see that script's own
docstring for shape, watermark, and continuation semantics) stay current
without a human remembering to run it by hand.

## What's here

- `com.comms.thread-compile.plist` -- a TEMPLATE (see its own header comment):
  `ProgramArguments` names `python3` and the absolute path to
  `comms_compile_threads.py`; `StartCalendarInterval` fires the job at 18:00
  and 01:00 daily; `StandardOutPath`/`StandardErrorPath` land under
  `~/Library/Logs/comms/`. launchd cannot expand `$HOME` or resolve a
  checkout's own location, so four placeholders in the template are filled
  in at install time.
- `install.sh` -- resolves those placeholders for THIS checkout and writes
  the result to `~/Library/LaunchAgents/com.comms.thread-compile.plist`.
  Idempotent: re-running compares bytes and leaves an unchanged install
  untouched, same as `adapters/discord/install.sh`'s own checks.

## Install

Run this from the MAIN checkout, not a worktree: `install.sh` resolves
`$SCRIPT` from its own location on disk and bakes that absolute path into the
plist, so a plist installed from a worktree points at a directory that
`git worktree remove` can delete out from under an already-scheduled job.

```
bash adapters/launchd/install.sh
```

This writes the plist and creates `~/Library/Logs/comms/`. It does **not**
call `launchctl` -- that changes this machine's running launchd state, which
every install script in this repo leaves to a human to do on purpose (see
`adapters/discord/install.sh` and `adapters/claude-code/ambient/install.sh`,
neither of which auto-loads what they print either). Run the two commands
`install.sh` prints when you are ready:

```
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.comms.thread-compile.plist
launchctl kickstart -k gui/$(id -u)/com.comms.thread-compile   # force an immediate run, for testing
```

## Uninstall

```
launchctl bootout gui/$(id -u)/com.comms.thread-compile
rm ~/Library/LaunchAgents/com.comms.thread-compile.plist
```

## Env knobs the compile job reads

Same names as the rest of this stack (see `lib/swarm_mailbox.py`,
`lib/swarm_threads.py`, `adapters/discord/mirror.py`):

- `COMMS_ROOT` -- mailbox root (default `/tmp`)
- `COMMS_STATE_DIR` -- cursor/watermark state (default `~/.comms/state`);
  this job's own watermarks live under `<state>/thread-compile/<board>.watermark.json`
- `COMMS_THREAD_ALIVE_SECONDS` / `COMMS_THREAD_ALIVE_SEATS` -- the same alive
  predicate knobs the board lane and `swarm_threads.py threads` read, used
  here only for the note's own `threads_alive` front-matter count
- `COMMS_VAULT_ROOT` -- where notes are written (default
  `~/brain-actual-intelligence`)

launchd jobs run with a minimal environment (no shell profile is sourced), so
a non-default value for any of these needs an `EnvironmentVariables` dict
added to the plist by hand after `install.sh` writes it -- the common case
(every default as-is) needs no edit.

## Verify

```
python3 scripts/comms_compile_threads.py
```

Exits 0 and prints `rows_inspected=N notes_written=M`, or exits 2 with a
stderr line if the mailbox has zero threaded rows to inspect (the positive
control -- see the script's docstring).
