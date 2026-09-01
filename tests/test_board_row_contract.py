#!/usr/bin/env python3
"""Executable half of docs/board-row-contract.md.

WHY THESE TESTS EXIST. On 2026-08-31 a dispatcher read an auto-posted
`kind="claim"` board row as registry ownership and told a human that a peer
session had misbehaved. It had not. The row means far less than its name, and
the fix was a written contract -- but a contract that lives only in prose rots
the first time somebody "cleans up" the predicate it describes.

So each test below pins ONE sentence of that document to code. A failure here
is not necessarily a bug: it means the world moved and docs/board-row-contract.md
must be re-ruled before the change lands. Read the doc, then decide.

These tests are pure (no files, no clock, no env) except where they read module
source to prove an ABSENCE, and every such absence carries a positive control:
a query that returns nothing proves nothing on its own.
"""

import datetime
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))
import swarm_mailbox as mb  # noqa: E402
import swarm_threads as st  # noqa: E402

CONTRACT = os.path.join(REPO, "docs", "board-row-contract.md")
BASE = datetime.datetime(2026, 8, 31, 22, 0, 0, tzinfo=datetime.timezone.utc)


def claim_row(seat, offset_s, thread="doc:comms/lib/swarm_threads.py"):
    """One auto-posted doc-enrol row, shaped exactly as the heartbeat emits it
    at adapters/claude-code/swarm-heartbeat.sh:784-787."""
    return {
        "seat": seat,
        "at": (BASE + datetime.timedelta(seconds=offset_s)).isoformat(),
        "kind": "claim",
        "text": "editing lib/swarm_threads.py",
        "topic": "board:comms",
        "thread": thread,
    }


# ---------------------------------------------------------------------------
# The contract document itself must stay reachable.
# ---------------------------------------------------------------------------


def test_contract_document_exists():
    """The ruling is the deliverable. If this file is gone, every pointer below
    is a lie and the naming defect is back with no record of the decision."""
    assert os.path.isfile(CONTRACT), (
        "docs/board-row-contract.md is missing; the kind=claim naming ruling "
        "has no home"
    )


@pytest.mark.parametrize(
    "source",
    ["README.md", os.path.join("lib", "swarm_threads.py")],
    ids=["readme", "swarm_threads"],
)
def test_readers_are_pointed_at_the_contract(source):
    """A reader meets these rows through the README or through the liveness
    predicate. Both must name the contract, or the reader re-derives it wrong
    the way the 2026-08-31 dispatcher did.

    Positive control is built in: the same read that must contain the pointer
    is asserted non-empty first, so a truncated or unreadable file fails as a
    missing FILE rather than silently passing as a missing pointer."""
    text = open(os.path.join(REPO, source), encoding="utf-8").read()
    assert text.strip(), "%s read as empty -- the assertion below would be vacuous" % source
    assert "docs/board-row-contract.md" in text, (
        "%s does not point at docs/board-row-contract.md" % source
    )


# ---------------------------------------------------------------------------
# "NOT that the seat holds a claim": no beat-path module consults any registry.
# ---------------------------------------------------------------------------

BEAT_PATH_SOURCES = [
    os.path.join("adapters", "claude-code", "swarm-heartbeat.sh"),
    os.path.join("lib", "swarm_mailbox.py"),
    os.path.join("lib", "swarm_arm.py"),
    os.path.join("lib", "swarm_threads.py"),
]
REGISTRY_TOKENS = ("claims.tsv", "claim.sh", "claim_guard")


def _code_lines(path):
    """Lines of `path` with whole-line `#` comments dropped -- the adapter
    DISCUSSES swarm_claims in a comment at line 25, and a contract about what
    the code DOES must not be broken by prose that says it does not."""
    out = []
    for line in open(path, encoding="utf-8"):
        if line.lstrip().startswith("#"):
            continue
        out.append(line)
    return out


def test_no_beat_path_module_reads_a_claim_registry():
    """The row is powerless because nothing on the emission path looks up a
    claim. If this test fails, somebody wired a registry in and the contract's
    first "NOT" is stale -- re-rule the document, do not delete the test.

    POSITIVE CONTROL: the same matcher is run over a synthetic line that does
    contain a registry token, and must find it. A grep that cannot find a
    known positive has not proven an absence."""
    control = ["if grep -q claims.tsv state/claims/claims.tsv; then\n"]
    assert any(t in ln for ln in control for t in REGISTRY_TOKENS), (
        "matcher failed its positive control; the absence below is meaningless"
    )

    offenders = []
    inspected = 0
    for rel in BEAT_PATH_SOURCES:
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path):
            continue
        inspected += 1
        for n, line in enumerate(_code_lines(path), 1):
            for token in REGISTRY_TOKENS:
                if token in line:
                    offenders.append("%s:%d %s" % (rel, n, line.strip()))
    # Silence is not evidence: a sweep that inspected zero subjects FAILED.
    assert inspected == len(BEAT_PATH_SOURCES), (
        "inspected %d of %d beat-path sources; a partial sweep cannot assert "
        "an absence" % (inspected, len(BEAT_PATH_SOURCES))
    )
    assert not offenders, (
        "a beat-path module now reads a claim registry:\n  %s\nRe-rule "
        "docs/board-row-contract.md before landing this." % "\n  ".join(offenders)
    )


