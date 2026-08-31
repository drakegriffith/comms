#!/bin/bash
# comms-retire-preflight.sh -- run this BEFORE deleting a duplicate comms
# install. It answers three questions, in this order, and refuses to
# green-light the deletion unless all three come back clean:
#
#   1. Is there really more than one install, and which one is AUTHORITATIVE?
#   2. Who REFERS to the one you are about to delete -- as a document, and as
#      an ACTIVE HOOK REGISTRATION -- and does any of those references resolve
#      to a file the reinstall will NOT put back?
#   3. Does the authoritative install actually round-trip a message right now?
#
# THE SEVEN REFUSAL CONDITIONS, in the order they are checked
#   R1  the doomed path IS the authoritative install, or contains it.
#   R2  a file under the doomed path has NO successor in the authoritative
#       install, so "delete and reinstall" is a deletion, not a round trip.
#   R3  uncommitted or untracked content under the doomed path -- bytes with
#       exactly one copy.
#   R4  no remote-tracking ref contains HEAD, so even the tracked bytes exist
#       only on this disk. (Not in a git checkout at all is the same refusal.)
#   R5  an ACTIVE HOOK REGISTRATION names a path under the doomed directory.
#       Escalated when the registration is GATE mode: deleting it does not
#       degrade a feature, it starts refusing every matching tool call on the
#       machine.
#   R6  a document referrer resolves to an R2 orphan -- a HARD referrer, one
#       that retargeting cannot fix because there is nothing to retarget to.
#   R7  the surviving install cannot round-trip a message right now.
#
# WHY THIS EXISTS -- THE ANTIPATTERN IT IS BUILT AGAINST
#   $HOME/.claude/hooks/swarm-heartbeat.sh exits 0 when its checkout is
#   missing. Silence on a missing dependency is how the duplicate install
#   survived unnoticed for weeks: nothing ever said "the thing I depend on is
#   not there". This checker therefore has NO quiet pass. Every outcome that
#   is not a proven green is either a REFUSAL (exit 1) or a
#   COULD-NOT-DETERMINE (exit 2), and exit 2 is NOT a pass.
#
# THE POSITIVE CONTROL RULE
#   A gate that inspected zero subjects has failed, not passed. This script
#   counts and PRINTS its subjects before it judges them: installs found,
#   referrer files scanned, files enumerated under the doomed path. If any of
#   those counts is zero, the scan did not happen and the verdict is exit 2.
#
# WHAT THIS DOES NOT DO (prior art it complements, not duplicates)
#   claude-harness carries lib/swarm/tests/test_mailbox_name_agrees_with_reader.py,
#   which asserts that the harness-side WRITER and the comms-side READER agree
#   on the mailbox directory name -- a RUNTIME agreement between two live
#   halves, asserted continuously. This script asks a different, one-shot
#   question: is this copy on disk safe to remove. Neither subsumes the other.
#   That test proves two survivors still talk; this checker proves the one you
#   are about to kill is not carrying anything unique. Run both.
#
# EXIT CODES
#   0  GREEN   -- every check has a stated enumerator with a nonzero count,
#                 and every count that had to be zero was zero.
#   1  REFUSE  -- a hard dependency, an orphan file, uncommitted content, a
#                 failed round-trip, or the doomed path IS the authority.
#   2  CANNOT DETERMINE -- the scan itself did not run. NOT A PASS.
#
# USAGE
#   scripts/comms-retire-preflight.sh --doomed <dir> [--authoritative <dir>]
#         [--referrer-root <path>]...   (repeatable; replaces the defaults)
#         [--marker <string>]...        (repeatable; ADDS to the defaults)
#         [--manifest <file>]           (where to write the backup manifest)
#
#   --doomed          the install you intend to delete.
#   --authoritative   the install that must survive. Omitted => inferred, and
#                     the inference is printed with its evidence.
#   --marker          an extra string that means "this refers to the doomed
#                     install". Defaults cover the ABSOLUTE path AND the
#                     RELATIVE spellings, because the referrers that hurt most
#                     are relative ("read lib/swarm/ROLES.md first") and an
#                     absolute-path grep is blind to every one of them.
#
# This script only READS. It writes exactly one file: the backup manifest,
# under a path you name (default: a mktemp file, printed).

set -uo pipefail

# ---------------------------------------------------------------------------
# args
# ---------------------------------------------------------------------------
DOOMED=""
AUTHORITATIVE=""
MANIFEST=""
REFERRER_ROOTS=()
EXTRA_MARKERS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --doomed)         DOOMED="${2:-}"; shift 2 ;;
    --authoritative)  AUTHORITATIVE="${2:-}"; shift 2 ;;
    --referrer-root)  REFERRER_ROOTS+=("${2:-}"); shift 2 ;;
    --marker)         EXTRA_MARKERS+=("${2:-}"); shift 2 ;;
    --manifest)       MANIFEST="${2:-}"; shift 2 ;;
    -h|--help)        sed -n '2,50p' "$0"; exit 2 ;;
    *) echo "preflight: unknown argument: $1" >&2; exit 2 ;;
  esac
