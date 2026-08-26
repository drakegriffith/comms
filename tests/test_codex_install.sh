#!/bin/bash
# tests/test_codex_install.sh -- exercise adapters/codex/install.sh in isolation.
#
# All writes go into a temp dir via COMMS_CODEX_HOOKS; the real
# ~/.codex/hooks.json is never touched.
#
# Exit: 0 all passed, 1 any failed. Prints a passed/failed count either way.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"        # <repo>/tests
INSTALL="$SELF_DIR/../adapters/codex/install.sh"
PASS=0
FAIL=0

# check <desc> <expected> <actual> [output] [required_substring]
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

# ---- (1) fresh file -> wrapped shape with one entry -------------------------
export COMMS_CODEX_HOOKS="$TMP/fresh-hooks.json"
out="$(bash "$INSTALL" 2>&1)"; rc=$?
check "fresh file exits 0" "0" "$rc" "$out" "added PostToolUse swarm-heartbeat entry"
shape="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print("wrapped" if isinstance(d.get("hooks"), dict) and "PostToolUse" in d["hooks"] else json.dumps(d))
' "$COMMS_CODEX_HOOKS")"
check "fresh file is wrapped shape" "wrapped" "$shape"
count="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(len(d["hooks"].get("PostToolUse", [])))
' "$COMMS_CODEX_HOOKS")"
check "fresh file has one entry" "1" "$count"
matcher="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(d["hooks"]["PostToolUse"][0].get("matcher", "MISSING"))
' "$COMMS_CODEX_HOOKS")"
check "fresh file entry matcher is *" "*" "$matcher"

# ---- (2) existing wrapped file with another hook -> append, keep other -----
export COMMS_CODEX_HOOKS="$TMP/wrapped-hooks.json"
cat > "$COMMS_CODEX_HOOKS" <<'JSON'
{
  "hooks": {
    "SessionStart": [
      {"matcher": "*", "hooks": [{"type": "command", "command": "bash /other-event.sh"}]}
    ]
  }
}
JSON
out="$(bash "$INSTALL" 2>&1)"; rc=$?
check "existing wrapped file exits 0" "0" "$rc" "$out" "added PostToolUse swarm-heartbeat entry"
other_count="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(len(d["hooks"].get("SessionStart", [])))
' "$COMMS_CODEX_HOOKS")"
check "existing wrapped keeps other hook" "1" "$other_count"
ptu_count="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(len(d["hooks"].get("PostToolUse", [])))
' "$COMMS_CODEX_HOOKS")"
check "existing wrapped adds heartbeat" "1" "$ptu_count"
other_cmd="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(d["hooks"]["SessionStart"][0]["hooks"][0].get("command", ""))
' "$COMMS_CODEX_HOOKS")"
check "existing wrapped other hook command intact" "bash /other-event.sh" "$other_cmd"

