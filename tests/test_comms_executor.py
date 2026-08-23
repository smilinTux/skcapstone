"""Tests for CommsExecutor (P4, card c6a87139): draft-only, never raises,
structurally unable to send."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from skcapstone.comms_executor import CommsExecutor

_MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "skcapstone" / "comms_executor.py"

#: Any of these appearing as an imported name would mean this module could
#: reach a real transport. None of them should ever show up here.
_FORBIDDEN_IMPORT_NAMES = {
    "smtplib",
    "requests",
    "httpx",
    "urllib",
    "socket",
    "skchat",
    "skcomms",
    "telegram",
}


def test_module_imports_no_send_capable_client():
    """Structural check, not just behavioral: parse the module's own AST and
    assert none of its imports name anything network/transport-capable, so
    there is nothing in this file able to perform an outbound side effect
    even before ``send`` is reached."""
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
    assert imported_roots.isdisjoint(_FORBIDDEN_IMPORT_NAMES)


def test_call_produces_a_draft_and_never_sends():
    executor = CommsExecutor()
    out = executor({"card_id": "alert-a1", "title": "GMKtec RMA", "instruction": "escalate"})
    assert out["links"]["draft"]["status"] == "draft"
    assert out["links"]["draft"]["subject"] == "GMKtec RMA"
    assert out["links"]["draft"]["body"] == "escalate"
    assert "not sent" in out["summary"].lower() or "drafted" in out["summary"].lower()
    assert out["activity"][0]["atype"] == "action"


def test_call_stamps_prepared_by_from_context_agent():
    executor = CommsExecutor()
    out = executor({"card_id": "a", "title": "t", "instruction": "i", "agent": "lumina"})
    assert out["links"]["draft"]["prepared_by"] == "lumina"


def test_call_degrades_prepared_by_to_unattributed():
    executor = CommsExecutor()
    out = executor({"card_id": "a", "title": "t", "instruction": "i"})
    assert out["links"]["draft"]["prepared_by"] == "unattributed"


def test_call_never_raises_on_malformed_context():
    executor = CommsExecutor()

    # A context whose .get() itself blows up: simulate with a non-dict-like
    # object that still has the same interface but raises internally.
    class Hostile(dict):
        def get(self, *a, **k):
            raise ValueError("boom")

    out = executor(Hostile())
    assert out["links"] == {}
    assert "comms draft failed" in out["summary"].lower()
    assert out["activity"][0]["atype"] == "error"


def test_send_always_raises():
    executor = CommsExecutor()
    with pytest.raises(RuntimeError, match="must never send"):
        executor.send({"subject": "x"})


def test_send_raises_regardless_of_arguments():
    executor = CommsExecutor()
    with pytest.raises(RuntimeError):
        executor.send()
    with pytest.raises(RuntimeError):
        executor.send("anything", armed=True, armed_by="chef")