done

say()    { echo "$*"; }
head2()  { echo; echo "== $* =="; }
cannot() { echo; echo "PREFLIGHT: CANNOT DETERMINE (exit 2 is NOT a pass): $*" >&2; exit 2; }

[ -n "$DOOMED" ] || cannot "no --doomed path given; there is nothing to check"
[ -d "$DOOMED" ] || cannot "--doomed $DOOMED is not a directory (already gone? then there is nothing to preflight, and nothing to restore)"

DOOMED="$(cd "$DOOMED" && pwd -P)"
[ -n "$AUTHORITATIVE" ] && [ -d "$AUTHORITATIVE" ] && AUTHORITATIVE="$(cd "$AUTHORITATIVE" && pwd -P)"

if [ "${#REFERRER_ROOTS[@]}" -eq 0 ]; then
  # $HOME/.comms is deliberately NOT here. It is machine STATE -- mailbox rows,
  # arm rosters, cursors -- and a row that mentions a path is a record of
  # something that happened, not a dependency that breaks. Sweeping it produced
  # 190+ "referrers" that were all one seat's subscription list, which is how a
  # useful refusal gets buried under a true-but-worthless one.
  REFERRER_ROOTS=(
    "$HOME/.claude"
    "$HOME/.codex"
    "$HOME/Library/LaunchAgents"
    "$HOME/.zshrc"
    "$HOME/.bashrc"
    "$HOME/.zshenv"
    "$HOME/.profile"
  )
fi

REFUSALS=()
refuse() { REFUSALS+=("$1"); echo "  REFUSE: $1"; }

say "comms retire preflight"
say "  doomed path : $DOOMED"
say "  run at      : $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# ---------------------------------------------------------------------------
# THE UNIT. Stated before anything is counted, because a count whose unit is
# undeclared is a number nobody can re-derive.
#
#   INSTALL   = a directory that contains BOTH a mailbox module and a
#               dispatcher, in one of two known shapes:
#                 repo shape           lib/swarm_mailbox.py + bin/comms
#                 pre-extraction shape lib/swarm/swarm_mailbox.py
#                                      + comms/bin/comms
#   REFERRER  = one FILE, anywhere under a referrer root, containing at least
#               one marker string for the doomed install.
#   SUBJECT   = one regular FILE under the doomed path, excluding __pycache__
#               and .git internals.
# ---------------------------------------------------------------------------

is_install() {  # is_install <dir> -> echoes shape, rc 0 if an install
  local d="$1"
  if [ -f "$d/lib/swarm_mailbox.py" ] && [ -f "$d/bin/comms" ]; then
    echo "repo"; return 0
  fi
  if [ -f "$d/lib/swarm/swarm_mailbox.py" ] && [ -f "$d/comms/bin/comms" ]; then
    echo "pre-extraction"; return 0
  fi
  return 1
}

head2 "1. installs found (unit: INSTALL, defined above)"

# The doomed path is often a SUBTREE of an install, not the install root:
# ~/.claude/comms is four files inside the ~/.claude install. Walk up to the
# enclosing install so the authority comparison has something to compare, but
# keep the deletion subject as the narrow path the user actually named. This
# distinction is load-bearing -- "delete the duplicate" and "delete the
# directory named comms" are different operations with different blast radii.
DOOMED_INSTALL="$DOOMED"
probe="$DOOMED"
while [ "$probe" != "/" ] && [ -n "$probe" ]; do
  if is_install "$probe" >/dev/null; then DOOMED_INSTALL="$probe"; break; fi
  probe="$(dirname "$probe")"
done
[ "$DOOMED_INSTALL" != "$DOOMED" ] && \
  say "  note: the doomed path is a SUBTREE of the install at $DOOMED_INSTALL"

