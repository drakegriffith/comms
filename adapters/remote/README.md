# Remote sync adapter -- one mailbox across two machines

Until this adapter, each machine's mailbox was an island: a laptop seat and a
Studio seat could both post, and neither could ever read the other. This
adapter makes one machine's mailbox the **hub** and gives the other machine a
push path (`post`) and a pull path (`pull`) over plain ssh.

    laptop seat  --ssh--> hub `bin/comms post`   (row lands in the hub mailbox)
    laptop       <--ssh-- hub `bin/comms read`   (rows land in the local mirror file)

Nothing new runs on the hub. The hub side is the `bin/comms` CLI that is
already there.

## Quickstart

```
# one-time preflight (checks ssh, the remote CLI, and prints wiring)
adapters/remote/install.sh

# push one row into the hub's mailbox (queues locally if the hub is offline)
python3 adapters/remote/sync.py post machine-ops alpha comment "landed #150" --topic ops

# pull the hub's new rows into this machine's mailbox
python3 adapters/remote/sync.py pull machine-ops

# both, in one call; or poll
python3 adapters/remote/sync.py sync machine-ops
python3 adapters/remote/sync.py --follow machine-ops --interval 15
```

After a `pull`, the hub's rows are ordinary sibling rows in the local
mailbox, so every existing reader sees them with zero changes:
`bin/comms read machine-ops <seat>`, the Claude Code push heartbeat, the
Discord mirror, all of it.

## Configuration

| Knob | Default | Why the system cannot compute it |
| --- | --- | --- |
| `COMMS_REMOTE_HOST` | `studio` | The ssh alias is a fact about `~/.ssh/config`, not about this repo. |
| `COMMS_REMOTE_BIN` | `~/code/comms/bin/comms` | The hub's checkout path is a fact about the other machine's disk (its `$HOME` is a different username here). |
| `COMMS_REMOTE_LABEL` | value of `COMMS_REMOTE_HOST` | The provenance label written into pulled seat names. Defaults to the alias, which is already the human's name for that machine. |
| `COMMS_MACHINE_LABEL` | `hostname` up to the first dot | Shared with `adapters/discord/mirror.py` -- one label per machine, not two. Worth setting: this laptop's hostname is `Christophers-MacBook-Pro-2`, which becomes part of every seat name it exports. |
| `COMMS_REMOTE_SSH_TIMEOUT` | `10` (seconds) | Offline is the expected steady state, not an error; the wait before declaring it is a taste call. |

## Provenance: machine-qualified seat names

Every row that crosses a machine boundary gets its seat name qualified with
the machine it came from, separated by `~`:

    laptop seat `alpha` posting to the hub   -> hub sees seat `alpha~laptop`
    hub seat `bravo` pulled to the laptop    -> laptop sees seat `bravo~studio`

Qualification is idempotent (a seat name that already contains `~` is left
alone), so a row never collects a second machine tag as it moves.

This one convention buys three things at once:

1. **One writer per file survives the network.** The mailbox's core
   invariant is that each `<seat>.jsonl` has exactly one writer. A laptop
   seat `alpha` writing into the hub's `alpha.jsonl` would break that the
   moment the hub had its own `alpha`. `alpha~laptop.jsonl` is a file only
   one process on one machine ever appends to, so the invariant holds
   machine-wide with no lock, no lease, and no arbiter.
2. **Echo suppression is structural, not heuristic.** `pull` drops every
   hub row whose seat ends in `~<this machine's label>` -- rows this machine
   pushed, coming back. The suffix is one we wrote ourselves, so the test is
   about our own bookkeeping rather than a guess about content.
3. **Loops cannot form.** Pulled rows land in a mirror file
   (`remote~<hub label>.jsonl`), and `pull` also skips any hub row already
   sitting in a `remote~*` file. A third machine's rows are therefore
   pulled once and never re-exported.

## Push is explicit; pull is a mirror

`post` sends **one named row** to the hub. It does not tail the local
mailbox and forward it.

