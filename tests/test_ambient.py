"""Tests for the ambient lane (adapters/claude-code/ambient/).

EVERY write is isolated: COMMS_STATE_DIR and COMMS_ROOT point at tmp dirs, and
the installer is run against a FIXTURE settings.json, never the real one. HOME
is redirected too, so a script falling back to ~/.comms/state would write into
the sandbox and FAIL an assertion here rather than dirty real state.
"""

import json
import os
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AMBIENT = os.path.join(REPO, "adapters", "claude-code", "ambient")
SESSION_START = os.path.join(AMBIENT, "session-start.sh")
BRIDGE = os.path.join(AMBIENT, "sendmessage-bridge.sh")
INSTALL = os.path.join(AMBIENT, "install.sh")

SESSION_ID = "sess-abc123def456"


@pytest.fixture
def env(tmp_path):
    e = dict(os.environ)
    e["COMMS_STATE_DIR"] = str(tmp_path / "state")
    e["COMMS_ROOT"] = str(tmp_path / "root")
    e["HOME"] = str(tmp_path / "home")  # any ~ fallback lands in the sandbox
    e.pop("CLAUDE_SESSION_ID", None)
    e.pop("CLAUDE_MODEL", None)
    return e


def start_payload(cwd, session_id=SESSION_ID):
    return json.dumps(
        {
            "session_id": session_id,
            "cwd": cwd,
            "hook_event_name": "SessionStart",
            "source": "startup",
        }
    )


def bridge_payload(session_id=SESSION_ID, to="comms-b7",
                   summary="landed the fix", message="full body"):
    tool_input = {"to": to, "message": message}
    if summary is not None:
        tool_input["summary"] = summary
    return json.dumps(
        {
            "session_id": session_id,
            "cwd": "/anywhere",
            "hook_event_name": "PostToolUse",
            "tool_name": "SendMessage",
            "tool_input": tool_input,
            "tool_response": {"success": True},
        }
    )


