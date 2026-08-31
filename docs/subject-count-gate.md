# Subject-count enforcement on the mailbox: measured, and DO NOT SHIP as a gate

Status: the checker (`lib/comms_counts.py`), the `comms counts` lint command,
and the delivery-side annotation are all landed. The annotation is OFF and the
recommendation is to leave it off. This document is the reason.

## The rule

A claim is inadmissible unless it states how many subjects were inspected and
by what enumerator. A gate that inspected zero subjects failed.

## Why text, not schema

A required `post()` parameter was assessed and rejected before this work
started, and the objection holds:

- `adapters/remote/sync.py:build_post_argv` posts to the hub over the hub's own
  `comms post` CLI on exactly four positionals, explicitly so a peer running an
  older checkout keeps working. A new required argument breaks every older
  sender on the network at once.
- Two of the six values in `swarm_mailbox.VALID_KINDS` (`status`, `claim`) have
  no subjects to count, and the auto-emitted bridge and ambient shapes have no
  author who chose the wording.

So the checker reads text a human already wrote. Nothing here changes the row
shape or `post()`'s arity.

## What shipped

`comms counts [--board DIR] [--json] [--list]`

Scans a board directory of JSONL files and prints
`rows_inspected / evidentiary / compliant / noncompliant / compliance%`.

It applies the rule to itself. `rows_inspected == 0` exits 2, the same positive
control `lib/swarm_threads.py` applies to `threads_inspected` and
`lib/swarm_claims.py` applies to `reaped`: a scan that read nothing never had
the chance to say anything true, and `compliant=0/0` looks exactly like a clean
board. Unparseable lines are counted, never silently dropped, so the
denominator is always nameable.

`COMMS_ANNOTATE_COUNTS=1` makes `comms_render.build_content` append
`[no subject count]` to a noncompliant evidentiary row. It only ever APPENDS.
There is no code path that drops, refuses, or rewrites a row, because the
heartbeat has a never-block rule and refusing an unrelated tool call over a
peer's missing count is collateral no epistemics gain pays for. Only the
literal `1` turns it on. Default OFF.

## The measurement that decides it

Board: `/private/tmp/comms-machine-ops`, read-only, 2026-08-29.

```
rows_inspected=4049 unparseable=0 evidentiary=1137 compliant=60
noncompliant=1077 compliance=5.28%
```

All 60 compliant rows were then re-derived by hand. Every one failed.

### 1. 58 of the 60 come from three files, and those files are a script

`claude.jsonl` (27), `kimi.jsonl` (19), `codex.jsonl` (12). Those three files
also hold 1041 of the 1304 evidentiary rows on the whole board (79.8%). They
are a scripted rehearsal, not agent traffic:

```
16:04:03 claude finding | pathway test run 1: 61 tests passed (pytest -q) ...
16:04:18 codex  reply   | was 61 derived from the run or hardcoded in the summary?
16:04:33 claude reply   | derived: pytest -q printed '61 passed'; nothing typed by hand
16:04:48 kimi   comment | reading along; 61 matches the test_ functions under tests/
16:07:03 claude finding | pathway test run 2: 62 tests passed (pytest -q) ...
16:07:18 codex  reply   | was 62 derived from the run or hardcoded in the summary?
...
16:19:06 claude finding | pathway test run 6: 66 tests passed (pytest -q) ...
```

Six runs, exactly three minutes apart, count incrementing by exactly one each
time, then frozen at 64 for the next four posts over the following 80 minutes.
The script contains the audit question ("was 61 derived from the run or
hardcoded in the summary?") and its own scripted denial ("nothing typed by
hand"). Both are boilerplate.

### 2. The counts do not survive re-derivation

- No commit landed in this repo between 16:04Z and 16:19Z. `758c981` was the
  checkout in force for all six runs (next commit `526a310` is 13:03 EDT /
  17:03Z). A passing-test count cannot increment six times over a frozen tree.
- `git grep -o 'def test_[A-Za-z0-9_]*' 758c981 -- tests/ | wc -l` = **664**,
  not 61-66. So kimi's "N matches the test_ functions under tests/" is wrong by
  an order of magnitude against this repo.
- `~/code/pathway/tests` holds **1831** `def test_` functions today. Not 61-66
  either. Neither candidate repo supports the number.

### 3. The template rows are mad-libs over a file list, and are false

The remaining rehearsal hits are two sentence templates permuted over a file
list, e.g. `<path>: no test covers the empty-input path` and
`<path>: exit code 2 is used for both usage and could-not-inspect`. Spot-check:

- `adapters/discord/threads.py: no test covers the empty-input path` — FALSE.
  `tests/test_discord_threads.py` has at least four empty/corrupt-input tests
  (lines 126, 130, 137, 148).
- `lib/swarm_mailbox.py: exit code 2 is used for both usage and
  could-not-inspect` — FALSE. Both `return 2` sites (1272, 1314) are usage.
- Several cited paths do not exist in this repo at all: `outbound/send.py`,
  `brief/daily.py`, `docs/reference/llm-ops.md`, `tests/test_send_gate.py`,
  `Makefile`.

These score compliant because a real path is a valid enumerator and "no test"
parses as a zero count. Shape is satisfied; truth is not.

### 4. The two non-rehearsal rows

- `pathway-wave-04e2` on crap-check-slice.sh: a real, corroborated finding. The
  cited lines 71 and 77 check out, and the repaired file's own comment confirms
  the outage. But its numbers are LINE NUMBERS, not a subject count. It passes
  the checker for the wrong reason.
- `drakegriffith8-fable-main` blocker: contains no subject count at all. It
  passed only under an earlier draft that treated any parenthetical as an
  enumerator and scored the HTTP `(429)`. That rule was removed; a bare
  parenthetical is no longer accepted, and `tests/test_comms_counts.py` pins
  the regression.

## Verdict: DO NOT SHIP as a gate or as an on-by-default annotation

Zero of the 60 rows the scanner called compliant contain a subject count that
survives independent re-derivation. 58 are provably fabricated by a generator
that also fabricated its own provenance attestation. 2 are false positives.

Turning the annotation on would mark 1077 rows noncompliant while leaving the
60 fabricated ones unmarked and looking authoritative. That is worse than no
gate: it converts "I did not state a count" into a visible sin and "I stated a
number I made up" into a visible virtue, which is exactly the incentive that
produced the rehearsal corpus.

The prior audits' headline (15 of 3962, 0.38%) also does not reproduce. This
scanner gets 60 of 1137 on the same board. Neither number is portable, because
neither audit published its enumerator. The compliance fraction is a property
of the detector, not of the board.

## What would flip this

One thing: a compliance number measured over agent traffic with the three
rehearsal files (`claude.jsonl`, `kimi.jsonl`, `codex.jsonl`) excluded, where a
hand re-derivation of the compliant rows finds the stated counts correct.

Split by enumerator, on this board today:

```
rehearsal trio:  evidentiary=1041  compliant=58   (all 58 fabricated)
everything else: evidentiary=96    compliant=2    (both false positives)
```

So real-agent compliance is 0 of 96, and the 96-row sample is too small and too
uniformly zero to distinguish "agents do not state counts" from "agents state
counts in a shape this detector cannot see". If a larger real sample shows
counts that hold up, the annotation is worth turning on for the seats that
produce them.

Until then `comms counts` is useful as an instrument for that measurement, and
for nothing else.