That asymmetry is deliberate. A forwarding tail on both sides is how a sync
loop is born, and it also means every local row -- including a seat's private
chatter with a seat on its own machine -- crosses the network whether anyone
wanted it to or not. Choosing to address the other machine is the same
choice a seat already makes with `--to` for unicast, so the mental model is
one the reader already has.

Cost, stated plainly: a seat that types `bin/comms post` reaches only its own
machine. Reaching the hub is `sync.py post`. That is one extra thing to know.

## Offline by design: the outbox

The laptop closes at the end of the day. Being unreachable is a normal
state, not a failure, so `post` never loses a row to it:

* Every `post` **appends to the outbox first**, then tries to flush the whole
  outbox in order. A row is durable on local disk before any network call.
* Because `post` always enqueues before sending, a fresh row can never jump
  ahead of a row still queued from this morning. Ordering is preserved without
  a sequence number.
* A flush that fails stops at the first failure and leaves the rest queued;
  it never drops or reorders. The next `post`, `flush`, `sync`, or `--follow`
  pass retries.
* The outbox is `$COMMS_STATE_DIR/remote-sync/<host>/outbox.jsonl`, rewritten
  via tmp + `os.replace`, so a crash mid-flush leaves either the old queue or
  the new one, never half of one.

`post` therefore has two success shapes and they are different exit codes:
**0 delivered, 1 queued-but-not-delivered.** Queued is not a pass and is not
a crash; it is a durable "later", and a caller that wants to know which one
happened can read the code instead of parsing prose.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Delivered / pulled. `pull` printing `inspected=0` still means it reached the hub and the hub had nothing. |
| 1 | Queued, not delivered (hub unreachable). The row is on local disk. |
| 2 | Usage error, or **could not inspect** -- `pull` could not reach the hub, or the remote CLI is missing. Never a pass. |

`pull` reports `inspected=<n> mirrored=<n> echo=<n>` on every pass. A pass
that inspected zero rows says so out loud, because a sync that never reached
the hub and a hub with nothing new are otherwise byte-identical.

## Design record (2026-08-24, claude-harness#150)

Ratified candidate: **hub mailbox on the always-on machine**, driven from the
intermittent one over the ssh path that already works. What follows is the
design-it-twice record, including the mechanism alternatives inside that
candidate, since the discarded options are the part that rots out of memory.

### Measured facts the design rests on

Re-derived on 2026-08-24 rather than inherited from the issue:

* Non-interactive ssh laptop -> Studio works: `ssh -o BatchMode=yes -o
  ConnectTimeout=5 studio true` -> `rc=0`.
* The Studio runs the comms stack at `~/code/comms` under a *different*
  username (`/Users/drakegriffith8`), so no absolute path from the laptop is
  portable to it.
* Both machines default `COMMS_ROOT` to `/tmp` and both already have the
  standing `machine-ops` run armed.
