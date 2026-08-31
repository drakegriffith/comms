# Discord mirror adapter

One Discord channel as the cross-machine live dashboard. Each machine runs its
own local comms mailbox (see `lib/swarm_mailbox.py`); this adapter tails that
mailbox and posts each row as its OWN Discord author, using the webhook
`username` field, so a channel reads as a real multi-party conversation
instead of a wall of identical bot lines:

    alpha (studio): 📬✅ cursor logic landed, tests green
    beta (macbook): 🚧 port 7778 already bound

(the `name:` above is the Discord message's author, not text in the body --
each line is posted BY that seat, not narrated about it). A seat that
enrolled with identity metadata (see below) renders its author line as prose
a human can read at a glance -- what kind of agent it is and what it is
working on:

    kimi1 · Kimi K3 on agent-os (macbook): 📬✅ hook rot in leg 2

Discord is the merge point AND the dashboard. There is no cross-machine file
sync: two machines never read each other's mailboxes; they both post into the
same channel, and the author line (seat + machine, see Rendering below) keeps
provenance. Command direction (typing in Discord to steer a machine) is
deliberately out of scope here -- durable commands go through the GitHub
board.

## Rendering: three visible verbs

A human watching the channel should see an agent's whole lifecycle without
decoding anything:

| Verb | Source | Trigger | Content |
| --- | --- | --- | --- |
| agent born | mailbox (`mirror.py`) | the ambient "session started in `<dir>`" status row | "🐣 I am awake in `<dir>`" |
| posted to mailbox | mailbox (`mirror.py`) | any other mailbox row | kind emoji + text (see below) |
| heard from mailbox | heartbeat telemetry (`ingest_mirror.py`) | the heartbeat hook actually injects new rows into an agent's context | "👁️ read N row(s) from `<seats>`" |

Every message's Discord **author** (the webhook `username`, not text in the
body) is `<seat> · <model> on <project> (<machine>)` when the seat enrolled
with identity, else `<seat> (<machine>)` -- see Seat identity below. Because
Discord's `username` is one value per POST, rows batched into a single
message always share one author; a run of consecutive same-seat rows batches
together, but a seat change always starts a new message even if it would
still fit under the content cap.

"Posted to mailbox" rows get one leading emoji chosen by event shape:

