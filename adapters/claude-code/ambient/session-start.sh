#!/bin/bash
# adapters/claude-code/ambient/session-start.sh -- SessionStart hook body for
# the AMBIENT LANE: make every Claude Code session on this machine visible in
# one standing mailbox run ("machine-ops"), mirrored to Discord by
# adapters/discord/mirror.py --follow machine-ops.
#
# WHY: sessions on one machine are mutually invisible by default -- one
# terminal can be deleting hooks while another, unaware, depends on them. The
# ambient lane is the standing channel that makes "who is running, where"
# observable without anyone polling.
#
# WHAT IT DOES (one python spawn, well under 1s):
#   1. Ensures the standing run `machine-ops` is armed (idempotent -- arm only
#      when not already armed, so meta.json is not rewritten every session).
#   2. Enrolls this session as a participant:
#        agent_id = session_id from the hook payload, else $CLAUDE_SESSION_ID,
#                   else "pid-<parent pid>" (last resort; documented caveat:
#                   the SendMessage bridge cannot rederive a PID fallback, so
#                   bridged rows require a real session id).
#        seat     = "<basename of cwd>-<first 4 chars of agent_id>" -- the
#                   short-session-name convention, unique per session.
#        identity = model ($CLAUDE_MODEL, else "claude"), project (basename of
#                   cwd), area (cwd). Display-only, rendered by the mirror.
#      Enrollment is write-once in swarm_arm, so re-running (resume, /clear)
#      never clobbers or duplicates.
#   3. On FIRST enrollment only, posts one row:
#        status "session started in <cwd>"  (topic ops)
#      A resume/clear beat that finds the session already enrolled posts
#      nothing -- one session, one arrival row, no flooding.
#
# BYSTANDER SILENCE IS PRESERVED FOR EVERY OTHER RUN. machine-ops is the ONE
# deliberate machine-wide run and its rows stay in topic "ops"; unicast and
# every other run/topic behave exactly as before. A session enrolled here
# subscribes to ["ops"] only, so no other run's board can reach it through
# this enrollment.
#
# FAILURE CONTRACT: this hook NEVER fails the session. Every error path exits
# 0; problems are appended to $COMMS_STATE_DIR/ambient.log (never stdout --
# SessionStart stdout is injected into the session's context, so success is
# byte-silent).
#
# SKIP CONDITIONS (exit 0, no write, before the session is touched):
#   * COMMS_AMBIENT_OPTOUT is non-empty -- checked FIRST, before even the
#     state dir is created. Test harnesses and the mutation gate export this.
#   * the session cwd is a throwaway directory: under $TMPDIR, /tmp,
#     /private/tmp, /private/var/folders, or a path with a `mutgate-wt.*`
#     segment (a mutation-gate worktree). One line naming the skipped cwd
#     goes to ambient.log; nothing is enrolled and no row is posted, so a
#     board reader never sees "session started in /private/var/folders/...".
#     Escape hatch: COMMS_AMBIENT_FORCE=1 overrides this guard for a session
#     that legitimately runs in a throwaway-shaped path.
#
# ISOLATION KNOBS (tests set these; production uses the defaults):
#   COMMS_STATE_DIR      arm/roster state + ambient.log (default ~/.comms/state)
#   COMMS_ROOT           mailbox root (default /tmp)
#   COMMS_AMBIENT_OPTOUT non-empty -> exit 0 before any write
#   COMMS_AMBIENT_FORCE  non-empty -> bypass the throwaway-cwd guard
#
# COMPLETENESS MARKER: the FINAL line of this file is
#   # hook-eof-marker v1 do-not-remove
# It is LOAD-BEARING, not a comment to tidy away: the dispatch shim
# (~/.claude/state/bin/hook-shim.sh) validates a hook file against mid-write
# tears by checking that exact final line before dispatch. Removing it makes
# the shim treat this file as torn and skip it.

set -uo pipefail

# ---- opt-out: honored BEFORE any write, even before STATE_DIR is touched --
if [ -n "${COMMS_AMBIENT_OPTOUT:-}" ]; then
  exit 0
fi

STATE_DIR="${COMMS_STATE_DIR:-$HOME/.comms/state}"

