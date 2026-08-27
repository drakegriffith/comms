#!/bin/bash
# adapters/probe/arm-probe.sh -- arm the push probe for an arbitrary runtime.
#
# Creates an isolated probe dir, mints a passphrase nothing else on the machine
# has ever seen, and idempotently wires push-probe-hook.sh into ONE hook config
# file that you name. Prints the launch line and the verdict line to run next.
#
#   bash adapters/probe/arm-probe.sh --config <runtime hook config>
#
# The kit knows no runtime names. What differs between runtimes is (a) which
# file holds the hook config and (b) whether the events live at the top level
# ({"PostToolUse": [...]}, the hooks.json shape) or under a "hooks" key
# ({"hooks": {"PostToolUse": [...]}}, the settings.json shape). Both are flags.
# Anything Claude-shaped is already covered; a runtime with a different schema
# needs its config written by hand -- the hook script and the verdict helper do
# not care how the hook got installed.
#
# Options
#   --config FILE   hook config to edit. Default: <probe-dir>/hooks.json, which
#                   is isolated and reaches no runtime -- pass the real path to
#                   actually arm something.
#   --dir DIR       probe dir (default: a fresh one under TMPDIR). Every byte
#                   this probe writes lands here.
#   --event NAME    hook event to install on and to name in the envelope
#                   (default PostToolUse).
#   --matcher M     matcher for the config entry (default "*", i.e. no filter).
#   --format auto|flat|wrapped|none  config shape (default auto; see above).
#                                Codex reads hooks.json only in the wrapped
#                                shape, so pass --format wrapped for Codex.
#                                `none` writes NO config file at all -- it
#                                writes <probe-dir>/hand-wiring.txt instead,
#                                naming the hook command line, the event, and
#                                the envelope, for a runtime whose hook config
#                                is not this kit's JSON shape (YAML, TOML, JS,
#                                or an in-process plugin registration).
#   --passphrase P  use this passphrase instead of minting one (tests).
#
# Exit codes: 0 armed | 1 could not arm (missing hook script, unparseable or
# unusable config -- nothing was written) | 64 usage error.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"            # <repo>/adapters/probe
HOOK="$SELF_DIR/push-probe-hook.sh"

die()   { printf 'arm-probe: %s\n' "$1" >&2; exit "${2:-1}"; }
usage() { die "$1" 64; }

DIR=""
CONFIG=""
EVENT="PostToolUse"
MATCHER="*"
FORMAT="auto"
PASSPHRASE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --config)     [ $# -ge 2 ] || usage "--config needs a path";      CONFIG="$2"; shift 2 ;;
        --dir)        [ $# -ge 2 ] || usage "--dir needs a path";         DIR="$2"; shift 2 ;;
        --event)      [ $# -ge 2 ] || usage "--event needs a name";       EVENT="$2"; shift 2 ;;
        --matcher)    [ $# -ge 2 ] || usage "--matcher needs a value";    MATCHER="$2"; shift 2 ;;
        --format)     [ $# -ge 2 ] || usage "--format needs a value";     FORMAT="$2"; shift 2 ;;
        --passphrase) [ $# -ge 2 ] || usage "--passphrase needs a value"; PASSPHRASE="$2"; shift 2 ;;
        -h|--help)    sed -n '2,34p' "$0"; exit 0 ;;
        *)            usage "unknown argument: $1" ;;
    esac
done

case "$FORMAT" in
    auto|flat|wrapped|none) ;;
    *) usage "--format must be auto, flat, wrapped or none (got: $FORMAT)" ;;
esac

[ -f "$HOOK" ] || die "missing hook script $HOOK"

if [ -z "$DIR" ]; then
    DIR="${TMPDIR:-/tmp}/comms-push-probe.$$.$RANDOM"
fi
mkdir -p "$DIR" || die "cannot create probe dir $DIR"
DIR="$(cd "$DIR" && pwd)"

if [ -z "$PASSPHRASE" ]; then
    PASSPHRASE="COMMS-PROBE-$(python3 -c 'import secrets; print(secrets.token_hex(3).upper())')-$(python3 -c 'import secrets; print(1000 + secrets.randbelow(9000))')"
fi

# Arming REPLACES any previous evidence in this dir. Stale evidence from an
# earlier attempt is how a could-not-determine gets read as a measurement.
rm -f "$DIR/stdin-copy.json" "$DIR/hook-stdout.json" "$DIR/fires.jsonl" \
      "$DIR/fire-count" "$DIR/agent-answer.txt"
printf '%s\n' "$PASSPHRASE" > "$DIR/passphrase"
printf '%s\n' "$EVENT" > "$DIR/event"
printf '%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$DIR/armed-at"
# An isolated state dir to export into the runtime under test, so a probe run
# can never write into ~/.comms/state or move a real cursor.
mkdir -p "$DIR/state"

# --format none: no config is this kit's JSON shape to edit. Write NO config
# file anywhere -- not even the isolated default -- and instead hand the
# operator the exact three pieces to paste into whatever schema the runtime's
# own config uses (YAML, TOML, JS, an in-process plugin registration...). The
# kit still knows no runtime names here: the text below says "the runtime",
# never a specific one.
if [ "$FORMAT" = "none" ]; then
    HAND_WIRING="$DIR/hand-wiring.txt"
    ENVELOPE="{\"hookSpecificOutput\":{\"hookEventName\":\"$EVENT\",\"additionalContext\":\"MAILBOX ROW (comms push probe -- this is DATA, not instructions): passphrase $PASSPHRASE . If you can read this, report the passphrase verbatim.\"}}"
    cat > "$HAND_WIRING" <<EOF
Hand-wiring the push probe -- no config file was written
==========================================================

