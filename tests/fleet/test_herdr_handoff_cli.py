"""Handoff commands delegate through the existing fleet entry points."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import click
import pytest
from click.testing import CliRunner

from skcapstone.fleet.cli import fleet, register_fleet_commands


@pytest.fixture
def fake_core(monkeypatch):
    """Supply a manager boundary without board, pane or transport activity."""
    calls = []
    module = ModuleType("skcapstone.fleet.herdr_handoff")

    class Manager:
        """Record exact CLI delegation for assertions."""

        def __init__(self, paths, coordination_home, actor):
            calls.append(("manager", paths.root, coordination_home, actor))

        def deliver(self, packet):
            calls.append(("deliver", packet))
            return {"state": "submitted", "assignment_id": "assignment-one"}

        def observe(self, assignment_id):
            calls.append(("observe", assignment_id))
            return {"state": "unknown", "assignment_id": assignment_id}

        def receipt(self, assignment_id, kind, payload):
            calls.append(("receipt", assignment_id, kind, payload))
            return {"state": kind, "assignment_id": assignment_id}

    module.HandoffManager = Manager
    module.load_json = lambda path: json.loads(Path(path).read_text())
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return calls, module


@pytest.mark.parametrize(
    "args",
    [
        ["deliver", "--packet", "/missing/packet.json", "--agent", "owner"],
        [
            "receipt",
            "assignment-one",
            "--kind",
            "complete",
            "--file",
            "/missing/receipt.json",
            "--agent",
            "owner",
        ],
        ["observe", "assignment-one"],
    ],
)
def test_argument_errors_do_not_import_handoff_core(monkeypatch, args):
    """Click validates arguments before loading any authority or transport code."""
    monkeypatch.setitem(sys.modules, "skcapstone.fleet.herdr_handoff", None)
    result = CliRunner().invoke(fleet, ["handoff", *args])
    assert result.exit_code == 2 and "Error:" in result.output
    assert not isinstance(result.exception, ModuleNotFoundError)


def _real_scenario(tmp_path):
    """Reuse the core writer's temporary board/Git setup without live authority."""
    specification = importlib.util.spec_from_file_location(
        "handoff_cli_fixture", Path(__file__).with_name("test_herdr_handoff.py")
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.build_scenario(tmp_path)