def test_the_three_claims_are_three_different_mechanisms():
    """`kind="claim"` is a row on the board. `lib/swarm_claims.py` is a real
    arbiter with mutual exclusion. `~/.claude/hooks/claim.sh` is a registry.
    Only the row is powerless, and it is the one that reads like ownership --
    that is the whole naming defect. Pin that the row's kind is not somehow
    produced by the arbiter."""
    assert "claim" in mb.VALID_KINDS
    import swarm_claims  # noqa: F401  -- the arbiter exists and is separate

    arbiter_src = open(
        os.path.join(REPO, "lib", "swarm_claims.py"), encoding="utf-8"
    ).read()
    assert arbiter_src.strip(), "swarm_claims.py read as empty"
    assert "swarm_mailbox.post" not in arbiter_src, (
        "the arbiter now posts board rows; the two mechanisms have merged and "
        "the contract's disambiguation table is stale"
    )


# ---------------------------------------------------------------------------
# "NOT that anybody said anything": alive() is co-presence, exchange() is talk.
# ---------------------------------------------------------------------------


def test_alive_is_copresence_and_counts_auto_posted_rows():
    """Two auto-posted claim rows from two seats make a thread alive. This is
    DESIRED -- unclaimed co-presence is the collision case the write-set
    arbiters structurally cannot see -- but it is why `alive` must never be
    reported to a human as "a conversation"."""
    rows = [claim_row("seat-a", 0), claim_row("seat-b", 60)]
    assert st.alive(rows) is True


def test_exchange_rejects_what_alive_accepts():
    """The same rows are NOT an exchange. If these two ever agree on this
    input, the board has lost its only way to distinguish co-presence from
    somebody actually answering somebody."""
    rows = [claim_row("seat-a", 0), claim_row("seat-b", 60)]
    assert st.alive(rows) is True
    assert st.exchange(rows) is False


def test_exchange_still_fires_on_real_talk():
    """POSITIVE CONTROL for the test above: exchange() must not be a function
    that returns False for everything. Two comment rows from two seats inside
    the window are a real exchange and must read True."""
    rows = [
        dict(claim_row("seat-a", 0), kind="comment", text="taking the parser"),
        dict(claim_row("seat-b", 60), kind="reply", text="ack, I am on the lexer"),
    ]
    assert st.exchange(rows) is True


def test_claim_is_neither_conversation_nor_evidence():
    """No counting or chatter path may treat an auto-posted row as something a
    seat said. CONVO_KINDS drives the Discord conversation lane;
    EVIDENTIARY_KINDS drives the subject-count lint."""
    import comms_counts

    assert "claim" not in mb.CONVO_KINDS
    assert "claim" not in comms_counts.EVIDENTIARY_KINDS
    # Positive control: both vocabularies are non-empty and DO contain a kind
    # that is genuinely speech, so the two assertions above are not vacuous.
    assert "comment" in mb.CONVO_KINDS
    assert "finding" in comms_counts.EVIDENTIARY_KINDS


# ---------------------------------------------------------------------------
# DEF-6: the board carries no writer-side lifecycle, and that is the ruling.
# ---------------------------------------------------------------------------


def test_recency_is_a_reader_side_window_not_a_row_ttl():
    """The lifecycle ruling in prose: a past-tense observation does not expire,
    so recency is computed at READ time from the row's own `at` against a
    caller-supplied window. Pin that the window is a parameter -- the moment it
    becomes a property of the row, the ruling has been reversed.

    Same two rows, two windows, two answers. A row-baked TTL could not do
    this."""
    rows = [claim_row("seat-a", 0), claim_row("seat-b", 60)]
    assert st.alive(rows, window_s=3600) is True
    assert st.alive(rows, window_s=30) is False
    assert st.DEFAULT_WINDOW_S == 1800


def test_no_release_or_expiry_kind_was_added_to_the_wire():
    """DEF-6 was ruled AGAINST writer-side lifecycle. If a release/expiry kind
    shows up in VALID_KINDS, the board grew a lifecycle after all and
    docs/board-row-contract.md's four-reason argument needs re-litigating --
    including the cross-machine cost that argument turns on
    (adapters/remote/sync.py re-validates peer rows against VALID_KINDS)."""
    banned = {"release", "unclaim", "expire", "expiry", "done"}
    assert not (banned & set(mb.VALID_KINDS)), (
        "a lifecycle kind entered the wire vocabulary: %s"
        % sorted(banned & set(mb.VALID_KINDS))
    )
    # Positive control: the set operation works and VALID_KINDS is populated.
    assert {"claim", "status"} & set(mb.VALID_KINDS) == {"claim", "status"}
