# Retiring a duplicate comms install

A second copy of this stack exists on a machine and someone wants it gone. This
runbook is the order of operations for that, and its first job is to talk you
out of it when the copy is not what it looks like.

Read the whole thing before running any of it. The steps are ordered because
the order is the safety property, not because of tidiness: step 4 exists to
make step 6 survivable, and step 3 exists because doing step 6 first takes the
machine down.

---

## 0. The premise, attacked

"It is a duplicate, it writes to a dead-end mailbox, delete it and reinstall
from the repo" is a claim with three parts, and each one fails differently.

**"It is a duplicate."** Usually false at the directory level even when true at
the file level. The pre-extraction tree at `~/.claude/lib/swarm/` holds 21
files; 4 have successors in this repo and 17 do not, because the extraction
deliberately left harness-native tooling behind: `swarm.py`, `cc-inject.py`,
`codex_seat.sh`, `glm_swarm.sh`, `reduce/`, `pi-AGENTS.md`, `ROLES.md`,
`ROLES-claude-runtime.md` and their tests. `ROLES.md` is the seat-allocation
doctrine that the harness's own `AGENTS.md` requires every multi-agent dispatch
to read first. Delete-and-reinstall would not restore one of those 17 files.
The preflight checker exists to force this comparison rather than leave it to
whoever is holding the `rm`.

**"It writes to a dead-end mailbox."** Half true, and the true half is not the
half people repeat. Both copies default `COMMS_ROOT` to `/tmp`. The divergence
is in the STATE dir: the pre-extraction `swarm_arm.py` defaults to
`~/.claude/state`, this repo's defaults to `~/.comms/state`. Arming is what
lives in the state dir, so the old copy is deaf to every run armed by the new
one. That is a real defect and it is not fixed by deleting anything.

**"So delete it."** The dangerous word is "it". The unit people mean is
"the duplicate"; the unit `rm -rf` takes is a path. Those differ, and the gap
is where content dies.

### The failure mode this runbook is built against

`~/.claude/hooks/swarm-heartbeat.sh` used to end:

```
if [ -f "$COMMS_HB" ]; then exec bash "$COMMS_HB" "$@"; fi
exit 0     # checkout missing: exit 0 silently
```

For as long as the checkout was absent, that hook reported success on every
tool call while delivering nothing. No log, no stderr, no counter, no exit
code. **A missing dependency that is silent is a missing dependency nobody
finds**, and it is why the divergence above lived long enough to become a
migration. `adapters/claude-code/shim/swarm-heartbeat.sh` in this repo is the
replacement: same never-block behaviour, three loud channels on a miss (see
step 3). Install it with `adapters/claude-code/install-shim.sh`; do not hand-
edit the live file, because hand-editing the live file is how the two copies
diverged in the first place.

---

## What is UNRECOVERABLE if this goes wrong

Be specific, because the answer drives what the backup has to capture.

| Thing | Recoverable from | Unrecoverable if |
| --- | --- | --- |
| Tracked files whose commits are pushed | the remote | never, given the push |
| Tracked files whose commits are LOCAL ONLY | nothing but this disk | you delete before pushing. `git branch -r --contains HEAD` returning 0 is this case, and the preflight refuses on it |
| UNTRACKED / ignored files under the copy | nothing | always. `.gitignore`d state, scratch notes, a half-finished script |
| File MODES (the executable bit) | git, for tracked files | for untracked files, and for anything restored by a copy tool that drops modes |
| The 17 harness-native files listed above | claude-harness git history only | you delete them expecting `install.sh` to bring them back. It cannot: they were never in this repo |
| Live mailbox rows under `$COMMS_ROOT` | nothing | you delete the mailbox root. Not part of this migration, but `/tmp` is one careless glob away |
| `~/.comms/state` arm rosters, cursors, claims | nothing | same |
| The BODY of a pre-extraction hook you overwrite | claude-harness history, if it was committed there | it was a local edit. This is why `install-shim.sh` backs up rather than overwrites |

**Therefore the backup must capture, at minimum:**

1. The entire doomed path, **including untracked and ignored files**, with
   **modes preserved**. `tar -pcz` or `cp -Rp`, never `git archive` (which
   silently skips everything untracked -- exactly the unrecoverable set).
2. The existing `~/.claude/hooks/swarm-heartbeat.sh`, byte for byte.
3. `~/.claude/settings.json` before any rewiring, byte for byte.
4. The output of the preflight checker, including its backup manifest. It is
   the record of what the copy contained and what was believed to succeed it.
5. The current commit id of every git checkout involved, and proof each one is
   on a remote (`git branch -r --contains HEAD`).

Not needed, and do not touch: `~/.comms/state`, `$COMMS_ROOT`. Copying them is
not harmful; deleting or "cleaning" them is.

---

## 1. Preflight -- must be GREEN

```
bash scripts/comms-retire-preflight.sh \
     --doomed <path-you-intend-to-delete> \
     --authoritative <path-that-must-survive> \
     --manifest ~/retire-manifest.txt
```

