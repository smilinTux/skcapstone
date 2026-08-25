"""Regression tests for the check-mode-only guard on run_ansible_playbook
(coord card e51a3e7e, SKW-AUTONOMY-E4).

Before this guard, ansible_tools.py ran ansible-playbook as a raw subprocess
with no freeze check, no capauth check, no ITIL requirement, and no
allowlist of playbooks or inventories: any agent session holding this MCP
tool could run any playbook against any inventory, live, just by omitting
dry_run or passing dry_run=false.

The full authorization gate (actuation readiness, allowlisted playbook
root, approved ITIL change id) is out of scope here and depends on a guard
helper another card is building. This is the fence, not the gate: a live
run must refuse unconditionally, and check mode must keep working exactly
as before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.mcp_tools import ansible_tools


class _FakeStream:
    """Stand-in for an asyncio.StreamReader that yields no output lines."""

    async def readline(self) -> bytes:
        return b""


class _FakeProc:
    def __init__(self) -> None:
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self.returncode = 0

    async def wait(self) -> int:
        return 0


def _payload(contents) -> dict:
    assert len(contents) == 1
    return json.loads(contents[0].text)


@pytest.fixture
def fake_playbook(tmp_path: Path) -> Path:
    playbook = tmp_path / "site.yml"
    playbook.write_text("- hosts: all\n  tasks: []\n", encoding="utf-8")
    return playbook


@pytest.fixture
def subprocess_spy(monkeypatch):
    """Record every argv list ansible_tools would hand to the subprocess,
    and stand in for the ansible-playbook binary so the check never depends
    on Ansible actually being installed or touching real inventory."""
    calls: list[list[str]] = []

    async def _fake_create_subprocess_exec(*cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeProc()

    monkeypatch.setattr(ansible_tools.shutil, "which", lambda name: "/usr/bin/ansible-playbook")
    monkeypatch.setattr(ansible_tools.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    import skcapstone.memory_engine as memory_engine

    monkeypatch.setattr(memory_engine, "store", lambda **kwargs: None)

    return calls


@pytest.mark.asyncio
async def test_live_run_refused(fake_playbook, subprocess_spy):
    """A live run (dry_run=false) must refuse before touching the subprocess.

    This is the sensitivity proof for card e51a3e7e: against the pre-guard
    code this assertion fails, because the handler happily builds a live
    ansible-playbook command (no --check) and hands it to the subprocess.
    """
    out = await ansible_tools._handle_run_ansible_playbook(
        {
            "playbook_path": str(fake_playbook),
            "inventory": "localhost,",
            "dry_run": False,
        }
    )
    payload = _payload(out)
    assert "error" in payload
    assert "e51a3e7e" in payload["error"]
    assert "dry_run" in payload["error"]
    assert subprocess_spy == [], "a live run must never reach the subprocess"


@pytest.mark.asyncio
async def test_live_run_refused_when_dry_run_omitted(fake_playbook, subprocess_spy):
    """dry_run defaults to false when omitted entirely, so an omitted
    dry_run is also a live run and must also refuse."""
    out = await ansible_tools._handle_run_ansible_playbook(
        {
            "playbook_path": str(fake_playbook),
            "inventory": "localhost,",
        }
    )
    payload = _payload(out)
    assert "error" in payload
    assert "e51a3e7e" in payload["error"]
    assert subprocess_spy == []


@pytest.mark.asyncio
async def test_check_mode_still_runs_with_check_flag(fake_playbook, subprocess_spy):
    """Check mode (dry_run=true) is unaffected: dry inspection never
    requires an authorization, so it must still reach the subprocess with
    --check set, and extra_vars must still travel as a single --extra-vars
    argv token rather than being exploded into extra flags."""
    out = await ansible_tools._handle_run_ansible_playbook(
        {
            "playbook_path": str(fake_playbook),
            "inventory": "localhost,",
            "dry_run": True,
            "extra_vars": {"foo": "bar --check=false"},
        }
    )
    payload = _payload(out)
    assert payload["dry_run"] is True
    assert payload["exit_code"] == 0

    assert len(subprocess_spy) == 1
    cmd = subprocess_spy[0]
    assert "--check" in cmd
    assert cmd.count("--check") == 1

    # extra_vars is one argv token (a JSON string), never multiple argv
    # entries: a malicious value inside it cannot smuggle its own flags
    # onto the ansible-playbook command line.
    assert "--extra-vars" in cmd
    extra_vars_index = cmd.index("--extra-vars") + 1
    assert cmd[extra_vars_index] == json.dumps({"foo": "bar --check=false"})
    assert len(cmd) == extra_vars_index + 1, "extra_vars must be the last argv token, not several"
