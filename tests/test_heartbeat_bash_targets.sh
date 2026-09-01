#!/bin/bash
# test_heartbeat_bash_targets.sh -- the doc-enrol Bash leg may only announce a
# path the COMMAND ITSELF names as a write target.
#
# WHY THIS FILE EXISTS (incident 2026-08-31T22:48:09Z, panel
# 2026-08-31-4514-board-integrity). The Bash leg used to run `git status` in the
# payload cwd and announce every dirty path whose BASENAME occurred anywhere in
# the command text. Three consequences, all reproduced below as red tests:
#
#   DEF-1 PROSE-AS-WRITE. Seat 4514-sub-afc8 ran `git commit ... <<'MSG'` whose
#         COMMIT MESSAGE mentioned hooks/test_hook_health.py. The commit's real
#         write set was six unrelated files in another checkout. Two board rows
#         announced files the seat never touched, belonging to a peer session
#         that held the claim.
#   DEF-2 SUBSTRING CONTAINMENT. `basename in command` matched "hook_health.py"
#         inside "test_hook_health.py", so ONE prose mention announced TWO files.
#   DEF-3 CWD PATH DOUBLING. `git status --porcelain` paths are REPO-ROOT
#         relative; joining them onto the payload cwd fabricates
#         <cwd>/<repo-rel-path>, which for a cwd below the repo root is a
#         doubled, non-existent path -- and thread_key mints a key for it.
#
# The contract asserted here: a board row means the command text named that path
# under a write verb or a redirect, and the path exists after the call. When the
# evidence is weaker than that, the leg emits NOTHING. Silence is a pass.
#
# Every negative assertion below is paired with a positive control in the same
# repository and the same seat shape, so a suite that stopped emitting rows
# entirely fails instead of going green.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"                 # <repo>/tests
# HB_HOOK_UNDER_TEST points this suite at ANOTHER copy of the adapter. It
# exists for one job: replaying these assertions against the PRE-FIX adapter,
# so "these tests were red before the fix" stays a command anyone can re-run
# instead of a sentence in a commit message. Build the other copy as the
# adapter's own header instructs -- a tree of its own with a lib/ beside it --
# because the hook resolves lib/ from its own resolved path, never from $PWD.
HOOK="${HB_HOOK_UNDER_TEST:-$SELF_DIR/../adapters/claude-code/swarm-heartbeat.sh}"
SA="$SELF_DIR/../lib/swarm_arm.py"
pass=0
fail=0

ck() {  # ck <name> <expected> <actual>
    if [ "$2" = "$3" ]; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1))
        printf '  FAIL %s\n    expected: %s\n    actual:   %s\n' "$1" "$2" "$3" >&2
    fi
}

STATE="$(mktemp -d)" || exit 1
ROOT="$(mktemp -d)" || exit 1
REPO="$(mktemp -d)" || exit 1
ELSEWHERE="$(mktemp -d)" || exit 1
REPONAME="$(basename "$REPO")"
RUN="hbtargets$$x$RANDOM"

cleanup() { rm -rf "$STATE" "$ROOT" "$REPO" "$ELSEWHERE"; }
trap cleanup EXIT

arm_run() { COMMS_STATE_DIR="$STATE" python3 "$SA" arm "$1" >/dev/null; }
enroll_agent() {  # enroll_agent <runid> <agent_id> <topic> <seat>
    COMMS_STATE_DIR="$STATE" python3 "$SA" enroll "$1" --agent-id "$2" \
        --topics "$3" --seat "$4" >/dev/null
}

command_payload() {  # command_payload <agent_id> <tool_name> <cwd> <command>
    python3 -c 'import json,sys
print(json.dumps({"hook_event_name":"PostToolUse", "tool_name":sys.argv[2],
 "session_id":"sess-targets", "agent_id":sys.argv[1], "cwd":sys.argv[3],
 "tool_input":{"command":sys.argv[4]}}))' "$1" "$2" "$3" "$4"
}

beat() {  # beat <agent_id> <cwd> <command>
    command_payload "$1" Bash "$2" "$3" |
        COMMS_STATE_DIR="$STATE" COMMS_ROOT="$ROOT" /bin/bash "$HOOK" >/dev/null 2>&1
}

# Number of rows in a seat's mailbox whose `thread` is exactly this key.
rows_for() {  # rows_for <seat> <thread-key>
    python3 -c 'import json,os,sys
path = os.path.join(sys.argv[1], "comms-" + sys.argv[2], sys.argv[3] + ".jsonl")
n = 0
if os.path.exists(path):
    for line in open(path):
        line = line.strip()
        if line and json.loads(line).get("thread") == sys.argv[4]:
            n += 1
print(n)' "$ROOT" "$RUN" "$2" "$3"
}

# Every thread key this seat ever announced, comma-joined. "" means silence.
keys_for() {  # keys_for <seat>
    python3 -c 'import json,os,sys
path = os.path.join(sys.argv[1], "comms-" + sys.argv[2], sys.argv[3] + ".jsonl")
keys = []
if os.path.exists(path):
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        key = json.loads(line).get("thread")
        if key:
            keys.append(key)
print(",".join(keys), end="")' "$ROOT" "$RUN" "$2"
}

