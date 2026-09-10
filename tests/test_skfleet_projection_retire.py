from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PATH = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-projection-retire"


def load_tool():
    loader = importlib.machinery.SourceFileLoader("skfleet_projection_retire", str(PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def stamp_age(path: Path, days: float) -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    os.utime(path, (old, old))


def write_projection(base: Path, name: str, payload: dict, age_days: float) -> Path:
    path = base / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    stamp_age(path, age_days)
    return path


def build_world(tmp_path: Path) -> Path:
    agents = tmp_path / ".skcapstone" / "coordination" / "agents"
    agents.mkdir(parents=True)
    old_seen = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    naive_seen = (datetime.now() - timedelta(days=40)).isoformat()
    rollback_seen = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat()
    write_projection(
        agents,
        "pi-a-host-aaaa1111.json",
        {"agent": "pi-a-host-aaaa1111", "current_task": None, "last_seen": old_seen},
        40,
    )
    write_projection(
        agents,
        "pi-a-host-bbbb2222.json",
        {"agent": "pi-a-host-bbbb2222", "current_task": "", "last_seen": old_seen},
        40,
    )
    write_projection(
        agents,
        "pi-a-host-cccc3333.json",
        {"agent": "pi-a-host-cccc3333", "current_task": "dddd4444", "last_seen": old_seen},
        40,
    )
    write_projection(
        agents,
        "pi-a-host-eeee5555.json",
        {"agent": "pi-a-host-eeee5555", "current_task": None, "last_seen": old_seen},
        1,
    )
    write_projection(
        agents,
        "pi-a-host-ffff6666.json",
        {"agent": "pi-a-host-ffff6666", "current_task": None, "last_seen": None},
        40,
    )
    write_projection(
        agents,
        "pi-a-host-77777777.json",
        {"agent": "someone-else", "current_task": None, "last_seen": old_seen},
        40,
    )
    write_projection(
        agents,
        "pi-a-host-naive0000.json",
        {"agent": "pi-a-host-naive0000", "current_task": None, "last_seen": naive_seen},
        40,
    )
    write_projection(
        agents,
        "pi-a-host-roll1111.json",
        {"agent": "pi-a-host-roll1111", "current_task": None, "last_seen": rollback_seen},
        40,
    )
    write_projection(
        agents,
        "pi-stem.json.json",
        {"agent": "pi-stem.json", "current_task": None, "last_seen": old_seen},
        40,
    )
    broken = agents / "pi-a-host-88888888.json"
    broken.write_text("{not json", encoding="utf-8")
    stamp_age(broken, 40)
    write_projection(
        agents,
        "pi-a-host-99999999.sync-conflict-1.json",
        {"agent": "pi-a-host-99999999", "current_task": None, "last_seen": old_seen},
        40,
    )
    return agents


def test_dry_run_lists_only_old_identity_valid_idle(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    tool = load_tool()
    agents = build_world(tmp_path)
    assert tool.main([]) == 0
    out = capsys.readouterr().out
    assert "RETIRE pi-a-host-aaaa1111.json" in out
    assert "RETIRE pi-a-host-bbbb2222.json" in out
    assert "cccc3333" not in out  # holds a task
    assert "eeee5555" not in out  # seen 1 day ago
    assert "ffff6666" not in out  # unreadable last_seen
    assert "77777777" not in out  # identity mismatch
    assert "naive0000" not in out  # offset-naive last_seen
    assert "roll1111" not in out  # last_seen newer than mtime
    assert "pi-stem.json" not in out  # stem trick
    assert "88888888" not in out  # malformed
    assert "99999999" not in out  # sync-conflict copy
    assert agents.joinpath("pi-a-host-aaaa1111.json").exists()


def test_apply_moves_manifests_and_is_idempotent(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    tool = load_tool()
    agents = build_world(tmp_path)
    assert tool.main(["--apply"]) == 0
    quarantined = tool.quarantine_dir()
    assert quarantined.joinpath("pi-a-host-aaaa1111.json").is_file()
    assert quarantined.joinpath("pi-a-host-bbbb2222.json").is_file()
    for kept in (
        "pi-a-host-cccc3333.json",
        "pi-a-host-eeee5555.json",
        "pi-a-host-ffff6666.json",
        "pi-a-host-77777777.json",
        "pi-a-host-88888888.json",
        "pi-a-host-99999999.sync-conflict-1.json",
    ):
        assert agents.joinpath(kept).exists(), kept
    records = [
        json.loads(line) for line in tool.manifest_path().read_text(encoding="utf-8").splitlines()
    ]
    assert [r["event"] for r in records] == ["moved", "moved"]
    assert all(r["sha256"] and r["original_path"] for r in records)
    before = agents.joinpath("pi-a-host-cccc3333.json").read_bytes()
    assert tool.main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert "already quarantined" not in out
    assert len(list(quarantined.glob("*.json"))) == 2
    assert agents.joinpath("pi-a-host-cccc3333.json").read_bytes() == before


def test_restore_returns_file_and_updates_manifest(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    tool = load_tool()
    agents = build_world(tmp_path)
    assert tool.main(["--apply"]) == 0
    assert tool.main(["--restore", "pi-a-host-aaaa1111.json"]) == 0
    assert agents.joinpath("pi-a-host-aaaa1111.json").is_file()
    assert not tool.quarantine_dir().joinpath("pi-a-host-aaaa1111.json").exists()
    assert "pi-a-host-aaaa1111.json" not in tool.moved_records()
    # Restoring again fails closed instead of duplicating.
    assert tool.main(["--restore", "pi-a-host-aaaa1111.json"]) == 1


def test_restore_refuses_sibling_escape_path(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    tool = load_tool()
    agents = build_world(tmp_path)
    assert tool.main(["--apply"]) == 0
    record = tool.moved_records()["pi-a-host-aaaa1111.json"]
    record["original_path"] = str(agents.parent / "agents-evil" / "pi-a-host-aaaa1111.json")
    lines = tool.manifest_path().read_text(encoding="utf-8").splitlines()
    lines[0] = json.dumps(record, sort_keys=True)
    tool.manifest_path().write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert tool.main(["--restore", "pi-a-host-aaaa1111.json"]) == 1
    assert not (agents.parent / "agents-evil").exists()
    assert tool.quarantine_dir().joinpath("pi-a-host-aaaa1111.json").is_file()


def test_corrupt_manifest_refuses_all_mutation(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    tool = load_tool()
    agents = build_world(tmp_path)
    tool.quarantine_dir().mkdir(parents=True)
    tool.manifest_path().write_text("{bad-json\n", encoding="utf-8")
    assert tool.main(["--apply"]) == 1
    assert agents.joinpath("pi-a-host-aaaa1111.json").is_file()
    assert not tool.quarantine_dir().joinpath("pi-a-host-aaaa1111.json").exists()
    assert tool.main(["--restore", "pi-a-host-aaaa1111.json"]) == 1


def test_restore_refuses_digest_mismatch(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    tool = load_tool()
    build_world(tmp_path)
    assert tool.main(["--apply"]) == 0
    quarantined = tool.quarantine_dir().joinpath("pi-a-host-aaaa1111.json")
    quarantined.write_text("tampered bytes", encoding="utf-8")
    assert tool.main(["--restore", "pi-a-host-aaaa1111.json"]) == 1
    assert quarantined.is_file()