# ---- locate this repo from THIS script's resolved path --------------------
AMB_SELF="${BASH_SOURCE[0]:-$0}"
while [ -L "$AMB_SELF" ]; do
  _t="$(readlink "$AMB_SELF")"
  case "$_t" in
    /*) AMB_SELF="$_t" ;;
    *)  AMB_SELF="$(dirname "$AMB_SELF")/$_t" ;;
  esac
done
AMB_SELF_DIR="$(cd "$(dirname "$AMB_SELF")" && pwd -P)"  # <repo>/adapters/claude-code/ambient
AMB_REPO_ROOT="$(cd "$AMB_SELF_DIR/../../.." && pwd)"    # <repo>

# ---- bounded stdin read (vendored helper, one dir up) ---------------------
_amb_lib="$AMB_SELF_DIR/../stdin-bounded.sh"
if [ -r "$_amb_lib" ]; then
  . "$_amb_lib"
fi
if type read_stdin_bounded >/dev/null 2>&1; then
  read_stdin_bounded
  input="$HOOK_STDIN"
else
  input=""
fi

export AMB_PAYLOAD="$input"
export AMB_STATE_DIR="$STATE_DIR"
export AMB_SWARM_LIB="$AMB_REPO_ROOT/lib"
export AMB_PPID="$PPID"

python3 <<'PY' || true
import datetime
import json
import os
import re
import sys

state_dir = os.environ.get("AMB_STATE_DIR") or os.path.expanduser("~/.comms/state")

def log(msg):
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "ambient.log"), "a") as fh:
            at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            fh.write("%s session-start: %s\n" % (at, msg))
    except OSError:
        pass


MUTGATE_WT_RE = re.compile(r"/mutgate-wt\.[^/]*/")


def throwaway_cwd(cwd):
    """True when cwd sits under a throwaway root: $TMPDIR, /tmp,
    /private/tmp, /private/var/folders, or a path with a mutgate-wt.*
    segment (a mutation-gate worktree)."""
    norm = (cwd or "").rstrip("/") or "/"
    roots = ["/tmp", "/private/tmp", "/private/var/folders"]
    tmpdir_env = os.environ.get("TMPDIR")
    if tmpdir_env:
        roots.append(tmpdir_env.rstrip("/"))
    for root in roots:
        if root and (norm == root or norm.startswith(root + "/")):
            return True
    return bool(MUTGATE_WT_RE.search(norm + "/"))

try:
    sys.path.insert(0, os.environ.get("AMB_SWARM_LIB") or "")
    import swarm_arm
    import swarm_mailbox

    try:
        payload = json.loads(os.environ.get("AMB_PAYLOAD", "") or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    RUNID = "machine-ops"
    TOPIC = "ops"

    agent_id = (
        payload.get("session_id")
        or os.environ.get("CLAUDE_SESSION_ID")
        or "pid-%s" % os.environ.get("AMB_PPID", os.getppid())
    )
    cwd = payload.get("cwd") or os.getcwd()

    if throwaway_cwd(cwd) and not os.environ.get("COMMS_AMBIENT_FORCE"):
        log("skipped throwaway cwd: %s" % cwd)
        sys.exit(0)

    project = os.path.basename(cwd.rstrip("/")) or "root"
    safe_project = "".join(c for c in project if c.isalnum() or c in "-_.") or "root"
    safe_id = "".join(c for c in agent_id if c.isalnum()) or "0000"
    seat = "%s-%s" % (safe_project, safe_id[:4])
    model = os.environ.get("CLAUDE_MODEL") or "claude"

    # 1. Standing run, armed once. Guarded so meta.json (and its default-topic
    # subscription) is not rewritten on every session start.
    if not swarm_arm.is_armed(RUNID, state_dir=state_dir):
        swarm_arm.arm(RUNID, topic=TOPIC, state_dir=state_dir)

    # 2 + 3. Enroll once; post the arrival row only on the FIRST enrollment,
    # so resume/clear re-fires never flood the board.
    if not swarm_arm.is_participant(RUNID, agent_id, state_dir=state_dir):
        ok = swarm_arm.enroll(
            RUNID, agent_id, topics=[TOPIC], seat=seat, state_dir=state_dir,
            model=model, project=project, area=cwd,
        )
        if ok:
            swarm_mailbox.post(
                RUNID, seat, "status", "session started in %s" % cwd, topic=TOPIC
            )
        else:
            log("enroll refused (run not armed?) agent_id=%s" % agent_id)
except Exception as exc:  # NEVER fail the session; the log is the record.
    log("error: %s: %s" % (exc.__class__.__name__, exc))
sys.exit(0)
PY

exit 0
# hook-eof-marker v1 do-not-remove
