#!/bin/bash
# tests/test_gemini_install.sh -- isolated Gemini settings installer contract.

set -uo pipefail
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL="$SELF_DIR/../adapters/gemini/install.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export COMMS_GEMINI_SETTINGS="$TMP/settings.json"

PASS=0
FAIL=0
check() {
  local desc="$1" want="$2" got="$3"
  if [ "$want" = "$got" ]; then echo "ok:   $desc"; PASS=$((PASS + 1))
  else echo "FAIL: $desc (want [$want], got [$got])"; FAIL=$((FAIL + 1)); fi
}

# Fresh install has the exact Gemini settings shape and adapter command.
out="$(bash "$INSTALL" 2>&1)"; rc=$?
check "fresh install exits 0" 0 "$rc"
shape="$(python3 -c 'import json,sys
d=json.load(open(sys.argv[1])); e=d["hooks"]["AfterTool"]
ok=(len(e)==1 and e[0].get("matcher")=="*" and len(e[0].get("hooks",[]))==1
 and e[0]["hooks"][0].get("type")=="command"
 and e[0]["hooks"][0].get("command","").endswith("/adapters/gemini/hook.sh")
 and set(e[0]["hooks"][0])=={"type","command"})
print("exact" if ok else json.dumps(d,sort_keys=True))' "$COMMS_GEMINI_SETTINGS")"
check "fresh install writes exact AfterTool shape" exact "$shape"

# Re-running is byte-idempotent.
before="$(cat "$COMMS_GEMINI_SETTINGS")"
out="$(bash "$INSTALL" 2>&1)"; rc=$?
check "repeat install exits 0" 0 "$rc"
check "repeat install is byte-identical" "$before" "$(cat "$COMMS_GEMINI_SETTINGS")"

# Existing unrelated keys and hooks survive installation.
export COMMS_GEMINI_SETTINGS="$TMP/existing.json"
printf '%s\n' '{"theme":"dark","hooks":{"BeforeTool":[{"matcher":"x","hooks":[{"type":"command","command":"other"}]}],"AfterTool":[{"matcher":"y","hooks":[{"type":"command","command":"keep"}]}]}}' > "$COMMS_GEMINI_SETTINGS"
bash "$INSTALL" >/dev/null 2>&1; rc=$?
check "existing settings install exits 0" 0 "$rc"
preserved="$(python3 -c 'import json,sys
d=json.load(open(sys.argv[1])); print("yes" if d.get("theme")=="dark" and d["hooks"]["BeforeTool"][0]["matcher"]=="x" and any(h.get("command")=="keep" for e in d["hooks"]["AfterTool"] for h in e["hooks"]) and len(d["hooks"]["AfterTool"])==2 else "no")' "$COMMS_GEMINI_SETTINGS")"
check "unrelated settings and hooks survive" yes "$preserved"

# Uninstall removes only this adapter entry.
out="$(bash "$INSTALL" --uninstall 2>&1)"; rc=$?
check "uninstall exits 0" 0 "$rc"
remaining="$(python3 -c 'import json,sys
d=json.load(open(sys.argv[1])); print("yes" if d.get("theme")=="dark" and len(d["hooks"]["AfterTool"])==1 and d["hooks"]["AfterTool"][0]["hooks"][0]["command"]=="keep" else "no")' "$COMMS_GEMINI_SETTINGS")"
check "uninstall removes only Gemini adapter" yes "$remaining"

# Invalid JSON is refused and preserved.
export COMMS_GEMINI_SETTINGS="$TMP/broken.json"
printf '%s' '{broken' > "$COMMS_GEMINI_SETTINGS"
out="$(bash "$INSTALL" 2>&1)"; rc=$?
check "invalid JSON exits 1" 1 "$rc"
check "invalid JSON remains untouched" '{broken' "$(cat "$COMMS_GEMINI_SETTINGS")"

echo "gemini install test: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
