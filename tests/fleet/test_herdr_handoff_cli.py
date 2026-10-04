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


@pytest.mark.parametrize("nested", [False, True])
def test_deliver_uses_existing_entry_points_and_explicit_roots(fake_core, tmp_path, nested):
    """Both entry points share one command implementation and authority actor."""
    packet = tmp_path / "packet.json"
    packet.write_text(json.dumps({"assignment_id": "assignment-one"}))
    root = click.Group("skcapstone")
    register_fleet_commands(root)
    args = (["fleet"] if nested else []) + [
        "handoff",
        "deliver",
        "--packet",
        str(packet),
        "--agent",
        "parent-owner",
        "--home",
        str(tmp_path / "coord"),
    ]
    result = CliRunner().invoke(
        root if nested else fleet,
        args,
        env={
            "SKFLEET_ROOT": str(tmp_path / "fleet"),
            "SKCAPSTONE_HOME": str(tmp_path / "must-not-use-ambient-home"),
        },
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["state"] == "submitted"
    assert fake_core[0] == [
        ("manager", tmp_path / "fleet", tmp_path / "coord", "parent-owner"),
        ("deliver", {"assignment_id": "assignment-one"}),
    ]


def test_observe_uses_configured_coordination_home(fake_core, tmp_path):
    """The default coordination root honors the existing runtime setting."""
    result = CliRunner().invoke(
        fleet,
        ["handoff", "observe", "assignment-one", "--agent", "helper-owner"],
        env={"SKFLEET_ROOT": str(tmp_path / "fleet"), "SKCAPSTONE_HOME": str(tmp_path / "coord")},
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"assignment_id": "assignment-one", "state": "unknown"}
    assert fake_core[0][-1] == ("observe", "assignment-one")
    assert fake_core[0][0][2:] == (tmp_path / "coord", "helper-owner")


@pytest.mark.parametrize("kind", ["pickup", "result"])
def test_receipt_delegates_exact_kind_and_payload(fake_core, tmp_path, kind):
    """Receipt authority and contents are validated by the manager."""
    payload = {"assignment_id": "assignment-one", "packet_sha256": "a" * 64}
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(payload))
    result = CliRunner().invoke(
        fleet,
        [
            "handoff",
            "receipt",
            "assignment-one",
            "--kind",
            kind,
            "--file",
            str(receipt),
            "--agent",
            "helper-owner",
            "--home",
            str(tmp_path / "coord"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake_core[0][-1] == ("receipt", "assignment-one", kind, payload)


def test_help_does_not_import_manager_or_transport(monkeypatch):
    """Help remains usable without importing execution dependencies."""
    monkeypatch.setitem(sys.modules, "skcapstone.fleet.herdr_handoff", None)
    result = CliRunner().invoke(fleet, ["handoff", "--help"])
    assert result.exit_code == 0, result.output
    assert all(name in result.output for name in ("deliver", "observe", "receipt"))


@pytest.mark.parametrize(
    "error", [ValueError("exact claim changed"), OSError("unreadable receipt")]
)
def test_expected_manager_errors_are_click_errors(fake_core, error):
    """Expected refusals produce a concise nonzero CLI result."""

    def refused(self, assignment_id):
        raise error

    fake_core[1].HandoffManager.observe = refused
    result = CliRunner().invoke(
        fleet, ["handoff", "observe", "assignment-one", "--agent", "owner"]
    )
    assert result.exit_code == 1
    assert f"Error: {error}" in result.output
    assert "Traceback" not in result.output


def test_bad_json_is_rejected_before_manager_creation(fake_core, tmp_path):
    """Invalid input cannot trigger manager or destination discovery."""
    packet = tmp_path / "bad.json"
    packet.write_text("{")
    result = CliRunner().invoke(
        fleet, ["handoff", "deliver", "--packet", str(packet), "--agent", "owner"]
    )
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert fake_core[0] == []


def test_actor_is_required_before_any_manager_call(fake_core):
    """Identity cannot silently fall back to an ambient agent."""
    result = CliRunner().invoke(fleet, ["handoff", "observe", "assignment-one"])
    assert result.exit_code == 2 and "--agent" in result.output
    assert fake_core[0] == []


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


def test_real_manager_cli_deliver_observe_and_receipts_preserve_ownership(tmp_path, monkeypatch):
    """Real registration and manager close a local receipt flow through fake Herdr."""
    from skcapstone.fleet import herdr_transport

    scenario = _real_scenario(tmp_path)
    monkeypatch.setattr(herdr_transport, "HerdrTransport", lambda: scenario.transport)
    before = [scenario.cards.fold(card).model_dump() for card in ("aaaa1111", "bbbb2222")]
    assert all(
        path.is_relative_to(tmp_path)
        for path in (
            scenario.home,
            scenario.paths.root,
            scenario.workspace,
        )
    )
    packet = tmp_path / "packet.json"
    packet.write_text(json.dumps(scenario.packet))
    runner = CliRunner()

    def invoke(arguments, actor="jarvis"):
        output = runner.invoke(
            fleet,
            ["handoff", *arguments, "--agent", actor, "--home", str(scenario.home)],
            env={"SKFLEET_ROOT": str(scenario.paths.root)},
        )
        assert output.exit_code == 0, output.output
        return json.loads(output.output)

    delivered = invoke(["deliver", "--packet", str(packet)])
    assert delivered["state"] == "submitted"
    assert invoke(["deliver", "--packet", str(packet)]) == delivered
    scenario.transport.agent.update(state="working", state_change_seq=11)
    observed = invoke(["observe", "assignment-1"], "pi-helper-test")
    assert observed["state"] == "submitted" and "pickup" not in observed
    payload = {
        "assignment_id": "assignment-1",
        "packet_sha256": delivered["packet_sha256"],
        "helper_id": "bbbb2222",
        "claim_revision": "helper-1",
        "base_revision": delivered["contract"]["base_revision"],
        "cwd": str(scenario.workspace),
    }
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(payload))
    picked_up = invoke(
        [
            "receipt",
            "assignment-1",
            "--kind",
            "pickup",
            "--file",
            str(receipt),
        ],
        "pi-helper-test",
    )
    assert picked_up["state"] == "acknowledged"
    artifact = scenario.workspace / "result.txt"
    artifact.write_bytes(b"bounded worker result\n")
    payload.update(
        artifacts=[
            {"path": "result.txt", "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
        ],
        tests=[{"command": "pytest -q", "outcome": "worker reported one passing check"}],
    )
    receipt.write_text(json.dumps(payload))
    result = invoke(
        [
            "receipt",
            "assignment-1",
            "--kind",
            "result",
            "--file",
            str(receipt),
        ],
        "pi-helper-test",
    )
    assert result["state"] == "delivered"
    assert result["result"]["artifacts"] == payload["artifacts"]
    assert len(scenario.transport.sends) == 1
    assert [scenario.cards.fold(card).model_dump() for card in ("aaaa1111", "bbbb2222")] == before


@pytest.mark.parametrize("data", [b'{"a":1,"a":2}', b"x" * 32769, b"[]"])
def test_cli_keeps_real_loader_bounds_without_manager_or_transport(tmp_path, monkeypatch, data):
    """The CLI preserves strict JSON checks rather than parsing input itself."""
    from skcapstone.fleet import herdr_handoff_cli

    monkeypatch.setattr(
        herdr_handoff_cli, "_manager", lambda *_args: pytest.fail("manager created")
    )
    path = tmp_path / "invalid.json"
    path.write_bytes(data)
    result = CliRunner().invoke(
        fleet,
        [
            "handoff",
            "deliver",
            "--packet",
            str(path),
            "--agent",
            "jarvis",
            "--home",
            str(tmp_path / "coord"),
        ],
    )
    assert result.exit_code == 1 and "Error:" in result.output
    assert "manager created" not in result.output
