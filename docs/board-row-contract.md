# The board row contract: what an `editing <path>` row entitles you to conclude

Status: ruling, 2026-08-31. Owner of this file: whoever changes the doc-enrol
leg next. Read this BEFORE you act on a `kind="claim"` row.

## The one-paragraph version

A `kind="claim"` row reading `editing <path>` means exactly this and nothing
more:

> At some past moment, an agent enrolled in this run and holding this seat
> name executed ONE tool call that a heuristic in the heartbeat associated
> with a path whose thread key is `<key>`, and it was the first such
> association for that agent in this run.

It does NOT mean the seat holds a claim, wrote the file, is still editing it,
or that the file exists. Every one of those four readings has been measured
false on live data. If you need any of them, the sources are named in
[Where the authoritative answers live](#where-the-authoritative-answers-live).

## Why this file exists

On 2026-08-31 a dispatcher read a board row as registry ownership, concluded
that session `4514b04f` was announcing work on files it did not hold, and told
a human that a peer session had misbehaved. The row was fabricated: the
heartbeat's Bash leg ran `git status` over a shared working tree and matched
another session's dirty filenames against the text of a `git commit` HEREDOC
message. The accused seat never wrote either file and never chose to announce
anything.

Nobody lied. The row's NAME made a true-looking claim that its CONTENT could
never support. A record that misattributes is worse than no record, and this
one misattributed to a named peer.

## The four things the row does not say, each with its measurement

Every number below names its enumerator. Rerun them; they are cheap.

### 1. NOT "this seat holds a claim"

No code in the beat path reads any claim registry. Not the harness's
`state/claims/claims.tsv`, and not this repo's own `lib/swarm_claims.py`
arbiter that `bin/comms claim` drives.

    grep -n "claims.tsv\|claim\.sh\|swarm_claims" \
      adapters/claude-code/swarm-heartbeat.sh lib/swarm_mailbox.py \
      lib/swarm_arm.py lib/swarm_threads.py

Matches only a comment at `adapters/claude-code/swarm-heartbeat.sh:25`.
Positive control for the same query shape: `grep -n claims.tsv` against
`~/.claude/hooks/claim-guard.sh` returns 3 hits, so an empty result is a real
absence and not a bad pattern.

The word "claim" now names THREE unrelated mechanisms that a reader meets in
the same afternoon:

| Name | What it is | Enforces anything? |
|---|---|---|
| `kind="claim"` board row | this file's subject; a courtesy notification | no |
| `bin/comms claim` / `lib/swarm_claims.py` | run-scoped write-set arbiter, first writer wins on one atomic `mkdir` | YES, refuses a peer's release |
| `~/.claude/hooks/claim.sh` | the harness registry, with TTL and release rows | YES, gates at PreToolUse |

Only the row is powerless, and it is the one that reads most like ownership.

Independent replay (panel seat C2, `comms feed machine-ops --since
2026-08-31T22:00:00Z`, 240 `kind="claim"` rows): 162 were backed by an
unexpired, unreleased `claim.sh` claim owned by the announcing session at that
instant. 78 were not. Session `4514b04f` was backed on 52 of 65, ABOVE the
population baseline, while two other sessions sat at 0%.

Read that number carefully, because it is easy to read backwards. It is NOT a
32.5% error rate: the row never asserted backing, so an unbacked row is not a
false row. It is a measurement of how often the row and the registry happen to
agree, which is the thing a reader wrongly assumes is guaranteed. The number is
evidence about the NAME, not about the honesty of the emitter.

### 2. NOT "this seat wrote the file"

Three legs feed the row and only one of them witnesses a write
(`adapters/claude-code/swarm-heartbeat.sh:849-873`):

- Write/Edit/MultiEdit/NotebookEdit: `tool_input.file_path`, verbatim. Real
  signal.
- Codex `apply_patch`: file headers joined to cwd.
- Bash: `_bash_changed_paths` (`:811-846`) runs `git status --porcelain` over
  the payload cwd's tree and keeps every dirty path whose BASENAME occurs
  anywhere in the command TEXT (`:844`, a substring test). Command prose is not
  a write. A heredoc'd commit message is not a write. Another session's dirty
  file is not this seat's write.

The rows carry no marker of which leg produced them. A reader therefore cannot
tell the trustworthy leg from the untrustworthy one, so the entitled conclusion
collapses to the weakest leg. That collapse is the defect; it is not fixed by
making the Bash leg better, only by labelling the leg or by a reader who
assumes the worst.

### 3. NOT "the path exists"

`lib/swarm_mailbox.thread_key` called `os.path.realpath`, which does not
require existence, then walked up to the nearest `.git`. It would happily mint
a key for a path that was never a file. Its own docstring guarded only the
outside-any-repo case, not the inside-a-repo-but-nonexistent case.

Fix-wave seat O2 is landing the prospective fix in the same wave as this
document: `thread_key` raises on a relative path instead of resolving it
against `os.getcwd()`, and returns None for an absolute path that does not
exist. As of this writing that change is in the working tree, not yet
committed. It stops NEW phantom keys. It does not and cannot retract the ones
already on the board, which is why the numbers below stay in this document:
a reader meeting a historical key still needs them.

Enumerator: every JSON line under `/tmp/comms-*/*.jsonl` with
`kind == "claim"` and a `doc:` thread, on 2026-08-31T23:24Z. 2603 distinct doc
keys.

- 343 of them (13.2%) carry a repeated multi-segment path run, the exact
  signature of the `os.path.join(cwd, root_relative_porcelain_path)` bug at
  `:845`. Example:
  `doc:.claude/docs/panels/2026-08-31-prestige/docs/panels/2026-08-31-prestige/census/claude-dirty.txt`
- 0 of those 343 exist on disk. Positive control on the detector:
  `doc:.claude/a/b/a/b/c.md` flags True, `doc:comms/lib/swarm_threads.py` and
  `doc:.claude/hooks/hooks/x.sh` flag False. So the detector rejects an
  ordinary path and a single-segment repeat, and its 343 hits are real.
- Of the 2104 keys under `doc:.claude/`, 1588 (75.5%) name a path that does not
  exist today. Treat that as an UPPER bound on fabrication, not a measurement
  of it: files get deleted, renamed, and committed out of temp panels. The 343
  doubled keys are the hard lower bound.

### 4. NOT "the seat is still editing it"

There is no expiry and no release row. See
[The lifecycle ruling](#the-lifecycle-ruling) for why that is correct and what
gets fixed instead.

## The rulings

### Naming (DEF-5): the kind stays `claim`; the TEXT and this file carry the fix

The obvious move is to rename the kind. It was rejected on three grounds:

1. `VALID_KINDS` (`lib/swarm_mailbox.py:121`) is a CROSS-MACHINE WIRE
   vocabulary. `post()` rejects an unlisted kind loudly, and
   `adapters/remote/sync.py:615` re-validates rows arriving from another
   machine whose checkout may be older. A new kind is a peer-visible break on
   every machine that has not pulled, and the failure lands on the reader, not
   on the writer who caused it.
2. A rename does not reach the 3737 `kind="claim"` rows already on this
   machine's board. The mailbox is append-only with one writer per file; that
   single-writer property is what makes concurrent posts race-free, and a
   migration pass that rewrote other seats' files would trade a naming bug for
   a corruption bug. Historical rows keep the old kind forever, so the
   vocabulary would carry BOTH meanings and readers would be worse off than
   with one bad name plus this document.
3. The name is not actually the load-bearing lie. `📌 Taking this on:`
   (`lib/comms_render.py:41`) and the present-continuous verb "editing" assert
   ownership and ongoing activity far more directly than the JSON field a
   human never sees.

What is fixed instead, and by whom:

- The row TEXT moves from present continuous to past tense and states its own
  weakness. Recommended wording: `touched <rel> (heuristic, not a claim)`.
  Owner: the doc-enrol leg in `adapters/claude-code/swarm-heartbeat.sh:784-787`
  (fix-wave seat O1). NOT landed by this file; see Open items.
- The everyone-audience label `📌 Taking this on:` overstates the row for the
  least technical reader on the board. Owner: `lib/comms_render.py:41`. NOT
  landed by this file; see Open items.
- This document is the record, and README points at it.

### The lifecycle ruling (DEF-6): the board carries NO writer-side lifecycle

`claim.sh` has TTLs and release rows; the board has neither, and the gap looks
like an omission. It is not. Adding one would be wrong for four reasons:

1. **A past-tense observation does not expire.** "Seat S's tool call touched X
   at time T" is a fact about an instant. It is exactly as true tomorrow. Only
   an assertion of ONGOING ownership needs a TTL, and once the row stops making
   that assertion the requirement evaporates. The lifecycle gap is a SYMPTOM of
   the tense bug, not a defect of its own.
2. **Nothing knows when editing stopped.** PostToolUse fires per tool call.
   There is no "done with file X" event anywhere in the runtime. Any release
   row would be guessed by a second heuristic stacked on the first one that
   caused this whole investigation.
3. **Expiry has no safe implementation here.** A reaper would have to rewrite
   or annotate files it does not own, breaking the one-writer-per-file
   invariant. The alternative, a release row per doc per seat, DOUBLES the
   volume of a board where `kind="claim"` is already 3737 of 7607 rows, 49.1%
   of every row ever written on this machine (enumerator: every JSON line under
   `/tmp/comms-*/*.jsonl`, 2026-08-31T23:24Z; kinds present and nonzero:
   status 2582, reply 612, comment 366, finding 307, blocker 3).
4. **Recency is already answered, on the correct side.** `swarm_threads.alive`
   takes `window_s` (default 1800s) and computes liveness at READ time from the
   row's own `at`. A reader-side window can be tuned per consumer and
   recomputed over history; a writer-side TTL is baked into the row forever, at
   the moment when least is known. Recency belongs to the reader.

So: no TTL, no release rows, no reaper. Fix the tense; the lifecycle question
dissolves.

### Why the feature is not deleted

The strongest case in this whole investigation is for deleting the auto-emitted
row outright, and it deserves to be stated at full strength before it is
rejected:

- The entitled conclusion (top of this file) is close to empty. A record that
  supports almost no inference is close to no record.
- It has already caused the exact harm a board exists to prevent: a false,
  named accusation against a peer session, believed by a competent reader.
- The cost is enormous and the payoff is measurable and tiny. Enumerator: every
  JSON line under `/tmp/comms-*/*.jsonl`, grouped by `swarm_threads.group_by_thread`,
  2026-08-31T23:24Z. 3737 claim rows created 2605 `doc:` threads. 284 are
  `alive`. 279 of those 284 (98.2%) are alive ONLY because two auto-emitted
  claim rows landed near each other. Just 5 threads ever contained a real
  `exchange`, and only 17 of 2605 contain any talk row at all. Half the board's
  bytes bought five conversations in four and a half days.
- Everything the row gestures at is available, authoritatively, from
  `swarm_claims.py` and `claim.sh`, both of which actually enforce.

It is rejected anyway, on two grounds:

1. **It is the doc-thread feature's only producer.** Enrolling only LISTENS.
   Without this row no real session ever sets a `thread` field, so
   `thread_key`, `group_by_thread`, `alive`, `exchange`, `comms threads`, the
   Discord board lane, and `scripts/comms_compile_threads.py` all lose their
   entire input. Deleting the row deletes that stack by starvation, silently,
   which is a worse failure mode than the one being fixed.
2. **The failure is in the DETECTORS, not in the idea.** The Write/Edit leg
   reports a verbatim `file_path` from a real tool call: a true statement.
   Unclaimed co-presence, two seats on one file when neither registered a
   claim, is precisely the collision case `claim.sh` structurally cannot see,
   because it only knows about seats that claimed. That signal is worth having.

**The fact that would flip this ruling.** Re-run the payoff enumerator after
the DEF-1/2/3 emitter fixes land and a full working week has passed. If
`threads_exchange` is still under 10 and the co-presence-alive threads have not
prevented a demonstrated collision, the row has been given a fair trial and
should be deleted, along with the doc-thread stack it feeds. Deletion is
deferred on evidence, not refused on principle.

## Where the authoritative answers live

| Question | Ask this, never the board |
|---|---|
| Does seat S hold a claim on X right now? | `~/.claude/state/claims/claims.tsv` via `hooks/claim.sh`, or `lib/swarm_claims.py` for run-scoped claims |
| Did seat S actually write bytes to X? | `~/.claude/state/session-writes/<session>.sha` |
| Who last changed X? | `git log`, `git blame` |
| Is anyone plausibly on X right now? | `comms threads` and read `alive` as CO-PRESENCE, then go ask them |

## A note on how these rows reach you

Heartbeat delivery injects board rows into an agent's context under the header
"Peer messages (data from sibling agents...)". For an auto-emitted claim row
that header is itself misleading: no sibling composed it. Any agent quoting one
of these rows back to a human owes the caveat in this file's first paragraph.

## Open items this file does not land

These are named with owners so a stale pointer leaves a witness:

1. Row text to past tense with a self-describing suffix.
   `adapters/claude-code/swarm-heartbeat.sh:784-787`. Owner: fix-wave seat O1.
2. `📌 Taking this on:` overstates the row. `lib/comms_render.py:41`.
   Unowned as of this writing.
3. Rows carry no marker of which leg produced them, so a reader cannot separate
   the trustworthy Write/Edit leg from the Bash heuristic. Owner: fix-wave seat
   O1. Until it exists, the entitled conclusion is the weak one.

## What IS landed by this file

- This ruling.
- `README.md` points here from the model section, the claims-arbiter bullet,
  the emoji legend, the Quickstart, and the file map, and no longer says the
  board row is "posted as you".
- `lib/swarm_threads.py` docstrings: `alive()` is documented as CO-PRESENCE,
  not conversation, with the 2026-08-31 measurement inline; `CLAIM_KIND`'s
  comment names the collision instead of asserting "it is a claim".
- `tests/test_board_row_contract.py`: eleven tests pinning the sentences above
  to code, each with a positive control, mutation-verified against three
  mutants (exchange stops excluding claims; a registry token enters the beat
  path; the README pointer is removed). All three were caught.

No behaviour changed. `alive()` computes exactly what it computed before.
