# Push installer checklist

Fill this in AFTER `probe-verdict.sh` exits 0 for your runtime. It takes you
from a passing probe to an adapter at parity with `adapters/codex/`. Exit 1 or
exit 2 means stop: there is no push adapter to write yet.

Copy the block at the bottom into the PR or the ticket and tick it there, so the
evidence lands where someone can find it later.

## 0. The verdict is a measurement

- [ ] Runtime name and **version**: ______________________
- [ ] Date of the probe: ______________________
- [ ] `probe-verdict.sh` exit code was **0**, positive control PASSED.
- [ ] Evidence paths recorded (probe dir, stdin copy, hook stdout, agent answer)
      or their contents quoted in the adapter README.
- [ ] One sentence naming what re-probe would FLIP this verdict. A category is a
      dated measurement, not a property of the product.

## 1. Reuse the one heartbeat

- [ ] The adapter wires `adapters/claude-code/swarm-heartbeat.sh`. It does not
      copy it, fork it, or wrap it in a second script that re-implements any of
      the identity gate, the arm gate, the subscription filter, or the cursor
      rules. Those live in one file so they have one place to drift.
- [ ] Nothing under `bin/` or `lib/` changed. Check it:
      `git diff --stat origin/master -- bin lib` is empty. If it is not, you
      have a core ticket, not an adapter.
- [ ] The runtime's name appears in no file under `bin/` or `lib/`.

## 2. The installer

Model: `adapters/codex/install.sh`. It is one screen; read it before writing
yours.

- [ ] `adapters/<name>/install.sh`, run as `bash adapters/<name>/install.sh`.
- [ ] **Idempotent**: an entry already mentioning `swarm-heartbeat.sh` is
      detected and left alone. Running it twice adds one entry, not two.
- [ ] **Never clobbers**: unrelated hooks, other events, and unrelated keys in
      the config survive. An unparseable config is REFUSED, not rewritten --
      rewriting destroys whatever the broken bytes were.
- [ ] **Creates what is missing**: absent file and absent parent dir are fine.
- [ ] **Writes atomically**: temp file then `os.replace`, so an interrupted
      install cannot leave a half-written config.
- [ ] **Env override for the target file** (`COMMS_SETTINGS`,
      `COMMS_CODEX_HOOKS`, `COMMS_<NAME>_HOOKS`), so tests never touch the real
      one. Documented in the README.
- [ ] Exit codes: `0` wired or already wired, `1` failed. Failure prints WHAT
      failed to stderr.

## 3. Honest post-install verification

- [ ] The installer's success message claims only what it checked. "Wired" means
      the entry is in the file, not that delivery works.
- [ ] Anything that could not be verified exits non-zero and says
      could-not-verify. **Could-not-verify is not a pass.** Silence is not
      evidence.
- [ ] The README names the DELIVERY ORACLE for this runtime: `swarm-heartbeat.log`
      in the state dir plus the mailbox files. Seat self-reports UNDERCOUNT -- an
      agent that received an injection does not reliably mention it.

## 4. The README

Shape: `adapters/grok/README.md`. Category and the one-line reason, then the
measurement, then the install command, then the hazards.

- [ ] Opens with the category (`push`) and why, in one line.
- [ ] The measurement: version, date, what was observed, pointer to the
      evidence.
- [ ] The install command, and the env override that redirects it.
- [ ] The closed kind vocabulary quoted verbatim:
      `finding|claim|blocker|comment|reply|status`.
- [ ] Cursor semantics named, not blurred: the CLI read cursor
      (`(runid, seat, view)`) and the heartbeat cursor (`(runid, agent_id)`) are
      different files with different keys. Push adapters INHERIT the heartbeat
      cursor by reusing the script; they do not redefine it.
- [ ] Hazards, **including the ones you did not fix** -- silent trust gates,
      configs the runtime scans by default, headless-only flags.
- [ ] No secrets: webhook URLs, hosts and keys live in the environment.

## 5. Registration and tests

- [ ] One row added to the README's per-runtime table and one line to its Layout
      block. There is no registry file; that is the whole registration step.
- [ ] One row added to the category table in `adapters/CONTRACT.md`, with what
      was measured and when.
- [ ] A test covering the installer's idempotence and its refusal to clobber,
      pointed at a temp file via the env override. Isolated: it writes nothing
      into real state and is green on a repeat run.
- [ ] `python3 -m pytest tests -q`, `bash tests/test_swarm_heartbeat.sh`,
      `bash tests/test_comms_cli.sh` and `bash tests/test_push_probe.sh` all
      green.

## Paste-into-the-PR block

```
Runtime:        <name> <version>
Probe date:     <YYYY-MM-DD>
Verdict:        PUSH (probe-verdict.sh exit 0, positive control PASSED)
Evidence:       <probe dir or quoted stdin-copy.json / hook-stdout.json / answer>
Would flip it:  <the re-probe that would change this>
Heartbeat:      reuses adapters/claude-code/swarm-heartbeat.sh (not forked)
Core touched:   none (git diff --stat origin/master -- bin lib is empty)
Install:        bash adapters/<name>/install.sh   (override: COMMS_<NAME>_HOOKS)
Idempotent:     yes -- second run detects and leaves alone
Hazards:        <the ones you did not fix>
```
