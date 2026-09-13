from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

PATH = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-projection-retire"


def load_tool():
    """Load the extensionless command as a Python module."""
    loader = importlib.machinery.SourceFileLoader("projection_quarantine", str(PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def world(tmp_path: Path, *, task: str | None = None) -> tuple[Path, Path, str]:
    """Create one canonical projection and one mismatched conflict copy."""
    home = tmp_path / ".skcapstone"
    agents = home / "coordination" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    canonical = agents / "worker.json"
    canonical.write_text('{"agent":"worker"}\n', encoding="utf-8")
    conflict = agents / "worker.sync-conflict-20260912-010203-DEVICE.json"
    conflict.write_text(
        json.dumps({"agent": "worker", "current_task": task, "claimed_tasks": []}) + "\n",
        encoding="utf-8",
    )
    return home, conflict, hashlib.sha256(conflict.read_bytes()).hexdigest()


def test_exact_quarantine_and_hash_guarded_restore(tmp_path, monkeypatch) -> None:
    """Quarantine and restore preserve exact bytes and the canonical sibling."""
    tool = load_tool()
    home, source, digest = world(tmp_path)
    monkeypatch.setattr(tool, "matching_processes", lambda *args: [])
    relative = f"coordination/agents/{source.name}"

    assert (
        tool.main(
            [
                "--home",
                str(home),
                "--quarantine-malformed",
                relative,
                "--expected-sha256",
                digest,
                "--actor",
                "jarvis",
            ]
        )
        == 0
    )
    destination = home / "coordination" / "recovery" / "sync-conflict-quarantine" / source.name
    assert destination.read_bytes()
    assert not source.exists()
    assert (home / "coordination" / "agents" / "worker.json").exists()
    receipt = json.loads((destination.parent / "manifest.jsonl").read_text().splitlines()[0])
    assert receipt["sha256"] == digest
    assert receipt["original_path"] == relative
    assert receipt["rollback_command"].startswith("skfleet-projection-retire --home ")

    assert (
        tool.main(
            [
                "--home",
                str(home),
                "--restore-malformed",
                source.name,
                "--expected-sha256",
                digest,
                "--actor",
                "jarvis",
            ]
        )
        == 0
    )
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert not destination.exists()


def test_quarantine_refuses_drift_identity_liveness_and_unsafe_paths(
    tmp_path, monkeypatch
) -> None:
    """Every source, identity, and generation guard fails closed."""
    tool = load_tool()
    monkeypatch.setattr(tool, "matching_processes", lambda *args: [])
    home, source, digest = world(tmp_path)
    base = [
        "--home",
        str(home),
        "--quarantine-malformed",
        f"coordination/agents/{source.name}",
        "--expected-sha256",
    ]
    assert tool.main([*base, "0" * 64, "--actor", "jarvis"]) == 1
    assert (
        tool.main(
            [
                "--home",
                str(home),
                "--quarantine-malformed",
                "../agents/x.json",
                "--expected-sha256",
                digest,
                "--actor",
                "jarvis",
            ]
        )
        == 1
    )
    source.write_text('{"agent":"not-worker","current_task":null,"claimed_tasks":[]}\n')
    changed = hashlib.sha256(source.read_bytes()).hexdigest()
    assert tool.main([*base, changed, "--actor", "jarvis"]) == 1

    source.unlink()
    _, source, digest = world(tmp_path, task="abcd1234")
    assert tool.main([*base, digest, "--actor", "jarvis"]) == 1
    assert source.exists()

    source.unlink()
    _, source, digest = world(tmp_path)
    hardlink = source.with_name(source.name + ".hardlink")
    hardlink.hardlink_to(source)
    assert tool.main([*base, digest, "--actor", "jarvis"]) == 1
    assert source.exists()


def test_quarantine_refuses_destination_collision_and_matching_process(
    tmp_path, monkeypatch
) -> None:
    """Live runtime evidence and occupied destinations prevent mutation."""
    tool = load_tool()
    home, source, digest = world(tmp_path)
    relative = f"coordination/agents/{source.name}"
    monkeypatch.setattr(tool, "matching_processes", lambda *args: ["live worker"])
    args = [
        "--home",
        str(home),
        "--quarantine-malformed",
        relative,
        "--expected-sha256",
        digest,
        "--actor",
        "jarvis",
    ]
    assert tool.main(args) == 1
    monkeypatch.setattr(tool, "matching_processes", lambda *args: [])
    destination = home / "coordination" / "recovery" / "sync-conflict-quarantine" / source.name
    destination.parent.mkdir(parents=True)
    destination.write_text("existing", encoding="utf-8")
    assert tool.main(args) == 1
    assert source.exists()
    assert destination.read_text() == "existing"


def test_restore_refuses_original_path_collision(tmp_path, monkeypatch) -> None:
    """Rollback never overwrites a new occupant at the original path."""
    tool = load_tool()
    home, source, digest = world(tmp_path)
    monkeypatch.setattr(tool, "matching_processes", lambda *args: [])
    relative = f"coordination/agents/{source.name}"
    assert (
        tool.main(
            [
                "--home",
                str(home),
                "--quarantine-malformed",
                relative,
                "--expected-sha256",
                digest,
                "--actor",
                "jarvis",
            ]
        )
        == 0
    )
    source.write_text("new occupant", encoding="utf-8")
    assert (
        tool.main(
            [
                "--home",
                str(home),
                "--restore-malformed",
                source.name,
                "--expected-sha256",
                digest,
                "--actor",
                "jarvis",
            ]
        )
        == 1
    )
    assert source.read_text() == "new occupant"
