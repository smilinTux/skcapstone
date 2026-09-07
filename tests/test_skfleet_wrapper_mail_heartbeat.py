"""Tests for the wrapper's periodic, claim-neutral SKMail status signal."""

from __future__ import annotations

import argparse
import importlib.util
import threading
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "fleet" / "skfleet-worker-wrapper.py"


def _wrapper():
    spec = importlib.util.spec_from_file_location("skfleet_wrapper_mail_heartbeat", WRAPPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        owner="pi-codex-chiap08-abcd1234",
        card="abcd1234",
        claim_revision="r1",
        host="chiap08",
        lane="codex",
        model="sk-codex",
        mail_recipient="jarvis",
    )


def test_interval_defaults_and_rejects_nonpositive_values(monkeypatch) -> None:
    module = _wrapper()
    monkeypatch.delenv("SKFLEET_MAIL_HEARTBEAT_INTERVAL", raising=False)
    assert module.mail_heartbeat_interval() == 300
    monkeypatch.setenv("SKFLEET_MAIL_HEARTBEAT_INTERVAL", "0")
    assert module.mail_heartbeat_interval() == 300
    monkeypatch.setenv("SKFLEET_MAIL_HEARTBEAT_INTERVAL", "not-a-number")
    assert module.mail_heartbeat_interval() == 300
    monkeypatch.setenv("SKFLEET_MAIL_HEARTBEAT_INTERVAL", "7")
    assert module.mail_heartbeat_interval() == 7


def test_seat_metadata_is_derived_without_granting_authority() -> None:
    module = _wrapper()
    assert module.seat_from_owner("pi-link-chiap08-abcd1234") == "link"
    assert module.seat_from_owner("pi-codex-chiap08-abcd1234") == "codex"


def test_heartbeat_emits_status_until_stopped() -> None:
    module = _wrapper()
    stop = threading.Event()
    sent: list[tuple[str, str]] = []

    def capture(_args, kind: str, body: str) -> None:
        sent.append((kind, body))
        stop.set()

    with patch.object(module, "emit_work_mail", side_effect=capture):
        module.mail_heartbeat_loop(_args(), stop, interval=0)

    assert len(sent) == 1
    assert sent[0][0] == "agent.status"
    assert sent[0][1].startswith("phase=running seat=codex lane=codex model=sk-codex")
    assert "mailbox=" in sent[0][1]
    assert "claim" not in sent[0][1]


def test_heartbeat_loop_is_observability_only() -> None:
    source = WRAPPER.read_text(encoding="utf-8")
    assert "agent.status" in source
    assert "SKFLEET_MAIL_HEARTBEAT_INTERVAL" in source
    assert "coord claim" not in source


def test_startup_defaults_to_all_mail_recipient() -> None:
    module = _wrapper()
    parser = module.parse_args
    with patch.object(module.sys, "argv", ["wrapper", "--card", "abcd1234", "--owner", "pi-link-chiap08-abcd1234", "--claim-revision", "r1", "--host", "chiap08", "--lane", "codex", "--model", "sk-codex", "--stdout", "/tmp/x", "--evidence-dir", "/tmp/e", "--", "true"]):
        assert parser().mail_recipient == "all"