| Shape | Emoji | Content |
| --- | --- | --- |
| broadcast `finding` | 📬✅ | `<emoji> <text>` |
| broadcast `comment` | 📬💬 | `<emoji> <text>` |
| `reply` | ↩️ | `<emoji> <text>` |
| `claim` | 📌 | `<emoji> <text>` |
| `blocker` | 🚧 | `<emoji> <text>` |
| `status` (non-ambient) | ℹ️ | `<emoji> <text>` |
| unicast (`topic` starts with `@`) | 📨 | `📨 to <seat>: <text>` (overrides the kind emoji -- a direct message is conversation regardless of kind, same rule the convo lane's filter already uses) |
| sendmessage-bridge row (`text` starts with `-> `) | kind emoji | target rendered readably; a bare agent_id target (the exact complaint this feature fixes -- e.g. `-> aecd8555b8a274737: comment`) is shortened to its first 8 hex chars and is NEVER the bare object of the sentence: `📬💬 sent to a subagent (aecd8555): comment` |

An unknown `kind` falls back to ℹ️ (kind-agnostic mirroring, see Behavior
guarantees, is unaffected -- an unrecognized kind still renders, just with
the generic emoji).

### The `everyone` audience

`COMMS_AUDIENCE=everyone` (env, or a line in the secrets file; default
`engineer`) swaps every renderer's vocabulary for one a non-engineer can
read. Same shapes, same precedence, different words:

| Shape | engineer | everyone |
| --- | --- | --- |
| author line | `<seat> · <model> on <project> (<machine>)` | `<seat> · <model>, working on <project>` (no machine) |
| agent born | `🐣 I am awake in /Users/x/code/comms` | `👋 Joined, working in comms` (folder name only) |
| broadcast `finding` | `📬✅ <text>` | `✅ Found something: <text>` |
| broadcast `comment` | `📬💬 <text>` | `💬 <text>` |
| `reply` | `↩️ <text>` | `↩️ Replying: <text>` |
| `claim` | `📌 <text>` | `📌 Taking this on: <text>` |
| `blocker` | `🚧 <text>` | `🚧 Stuck: <text>` |
| `status` / unknown kind | `ℹ️ <text>` | `ℹ️ Update: <text>` |
| unicast | `📨 to <seat>: <text>` | `📨 Message to <seat>: <text>` |
| bridge row, bare agent id (17 hex) | `📬💬 sent to a subagent (aecd8555): <s>` | `💬 Sent a note to a helper agent: <s>` (any kind; a target that is not a 17-hex id is shown as typed) |
| heard from mailbox | `👁️ read 3 row(s) from a, b` | `👀 Read 3 new messages from a and b` |
| forum thread title | `comms/adapters/discord/mirror.py` | `mirror.py · comms` |

`main()` pins the value for the life of the process, so a follower picks up
a change only when restarted; rows already posted are never rewritten. The author-line
80-char cap and the mention/zero-width sanitizing apply to both. The thread
map is keyed on the thread key, not the title, so switching audiences after
a thread exists neither renames it nor opens a second one. An unlisted value
is a usage error: `--once` exits 2 naming both legal values (checked before
any webhook is touched), and `install.sh` reports the configured value.

## Setup

1. Create the webhook (one time, in Discord):
   Server Settings -> Integrations -> Webhooks -> New Webhook -> pick the
   channel -> Copy Webhook URL.

2. Drop the secret in (3 steps, for Drake):
   1. `open -e ~/.secrets/comms.env`
   2. add line: `DISCORD_COMMS_WEBHOOK_URL=<paste webhook URL from Discord channel settings>`
   3. `chmod 600 ~/.secrets/comms.env`

   The URL is a credential: never commit it, never echo it. The mirror reads
   it from the environment first, then from that file; it prints only the
   drop-in instructions when it is missing (exit 2), never any value.

3. Preflight + run:

       bash adapters/discord/install.sh          # checks wiring, prints run cmds
       python3 adapters/discord/mirror.py --once <runid>
       python3 adapters/discord/mirror.py --follow <runid>   # poll loop

   Run one mirror per machine per run. `install.sh` also prints a launchd
   plist template if you want it supervised.

## Lanes: a second channel for agent-to-agent chatter

By default (`--lane` omitted, or `--lane all`) the mirror behaves exactly as
above -- every row, one channel. Pass `--lane convo` to mirror agent-to-agent
conversation to a SECOND webhook/channel instead. The predicate a row must
match, exactly:

    topic.startswith("@")          # a unicast -- a message to ONE seat,
                                    # of ANY kind (finding/status/blocker/
                                    # claim included: a direct message is
                                    # conversation regardless of what kind
                                    # carries it)
    or kind in ("comment", "reply")  # a broadcast conversational row

so the convo lane is not "findings vs. chatter" -- a `--to` unicast lands in
convo even if it's kind `finding`, unless the row carries a `thread`. At most
one Discord message per row outside the all lane; a row carrying both `to` and
`thread` appears in the document thread only, never also in convo.
`lib/swarm_mailbox.CONVO_KINDS` is the kind-half of this predicate, defined
next to `VALID_KINDS`:

    python3 adapters/discord/mirror.py --once <runid> --lane convo
    python3 adapters/discord/mirror.py --follow <runid> --lane convo

A row the convo lane skips still advances that lane's cursor -- it is never
re-scanned on the next pass, it is just never posted. The two lanes never
share a cursor, a skipped-rows log, or a secret:

| Lane | Secret var | State dir | What it posts |
| --- | --- | --- | --- |
| `all` (default) | `DISCORD_COMMS_WEBHOOK_URL` | `discord-mirror/` | every row, one channel, on arrival |
| `convo` | `DISCORD_COMMS_CONVO_WEBHOOK_URL` | `discord-mirror-convo/` | unicasts + `comment`/`reply` that carry no `thread`, on arrival |
| `board` | `DISCORD_COMMS_FORUM_WEBHOOK_URL` | `discord-mirror-board/` | rows carrying a `thread`, into one forum thread per document, **once that document's conversation is alive** (see below) |

GitHub landings are NOT a lane of this table: `adapters/github/landings.py`
is a separate poller with its own source (`gh api`, not the mailbox) and its
own secret, `DISCORD_COMMS_LANDINGS_WEBHOOK_URL`, falling back to
`DISCORD_COMMS_WEBHOOK_URL` -- the `all` lane's channel -- when that var is
set nowhere. See `adapters/github/README.md`.

Set up the convo lane's secret the same way as the default lane's (Setup
step 2 above), just with the `_CONVO_` var name and, in Discord, a second
webhook pointed at a second channel.

### Mirroring every run at once

    python3 adapters/discord/mirror.py --follow-all [--interval N] [--lane convo]

Each pass globs the mailbox root for every `comms-*` run directory and mirrors
each one, so a new run that gets armed while the process is running is picked
up without a restart.

### launchd safety

`--follow` and `--follow-all` are meant to run under a launchd `KeepAlive`
job, which restarts anything that exits nonzero. A job that exits every time
it polls a not-yet-configured secret would crash-loop. So in those two modes
only, a missing secret does NOT exit: one stderr line, then a 60s retry.
`--once` is unaffected -- it still exits 2 and names the exact drop-in line,
because a one-shot invocation (a human, or a script checking the result)
needs the loud failure. A per-run exception (a bad row, an unwritable state
dir) is likewise caught, named on one stderr line with the runid and
exception class, and does not stop the loop or the rest of the pass.

### Concurrency: one poller per (run, lane), enforced

Two pollers on one (run, lane) both read the same cursor, both post, and BOTH
advance it -- double-posted rows in the channel, not a race that merely
errors. That used to be a rule in this paragraph. It is now a lock.

Every pass takes an exclusive `fcntl.flock` on
`<COMMS_STATE_DIR>/<lane state dir>/<runid>.lock`. The second poller **does
not block and does not post**: it writes one stderr line naming the run and
lane, returns 0, and its rows are delivered by whichever poller holds the
lock. Nothing is dropped -- anything that arrives after the winner's read is
picked up on the loser's next poll.

    discord mirror: another poller holds run 'comms-...' lane 'all'; skipping this pass (its rows are that poller's to post)

Why skip rather than wait: `--follow` runs under a launchd `KeepAlive` job, so
a blocking poller would pile processes up behind a lock the first one holds
for its whole life, and every one of them would post the same backlog when it
finally won. A skipped pass costs one `--interval`.

Scope, deliberately narrow:

- **Per (runid, lane).** Different runs never contend, so `--follow-all` still
  walks the whole fleet in one pass, and one lane's `all` job beside another's
  `convo` job on the same runid stays a supported pair (separate state dirs,
  separate locks).
- **Per pass, not per process.** The lock is released between polls, so an
  ad-hoc `--once` slots between a follower's passes instead of being locked
  out for as long as the follower runs.
- **Same machine only.** `flock` is a local-filesystem lock. Two machines
  mirroring the same runid is not a shape this repo has (each machine keeps
  its own mailbox), and the cross-machine duplicate that DOES exist is
  handled by the pulled-row rule below, not by a lock.

The board lane takes a **second, independent lock**, and it has to: a thread
key spans runs (two runs discuss one document), which the per-(run, lane)
lock deliberately does not. `adapters/discord/threads.py` flocks
`<lane state dir>/threads.lock` and holds it across read map -> create thread
-> persist map, so a check-then-act cannot interleave and orphan a thread.
Unlike the pass lock it BLOCKS rather than skipping: the loser has rows of
its own to deliver into a thread the winner is creating, and the wait is one
HTTP round trip. It is a sidecar file and not the map itself because the map
is written tmp + `os.replace`, so an fd locked on the map names an inode that
stops being the map the instant it is saved. Two MACHINES racing is still
unfixed -- a local lock cannot serialize them; the cost is at most one
duplicate thread per document per machine, and the revisit condition is a
second machine running the board lane.

A missing webhook secret still exits 2 under `--once` even when another poller
holds the lock: that exit is the contract with the human running it, and a
lock someone else happens to hold must not turn it into a quiet 0.

### Pulled rows are counted, never posted

The run directory also holds `remote~<hub>.jsonl` -- the file
`adapters/remote` appends rows PULLED off another machine to. Those rows are
copies of hub rows that the hub's own mirror already posted to this same
channel, so mirroring them again posts everything twice, once per machine.
The mirror drops them from what it posts and still counts them against the
cursor (count-but-skip, the same shape the lane filter uses, so they are never
re-scanned).

**The discriminator is the source FILE, never the seat string.** Direction
matters:

| Row | Where it lives | Who mirrors it |
| --- | --- | --- |
| pushed by this machine to the hub | first-class `alpha~macbook.jsonl` on the HUB | the hub's mirror, and it must keep posting it |
| pulled from the hub to a spoke | `remote~<hub>.jsonl` on the SPOKE | nobody: the hub already posted the original |

So "skip any seat containing `~`" would silence exactly the rows that most
need posting. See issue #20.

### Cursor format

`<COMMS_STATE_DIR>/<lane state dir>/<runid>.cursor.json`, a flat JSON object:

    {"alpha/alpha.jsonl#8912345": 12, "beta~studio/remote~studio.jsonl#8912346": 4}

The key is `<seat>/<source file>#<inode>`; the value is how many of that
seat's rows the mirror has read OUT OF THAT FILE. The separator is `/`
because it is the only character neither half can contain -- `@` and `#` are
both legal in a seat name and in a machine label, so `beta~studio#2` is a
nameable seat whose mirror file is `remote~studio#2.jsonl`, and a separator
either half can contain makes the key ambiguous. Three properties earn the
shape:

- **Per file, not per seat.** One seat can own rows in two files at once (its
  own, and the pull mirror), and a pulled row with an older `at` used to shift
  that seat's merged sequence and push an already-delivered row back under the
  count -- it then posted twice (issue #23). What lands in one file cannot
  move another file's indices.
- **Inode, not just name.** The key is a file IDENTITY. A purged and
  re-created `<seat>.jsonl` reads as a new source and starts a fresh count, so
  its rows post again rather than being skipped by a count the old file
  earned. A visible duplicate beats an invisible loss; ordinary appends never
  change the inode.
- **Old cursors migrate in place.** A pre-existing `{"<seat>": N}` file means
  "the first N rows of this seat, in merged order, are already seen". The next
  poll spends that count exactly that way, writes the per-file keys, and drops
  the bare seat key. Deploying this never re-posts history and never skips a
  row. One case is deliberately not behavior-preserving: if the legacy count
  is LARGER than the rows now visible (a truncated or replaced seat file), the
  surplus is not banked, so rows appended later post rather than being
  swallowed by a count that dead rows earned.

## The board lane: one forum thread per document

`--lane board` mirrors rows that say what document they are ABOUT into a
Discord **forum** channel, one thread per document. It is the only lane whose
delivery is deferred, and the only one with a second state file.

    python3 adapters/discord/mirror.py --once <runid> --lane board
    python3 adapters/discord/mirror.py --follow-all --lane board

### What makes a row eligible

A row carries a `thread` field, written by `swarm_mailbox.post(...,
thread=)`. The key comes from `swarm_mailbox.thread_key(path)`:
`doc:<repo>/<relpath>`, where `<repo>` is the basename of the nearest
ancestor holding a `.git` entry (a directory in a clone, a FILE in a linked
worktree) and the path is realpath'd first, so a symlink and a `..` and
`/tmp` vs `/private/tmp` all key on ONE thread. A path outside every repo
returns `None`, and a row with no `thread` is count-but-skipped by this lane
(a forum webhook rejects a post with neither `thread_name` nor `?thread_id=`,
and the `all` lane already mirrors those rows anyway).

### The alive rule: two seats, close together

A document does NOT get a thread the moment somebody mentions it. It gets one
when its rows are a conversation, which `lib/swarm_threads.alive` defines as
both of:

- at least `COMMS_THREAD_ALIVE_SEATS` (default **2**) distinct seats have
  posted a non-`status` row in it, **and**
- some **consecutive** pair of those rows comes from two DIFFERENT seats no
  more than `COMMS_THREAD_ALIVE_SECONDS` (default **1800**, i.e. 30 minutes)
  apart, with a strictly positive gap.

Both halves earn their place. Distinct seats alone renders a thread where one
seat posted and another posted a week later: two speakers, no conversation.
A timely pair alone renders one seat posting twice in a minute: a monologue
with good rhythm. `status` rows -- the ambient "session started" birth
announcement -- are excluded from both halves, or every document one agent
so much as opened would look like a two-party exchange.

Until a document goes alive its rows WAIT. When it goes alive, **the whole
backlog posts**, oldest first, across as many messages as the seat changes
force (Discord's webhook `username` is per-POST, so a seat change always
starts a new message). `lib/swarm_threads` is a pure module with no I/O
precisely so `bin/comms threads` can ask the identical question without a
second copy of this rule.

**Alive is a one-way transition, and `threads.json` is its record.** The
predicate decides whether to OPEN a thread; it never decides whether to
deliver into one that already exists. Once a document has a thread, every
later row for it posts straight into that thread -- one seat, months later,
no second speaker required. Re-asking the predicate each pass would stall
those rows in held forever, because a drained thread leaves no rows behind to
be alive with.

**Scope, exactly (design note D2, unchanged in v1): alive is evaluated per
(run, lane); the thread map is fleet-wide.** A pass judges a document using
only the rows of the run it is mirroring -- that run's fresh rows plus its own
held ones. The cost, named: a document discussed by two different runs (or
two machines) needs **two seats within ONE run** before its thread is ever
opened; alpha in run A and bravo in run B do not add up to a live
conversation. Once ANY run opens it, though, the map is fleet-wide and every
run's rows land in that same thread. Cross-run liveness is deferred work, not
a claim this build makes.

### State files (all in `<COMMS_STATE_DIR>/discord-mirror-board/`)

| File | Scope | Shape | Answers |
| --- | --- | --- | --- |
| `<runid>.cursor.json` | per run | `{"<seat>/<file>#<inode>": count}` | what have I READ |
| `<runid>.held.json` | per run | `{"doc:<repo>/<path>": [row, ...]}`, rows in `at` order | what do I still OWE |
| `threads.json` | **fleet-wide, per lane** | `{"doc:<repo>/<path>": "<discord thread id>"}` | which Discord thread is this document |
| `threads.lock` | fleet-wide, per lane | empty | the map's flock (see Concurrency) |
| `<runid>.lock`, `<runid>.skipped.jsonl` | per run | as the other lanes | -- |

Two files because there are two questions, and the cursor structurally cannot
answer the second: its keep-predicate is a bool over one row, so "post this
later" is unrepresentable there. The thread map is fleet-wide and NOT
per-run on purpose -- one document is discussed by seats in different runs,
and a per-run map opens a second thread for the same file.

### The per-pass order, which is the whole safety argument

1. load the held file
2. `collect_new` (fresh rows + the new cursor)
3. bucket held + fresh by thread key, sorted by `at`
4. **write held**, including the buckets about to post
5. **save the cursor**
6. for each ALIVE key: get-or-create its thread, post the whole backlog
7. rewrite held minus what each chunk delivered, **after each chunk**

Step 4 before step 5 is the load-bearing pair. The cursor advancing means "I
have read these"; the held file existing means "I still owe these". Making
held durable first is what makes advancing the cursor safe -- and *durable*
is meant literally: every one of the three state files (held, cursor, thread
map) is written to a temp file, `flush`ed, `fsync`ed, and only then renamed
into place, with a best-effort `fsync` of the directory after. Ordering two
Python writes does not order two disk writes; without the `fsync` both files
are still dirty page cache when `os.replace` returns, and a power cut is free
to keep the newer cursor while losing the held file it depends on -- a crash between
4 and 6 re-posts nothing and loses nothing, a crash between 6 and 7 duplicates
a message, which is the same at-least-once trade every cursor in this repo
makes. The reverse order would define a row that is read, not posted, and
remembered nowhere.

Step 7 per chunk, not once at the end: a drain of a long backlog is many
POSTs over many seconds, and rewriting after each one means a failure in the
middle costs only the chunks not yet delivered. The once-at-the-end version
re-posts the ENTIRE backlog on the next pass every time one POST fails.

### When something breaks

| Point | What happens |
| --- | --- |
| `DISCORD_COMMS_FORUM_WEBHOOK_URL` missing | `--once --lane board` exits 2 with the drop-in; `--follow` warns once and retries in 60s. The other lanes are unaffected. |
| create-thread POST fails (4xx/5xx, or a body with no id) | `thread_for` returns `None`, nothing posts, the rows stay held, next pass tries again |
| the thread-map lock cannot be taken (unwritable state dir, refused `flock`, no descriptors) | `None` and one stderr line, same as any other failure -- the rows stay held and the OTHER documents in the pass still drain |
| create succeeded but the map could not be saved | `None`, one stderr line, rows stay held; one empty thread is leaked and auto-archives -- better than posting into a thread nothing remembers |
| map file corrupt | read as `{}`, one stderr line, the thread is recreated (at most one duplicate) |
| post-into-thread fails after retries | that chunk goes to `<runid>.skipped.jsonl` and is dropped from held (one bad batch must not wedge every row behind it); that thread's remainder waits for the next pass |
| held file corrupt or unreadable | read as `{}`, one LOUD stderr line. The un-posted backlog in it is genuinely lost -- the cursor is already past it. |
| a crash (or a failed cursor save) between the held write and the cursor save | the next pass re-reads those rows against the old cursor; merging them into held is idempotent, so each row stays exactly once and posts once |
| a document never goes alive | its rows sit in held until `COMMS_THREAD_HOLD_MAX`, then the oldest are dropped and recorded in the skipped log |
| a document already has a thread | its rows post immediately, no predicate, no create call -- including a single row from a single seat |
| two agents enrolled on one seat name | one stderr line per pass naming the seat and both agent ids; nothing is blocked (see `swarm_arm.seat_collisions`, issue #42). Also reported by `swarm_arm.py status <runid>` as a `seat_collisions` key, so the collision stays visible on a board with no Discord webhook configured. Note the row can be DROPPED, not just duplicated, when the two share a `comms read` cursor -- see `seat_identities`' docstring |

Every board POST carries `allowed_mentions: {"parse": []}`. That is a
constant, not a knob: a mailbox row is prose an agent wrote, and prose
containing `@everyone` must never ring a phone.

### Setup (a human step in the Discord UI first)

1. In Discord, create a **forum** channel -- not a text channel. One thread
   per document is a shape only a forum holds.
2. That channel's Settings -> Integrations -> Webhooks -> New Webhook -> Copy
   Webhook URL.
3. `open -e ~/.secrets/comms.env`, add
   `DISCORD_COMMS_FORUM_WEBHOOK_URL=<that URL>`, then `chmod 600` the file.
4. `bash adapters/discord/install.sh` reports whether it is configured
   (existence only, never the value) and does not block on it -- the board
   lane is opt-in.

**Live check is a manual step, not code.** These tests prove the payload
shape (`thread_name` + `wait=true` on create, `?thread_id=` on the posts) and
every failure branch against a fake poster, never a live Discord. That the
resulting thread looks right in the forum is verified by hand once the
webhook exists.

## Live rehearsal checklist

Every guarantee above is proven by tests against a local fake webhook. The
rehearsal is the other kind of evidence: real Discord, real launchd jobs, two
machines. Work top to bottom -- each step names what would count as a failure,
because a step nobody could fail is not a check.

**Before (human steps, not scripted):**

1. Discord UI: the forum channel exists and has a webhook. Drop the URL in
   yourself -- `open -e ~/.secrets/comms.env`, add
   `DISCORD_COMMS_FORUM_WEBHOOK_URL=<url>`, `chmod 600 ~/.secrets/comms.env`.
   Never paste the URL into a terminal that is being transcribed.
2. `bash adapters/discord/install.sh` reports all three webhook vars as
   configured -- the forum one is the `board` lane's secret. It reports
   EXISTENCE, never values, and writes nothing.
3. Note the current row counts you expect to see, per machine. A rehearsal
   with no expected number is a demo.

**Rehearsal, in order:**

| # | Step | Pass looks like | Fail looks like |
| --- | --- | --- | --- |
| 1 | `comms post <runid> <seat> finding "rehearsal 1"` on the studio | one message in the `all` channel, authored `<seat> (studio)` | nothing (secret/lane wrong), or a bot-named line (username field dropped) |
| 2 | repeat the same command twice more, watch the follower | three messages, in order, no repeats | a repeat = cursor not advancing; a gap = a row filtered out |
| 3 | restart the follower job (`launchctl kickstart -k`), post again | only the NEW row appears | history re-posted = the cursor file was not read (check the state dir path) |
| 4 | start a SECOND `--follow <runid>` by hand, same lane | its stderr says another poller holds the run; the channel gets each row exactly once | the row appears twice = the lock is not being taken |
| 5 | kill -9 the first follower mid-post, poll again | the hand-started one takes over on its next pass; at most the in-flight message repeats (at-least-once, see Behavior guarantees) | it stays locked out = a stale lock (should be impossible: flock dies with the process); a row MISSING is the finding |
| 6 | on the laptop: `adapters/remote/sync.py pull` from the studio | rows land in the laptop's `remote~studio.jsonl` and NOTHING new appears in Discord | the pulled rows appear a second time = #20 is back |
| 7 | with the laptop's mirror running, push a row from laptop to studio, then pull it back | the row appears exactly once, authored by the laptop's seat | twice = the echo/pulled discrimination broke |
| 8 | post one unicast (`--to <seat>`) and one `comment` | both appear in the `convo` channel; the plain findings from step 1 do NOT | a finding in `convo` = lane filter; nothing at all = `DISCORD_COMMS_CONVO_WEBHOOK_URL` |
| 9 | inspect `<runid>.cursor.json` | keys read `<seat>/<file>#<inode>`; one key per (seat, file) | a bare `<seat>` key left over = migration did not run this pass |
| 10 | `<runid>.skipped.jsonl` | absent, or every entry explained | any entry nobody can explain is the finding of the rehearsal |
| 11 | with `--lane board` running, post ONE row with `--thread doc:comms/x.md` | nothing appears in the forum; the row is in `<runid>.held.json` under that key | a thread appears = the alive rule is not gating |
| 12 | from a SECOND seat, post another row with the same `--thread`, within 30 min | a forum thread named `comms/x.md` appears holding BOTH rows, oldest first | only the second row = the drain posted the trigger and abandoned the backlog, the exact bug #40 names |
| 13 | post a third row on the same key | it lands in the SAME thread | a second thread = `threads.json` was not read or not persisted |
| 14 | inspect `threads.json` | one entry per document, fleet-wide (no runid in the filename) | a per-run map = a duplicate thread the first time two runs discuss one file |

**After:** put the counts from step 3 next to what the channel shows. Equal is
the result; "looked fine" is not. Anything unexplained goes to a GitHub issue
before anything else lands on top of it.

## Env knobs

| Var | Default | Meaning |
| --- | --- | --- |
| `COMMS_AUDIENCE` | `engineer` | `engineer` or `everyone`: the vocabulary every renderer speaks (see The `everyone` audience). Also read from the secrets file, since launchd jobs inherit no shell |
| `COMMS_MACHINE_LABEL` | `hostname -s` | machine half of the author line, `<seat> (<machine>)`; not shown under `everyone` |
| `COMMS_ROOT` | `/tmp` | mailbox root (same knob as the mailbox itself) |
| `COMMS_STATE_DIR` | `~/.comms/state` | cursor, poller lock, skipped-row records |
| `COMMS_SECRETS_FILE` | `~/.secrets/comms.env` | where the webhook line(s) live |
| `COMMS_MIRROR_INTERVAL` | `5` | `--follow`/`--follow-all` poll seconds |
| `DISCORD_COMMS_CONVO_INGEST` | `1` | convo lane: `0` stops posting the `👁️ read N row(s)` ingestion events; the cursor still advances, so re-enabling replays nothing (see Ingestion events) |
| `COMMS_THREAD_ALIVE_SECONDS` | `1800` | board lane: how close two seats' rows must be for a document's conversation to count as alive |
| `COMMS_THREAD_ALIVE_SEATS` | `2` | board lane: how many distinct non-`status` seats a document needs before it renders |
| `COMMS_THREAD_HOLD_MAX` | `500` | board lane: rows one document may hold un-posted; past it the oldest are dropped and recorded in the skipped log |

A junk value in any of the three board knobs (a plist typo) warns on stderr
and uses the default. The lane's job is delivering rows; refusing to run
because a tuning parameter was misspelled loses more than it protects.

## Seat identity (optional)

Declare who a seat is at ENROLLMENT -- the one place a seat already announces
itself -- and the mirror joins it to that seat's Discord author line at
format time:

    bin/comms enroll <runid> --agent-id agent-k --seat kimi1 \
        --model "Kimi K3" --project agent-os --area hooks/

| Field | Example | Renders as (in the author line) |
| --- | --- | --- |
| `--model` | `"Kimi K3"` | `kimi1 · Kimi K3 ...` |
| `--project` | `agent-os` | `... on agent-os ...` |
| `--area` | *(not shown in the author line -- see below)* | -- |

`--model`/`--project` are optional free text (display-only prose; identity
never gates routing or delivery -- deliberately NOT a closed vocabulary). The
author line is `<seat> · <model> on <project> (<machine>)`; a seat with no
declared identity renders `<seat> (<machine>)`. (`--area` is still accepted
and stored in the roster for other consumers of `swarm_arm.seat_identities`;
this adapter's author line does not include it, having traded that slot for
the machine label so the SAME line also carries provenance across machines.)
The author line is sanitized against `@everyone`/`@here` and zero-width
characters before it ever reaches Discord, so a seat literally named
`@everyone` cannot render as a mention.

## Ingestion events ("heard from mailbox")

`adapters/discord/ingest_mirror.py` is a SEPARATE small tailer for a
different source file: `$COMMS_STATE_DIR/swarm-heartbeat.log`, the telemetry
the `PostToolUse` heartbeat hook (`adapters/claude-code/swarm-heartbeat.sh`)
appends every beat it runs for an enrolled agent -- one JSON line per beat:

    {"at": <iso8601>, "agent_id": <str>, "runid": <str>, "topic": <str>,
     "rows_inspected": <int>, "delta_emitted": <int>, "short_circuit": <bool>}

A line with `delta_emitted > 0` means the hook actually injected that many
NEW mailbox rows into that agent's context on that beat -- a delivery event.
`ingest_mirror.py` posts ONE Discord message per delivery event (aggregated
per beat, never per row): `👁️ read <N> row(s) from <distinct sender seats>`,
authored as the RECEIVING seat (identity roster if resolvable, same rules as
above). The log carries a count but not which seat(s) posted the delivered
rows, so this module re-derives that by replaying swarm-heartbeat's own
selection (topic/seat filter, own-seat exclusion, the one mailbox parser,
capped the same way) bounded by its own persisted watermark -- see the
module's docstring for the full "why", including the sanity check against
`delta_emitted` (a count is never trusted without a positive control).

Its cursor is a plain byte offset into the heartbeat log
(`discord-mirror-convo/heartbeat-ingest.cursor`, tmp + `os.replace`,
PID-suffixed tmp -- same safety shape as the row mirror's cursor). If the log
rotates or is truncated (offset larger than the file's current size), the
byte offset resets to 0.

**Wire-up:** `mirror.py --follow-all --lane convo` also tails this module
once per pass, in the SAME process -- no second launchd job. The heartbeat
log is fleet-wide, not scoped to one run, so this lives in `--follow-all`'s
whole-fleet pass; `mirror.py --follow <runid> --lane convo` mirrors that
run's mailbox rows only and does not tail ingestion (see `follow_all`'s
docstring for this scope choice). It also has its own standalone CLI:

    python3 adapters/discord/ingest_mirror.py --once
    python3 adapters/discord/ingest_mirror.py --follow [--interval N]

**Turning it off:** `DISCORD_COMMS_CONVO_INGEST=0` (default `1`) stops the
convo lane posting these `👁️ read N row(s)` lines. The ingest cursor keeps
advancing while it is off, so re-enabling it posts what happens NEXT, never a
replay of the backlog accumulated meanwhile. Mailbox-row mirroring in the
convo lane is untouched by this knob -- unicasts and `comment`/`reply` rows
post exactly as before.

One hazard the general concurrency warning above does not cover: never run a
DISABLED standalone pass (`DISCORD_COMMS_CONVO_INGEST=0 ingest_mirror.py
--once`) while an ENABLED follower is live on the same state dir. The
disabled pass advances the shared cursor without posting, so rows it read
are delivered by neither process -- a silent drop, where two enabled pollers
would at worst double-post.

Same launchd safety as the row mirror: a missing `DISCORD_COMMS_CONVO_WEBHOOK_URL`
warns once and backs off 60s under `--follow` instead of crash-looping; a
per-pass exception is caught, named on one stderr line, and does not kill
the loop.

## Behavior guarantees

- **Kind-agnostic.** Whatever `kind` a row carries is mirrored verbatim; the
  vocabulary is enforced at write time by `lib/swarm_mailbox.VALID_KINDS`,
  which is being extended on a parallel branch. The mirror never hardcodes it.
- **Exactly once across pollers, at least once across a crash.** A per-run
  cursor (per-seat-per-source-file row counts, valid because seat files are
  append-only single-writer) lives in `COMMS_STATE_DIR`, and the lock means
  two pollers can never both post a row. The cursor is saved AFTER the posts,
  so a crash (or an unwritable state dir) between a delivered message and
  that save re-posts that message on the next pass. This is the deliberate
  direction: committing the cursor first would turn a duplicate a human can
  see and ignore into a lost row nobody can see. Recovery for a duplicate is
  nothing; there is no recovery for a lost row. An ordinary restart re-posts
  nothing, because the cursor was saved.
- **Upgrade-safe.** A cursor file written in ANY earlier format is honored and
  migrated in place on the next poll, so deploying never re-posts history --
  see Cursor format above.
- **One poller per (run, lane), enforced.** An `fcntl.flock` per pass, not a
  rule in a README; the second poller no-ops loudly. See Concurrency above.
- **Deferred, and never dropped WITHOUT A RECORD (board lane).** A row whose
  document is not yet a conversation is held in `<runid>.held.json`, which is
  made durable -- written, `fsync`ed, then renamed -- BEFORE the cursor
  advances past it, so a crash mid-pass re-posts at worst and forgets
  nothing. The held file is rewritten after each delivered chunk, so a
  failure partway through a long drain costs only the chunks that did not
  land. **What is NOT promised is unlimited automatic retry.** A chunk that
  exhausts its delivery retries is written to `<runid>.skipped.jsonl`
  (flushed and `fsync`ed) and REMOVED from held: it will not be retried on
  its own, and recovery is a human replaying that file. Retaining it instead
  would wedge every later row in that document behind one poisoned batch.
  Same for rows dropped at `COMMS_THREAD_HOLD_MAX`. The guarantee is "no row
  disappears without a durable record", not "every row eventually reaches
  Discord". See The board lane above.
- **Batched per author.** Rows that arrive together from the SAME seat go out
  as one message, chunked under Discord's 2000-char content cap; a seat
  change always starts a new message (Discord's `username` is one value per
  POST -- see Rendering above).
- **Rate-limit aware, never silently lossy.** 429 honours `Retry-After` with
  a capped retry budget; rows that still fail are written to
  `<runid>.skipped.jsonl` in the state dir and shouted to stderr, then the
  cursor advances (the skipped file is the durable record). That file is
  flushed and `fsync`ed before the cursor moves past the row, so a crash
  cannot persist the newer cursor while losing the record it points at.
  Recovery is that file: it holds each row as its author wrote it, so a
  human can re-post or re-inject it.

## Deliberately NOT synced

Claims, arming/enrollment, subscriptions, cursors: all machine-local state
stays machine-local. The mirror carries only the conversational rows. Syncing
coordination state across machines would reintroduce the shared-file races the
mailbox design exists to avoid.
