#!/bin/bash
# tests/test_claude_install_skill.sh -- exercise adapters/claude-code/
# install-skill.sh in isolation. All writes go into a temp dir via
# COMMS_SKILLS_DIR; the real ~/.claude/skills is never touched.
#
# Exit: 0 all passed, 1 any failed. Prints a passed/failed count either way.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"        # <repo>/tests
REPO_ROOT="$(cd "$SELF_DIR/.." && pwd)"
INSTALL="$REPO_ROOT/adapters/claude-code/install-skill.sh"
PASS=0
FAIL=0

check() {
  local desc="$1" want="$2" got="$3" out="${4-}" need="${5-}"
  if [ "$got" != "$want" ]; then
    echo "FAIL: $desc (got=$got, want=$want)"
    FAIL=$((FAIL + 1))
    return
  fi
  if [ -n "$need" ] && ! printf '%s' "$out" | grep -qF -- "$need"; then
    echo "FAIL: $desc (ok, output missing: $need)"
    FAIL=$((FAIL + 1))
    return
  fi
  echo "ok:   $desc"
  PASS=$((PASS + 1))
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export COMMS_SKILLS_DIR="$TMP/skills"
DST="$COMMS_SKILLS_DIR/comms-say/SKILL.md"

# ---- (1) fresh install: file appears, placeholder rendered ------------------
out="$(bash "$INSTALL" 2>&1)"; rc=$?
check "fresh install exits 0" "0" "$rc" "$out" "installed at $DST"
[ -e "$DST" ]; check "skill file exists" "0" "$?"
grep -qF "$REPO_ROOT/bin/comms post comment" "$DST"
check "placeholder rendered to checkout path" "0" "$?"
grep -qF "__COMMS_ROOT__" "$DST"; rendered_clean=$?
check "no raw placeholder remains" "1" "$rendered_clean"
grep -q "^name: comms-say$" "$DST"
check "frontmatter name present" "0" "$?"

# ---- (2) re-run: identical copy left untouched ------------------------------
out2="$(bash "$INSTALL" 2>&1)"; rc2=$?
check "re-run exits 0" "0" "$rc2" "$out2" "left untouched"

# ---- (3) drifted copy refreshed back to canonical ---------------------------
echo "local drift" >> "$DST"
out3="$(bash "$INSTALL" 2>&1)"; rc3=$?
check "drifted copy exits 0" "0" "$rc3" "$out3" "installed at $DST"
grep -qF "local drift" "$DST"; drift_gone=$?
check "drift overwritten with canonical content" "1" "$drift_gone"

echo "claude install-skill test: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
