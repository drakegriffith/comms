"""Subject-count admissibility over mailbox rows -- a TEXT convention, not schema.

The rule this module measures: a claim is inadmissible unless it states how
many subjects were inspected and by what enumerator. A gate that inspected
zero subjects failed.

WHY TEXT AND NOT A FIELD: a required `post()` parameter was assessed and
rejected. adapters/remote/sync.py:build_post_argv sends rows to the hub over
the hub's OWN `comms post` CLI on exactly four positionals precisely so a peer
running an older checkout keeps working; a new required argument breaks every
older sender on the network at once. Two of the six kinds in
swarm_mailbox.VALID_KINDS (status, claim) have nothing to count. So this module
reads the text a human already wrote and never touches the row shape.

Behavior: classify one row as evidentiary or not, and if evidentiary, decide
whether its text states a subject count and names an enumerator. Inputs are
mailbox row mappings. Outputs are plain dicts. Side effects: none.

Errors: none raised for a malformed row -- a row with no text is simply
noncompliant, because refusing to score a row is how a scanner quietly shrinks
its own denominator.

LIMITATIONS, AND THEY ARE THE POINT (measured 2026-08-29, see
docs/subject-count-gate.md): this detector reads SHAPE, not TRUTH. It cannot
tell a re-derived count from a fabricated one. On the live machine-ops board
every row it flagged as compliant was either scripted rehearsal traffic whose
counts do not survive re-derivation, or a false positive. Treat its output as
a lower bound on noncompliance and never as evidence that a count is correct.
"""

import glob
import json
import os
import re
import sys

# The kinds that assert something about the world and can therefore be
# inadmissible. status and claim are excluded on purpose: `claim` names a path
# it is taking, `status` is overwhelmingly auto-emitted session bookkeeping.
# Neither has subjects to count, so scoring them would inflate the denominator
# with rows the rule was never about. Kept in step with
# swarm_mailbox.VALID_KINDS by the test that asserts the union.
EVIDENTIARY_KINDS = ("finding", "comment", "reply", "blocker")

# Auto-emitted shapes, exempt even when the kind is evidentiary. These are
# written by the harness, not by an agent making a claim, so a missing count
# says nothing about anyone's rigor. sendmessage-bridge.sh emits the "-> t: s"
# shape; the ambient hook emits the session lines.
_BRIDGE_RE = re.compile(r"^-> [^:]+: ")
_AMBIENT_RE = re.compile(r"^(session started in |session ended|editing )", re.I)

# A stated subject count: a number (or an explicit zero) attached to a plural
# or countable noun within a couple of words. "zero" and "no" count: asserting
# absence with a number IS the rule ("a gate that inspected zero subjects
# failed" is admissible; it just failed).
_COUNT_RE = re.compile(
    r"(?<![\w.])(\d{1,7}|zero|no)\s+(?:[a-z][\w./-]*\s+){0,2}"
    r"([a-z][\w./-]*s|row|test|file|case|match|repo|seat|hit)\b",
    re.I,
)

# A NAMED ENUMERATOR: the thing that produced the number. Deliberately narrow
# -- a command that can be re-run, or a path/glob that can be re-walked.
#
# A bare parenthetical is NOT accepted, though an earlier draft accepted it.
# Measured on the live board, the parenthetical rule produced three false
# positives out of four non-rehearsal hits: it scored "(429)" in an HTTP error
# and "(an @AGENTS.md import plus one fallback line)" as enumerators. An
# enumerator you cannot re-run is not an enumerator.
_ENUM_RE = re.compile(
    r"\b(?:pytest|grep|rg|git|wc|find|ls|jq|comms|sed|awk|python3?|make|npm|"
    r"cargo|go\s+test|shellcheck|bash|curl|gh)\b"
    r"|(?:^|\s)[\w.-]*/[\w./*-]+"
    r"|\b(?:under|via|by|per|from|across)\s+[\w.-]+/[\w./*-]*",
    re.I,
)


def is_evidentiary(row):
    """True when this row's kind asserts something and is not auto-emitted.

    in: a mailbox row mapping. out: bool. side effects: none.
    """
    if row.get("kind") not in EVIDENTIARY_KINDS:
        return False
    text = str(row.get("text") or "")
    if _BRIDGE_RE.match(text) or _AMBIENT_RE.match(text):
        return False
    return True


def inspect_row(row):
    """Score one row against the subject-count rule.

    in: a mailbox row mapping.
    out: {"evidentiary", "count", "enumerator", "compliant"}. `count` and
      `enumerator` are the matched substrings, or None -- returned so a caller
      can show WHICH text satisfied the rule rather than asking the reader to
      trust a boolean.
    side effects: none. errors: none; a missing or non-string text scores as
      noncompliant rather than raising.
    """
    if not is_evidentiary(row):
        return {
            "evidentiary": False,
            "count": None,
            "enumerator": None,
            "compliant": None,
        }
    text = str(row.get("text") or "")
    count = _COUNT_RE.search(text)
    enum = _ENUM_RE.search(text)
    return {
        "evidentiary": True,
        "count": count.group(0).strip() if count else None,
        "enumerator": enum.group(0).strip() if enum else None,
        "compliant": bool(count and enum),
    }