Exit 0 = green. **Exit 2 is not a pass**; it means the scan did not run, and
the checker prints which enumerator came back zero. Exit 1 lists every refusal.

The checker refuses on: the doomed path being (or containing) the authority;
any file with no successor in the survivor; uncommitted or untracked content;
no remote ref containing HEAD; an active hook registration naming the doomed
path (escalated for gate-mode registrations); a document referrer resolving to
one of those successor-less files; and the survivor failing a live round trip.

Do not proceed on a refusal by arguing with it. Fix the condition -- land the
orphan files somewhere tracked, retarget the referrer, push the commits -- and
re-run. A green from an hour ago is not a green now; re-run it as the last
thing before step 6.

---

## 2. Quiesce

The copy may be in use by something that will not notice it vanishing.

```
# Anything holding a file open under the doomed path
lsof +D <doomed-path> 2>/dev/null | head

# Scheduled work that might fire mid-migration
launchctl list | grep -i -e comms -e swarm
ls ~/Library/LaunchAgents/ | grep -i -e comms -e swarm

# Runs currently armed (these are LIVE seats, not leftovers)
ls ~/.comms/state/swarm-arm/
```

Unload any launchd job that touches the stack for the duration, and note the
label so step 8 can put it back:

```
launchctl bootout gui/$(id -u)/<label>
```

Do not disarm live runs. Disarming retires their claims and the seats holding
them do not find out. Wait, or accept that the heartbeat is briefly wired to a
path in flux -- which is survivable precisely because the new shim is loud.

---

## 3. REWIRE FIRST, DELETE SECOND

**This is the step that prevents a machine-wide outage, and its ordering is not
negotiable.**

The live `~/.claude/settings.json` registers, with `"matcher": "*"`:

```
bash $HOME/.claude/state/bin/hook-shim.sh gate $HOME/.claude/hooks/swarm-heartbeat.sh
```

`gate` means the hook's exit code is a decision: exit 2 refuses the tool call
that fired it. `hook-shim.sh`'s `_lkg_eligible` guard withholds last-known-good
when the target does not EXIST, on the reasoning that a missing file is a
deletion rather than a tear, so the gate fails closed and loud. Compose those
two facts: **delete a gate-mode hook target while settings.json still names it
and every tool call in every session on the machine starts getting refused,
including sessions already running.** The blast radius is the machine, not the
mailbox.

So:

**3a. Point the registration at the surviving path.** Either install the new
shim at the same stable location (preferred -- the wiring never changes):

```
bash adapters/claude-code/install-shim.sh
```

which places `adapters/claude-code/shim/swarm-heartbeat.sh` at
`~/.claude/hooks/swarm-heartbeat.sh`, backs up whatever was there, records this
checkout in `~/.comms/state/checkout-path`, and refuses to place a file that
would fail the gate (`bash -n` must parse; the last line must be exactly
`# hook-eof-marker v1 do-not-remove`). Or edit `settings.json` to name the
repo's script by absolute path.

**3b. Prove the gate dispatch returns 0 BEFORE deleting anything.**

```
printf '{"agent_id":"a1","tool_name":"Bash"}' \
  | bash ~/.claude/state/bin/hook-shim.sh gate ~/.claude/hooks/swarm-heartbeat.sh \
    >/dev/null 2>&1; echo $?
```

**Must print 0.** Anything else and you stop here: the gate is already unhappy
and deleting the old copy will make it worse, not better. This is the
intermediate green, and it is a separate observation from step 7's round trip
-- one proves the machine still runs tool calls, the other proves messages
still move. Record both.

**3c. Re-run the preflight** (step 1). It re-reads `settings.json` and should
now report `registrations_naming_doomed = 0`.

Only now is deletion in scope.

---

## 4. Back up, and VERIFY the backup

```
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BK=~/backups/comms-retire-$STAMP
mkdir -p "$BK"

# 1. the doomed path, modes and untracked files included
tar -pczf "$BK/doomed.tgz" -C "$(dirname <doomed-path>)" "$(basename <doomed-path>)"

# 2, 3. the live hook and the settings file, before any further edits
cp -p ~/.claude/hooks/swarm-heartbeat.sh "$BK/swarm-heartbeat.sh.live"
cp -p ~/.claude/settings.json            "$BK/settings.json.live"

# 4. the preflight record
cp -p ~/retire-manifest.txt "$BK/"

# 5. commit ids and their reachability from a remote
for R in ~/.claude ~/code/comms; do
  echo "$R $(git -C $R rev-parse HEAD) remote_refs=$(git -C $R branch -r --contains HEAD | wc -l)"
done > "$BK/commits.txt"
```

**Verify it. An unverified backup is a hope.** A tarball that cannot be listed,
or that holds fewer files than the source, is worse than no backup because you
will delete on the strength of it.