# ---- fixture: a repo shaped like the incident ------------------------------
mkdir -p "$REPO/hooks/lib" "$REPO/sub"
git -C "$REPO" init -q
printf 'baseline\n' > "$REPO/hooks/lib/hook_health.py"
printf 'baseline\n' > "$REPO/hooks/test_hook_health.py"
printf 'baseline\n' > "$REPO/sub/b.py"
printf 'baseline\n' > "$REPO/notes.txt"
printf 'baseline\n' > "$REPO/notes2.txt"
git -C "$REPO" add -A >/dev/null
git -C "$REPO" -c user.email=t@t -c user.name=t commit -qm baseline
# Both hook_health files are DIRTY in the shared tree -- the peer session's
# work, exactly as at 22:48:09Z. Nothing this suite's seats will write.
printf 'peer edit\n' >> "$REPO/hooks/lib/hook_health.py"
printf 'peer edit\n' >> "$REPO/hooks/test_hook_health.py"
printf 'peer edit\n' >> "$REPO/sub/b.py"
# The two notes files are dirty as well, so the prose assertions below isolate
# the prose rule and not the old dirty-set gate.
printf 'peer edit\n' >> "$REPO/notes.txt"
printf 'peer edit\n' >> "$REPO/notes2.txt"
printf 'x\n' > "$ELSEWHERE/a.txt"
git -C "$ELSEWHERE" init -q
git -C "$ELSEWHERE" add a.txt >/dev/null

arm_run "$RUN"
for seat in Ctl Prose Quoted Heredoc Sub Read Tee Sed Fd Deep; do
    enroll_agent "$RUN" "agent$seat" board "seat$seat"
done

# ---- P0 POSITIVE CONTROL: a real redirect write IS announced ---------------
# Without this the negatives below could all pass on a hook that emits nothing.
beat agentCtl "$REPO" 'printf x >> hooks/test_hook_health.py'
ck "(P0) control: a redirect target is announced exactly once" "1" \
    "$(rows_for "$REPO" seatCtl "doc:$REPONAME/hooks/test_hook_health.py")"

# ---- DEF-2: substring containment ------------------------------------------
# The SAME beat must not also announce hooks/lib/hook_health.py, whose basename
# is a substring of the basename that really was written.
ck "(DEF-2) a longer basename does not announce the shorter dirty file" "0" \
    "$(rows_for "$REPO" seatCtl "doc:$REPONAME/hooks/lib/hook_health.py")"

# ---- DEF-1: the incident. Prose inside a heredoc is not a write ------------
prose_cmd="cd $ELSEWHERE && git commit -q -F - -- a.txt <<'MSG'
fix: unrelated work in another checkout

Discarded option: rewriting hooks/test_hook_health.py in place.
MSG"
beat agentProse "$REPO" "$prose_cmd"
ck "(DEF-1) a commit message naming a path announces nothing" "" \
    "$(keys_for "$REPO" seatProse)"

# ---- DEF-1b: prose in a quoted argument is not a write ---------------------
beat agentQuoted "$REPO" 'printf "see sub/b.py for details" > notes.txt'
ck "(DEF-1b) the redirect target is announced" "1" \
    "$(rows_for "$REPO" seatQuoted "doc:$REPONAME/notes.txt")"
ck "(DEF-1b) a dirty path merely named in a quoted argument is not" "0" \
    "$(rows_for "$REPO" seatQuoted "doc:$REPONAME/sub/b.py")"

# ---- DEF-1c: a heredoc BODY drives no attribution --------------------------
heredoc_cmd="cat > notes2.txt <<'EOF'
sub/b.py needs work
EOF"
beat agentHeredoc "$REPO" "$heredoc_cmd"
ck "(DEF-1c) the heredoc's redirect target is announced" "1" \
    "$(rows_for "$REPO" seatHeredoc "doc:$REPONAME/notes2.txt")"
ck "(DEF-1c) a path named in the heredoc body is not" "0" \
    "$(rows_for "$REPO" seatHeredoc "doc:$REPONAME/sub/b.py")"

# ---- DEF-3: repo-root-relative porcelain joined onto a SUBDIR cwd ----------
# Payload cwd is <repo>/hooks; the command writes lib/hook_health.py relative to
# it. The only honest key is doc:<repo>/hooks/lib/hook_health.py.
beat agentDeep "$REPO/hooks" 'printf x >> lib/hook_health.py'
ck "(DEF-3) a subdir cwd yields the true repo-relative key" \
    "doc:$REPONAME/hooks/lib/hook_health.py" "$(keys_for "$REPO" seatDeep)"

# ---- read-shaped commands stay silent --------------------------------------
beat agentRead "$REPO" 'grep -n baseline sub/b.py hooks/lib/hook_health.py'
ck "(read) a read-shaped command announces nothing" "" \
    "$(keys_for "$REPO" seatRead)"

# ---- other real write verbs still land -------------------------------------
beat agentTee "$REPO" 'printf x | tee -a sub/b.py'
ck "(tee) tee's operand is a write target" "1" \
    "$(rows_for "$REPO" seatTee "doc:$REPONAME/sub/b.py")"

beat agentSed "$REPO" "sed -i.bak 's/baseline/edited/' hooks/lib/hook_health.py"
ck "(sed) sed -i's file operand is a write target" "1" \
    "$(rows_for "$REPO" seatSed "doc:$REPONAME/hooks/lib/hook_health.py")"

# ---- fd duplication is not a filename --------------------------------------
beat agentFd "$REPO" 'grep -n baseline sub/b.py 2>&1 | head -1'
ck "(fd) 2>&1 announces nothing" "" "$(keys_for "$REPO" seatFd)"

printf 'test_heartbeat_bash_targets: %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
