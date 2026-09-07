"""Tests for worker mailbox polling."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).parents[1]
WRAPPER = ROOT / "scripts/fleet/skfleet-worker-wrapper.py"
SPEC = importlib.util.spec_from_file_location("skfleet_wrapper_mail_poll", WRAPPER)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_poll_mailbox_reads_all_without_ack(monkeypatch) -> None:
    class Result:
        returncode = 0
        stdout = "[NORMAL] sender -> all re HELP handoff\n(3 new)\n"
        stderr = ""

    monkeypatch.setattr(MODULE.shutil, "which", lambda name: "/bin/skmail")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command) or Result(),
    )
    value = MODULE.poll_mailbox("pi-seraph-chiap08-card")
    assert value.startswith("mailbox=ok new=3")
    assert "help_or_handoff=" in value
    assert calls == [["/bin/skmail", "read", "pi-seraph-chiap08-card"]]