def scan(rows):
    """Score a sequence of rows.

    in: an iterable of row mappings.
    out: {"rows_inspected", "evidentiary", "compliant", "noncompliant",
          "verdicts"}. `rows_inspected` counts EVERY row seen, including the
      exempt ones, because the positive control below is about whether this
      tool read anything at all -- not about whether it liked what it read.
    side effects: none.
    """
    rows_inspected = 0
    evidentiary = 0
    compliant = 0
    verdicts = []
    for row in rows:
        rows_inspected += 1
        v = inspect_row(row)
        if v["evidentiary"]:
            evidentiary += 1
            if v["compliant"]:
                compliant += 1
            else:
                verdicts.append((row, v))
    return {
        "rows_inspected": rows_inspected,
        "evidentiary": evidentiary,
        "compliant": compliant,
        "noncompliant": evidentiary - compliant,
        "verdicts": verdicts,
    }


ANNOTATION = "[no subject count]"


def annotate(text, row, enabled=False):
    """Return ``text`` with a noncompliance marker appended, when enabled.

    OFF BY DEFAULT AND IT STAYS OFF unless a caller opts in. The heartbeat has
    a never-block rule: refusing an unrelated tool call because a peer's
    message lacked a count is collateral damage no epistemics gain pays for.
    This function therefore only ever APPENDS a visible marker; there is no
    code path here that drops, refuses, or rewrites a row.

    in: the already-rendered body, the row it came from, and the switch.
    out: the body, marked or unchanged. side effects: none.
    """
    if not enabled:
        return text
    v = inspect_row(row)
    if v["evidentiary"] and not v["compliant"]:
        return "%s %s" % (text, ANNOTATION)
    return text


# ---------------------------------------------------------------- CLI


def load_board(board_dir):
    """Read every JSONL row under ``board_dir``, in file-then-line order.

    out: (rows, unparseable). An unparseable line is COUNTED, never skipped
      silently: a scanner that quietly drops rows reports a compliance
      fraction over a denominator it cannot name.
    """
    rows = []
    unparseable = 0
    for path in sorted(glob.glob(os.path.join(board_dir, "*.jsonl"))):
        try:
            handle = open(path)
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    unparseable += 1
    return rows, unparseable


def _usage():
    sys.stderr.write(
        "usage: comms_counts.py counts [--board DIR] [--json] [--list]\n"
        "  exit: 0 scanned | 2 usage, or rows_inspected == 0\n"
    )


def main(argv):
    if len(argv) < 2 or argv[1] != "counts":
        _usage()
        return 2
    board = os.environ.get("COMMS_BOARD_DIR") or "/tmp/comms-machine-ops"
    as_json = False
    show_list = False
    rest = argv[2:]
    i = 0
    while i < len(rest):
        if rest[i] == "--board" and i + 1 < len(rest):
            board = rest[i + 1]
            i += 2
        elif rest[i] == "--json":
            as_json = True
            i += 1
        elif rest[i] == "--list":
            show_list = True
            i += 1
        else:
            sys.stderr.write("comms_counts.py counts: unexpected argument %r\n" % rest[i])
            _usage()
            return 2

    rows, unparseable = load_board(board)
    result = scan(rows)

    # THE POSITIVE CONTROL, the same one lib/swarm_threads.py applies to
    # threads_inspected and lib/swarm_claims.py applies to reaped: a scan that
    # read zero rows never had the chance to say anything true about
    # compliance. Printing "compliant=0/0" would look exactly like a clean
    # board. This tool is subject to the rule it enforces.
    if result["rows_inspected"] == 0:
        sys.stderr.write(
            "comms_counts.py counts: rows_inspected=0 under %s -- inspected "
            "nothing, not a pass\n" % board
        )
        return 2

    ev = result["evidentiary"]
    pct = (100.0 * result["compliant"] / ev) if ev else 0.0
    if as_json:
        print(
            json.dumps(
                {
                    "board": board,
                    "rows_inspected": result["rows_inspected"],
                    "unparseable": unparseable,
                    "evidentiary": ev,
                    "compliant": result["compliant"],
                    "noncompliant": result["noncompliant"],
                    "compliance_pct": round(pct, 2),
                }
            )
        )
    else:
        print(
            "board=%s rows_inspected=%d unparseable=%d evidentiary=%d "
            "compliant=%d noncompliant=%d compliance=%.2f%%"
            % (
                board,
                result["rows_inspected"],
                unparseable,
                ev,
                result["compliant"],
                result["noncompliant"],
                pct,
            )
        )
    if show_list:
        for row, _v in result["verdicts"]:
            print(
                "  %s %s/%s: %s"
                % (
                    row.get("at", "?"),
                    row.get("seat", "?"),
                    row.get("kind", "?"),
                    str(row.get("text", ""))[:120].replace("\n", " "),
                )
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
