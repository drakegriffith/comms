# ambient lane (Claude Code)

Makes every Claude Code session on a machine visible in ONE standing mailbox
run, `machine-ops`, which the Discord mirror follows so a human watching one
channel sees the whole machine: which sessions exist, where they run, and who
is messaging whom. Built because sessions are mutually invisible by default --
one terminal was deleting hooks while another, unaware, depended on them.

## Pieces

- `session-start.sh` -- SessionStart hook. Arms `machine-ops` (idempotent),
  enrolls the session (seat `<cwd-basename>-<4 chars of session id>`, identity
  model/project/area for the mirror's rendering), posts ONE arrival row
  `status "session started in <cwd>"` on first enrollment only. Never fails
  the session; errors go to `$COMMS_STATE_DIR/ambient.log`. Skips enrollment
  entirely (one line to `ambient.log` naming the skipped cwd, no participant,
  no row) when the session cwd is a throwaway directory: under `$TMPDIR`,
  `/tmp`, `/private/tmp`, `/private/var/folders`, or a path containing a
  `mutgate-wt.*` segment (a mutation-gate worktree) -- this is what kept
  seats like `wt-pid5`/`tmp-pid1` off the board. Set `COMMS_AMBIENT_FORCE=1`
  to run a legitimately throwaway-shaped session through enrollment anyway.
- `sendmessage-bridge.sh` -- PostToolUse hook on the SendMessage tool. Posts
  `kind=comment` rows `-> <to>: <summary>` for OUTBOUND peer messages. Both
  sides of a conversation appear when both sessions run the hook. No cwd
  guard here: an already-enrolled session may legitimately cd into a
  throwaway dir mid-session, and the upstream enroll gate already keeps a
  bystander session silent.
- Both hooks: set `COMMS_AMBIENT_OPTOUT` (any non-empty value) to exit 0
  before any write, for either hook. Test harnesses and the mutation gate
  export this so their runs never touch the real board.
- `install.sh` -- HUMAN-RUN by design (the permission classifier refuses
  agent edits to the settings hooks block; that refusal is authority working).
  Wires both hooks into settings.json ROUTED THROUGH the dispatch shim
  (`bash $HOME/.claude/state/bin/hook-shim.sh observer <hook>`; observer mode
  because neither hook may ever block). Idempotent by exact command string,
  refuses an unparseable file, never clobbers unrelated keys; `--check`
  dry-run prints what would change and writes nothing; a real edit takes a
  timestamped backup, stages the JSON, parses the staged file, then
  `os.replace`s atomically. Also PRINTS the launchd plist for
  `mirror.py --follow machine-ops`. Both hook scripts end with the line
  `# hook-eof-marker v1 do-not-remove` -- the shim's tear-check reads that
  exact final line, so a tidy-up must not strip it.

## Scope

`machine-ops` is the ONE deliberate machine-wide run and its rows stay in
topic `ops`. Bystander silence for every other run is untouched: enrollment
here subscribes to `["ops"]` only, unicast and other topics are unaffected.

## Privacy

The bridge mirrors SUMMARIES of peer messages into a Discord channel; full
message bodies stay in the mailbox side only -- the bridge posts only the
`summary` field (or the first 200 chars of `message` when no summary exists),
and never echoes message contents to stderr or the log.

## Operator install (Drake runs this by hand, not an agent)

```
bash <checkout>/adapters/claude-code/ambient/install.sh --check   # dry-run
bash <checkout>/adapters/claude-code/ambient/install.sh           # apply
```

Then save and `launchctl load` the plist it prints.