# ---- (3) existing flat file -> migrated, entries preserved, no top-level event key remains -----
export COMMS_CODEX_HOOKS="$TMP/flat-hooks.json"
cat > "$COMMS_CODEX_HOOKS" <<'JSON'
{
  "PostToolUse": [
    {"matcher": "*", "hooks": [{"type": "command", "command": "bash /existing.sh"}]}
  ],
  "SessionStart": [
    {"matcher": "*", "hooks": [{"type": "command", "command": "bash /other-event.sh"}]}
  ]
}
JSON
out="$(bash "$INSTALL" 2>&1)"; rc=$?
check "flat file exits 0" "0" "$rc" "$out" "migrated flat hooks.json to wrapped shape"
migrated_ok="$(python3 -c '
import json, sys
path = sys.argv[1]
d = json.load(open(path))
ok = isinstance(d.get("hooks"), dict)
ok = ok and "PostToolUse" in d.get("hooks", {})
ok = ok and "SessionStart" in d.get("hooks", {})
ok = ok and "PostToolUse" not in [k for k in d if k != "hooks"]
ok = ok and "SessionStart" not in [k for k in d if k != "hooks"]
print("yes" if ok else json.dumps(d))
' "$COMMS_CODEX_HOOKS")"
check "flat file migrated to wrapped shape" "yes" "$migrated_ok"
existing_preserved="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
cmds = [
    h.get("command", "")
    for e in d["hooks"].get("PostToolUse", [])
    for h in e.get("hooks", [])
    if isinstance(h, dict)
]
print("yes" if "bash /existing.sh" in cmds else "no")
' "$COMMS_CODEX_HOOKS")"
check "flat file preserves original PostToolUse entry" "yes" "$existing_preserved"
other_preserved="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
cmds = [
    h.get("command", "")
    for e in d["hooks"].get("SessionStart", [])
    for h in e.get("hooks", [])
    if isinstance(h, dict)
]
print("yes" if "bash /other-event.sh" in cmds else "no")
' "$COMMS_CODEX_HOOKS")"
check "flat file preserves original SessionStart entry" "yes" "$other_preserved"
heartbeat_added="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
cmds = [
    h.get("command", "")
    for e in d["hooks"].get("PostToolUse", [])
    for h in e.get("hooks", [])
    if isinstance(h, dict)
]
print("yes" if any("swarm-heartbeat.sh" in c for c in cmds) else "no")
' "$COMMS_CODEX_HOOKS")"
check "flat file adds heartbeat entry" "yes" "$heartbeat_added"

# ---- (3b) flat file with non-list top-level keys keeps them -----------------
export COMMS_CODEX_HOOKS="$TMP/flat-with-meta-hooks.json"
cat > "$COMMS_CODEX_HOOKS" <<'JSON'
{
  "version": 1,
  "meta": {"owner": "test", "tags": ["a", "b"]},
  "PostToolUse": [
    {"matcher": "*", "hooks": [{"type": "command", "command": "bash /existing.sh"}]}
  ]
}
JSON
out="$(bash "$INSTALL" 2>&1)"; rc=$?
check "flat with meta exits 0" "0" "$rc" "$out" "migrated flat hooks.json to wrapped shape"
version_kept="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get("version", "MISSING"))
' "$COMMS_CODEX_HOOKS")"
check "flat with meta keeps version" "1" "$version_kept"
meta_kept="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print("yes" if d.get("meta", {}).get("owner") == "test" else "no")
' "$COMMS_CODEX_HOOKS")"
check "flat with meta keeps meta object" "yes" "$meta_kept"
shape_ok="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
ok = isinstance(d.get("hooks"), dict)
ok = ok and "PostToolUse" in d.get("hooks", {})
ok = ok and "version" in d
ok = ok and "meta" in d
print("yes" if ok else json.dumps(d))
' "$COMMS_CODEX_HOOKS")"
check "flat with meta has wrapped hooks plus top-level keys" "yes" "$shape_ok"

# ---- (4) re-run -> no duplicate ---------------------------------------------
out2="$(bash "$INSTALL" 2>&1)"; rc2=$?
check "re-run exits 0" "0" "$rc2" "$out2" "already present"
ptu_count2="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(len(d["hooks"].get("PostToolUse", [])))
' "$COMMS_CODEX_HOOKS")"
check "re-run does not duplicate heartbeat" "2" "$ptu_count2"

# ---- (5) unparseable file -> exit 1, file untouched -------------------------
export COMMS_CODEX_HOOKS="$TMP/broken-hooks.json"
printf '%s' '{not json at all' > "$COMMS_CODEX_HOOKS"
out3="$(bash "$INSTALL" 2>&1)"; rc3=$?
check "unparseable file exits 1" "1" "$rc3" "$out3" "refusing to edit"
unchanged="$(cat "$COMMS_CODEX_HOOKS")"
check "unparseable file left untouched" '{not json at all' "$unchanged"

echo "codex install test: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
