"""Interface tests for the subject-count checker, its CLI, and the annotation.

These tests pin SHAPE detection. They deliberately do not pin truth: the
module cannot tell a re-derived count from a fabricated one, and
test_a_fabricated_count_still_reads_as_compliant is here to keep that
limitation visible to anyone who reaches for this as a gate.
"""

import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import comms_counts  # noqa: E402
import comms_render  # noqa: E402
import swarm_mailbox  # noqa: E402


def row(kind="finding", text="", **extra):
    r = {"seat": "s", "at": "2026-08-29T00:00:00+00:00", "kind": kind, "text": text}
    r.update(extra)
    return r


# ---- the kind partition ---------------------------------------------------


def test_evidentiary_kinds_are_a_subset_of_the_mailbox_vocabulary():
    # If a kind is added to VALID_KINDS, this forces a decision about whether
    # it has subjects to count instead of letting it default to unscored.
    assert set(comms_counts.EVIDENTIARY_KINDS) <= set(swarm_mailbox.VALID_KINDS)


@pytest.mark.parametrize("kind", ["status", "claim"])
def test_status_and_claim_are_never_evidentiary(kind):
    assert comms_counts.is_evidentiary(row(kind=kind, text="61 rows via pytest")) is False


@pytest.mark.parametrize("kind", comms_counts.EVIDENTIARY_KINDS)
def test_every_evidentiary_kind_is_scored(kind):
    assert comms_counts.is_evidentiary(row(kind=kind, text="anything")) is True


def test_a_bridge_row_is_exempt_even_though_its_kind_is_evidentiary():
    # sendmessage-bridge.sh writes this shape; no agent chose its wording, so
    # a missing count says nothing about anyone's rigor.
    assert comms_counts.is_evidentiary(row(kind="comment", text="-> worker: go")) is False


def test_an_ambient_session_row_is_exempt():
    assert comms_counts.is_evidentiary(
        row(kind="comment", text="session started in /tmp/x")
    ) is False


# ---- the two halves of the rule -------------------------------------------


def test_a_count_and_a_command_enumerator_is_compliant():
    v = comms_counts.inspect_row(row(text="664 test functions, counted with git grep"))
    assert v["compliant"] is True
    assert v["enumerator"] is not None


def test_a_count_and_a_path_enumerator_is_compliant():
    v = comms_counts.inspect_row(row(text="4 cases under tests/test_send_gate.py"))
    assert v["compliant"] is True


def test_an_explicit_zero_is_a_count_not_a_blank():
    # "a gate that inspected zero subjects failed" is an ADMISSIBLE claim.
    v = comms_counts.inspect_row(row(text="zero rows matched, per grep -c"))
    assert v["compliant"] is True


def test_a_count_with_no_enumerator_is_noncompliant():
    v = comms_counts.inspect_row(row(text="I looked at 12 files and they are fine"))
    assert v["count"] is not None
    assert v["enumerator"] is None
    assert v["compliant"] is False


def test_an_enumerator_with_no_count_is_noncompliant():
    v = comms_counts.inspect_row(row(text="ran pytest -q, all green"))
    assert v["count"] is None
    assert v["compliant"] is False


def test_a_bare_parenthetical_is_not_an_enumerator():
    # Measured regression: an earlier draft accepted any parenthetical and
    # scored "(429)" in an HTTP error as the enumerator for a subject count.
    v = comms_counts.inspect_row(row(kind="blocker", text="12 runs blocked (429)"))
    assert v["compliant"] is False


def test_a_row_with_no_text_scores_as_noncompliant_rather_than_raising():
    v = comms_counts.inspect_row({"kind": "finding"})
    assert v["compliant"] is False


def test_a_fabricated_count_still_reads_as_compliant():
    # THE KNOWN CEILING, kept executable so nobody mistakes this checker for a
    # truth oracle: this exact text shipped 10 times on the live board with a
    # number that does not survive re-derivation, and the checker likes it.
    v = comms_counts.inspect_row(
        row(text="pathway test run 1: 61 tests passed (pytest -q)")
    )
    assert v["compliant"] is True


# ---- scan -----------------------------------------------------------------


def test_scan_counts_every_row_seen_not_just_the_scored_ones():
    result = comms_counts.scan(
        [
            row(kind="status", text="up"),
            row(text="no count here"),
            row(text="3 files under lib/"),
        ]
    )
    assert result["rows_inspected"] == 3
    assert result["evidentiary"] == 2
    assert result["compliant"] == 1
    assert result["noncompliant"] == 1