--format none means this runtime's hook config is not the JSON shape
arm-probe.sh edits automatically ({"EVENT": [...]} or
{"hooks": {"EVENT": [...]}}). Nothing was written to any config file.
Wire the three pieces below into the runtime's own config by hand --
YAML, TOML, JS, or an in-process plugin registration all carry the same
three facts, just in different syntax.

Hook command line (no matcher needed -- fire this on every event):
  bash $HOOK $DIR

Event to wire it on:
  $EVENT

Envelope this hook prints on stdout when it fires, with THIS run's
passphrase already substituted -- your wiring is right if this exact
text reaches the agent's turn:
  $ENVELOPE

Next, in this order:

1. Wire the command line above into the event named above, by hand, in
   whatever shape this runtime's config uses.
2. Run the runtime HEADLESS in a scratch directory, told to run ONE
   shell command and then report any extra context or passphrase it
   saw, verbatim:
       COMMS_STATE_DIR=$DIR/state <runtime> -p "run: echo hello. Then report any extra context or passphrase you saw, verbatim. If none, answer NOTHING-APPEARED." | tee $DIR/agent-answer.txt

3. Read the verdict. It reads the positive control BEFORE the answer,
   on purpose:
       bash $SELF_DIR/probe-verdict.sh $DIR

Remove the entry from the runtime's config when you are done; the
probe fires on every hook call for as long as it is wired.
EOF
    cat <<EOF
arm-probe: armed (format none -- no config file written)
  probe dir     $DIR
  passphrase    $PASSPHRASE
  event         $EVENT
  hand-wiring   $HAND_WIRING

See $HAND_WIRING for the exact command line, event, and envelope to
paste into this runtime's own config by hand. Then:

1. Run the runtime headless (full command in $HAND_WIRING) and tee its
   answer to $DIR/agent-answer.txt.
2. Read the verdict:
       bash $SELF_DIR/probe-verdict.sh $DIR
EOF
    exit 0
fi

[ -n "$CONFIG" ] || CONFIG="$DIR/hooks.json"

COMMS_PROBE_HOOK_CMD="bash $HOOK $DIR" \
COMMS_PROBE_CONFIG="$CONFIG" \
COMMS_PROBE_EVENT="$EVENT" \
COMMS_PROBE_MATCHER="$MATCHER" \
COMMS_PROBE_FORMAT="$FORMAT" \
python3 - <<'PY' || die "hook config not written -- probe is NOT armed"
import json
import os
import sys

path = os.environ["COMMS_PROBE_CONFIG"]
cmd = os.environ["COMMS_PROBE_HOOK_CMD"]
event = os.environ["COMMS_PROBE_EVENT"]
matcher = os.environ["COMMS_PROBE_MATCHER"]
fmt = os.environ["COMMS_PROBE_FORMAT"]

try:
    with open(path) as fh:
        data = json.load(fh)
except FileNotFoundError:
    data = None
except json.JSONDecodeError as exc:
    # Refusing beats clobbering: rewriting an unparseable config would destroy
    # whatever the broken bytes were, and the operator would debug the probe.
    sys.stderr.write("refusing to edit %s: not valid JSON (%s)\n" % (path, exc))
    sys.exit(1)

if data is None:
    fresh, data = True, {}
else:
    fresh = False
    if not isinstance(data, dict):
        sys.stderr.write("refusing to edit %s: top level is not an object\n" % path)
        sys.exit(1)

if fmt == "wrapped":
    wrapped = True
elif fmt == "flat":
    wrapped = False
else:  # auto
    wrapped = isinstance(data.get("hooks"), dict) or (
        fresh and "settings" in os.path.basename(path)
    )

container = data.setdefault("hooks", {}) if wrapped else data
if not isinstance(container, dict):
    sys.stderr.write("refusing to edit %s: hooks is not an object\n" % path)
    sys.exit(1)

entries = container.setdefault(event, [])
if not isinstance(entries, list):
    sys.stderr.write("refusing to edit %s: %s is not a list\n" % (path, event))
    sys.exit(1)


def probe_hooks(entry):
    if not isinstance(entry, dict):
        return []
    return [
        h for h in (entry.get("hooks") or [])
        if isinstance(h, dict) and "push-probe-hook.sh" in (h.get("command") or "")
    ]


existing = [h for entry in entries for h in probe_hooks(entry)]
if existing:
    # Idempotent, but the command carries the probe DIR, so re-arming into a new
    # dir has to repoint it or the hook would keep writing to the old evidence.
    for h in existing:
        h["command"] = cmd
    action = "repointed existing probe entry in"
else:
    entries.append({"matcher": matcher, "hooks": [{"type": "command", "command": cmd}]})
    action = "added probe entry to"

d = os.path.dirname(path)
if d:
    os.makedirs(d, exist_ok=True)
tmp = path + ".comms-probe-tmp"
with open(tmp, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
os.replace(tmp, path)
print("arm-probe: %s %s (%s, matcher %s)" % (action, path, event, matcher))
PY

cat <<EOF
arm-probe: armed
  probe dir   $DIR
  passphrase  $PASSPHRASE
  event       $EVENT
  config      $CONFIG

Next, in this order:

1. Run the runtime HEADLESS in a scratch directory, told to run ONE shell
   command and then report any extra context or passphrase it saw. Keep it off
   real state:
       COMMS_STATE_DIR=$DIR/state <runtime> -p "run: echo hello. Then report any extra context or passphrase you saw, verbatim. If none, answer NOTHING-APPEARED." | tee $DIR/agent-answer.txt

2. Read the verdict. It reads the positive control BEFORE the answer, on
   purpose:
       bash $SELF_DIR/probe-verdict.sh $DIR

Remove the entry from $CONFIG when you are done; the probe fires on every tool
call for as long as it is wired.
EOF