CANDIDATES=("$DOOMED" "$DOOMED_INSTALL")
[ -n "$AUTHORITATIVE" ] && CANDIDATES+=("$AUTHORITATIVE")
CANDIDATES+=("$HOME/.claude" "$HOME/.comms" "$HOME/code/comms")
for d in "$HOME"/code/*/; do CANDIDATES+=("${d%/}"); done
[ -n "${COMMS_CHECKOUT:-}" ] && CANDIDATES+=("$COMMS_CHECKOUT")

INSTALL_PATHS=()
INSTALL_SHAPES=()
seen=""
for c in "${CANDIDATES[@]}"; do
  [ -d "$c" ] || continue
  p="$(cd "$c" && pwd -P)"
  case "$seen" in *"|$p|"*) continue ;; esac
  seen="$seen|$p|"
  if shape="$(is_install "$p")"; then
    INSTALL_PATHS+=("$p")
    INSTALL_SHAPES+=("$shape")
    say "  INSTALL  $p  (shape: $shape)"
  fi
done

INSTALL_COUNT="${#INSTALL_PATHS[@]}"
say "  installs_found = $INSTALL_COUNT"

# Positive control on the discovery itself. Zero installs means the detector
# is broken or pointed at the wrong roots -- it does NOT mean "nothing to
# worry about". One install means the premise of this whole exercise (a
# DUPLICATE exists) was not reproduced, so there is nothing safe to say about
# deleting anything.
[ "$INSTALL_COUNT" -ge 1 ] || cannot "installs_found = 0; the install detector matched nothing, including the --doomed path you named. Fix the detector, do not delete anything"

# Two installs is the DEFAULT requirement, not an absolute one. The doomed path
# is often a fragment -- one directory of a larger tree, or the remains of a
# partly-removed copy -- which no whole-install detector will ever match. In
# that case the operator names the survivor explicitly, and the burden of the
# claim moves to them: the survivor must be a detected install, and it must not
# be the thing being deleted. Everything after this point (orphans, hooks,
# referrers, round trip) works the same either way, and those are the checks
# that actually protect anything.
if [ "$INSTALL_COUNT" -lt 2 ]; then
  if [ -z "$AUTHORITATIVE" ]; then
    cannot "installs_found = 1 (${INSTALL_PATHS[0]}) and no --authoritative was named, so there is no second copy to fall back on. If $DOOMED is a fragment of a removed install, name the survivor with --authoritative; if it is the SOLE install, stop"
  fi
  found_auth=0
  for p in "${INSTALL_PATHS[@]}"; do
    [ "$p" = "$AUTHORITATIVE" ] && found_auth=1
  done
  [ "$found_auth" -eq 1 ] || cannot "the named --authoritative ($AUTHORITATIVE) is not a detected install, so the survivor cannot be confirmed to be one"
  say "  note: only one whole install matched; proceeding on the explicitly named survivor $AUTHORITATIVE"
fi

# ---------------------------------------------------------------------------
# 2. authority. Evidence, not assertion.
# ---------------------------------------------------------------------------
head2 "2. which install is authoritative"

# Evidence A -- WIRED: a runtime hook config names a file under this install.
# A registration writes "$HOME/...", so grep the $HOME-relative spelling as
# well as the absolute one. Grepping only the absolute path scores every real
# config at zero and makes the evidence look uniformly empty.
wired_score() {
  local p="$1" n=0 f rel=""
  case "$p" in "$HOME"/*) rel="\$HOME/${p#$HOME/}" ;; esac
  for f in "$HOME/.claude/settings.json" "$HOME/.claude/settings.local.json" \
           "$HOME/.codex/hooks.json" "$HOME/.gemini/settings.json"; do
    [ -f "$f" ] || continue
    if grep -Fq "$p/" "$f" 2>/dev/null; then n=$((n + 1)); continue; fi
    if [ -n "$rel" ] && grep -Fq "$rel/" "$f" 2>/dev/null; then n=$((n + 1)); fi
  done
  echo "$n"
}

# Evidence B -- LIVE STATE: import THIS install's swarm_arm and ask it where
# its default state dir is, then count armed runs there. The pre-extraction
# copy answers ~/.claude/state; the extracted repo answers ~/.comms/state.
# The install whose OWN default state dir holds armed runs is the one the
# live board is actually using. This is the measurement that distinguishes a
# copy that "works and self-tests" from the copy that is being used.
live_state_score() {
  local p="$1" libdir=""
  if [ -f "$p/lib/swarm_arm.py" ]; then libdir="$p/lib"
  elif [ -f "$p/lib/swarm/swarm_arm.py" ]; then libdir="$p/lib/swarm"
  else echo "0 (no swarm_arm.py)"; return; fi
  COMMS_PREFLIGHT_LIB="$libdir" python3 - <<'PY' 2>/dev/null || echo "0 (import failed)"
import os, sys
lib = os.environ["COMMS_PREFLIGHT_LIB"]
sys.path.insert(0, lib)
# Ask the module, never re-derive: a second copy of the default-state-dir
# rule here would be exactly the drift this whole exercise is cleaning up.
for k in ("COMMS_STATE_DIR", "SWARM_ARM_STATE_DIR"):
    os.environ.pop(k, None)
import swarm_arm
state = swarm_arm._default_state_dir()
armed = os.path.join(state, "swarm-arm")
try:
    runs = [d for d in os.listdir(armed) if os.path.isdir(os.path.join(armed, d))]
except OSError:
    runs = []
print("%d (default state %s, armed runs %d)" % (len(runs), state, len(runs)))
PY
}

# Evidence C -- TRACKED: is it a git checkout with a remote? An install that
# is not tracked anywhere is unrecoverable by definition.
git_score() {
  local p="$1"
  if git -C "$p" rev-parse --git-dir >/dev/null 2>&1; then
    local r; r="$(git -C "$p" remote get-url origin 2>/dev/null)"
    echo "tracked${r:+, origin $r}"
  else
    echo "UNTRACKED"
  fi
}

BEST=""
BEST_LIVE=-1
i=0
while [ "$i" -lt "$INSTALL_COUNT" ]; do
  p="${INSTALL_PATHS[$i]}"
  w="$(wired_score "$p")"
  l="$(live_state_score "$p")"
  g="$(git_score "$p")"
  lnum="${l%% *}"
  say "  $p"
  say "      wired into a runtime hook config : $w file(s)"
  say "      armed runs under its own default : $l"
  say "      version control                  : $g"
  if [ "$lnum" -gt "$BEST_LIVE" ]; then BEST_LIVE="$lnum"; BEST="$p"; fi
  i=$((i + 1))
done

if [ -z "$AUTHORITATIVE" ]; then
  AUTHORITATIVE="$BEST"
  say "  inferred authoritative: $AUTHORITATIVE (most armed runs under its own default state dir)"
else
  say "  authoritative (given) : $AUTHORITATIVE"
fi

[ -n "$AUTHORITATIVE" ] || cannot "no authoritative install could be identified; name one with --authoritative rather than guessing"

if [ "$BEST_LIVE" -le 0 ]; then
  cannot "every install reports 0 armed runs under its own default state dir, so the live-board evidence is empty. Either the board is genuinely idle (arm a run and re-run this) or the probe is broken. An authority claim with no evidence is a guess"
fi

head2 "verdict gate: is the doomed path itself the authority?"
if [ "$DOOMED" = "$AUTHORITATIVE" ]; then
  refuse "the path you asked to delete IS the authoritative install"
elif case "$AUTHORITATIVE" in "$DOOMED"/*) true ;; *) false ;; esac; then
  refuse "the authoritative install ($AUTHORITATIVE) lives INSIDE the doomed path; deleting the doomed path takes the authority with it"
else
  say "  ok: doomed is neither the authority nor a parent of it"
fi

# ---------------------------------------------------------------------------
# 3. what does the reinstall actually put back?
#
#   This is the map from "a relative path inside the doomed install" to "the
#   relative path in the authoritative install that replaces it". A doomed
#   file with NO entry here is an ORPHAN: reinstalling does not restore it,
#   so deleting it destroys it.
# ---------------------------------------------------------------------------
head2 "3. orphans (unit: SUBJECT -- one regular file under the doomed path)"

export COMMS_PF_DOOMED="$DOOMED" COMMS_PF_AUTH="$AUTHORITATIVE"
ORPHAN_LIST="$(python3 - <<'PY'
import os, sys

doomed = os.environ["COMMS_PF_DOOMED"]
auth = os.environ["COMMS_PF_AUTH"]

# The pre-extraction -> extracted rename map. Every rule here is a CLAIM that
# the extracted repo carries the same content under a new name; it is checked
# below by asserting the target actually exists.
RULES = [
    ("comms/bin/comms",          "bin/comms"),
    ("comms/install.sh",         "adapters/claude-code/install.sh"),
    ("comms/README.md",          "README.md"),
    ("comms/tests/",             "tests/"),
    ("lib/swarm/tests/",         "tests/"),
    ("lib/swarm/",               "lib/"),
    ("hooks/swarm-heartbeat.sh", "adapters/claude-code/swarm-heartbeat.sh"),
    ("bin/",                     "bin/"),
    ("tests/",                   "tests/"),
    ("lib/",                     "lib/"),
    ("install.sh",               "adapters/claude-code/install.sh"),
    ("README.md",                "README.md"),
]

def counterpart(rel):
    for src, dst in RULES:
        if src.endswith("/"):
            if rel.startswith(src):
                return dst + rel[len(src):]
        elif rel == src:
            return dst
    return None

subjects = 0
orphans = []
mapped = 0
for dirpath, dirnames, filenames in os.walk(doomed):
    dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
    for fn in filenames:
        if fn.endswith(".pyc") or fn == ".DS_Store":
            continue
        full = os.path.join(dirpath, fn)
        rel = os.path.relpath(full, doomed)
        subjects += 1
        cp = counterpart(rel)
        if cp is None or not os.path.exists(os.path.join(auth, cp)):
            orphans.append(rel)
        else:
            mapped += 1

sys.stderr.write("  subjects_inspected = %d\n" % subjects)
sys.stderr.write("  restored_by_reinstall = %d\n" % mapped)
sys.stderr.write("  orphans = %d\n" % len(orphans))
if subjects == 0:
    sys.stderr.write("  ZERO SUBJECTS -- the walk inspected nothing\n")
    sys.exit(3)
for o in sorted(orphans):
    print(o)
PY
)"
PY_RC=$?
if [ "$PY_RC" -eq 3 ]; then
  cannot "the orphan walk inspected 0 files under $DOOMED; a scan of nothing is not a clean scan"
elif [ "$PY_RC" -ne 0 ]; then
  cannot "the orphan walk failed (rc=$PY_RC); no orphan claim can be made"
fi

ORPHAN_COUNT=0
if [ -n "$ORPHAN_LIST" ]; then
  ORPHAN_COUNT="$(printf '%s\n' "$ORPHAN_LIST" | wc -l | tr -d ' ')"
  say "  files the reinstall will NOT restore:"
  printf '%s\n' "$ORPHAN_LIST" | sed 's|^|      |'
fi

if [ "$ORPHAN_COUNT" -gt 0 ]; then
  refuse "$ORPHAN_COUNT file(s) under the doomed path have no counterpart in $AUTHORITATIVE. Deleting is not 'removing a duplicate'; it is deleting content. Land these in the authoritative repo first, or move them somewhere they are tracked"
else
  say "  ok: every file under the doomed path has a counterpart in the authoritative install"
fi

# ---------------------------------------------------------------------------
# 4. uncommitted content. A file that exists nowhere but this disk is
#    unrecoverable no matter how good the referrer sweep was.
# ---------------------------------------------------------------------------
head2 "4. is the doomed content committed anywhere?"
if git -C "$DOOMED" rev-parse --git-dir >/dev/null 2>&1; then
  DIRTY="$(git -C "$DOOMED" status --porcelain -- "$DOOMED" 2>/dev/null)"
  DIRTY_COUNT=0
  [ -n "$DIRTY" ] && DIRTY_COUNT="$(printf '%s\n' "$DIRTY" | wc -l | tr -d ' ')"
  say "  git repo    : yes"
  say "  dirty_paths = $DIRTY_COUNT"
  if [ "$DIRTY_COUNT" -gt 0 ]; then
    printf '%s\n' "$DIRTY" | sed 's|^|      |'
    refuse "$DIRTY_COUNT uncommitted or untracked path(s) under the doomed install. Uncommitted bytes have exactly one copy, and the deletion is that copy's last day"
  fi
  # Recoverability is "some remote-tracking ref contains HEAD", not "the
  # current branch has an upstream". Asking the narrower question would refuse
  # a perfectly recoverable checkout, and a checker that cries wolf is a
  # checker people learn to skip.
  CONTAINING_REMOTES="$(git -C "$DOOMED" branch -r --contains HEAD 2>/dev/null | wc -l | tr -d ' ')"
  say "  remote refs containing HEAD = $CONTAINING_REMOTES"
  if [ "$CONTAINING_REMOTES" -eq 0 ]; then
    refuse "no remote-tracking ref contains HEAD, so the doomed content exists only on this disk. Push first; a deletion whose only undo is a local reflog is not reversible"
  else
    say "  ok: HEAD is reachable from a remote ref (git restores the tracked bytes)"
  fi
else
  say "  git repo    : NO"
  refuse "the doomed install is not inside any git checkout, so nothing under it is recoverable from a remote. Every byte here is single-copy"
fi

# ---------------------------------------------------------------------------
# 4b. ACTIVE HOOK REGISTRATION -- the strongest refusal in this script, and the
#     only one whose blast radius is the whole machine rather than the mailbox.
#
#     A hook is not a document reference. It is a command the runtime executes
#     on an event, and on this harness it runs through hook-shim.sh in one of
#     two modes:
#
#       observer  the hook's exit code is ignored. Deleting its target
#                 degrades a feature.
#       gate      THE HOOK'S EXIT CODE IS A DECISION. Exit 2 REFUSES the tool
#                 call that fired it. hook-shim's own _lkg_eligible guard
#                 withholds last-known-good when the target does not EXIST --
#                 a missing file is read as a deletion, not a tear -- so the
#                 gate fails closed. On a `"matcher": "*"` PostToolUse
#                 registration that means EVERY TOOL CALL IN EVERY SESSION ON
#                 THIS MACHINE starts getting refused, immediately, with the
#                 sessions already running.
#
#     So: delete a gate-mode target without rewiring first and the machine
#     stops, not the feature. The fix is an ORDER, not a warning -- rewire the
#     registration to the surviving path, prove the gate dispatch returns 0,
#     and only then delete. This section refuses until that has happened,
#     because the ordering is not something to leave to whoever is reading.
# ---------------------------------------------------------------------------
head2 "4b. active hook registrations naming the doomed path"

export COMMS_PF_DOOMED_HOOK="$DOOMED"
# NO APOSTROPHES inside this heredoc. A `<<'PY'` body nested in "$( ... )" is
# still scanned for the closing paren, and a lone ' in the body makes bash
# report an unterminated quote 200 lines away. Cost 15 minutes once; writing it
# down is cheaper than paying it again.
HOOK_REPORT="$(python3 - <<'PY'
import json, os, sys

doomed = os.environ["COMMS_PF_DOOMED_HOOK"]
home = os.path.expanduser("~")
# Both spellings of both sides. A registration says "$HOME/...", $HOME may be a
# symlink (/var -> /private/var on macOS), and the doomed path arrived here
# already resolved through realpath -- so one string comparison misses a
# that is unambiguously pointed at the doomed directory. Missing it here is a
# machine-wide outage, so this is the wrong place to be clever.
homes = {home, os.path.realpath(home)}
doomeds = {doomed, os.path.realpath(doomed)}
configs = [
    os.path.join(home, ".claude", "settings.json"),
    os.path.join(home, ".claude", "settings.local.json"),
    os.path.join(home, ".codex", "hooks.json"),
    os.path.join(home, ".gemini", "settings.json"),
]

inspected = 0
hits = []
for path in configs:
    if not os.path.exists(path):
        continue
    try:
        cfg = json.load(open(path))
    except Exception as exc:
        print("UNPARSEABLE\t%s\t%s" % (path, exc))
        continue
    hooks = cfg.get("hooks") or {}
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            matcher = entry.get("matcher", "")
            for h in entry.get("hooks") or []:
                if not isinstance(h, dict):
                    continue
                cmd = h.get("command") or ""
                inspected += 1
                # Expand the spellings a registration actually uses. This is
                # the whole reason a literal grep on the absolute path is not
                # enough: the live entry says "$HOME/.claude/hooks/...".
                matched = False
                for h_ in homes:
                    expanded = cmd.replace("$HOME", h_).replace("${HOME}", h_)
                    expanded = expanded.replace("~/", h_ + "/")
                    for d_ in doomeds:
                        if d_ in expanded:
                            matched = True
                if matched:
                    mode = "observer" if " observer " in cmd else (
                        "gate" if " gate " in cmd else "direct")
                    hits.append((path, event, matcher, mode, cmd))

print("INSPECTED\t%d" % inspected)
for path, event, matcher, mode, cmd in hits:
    print("HIT\t%s\t%s\t%s\t%s\t%s" % (path, event, matcher, mode, cmd))
PY
)"
HOOK_RC=$?
[ "$HOOK_RC" -eq 0 ] || cannot "the hook-registration scan failed (rc=$HOOK_RC)"

HOOK_INSPECTED="$(printf '%s\n' "$HOOK_REPORT" | awk -F'\t' '$1=="INSPECTED"{print $2}')"
[ -n "$HOOK_INSPECTED" ] || HOOK_INSPECTED=0
say "  hook_commands_inspected = $HOOK_INSPECTED"
# Zero registrations inspected is not "no hooks point at it". It is "the config
# files were absent or unreadable", and saying nothing points here on that
# basis is precisely the silent pass this script refuses to make.
[ "$HOOK_INSPECTED" -gt 0 ] || cannot "hook_commands_inspected = 0; no runtime hook config was read, so 'nothing is wired to the doomed path' rests on no evidence"

if printf '%s\n' "$HOOK_REPORT" | grep -q '^UNPARSEABLE'; then
  printf '%s\n' "$HOOK_REPORT" | grep '^UNPARSEABLE' | sed 's|^|      |'
  refuse "a runtime hook config could not be parsed, so its registrations could not be checked against the doomed path"
fi

HOOK_HITS="$(printf '%s\n' "$HOOK_REPORT" | grep '^HIT' || true)"
HOOK_HIT_COUNT=0
[ -n "$HOOK_HITS" ] && HOOK_HIT_COUNT="$(printf '%s\n' "$HOOK_HITS" | grep -c .)"
say "  registrations_naming_doomed = $HOOK_HIT_COUNT"

if [ "$HOOK_HIT_COUNT" -gt 0 ]; then
  GATE_HITS=0
  while IFS=$'\t' read -r _ cfg event matcher mode cmd; do
    say "      $cfg  $event  matcher=$matcher  mode=$mode"
    say "          $cmd"
    [ "$mode" = "gate" ] && GATE_HITS=$((GATE_HITS + 1))
  done <<< "$HOOK_HITS"
  if [ "$GATE_HITS" -gt 0 ]; then
    refuse "$GATE_HITS GATE-mode hook registration(s) name a path under the doomed directory. Deleting it makes the gate target vanish; hook-shim withholds last-known-good from a MISSING target and fails closed, so every matching tool call on this machine is refused from that instant. REWIRE the registration to the surviving path FIRST, prove the dispatch returns 0, delete SECOND. See docs/runbooks/retire-duplicate-install.md step 3"
  else
    refuse "$HOOK_HIT_COUNT active hook registration(s) name a path under the doomed directory. Retarget them before deleting; a hook whose target is gone is a feature that stops with no error attributable to the deletion"
  fi
else
  say "  ok: no active hook registration resolves into the doomed path"
fi

# ---------------------------------------------------------------------------
# 5. referrers.
# ---------------------------------------------------------------------------
head2 "5. referrers (unit: REFERRER -- one file containing >=1 marker)"

MARKERS=("$DOOMED")
case "$DOOMED" in
  "$HOME"/*) MARKERS+=("\$HOME/${DOOMED#$HOME/}" "~/${DOOMED#$HOME/}") ;;
esac
# Relative spellings. These are the ones an absolute-path grep never sees, and
# they are the ones that actually broke: a doctrine file that says "read
# lib/swarm/ROLES.md first" names no absolute path at all.
#
# Derived, never hand-listed: for each top-level entry E under the doomed
# path, the string "<basename>/E". That is the spelling a referrer uses when
# it interpolates a repo root -- bootstrap.sh writes "$REPO_ROOT/comms/
# install.sh", which no absolute-path grep and no "$HOME" grep will ever see,
# and which "comms/install.sh" catches on the first pass.
DOOMED_BASE="$(basename "$DOOMED")"
DOOMED_PARENT_BASE="$(basename "$(dirname "$DOOMED")")"
MARKERS+=("$DOOMED_PARENT_BASE/$DOOMED_BASE")
for e in "$DOOMED"/*; do
  [ -e "$e" ] || continue
  MARKERS+=("$DOOMED_BASE/$(basename "$e")")
done
for m in "${EXTRA_MARKERS[@]:-}"; do [ -n "$m" ] && MARKERS+=("$m"); done

say "  markers:"
for m in "${MARKERS[@]}"; do say "      $m"; done

# One grep, all markers, via -f. The obvious loop (for each file, for each
# marker, grep) is O(files x markers) process spawns and took over two minutes
# on a real ~/.claude with 17 markers -- long enough that an operator kills it,
# which turns the whole check into one nobody runs.
MARKERFILE="$(mktemp -t comms-pf-markers)"
printf '%s\n' "${MARKERS[@]}" > "$MARKERFILE"
trap 'rm -f "$MARKERFILE"' EXIT

FILELIST="$(mktemp -t comms-pf-files)"
trap 'rm -f "$MARKERFILE" "$FILELIST"' EXIT
: > "$FILELIST"
for root in "${REFERRER_ROOTS[@]}"; do
  [ -e "$root" ] || continue
  if [ -f "$root" ]; then
    printf '%s\n' "$root" >> "$FILELIST"
  else
    # TRANSCRIPTS ARE NOT REFERRERS. A session log that once mentioned the
    # doomed path is a record of the past; deleting the path cannot break it,
    # and including them buries the four files that CAN break under thousands
    # of megabyte JSONL rollouts. This is the one place the sweep is allowed to
    # be narrow, and the exclusions are named rather than guessed at by size
    # alone -- a big file is not the same claim as a log file.
    find "$root" -type f -size -2048k \
      ! -path '*/.git/*' ! -path '*/__pycache__/*' ! -name '*.pyc' \
      ! -path '*/sessions/*' ! -path '*/projects/*' ! -path '*/todos/*' \
      ! -path '*/shell-snapshots/*' ! -path '*/history/*' \
      ! -path '*/file-history/*' ! -path '*/state/*' ! -path '*/.venv/*' \
      ! -path '*/node_modules/*' ! -name '*.jsonl' ! -name '*.log' \
      ! -path "$DOOMED/*" >> "$FILELIST" 2>/dev/null
  fi
done
SCANNED="$(grep -c . "$FILELIST" | tr -d ' ')"
REFERRER_FILES=""
if [ "$SCANNED" -gt 0 ]; then
  REFERRER_FILES="$(tr '\n' '\0' < "$FILELIST" \
    | xargs -0 grep -lF -f "$MARKERFILE" -- 2>/dev/null)"
  [ -n "$REFERRER_FILES" ] && REFERRER_FILES="$REFERRER_FILES"$'\n'
fi

say "  files_scanned = $SCANNED"
# Positive control on the referrer sweep. A sweep that opened zero files
# proves nothing about referrers, and reporting "no referrers found" from it
# would be the exact silent-success failure this script exists to prevent.
[ "$SCANNED" -gt 0 ] || cannot "files_scanned = 0; the referrer sweep opened no files. Its roots (${REFERRER_ROOTS[*]}) do not exist or are unreadable. 'No referrers found' from a sweep that read nothing is not a finding"

REFERRER_COUNT=0
if [ -n "$REFERRER_FILES" ]; then
  REFERRER_COUNT="$(printf '%s' "$REFERRER_FILES" | grep -c . )"
fi
say "  referrers_found = $REFERRER_COUNT"

# Classify. A referrer is HARD when the thing it names is an orphan -- the
# reinstall will not put it back, so the reference breaks permanently. It is
# SOFT when the reinstall restores an equivalent, in which case the reference
# only needs its path updated.
HARD=0
SHOWN=0
SHOW_MAX="${COMMS_PF_SHOW_MAX:-25}"
if [ "$REFERRER_COUNT" -gt 0 ]; then
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    # Classify EVERY referrer; print the first SHOW_MAX. The count is the
    # finding, the listing is the evidence, and a listing long enough to scroll
    # the count off the screen destroys the finding it was meant to support.
    SHOWN=$((SHOWN + 1))
    quiet=0
    [ "$SHOWN" -gt "$SHOW_MAX" ] && quiet=1
    # Show at most 3 hits per file, each clipped to 160 columns. The evidence
    # a reader needs is "which line, roughly what it says", not the line.
    hits="$(grep -nF -f "$MARKERFILE" -- "$f" 2>/dev/null | head -3 | cut -c1-160)"
    hard_here=0
    if [ -n "$ORPHAN_LIST" ]; then
      while IFS= read -r orph; do
        [ -n "$orph" ] || continue
        base="$(basename "$orph")"
        if grep -Fq -- "$base" "$f" 2>/dev/null; then hard_here=1; break; fi
      done <<< "$ORPHAN_LIST"
    fi
    [ "$hard_here" -eq 1 ] && HARD=$((HARD + 1))
    if [ "$quiet" -eq 0 ]; then
      if [ "$hard_here" -eq 1 ]; then say "  HARD  $f"; else say "  soft  $f"; fi
      printf '%s\n' "$hits" | sed 's|^|          |'
    fi
  done <<< "$REFERRER_FILES"
  if [ "$SHOWN" -gt "$SHOW_MAX" ]; then
    say "  ... $((SHOWN - SHOW_MAX)) more referrer(s) not printed (COMMS_PF_SHOW_MAX to raise)"
  fi
fi

say "  hard_referrers = $HARD"
if [ "$HARD" -gt 0 ]; then
  refuse "$HARD referrer(s) resolve to a file the reinstall will not restore. Retarget or relocate each one BEFORE deleting"
fi

# ---------------------------------------------------------------------------
# 6. positive control: does the authoritative install round-trip right now?
#    Not a file-existence check -- post a message and read it back.
# ---------------------------------------------------------------------------
head2 "6. round-trip positive control on the authoritative install"
VERIFY="$(cd "$(dirname "$0")" && pwd -P)/comms-verify-roundtrip.sh"
if [ -x "$VERIFY" ]; then
  if bash "$VERIFY" --install "$AUTHORITATIVE"; then
    say "  ok: authoritative install round-trips"
  else
    refuse "the authoritative install failed its round-trip. Do not delete the fallback copy while the survivor cannot pass a message"
  fi
else
  cannot "round-trip verifier not found at $VERIFY; without it there is no proof the survivor works"
fi

# ---------------------------------------------------------------------------
# 7. backup manifest + verdict
# ---------------------------------------------------------------------------
head2 "7. backup manifest"
if [ -z "$MANIFEST" ]; then
  MANIFEST="$(mktemp -t comms-retire-manifest)"
fi
{
  echo "# comms retire preflight -- backup manifest"
  echo "# generated $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "# doomed:        $DOOMED"
  echo "# authoritative: $AUTHORITATIVE"
  echo "#"
  echo "# The backup MUST capture the whole doomed path. The lines below are the"
  echo "# subset that NOTHING ELSE has a copy of -- if the backup is lossy, these"
  echo "# are the bytes that are gone for good."
  if [ -n "$ORPHAN_LIST" ]; then
    printf '%s\n' "$ORPHAN_LIST"
  else
    echo "# (no orphans: every file has a counterpart in the authoritative install)"
  fi
} > "$MANIFEST"
say "  written: $MANIFEST"

head2 "verdict"
if [ "${#REFUSALS[@]}" -gt 0 ]; then
  say "PREFLIGHT: REFUSED -- do not delete $DOOMED"
  for r in "${REFUSALS[@]}"; do say "  - $r"; done
  exit 1
fi

say "PREFLIGHT: GREEN"
say "  installs_found     = $INSTALL_COUNT"
say "  files_scanned      = $SCANNED"
say "  hook_cmds_checked  = $HOOK_INSPECTED (naming the doomed path: $HOOK_HIT_COUNT)"
say "  referrers_found    = $REFERRER_COUNT (hard: $HARD)"
say "  orphans            = $ORPHAN_COUNT"
say "  round-trip         = pass"
say "  backup manifest    = $MANIFEST"
say ""
say "Green means the four counts above were MEASURED, not assumed. Proceed with"
say "docs/runbooks/retire-duplicate-install.md, which re-runs this checker as its"
say "own step 1 -- a green from an hour ago is not a green now."
exit 0