def test_scan_of_nothing_reports_zero_rather_than_guessing():
    assert comms_counts.scan([])["rows_inspected"] == 0


# ---- the positive control -------------------------------------------------


def _run(args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "lib", "comms_counts.py")] + args,
        capture_output=True,
        text=True,
        env=e,
    )


def test_an_empty_board_exits_2_because_inspecting_nothing_is_not_a_pass(tmp_path):
    # The same invariant lib/swarm_threads.py applies to threads_inspected and
    # lib/swarm_claims.py applies to reaped. The tool obeys its own rule.
    proc = _run(["counts", "--board", str(tmp_path)])
    assert proc.returncode == 2
    assert "rows_inspected=0" in proc.stderr


def test_a_board_with_rows_exits_0_and_prints_the_fraction(tmp_path):
    board = tmp_path / "seat.jsonl"
    board.write_text(
        json.dumps(row(text="3 files under lib/")) + "\n"
        + json.dumps(row(text="no count here")) + "\n"
    )
    proc = _run(["counts", "--board", str(tmp_path), "--json"])
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out["rows_inspected"] == 2
    assert out["evidentiary"] == 2
    assert out["compliant"] == 1
    assert out["compliance_pct"] == 50.0


def test_an_unparseable_line_is_counted_never_silently_dropped(tmp_path):
    (tmp_path / "seat.jsonl").write_text(
        json.dumps(row(text="3 files under lib/")) + "\nnot json\n"
    )
    proc = _run(["counts", "--board", str(tmp_path), "--json"])
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["unparseable"] == 1


def test_an_unknown_flag_is_a_usage_error(tmp_path):
    assert _run(["counts", "--board", str(tmp_path), "--nope"]).returncode == 2


def test_the_router_reaches_the_module_and_preserves_its_exit_code(tmp_path):
    proc = subprocess.run(
        [os.path.join(REPO_ROOT, "bin", "comms"), "counts", "--board", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "rows_inspected=0" in proc.stderr


# ---- the annotation, off by default ---------------------------------------


def test_annotation_is_off_by_default():
    assert comms_counts.annotate("body", row(text="no count")) == "body"


def test_annotation_marks_a_noncompliant_evidentiary_row_when_enabled():
    marked = comms_counts.annotate("body", row(text="no count"), enabled=True)
    assert marked == "body " + comms_counts.ANNOTATION


def test_annotation_leaves_a_compliant_row_alone():
    out = comms_counts.annotate("body", row(text="3 files under lib/"), enabled=True)
    assert out == "body"


def test_annotation_leaves_an_exempt_kind_alone():
    out = comms_counts.annotate("body", row(kind="status", text="up"), enabled=True)
    assert out == "body"


def test_build_content_is_byte_identical_by_default(monkeypatch):
    monkeypatch.delenv(comms_render.ANNOTATE_COUNTS_VAR, raising=False)
    r = row(text="something with no count at all")
    assert comms_render.build_content(r, "engineer") == "\U0001f4ec✅ %s" % r["text"]


def test_build_content_annotates_when_the_env_switch_is_on(monkeypatch):
    monkeypatch.setenv(comms_render.ANNOTATE_COUNTS_VAR, "1")
    out = comms_render.build_content(row(text="no count at all"), "engineer")
    assert out.endswith(comms_counts.ANNOTATION)


@pytest.mark.parametrize("value", ["", "0", "false", "yes"])
def test_only_the_literal_1_turns_the_switch_on(monkeypatch, value):
    monkeypatch.setenv(comms_render.ANNOTATE_COUNTS_VAR, value)
    assert comms_render.annotate_counts_enabled() is False


def test_the_explicit_argument_beats_the_env_switch(monkeypatch):
    monkeypatch.setenv(comms_render.ANNOTATE_COUNTS_VAR, "1")
    out = comms_render.build_content(row(text="no count"), "engineer", annotate=False)
    assert not out.endswith(comms_counts.ANNOTATION)


def test_the_annotation_never_removes_the_original_body(monkeypatch):
    # The never-block rule, made executable: annotation only ever APPENDS.
    monkeypatch.setenv(comms_render.ANNOTATE_COUNTS_VAR, "1")
    r = row(text="no count at all")
    assert r["text"] in comms_render.build_content(r, "engineer")