* **The Studio's checkout was three merged PRs behind the laptop's master**
  (`6d42670`, missing #14, #15, #16). This is the fact that shaped the
  design: a hub that must run *new* code needs a deploy step and a version
  lockstep, and the hub is the machine nobody is sitting at.
* End-to-end mechanism probe, before a line of code was written: a row
  posted from the laptop with `ssh studio '~/code/comms/bin/comms post ...'`
  was read back with `ssh studio '~/code/comms/bin/comms read ...'`, both
  `rc=0`.

### Chosen: A. Studio hub, laptop-driven, hub-side code = zero

The laptop pushes rows with the hub's *existing* `bin/comms post` and pulls
with its *existing* `bin/comms read`. Both subcommands have been stable since
PR #4/#6, so the adapter works against the checkout the Studio has today and
against any older or newer one that still has the CLI.

* **Buys:** no reverse ssh, no Remote Login on the laptop, no daemon on the
  hub, no deploy, no version lockstep, no external service. The whole
  cross-machine surface is two subprocess calls.
* **Costs:** the laptop drives, so nothing crosses while the laptop is
  closed (rows queue in the outbox and flush on the next pass). Delivery
  latency is one poll interval, not push. A hub-originated row is invisible
  to the laptop until the laptop asks.

### Discarded: B. Discord as an inbound bus (issue candidate 2)

Poll the Discord channel and ingest rows tagged for this machine.

* **Buys:** works without ssh in either direction; the channel already
  exists and already carries the traffic.
* **Costs:** makes a third-party service *load-bearing* for
  machine-to-machine correctness rather than display. Discord is currently
  a mirror -- if it breaks, a human loses a dashboard. Under this option, if
  it breaks, agents stop hearing each other. It also inverts the trust
  direction: inbound content from a chat channel becomes instructions to a
  tool-using agent, which is exactly the shape the harness treats as
  untrusted data. Rate limits and edit/delete semantics add latency and an
  ordering story that the file mailbox gets for free.
* **Verdict:** discarded. Not on latency -- on the dependency direction.

### Discarded: C. Bidirectional rsync/git sync of the state dir (issue candidate 3)

Sync `$COMMS_ROOT` and `$COMMS_STATE_DIR` both ways on a timer.

* **Buys:** conceptually trivial; no new vocabulary.
* **Costs:** it syncs the *wrong nouns*. Cursors, claims, and arming state
  are deliberately machine-local (see `adapters/discord/mirror.py`, WHAT IS
  NOT MIRRORED); replicating them would let one machine's mirror cursor
  suppress the other machine's Discord output, and would make the claims
  arbiter -- whose whole correctness argument is "one atomic mkdir on one
  filesystem" -- into a distributed lock with no arbiter. Two-way file sync
  also has to answer "what if both sides changed it", and the mailbox has no
  merge function. There is no liveness signal either: a stale copy and a
  quiet peer look the same.
* **Verdict:** discarded. Option A syncs rows, which are append-only and
  therefore mergeable by construction; C syncs state, which is not.

### Discarded: D. Hub pushes to the laptop (the symmetric version of A)

An agent or daemon on the Studio posts into the laptop's mailbox over ssh.

* **Buys:** true push latency in the direction that currently polls; the
  always-on machine does the work.
* **Costs:** requires Remote Login/sshd enabled on the laptop plus the
  Studio's pubkey installed -- a machine-security decision that is Drake's
  to make, explicitly out of scope for this build. And it aims push at the
  machine that is *offline by design*: the hub would spend most of its
  evening failing to reach a closed laptop and would need the same outbox
  this design already has, on the machine that is harder to inspect.
* **Verdict:** discarded, and it is the one to revisit first if Remote Login
  is ever enabled. `pull` becomes an optimization rather than the only
  inbound path; nothing in the row format or the qualification scheme would
  change.

### Discarded: E. Mount the hub's mailbox (sshfs/NFS) and let both machines write it

Skip the protocol; make it one directory.

* **Buys:** zero new code -- every existing reader and writer just works.
* **Costs:** the mailbox's one-writer-per-file safety argument is a claim
  about *local* append atomicity; over a network filesystem it is a claim
  nobody here has measured, and the failure mode is a silently interleaved
  line rather than a loud error. It also deletes offline operation
  outright: with the mount down, a seat's own `bin/comms post` fails instead
  of queueing, so the laptop loses the ability to talk *to itself*. A
  dependency that takes down the local case to enable the remote one is the
  wrong trade.
* **Verdict:** discarded.

### The one fact that would flip the chosen design

If the laptop gets Remote Login enabled (Drake's call), option D becomes
available and the inbound path stops being a poll. Everything else here --
the row format, the `~` qualification, the outbox, the mirror file -- is
unchanged by that, which is the reason the polling `pull` was built as a
separate subcommand rather than woven into `post`.

## Layout

```
sync.py       the adapter: post / flush / pull / sync / --follow
install.sh    preflight (ssh reachable, remote CLI present) + wiring instructions
```

`lib/swarm_mailbox.py` gained exactly two functions for this adapter:
`append_mirrored` (the only sanctioned way to write rows that another
machine authored) and `fresh_rows_by_seat` (the per-seat cursor arithmetic,
lifted out of `adapters/discord/mirror.py` so the two adapters share one
implementation instead of two that drift).
