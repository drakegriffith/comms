#!/bin/bash
# adapters/claude-code/install-skill.sh -- install the comms-say skill into
# the Claude Code skills dir, idempotently. Called by install.sh; standalone
# so tests can exercise it without the full installer's verification suites.
#
# Renders __COMMS_ROOT__ in the canonical SKILL.md to this checkout's absolute
# path and copies the result (copied, not symlinked: a symlink would ship the
# placeholder raw). An identical installed copy is left untouched. Target dir
# defaults to ~/.claude/skills; override with COMMS_SKILLS_DIR=<dir>.
#
# Exit codes: 0 installed or already current | 1 failed.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"       # <repo>/adapters/claude-code
REPO_ROOT="$(cd "$SELF_DIR/../.." && pwd)"      # <repo>
SKILLS_DIR="${COMMS_SKILLS_DIR:-$HOME/.claude/skills}"
SKILL_SRC="$SELF_DIR/skills/comms-say/SKILL.md"
SKILL_DST="$SKILLS_DIR/comms-say/SKILL.md"

fail() { echo "install-skill: FAILED: $*" >&2; exit 1; }

[ -e "$SKILL_SRC" ] || fail "missing $SKILL_SRC -- incomplete checkout?"
rendered="$(sed "s|__COMMS_ROOT__|$REPO_ROOT|g" "$SKILL_SRC")" || fail "render failed"
if [ -e "$SKILL_DST" ] && [ "$(cat "$SKILL_DST")" = "$rendered" ]; then
  echo "comms-say skill: already current at $SKILL_DST, left untouched"
  exit 0
fi
mkdir -p "$(dirname "$SKILL_DST")" || fail "cannot create $(dirname "$SKILL_DST")"
printf '%s\n' "$rendered" > "$SKILL_DST.comms-tmp" || fail "cannot write $SKILL_DST"
mv "$SKILL_DST.comms-tmp" "$SKILL_DST" || fail "cannot move into place at $SKILL_DST"
echo "comms-say skill: installed at $SKILL_DST"