def run(script, env, stdin=""):
    return subprocess.run(
        ["bash", script],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def mailbox_rows(env):
    d = os.path.join(env["COMMS_ROOT"], "comms-machine-ops")
    rows = []
    if not os.path.isdir(d):
        return rows
    for name in sorted(os.listdir(d)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(d, name)) as fh:
            rows.extend(json.loads(line) for line in fh if line.strip())
    return rows


def participants(env):
    d = os.path.join(env["COMMS_STATE_DIR"], "swarm-arm", "machine-ops",
                     "participants")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


# ---- session-start --------------------------------------------------------

def test_session_start_enrolls_and_posts_one_row(env, tmp_path):
    work = tmp_path / "myproj"
    work.mkdir()
    r = run(SESSION_START, env, start_payload(str(work)))
    assert r.returncode == 0
    assert r.stdout == ""  # SessionStart stdout is injected: success is silent
    parts = participants(env)
    assert len(parts) == 1
    rows = mailbox_rows(env)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "status"
    assert row["topic"] == "ops"
    assert row["text"] == "session started in %s" % work
    assert row["seat"].startswith("myproj-")
    # identity metadata landed for the mirror's rendering
    pfile = os.path.join(env["COMMS_STATE_DIR"], "swarm-arm", "machine-ops",
                         "participants", parts[0])
    with open(pfile) as fh:
        data = json.load(fh)
    assert data["model"] == "claude"
    assert data["project"] == "myproj"
    assert data["area"] == str(work)
    assert data["topics"] == ["ops"]


def test_session_start_idempotent_two_runs_one_enrollment(env, tmp_path):
    work = tmp_path / "myproj"
    work.mkdir()
    for _ in range(2):
        r = run(SESSION_START, env, start_payload(str(work)))
        assert r.returncode == 0
    assert len(participants(env)) == 1
    assert len(mailbox_rows(env)) == 1  # resume/clear never floods the board


def test_session_start_never_fails_on_garbage_stdin(env):
    r = run(SESSION_START, env, "not json at {{{ all")
    assert r.returncode == 0  # NEVER fail the session


# ---- sendmessage-bridge ---------------------------------------------------

def enroll_first(env, tmp_path):
    work = tmp_path / "myproj"
    work.mkdir(exist_ok=True)
    assert run(SESSION_START, env, start_payload(str(work))).returncode == 0


def test_bridge_row_shape(env, tmp_path):
    enroll_first(env, tmp_path)
    r = run(BRIDGE, env, bridge_payload())
    assert r.returncode == 0
    assert r.stdout == ""
    rows = [x for x in mailbox_rows(env) if x["kind"] == "comment"]
    assert len(rows) == 1
    row = rows[0]
    assert row["topic"] == "ops"
    assert row["text"] == "-> comms-b7: landed the fix"
    assert row["seat"].startswith("myproj-")  # same seat as the enrollment


def test_bridge_truncates_message_when_no_summary(env, tmp_path):
    enroll_first(env, tmp_path)
    long_msg = "x" * 500
    r = run(BRIDGE, env, bridge_payload(summary=None, message=long_msg))
    assert r.returncode == 0
    rows = [x for x in mailbox_rows(env) if x["kind"] == "comment"]
    assert rows[0]["text"] == "-> comms-b7: " + "x" * 200
    # the full body never leaves the payload
    assert "x" * 201 not in rows[0]["text"]


def test_bridge_skips_when_not_enrolled(env):
    r = run(BRIDGE, env, bridge_payload())
    assert r.returncode == 0
    assert mailbox_rows(env) == []  # bystander session: no row, no dir


def test_bridge_no_crash_on_garbage_stdin(env, tmp_path):
    enroll_first(env, tmp_path)
    before = mailbox_rows(env)
    r = run(BRIDGE, env, "}{ definitely not json \x00\x01")
    assert r.returncode == 0
    assert mailbox_rows(env) == before
    # privacy: nothing of the payload is echoed anywhere visible
    assert "definitely not json" not in r.stderr
    assert "definitely not json" not in r.stdout


def test_bridge_skips_other_tools(env, tmp_path):
    enroll_first(env, tmp_path)
    before = mailbox_rows(env)
    payload = json.loads(bridge_payload())
    payload["tool_name"] = "Bash"
    r = run(BRIDGE, env, json.dumps(payload))
    assert r.returncode == 0
    assert mailbox_rows(env) == before


# ---- installer ------------------------------------------------------------

FIXTURE_SETTINGS = {
    "model": "opus",
    "permissions": {"allow": ["Bash(ls:*)"]},
    "hooks": {
        "PostToolUse": [
            {"matcher": "*",
             "hooks": [{"type": "command",
                        "command": "bash /elsewhere/swarm-heartbeat.sh"}]}
        ]
    },
}


def write_fixture(tmp_path, content):
    path = tmp_path / "settings.json"
    if isinstance(content, str):
        path.write_text(content)
    else:
        path.write_text(json.dumps(content, indent=2) + "\n")
    return path


def install_stub_shim(env):
    """The installer requires the dispatch shim at $HOME/.claude/state/bin/
    hook-shim.sh (prerequisite check). HOME is the sandbox, so plant an
    executable stub there -- the installer only checks -x, never runs it."""
    shim = os.path.join(env["HOME"], ".claude", "state", "bin", "hook-shim.sh")
    os.makedirs(os.path.dirname(shim), exist_ok=True)
    with open(shim, "w") as fh:
        fh.write("#!/bin/bash\nexit 0\n")
    os.chmod(shim, 0o755)


def run_install(env, settings_path, *args):
    e = dict(env)
    e["COMMS_SETTINGS"] = str(settings_path)
    return subprocess.run(
        ["bash", INSTALL, *args],
        capture_output=True, text=True, env=e, timeout=60,
    )


SHIM_PREFIX = "bash $HOME/.claude/state/bin/hook-shim.sh observer "


def test_installer_wires_both_hooks_through_shim(env, tmp_path):
    install_stub_shim(env)
    path = write_fixture(tmp_path, FIXTURE_SETTINGS)
    r = run_install(env, path)
    assert r.returncode == 0, r.stderr
    got = json.loads(path.read_text())
    assert got["model"] == "opus"  # unrelated keys never clobbered
    assert got["permissions"] == {"allow": ["Bash(ls:*)"]}
    ptu = got["hooks"]["PostToolUse"]
    assert ptu[0]["hooks"][0]["command"].endswith("swarm-heartbeat.sh")
    bridge_entries = [e for e in ptu
                     if "sendmessage-bridge.sh" in e["hooks"][0]["command"]]
    assert len(bridge_entries) == 1
    assert bridge_entries[0]["matcher"] == "SendMessage"
    assert bridge_entries[0]["hooks"][0]["command"] == SHIM_PREFIX + BRIDGE
    ss = got["hooks"]["SessionStart"]
    assert len(ss) == 1
    assert ss[0]["hooks"][0]["command"] == SHIM_PREFIX + SESSION_START
    # the plist is printed, never installed
    assert "com.comms.discord-mirror.machine-ops" in r.stdout
    assert "COMMS_MACHINE_LABEL" in r.stdout
    # a timestamped backup sits beside the edited file
    assert any(".pre-ambient." in n for n in os.listdir(tmp_path))


def test_installer_idempotent_second_run_byte_identical(env, tmp_path):
    install_stub_shim(env)
    path = write_fixture(tmp_path, FIXTURE_SETTINGS)
    assert run_install(env, path).returncode == 0
    first = path.read_bytes()
    r2 = run_install(env, path)
    assert r2.returncode == 0
    assert path.read_bytes() == first
    assert "already present" in r2.stdout


def test_installer_check_mode_touches_nothing(env, tmp_path):
    install_stub_shim(env)
    path = write_fixture(tmp_path, FIXTURE_SETTINGS)
    before = path.read_bytes()
    r = run_install(env, path, "--check")
    assert r.returncode == 0, r.stderr
    assert path.read_bytes() == before          # dry-run wrote nothing
    assert "would add" in r.stdout              # and said what it would do
    assert "--check: nothing written" in r.stdout
    assert list(tmp_path.glob("settings.json.pre-ambient.*")) == []


def test_installer_refuses_corrupt_json(env, tmp_path):
    install_stub_shim(env)
    corrupt = "{this is not json,,,"
    path = write_fixture(tmp_path, corrupt)
    r = run_install(env, path)
    assert r.returncode == 1
    assert "refusing" in r.stderr
    assert path.read_text() == corrupt  # untouched, not clobbered


def test_installer_requires_shim(env, tmp_path):
    # No stub shim planted: prerequisite failure is exit 2, settings untouched.
    path = write_fixture(tmp_path, FIXTURE_SETTINGS)
    before = path.read_bytes()
    r = run_install(env, path)
    assert r.returncode == 2
    assert path.read_bytes() == before


def test_installer_creates_settings_when_absent(env, tmp_path):
    install_stub_shim(env)
    path = tmp_path / "settings.json"  # does not exist yet
    r = run_install(env, path)
    assert r.returncode == 0, r.stderr
    got = json.loads(path.read_text())
    assert "SessionStart" in got["hooks"]
    assert "PostToolUse" in got["hooks"]


# ---- completeness markers -------------------------------------------------

def test_hook_scripts_end_with_eof_marker():
    """The dispatch shim validates against mid-write tears on this exact final
    line; a tidy-up that strips it makes the shim skip the hook."""
    for script in (SESSION_START, BRIDGE):
        with open(script) as fh:
            lines = fh.read().splitlines()
        assert lines[-1] == "# hook-eof-marker v1 do-not-remove", script