```
# file count must MATCH, and must be nonzero -- a zero-file tar lists cleanly
SRC=$(find <doomed-path> -type f | wc -l)
BAK=$(tar -tzf "$BK/doomed.tgz" | grep -vc '/$')
echo "source=$SRC backup=$BAK"
[ "$SRC" -gt 0 ] && [ "$SRC" -eq "$BAK" ] && echo "BACKUP OK" || echo "BACKUP BAD -- STOP"

# and prove it restores, into a scratch dir, before you trust it
SCRATCH=$(mktemp -d)
tar -xzf "$BK/doomed.tgz" -C "$SCRATCH"
diff -rq <doomed-path> "$SCRATCH/$(basename <doomed-path>)" && echo "RESTORE OK"
rm -rf "$SCRATCH"
```

`source=0` is the trap: `find` on a mistyped path returns nothing, `tar` on it
returns nothing, and `0 -eq 0` reads as success. The `-gt 0` test is the
positive control on the backup itself.

---

## 5. Delete

Only after steps 1 through 4 have each produced their stated evidence.

```
rm -rf <doomed-path>
```

No globs. No `rm -rf $VAR/` where `$VAR` might be empty. Paste the literal
path the preflight printed as `doomed path :`.

---

## 6. Reinstall

```
cd ~/code/comms
bash adapters/claude-code/install.sh          # heartbeat wiring + comms-say skill
bash adapters/claude-code/ambient/install.sh  # ambient bridge, if it was in use
bash adapters/claude-code/install-shim.sh     # only if the stable hook path is wired
```

`install.sh` exits 0 installed-and-verified, 1 failed, 2 could-not-verify.
**Exit 2 is not a pass** -- it means the post-install suites never ran, which
says nothing about whether the install works. Treat it as a stop.

---

## 7. Verify -- positive control, not a file listing

`ls` proving files exist is precisely the evidence the broken copy also had. It
was complete, it passed its own suite, and it still delivered nothing anyone
read. Prove the property that matters: a message goes in and comes back out.

```
bash scripts/comms-verify-roundtrip.sh --install ~/code/comms
```

It posts a unique passphrase to a throwaway runid under a `mktemp`
`COMMS_ROOT`, reads it back through the install's own CLI, and asserts the row
is on disk under that root. It runs a negative control first (an untouched
runid must read back empty), so a verifier that echoed its own argument would
fail rather than pass. It writes nothing into the live board. Exit 0 = proven,
1 = failed, 2 = could not run, which is not a pass.

Then re-assert the gate from step 3b -- the same command, still 0 -- and check
the shim's miss counter is not growing:

```
wc -l ~/.comms/state/heartbeat-shim-missing.log 2>/dev/null || echo "no misses logged"
```

A file that exists and is growing means the shim cannot find the checkout: the
reinstall did not take, and the new shim is telling you so instead of exiting 0
in silence. That line is the whole point of the shim change.

---

## 8. Rollback

Trigger rollback on any of: the gate probe not returning 0, `install.sh`
exiting 1 or 2, the round trip failing, or the miss log growing.

```
# a. restore the deleted tree, modes intact
tar -pxzf "$BK/doomed.tgz" -C "$(dirname <doomed-path>)"

# b. restore the hook and the wiring, in that order
cp -p "$BK/swarm-heartbeat.sh.live" ~/.claude/hooks/swarm-heartbeat.sh
cp -p "$BK/settings.json.live"      ~/.claude/settings.json

# c. re-prove the gate BEFORE walking away
printf '{"agent_id":"a1","tool_name":"Bash"}' \
  | bash ~/.claude/state/bin/hook-shim.sh gate ~/.claude/hooks/swarm-heartbeat.sh \
    >/dev/null 2>&1; echo $?      # must be 0

# d. re-prove message flow
bash scripts/comms-verify-roundtrip.sh --install ~/code/comms

# e. put back anything step 2 unloaded
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
```

For tracked files, `git -C ~/.claude checkout -- <path>` is faster than the
tarball and does not need one. It is not a substitute: it restores nothing
untracked, which is the unrecoverable set.

Rollback is not complete until (c) and (d) have both printed their result. A
rollback that restored files and was never exercised is the same shape of
non-evidence as the silent hook this whole document is about.

---

## Known referrers to retarget

Found by the preflight sweep; listed here because they are the ones a human has
to decide about.

| Referrer | Names | Do |
| --- | --- | --- |
| `~/.claude/bootstrap.sh` (~line 204) | `$REPO_ROOT/comms/install.sh` | Retarget to `~/code/comms/adapters/claude-code/install.sh`. It is a variable-interpolated path, so no absolute-path grep finds it -- the preflight catches it with the relative marker `comms/install.sh` |
| `~/.claude/AGENTS.md` | `lib/swarm/ROLES.md`, twice | Leave alone. `lib/swarm/` is NOT being retired; it has 17 files with no successor here. If it is ever retired, this reference is HARD and must move first |
| `~/.claude/docs/panels/**/dispatch_*.sh` | `lib/swarm/codex_seat.sh` | Leave alone, same reason. These are executable and would break, not merely go stale |

The panel `*.out` and `*.err` artifacts the sweep also reports are archived
agent output. They cannot break. They are counted rather than filtered because
a referrer gate that guesses which references matter is a gate that eventually
guesses wrong; raise `COMMS_PF_SHOW_MAX` if you want to read them all.
