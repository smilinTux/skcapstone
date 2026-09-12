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


def test_provider_neutral_ephemeral_identities_retire_but_standing_and_young_stay(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    tool = load_tool()
    agents = tmp_path / ".skcapstone" / "coordination" / "agents"
    agents.mkdir(parents=True)
    old_seen = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    identities = (
        ("pi-worker-a1b2c3d4", "pi-worker-01a1b2c3"),
        ("codex-worker-b2c3d4e5", "codex-worker-02b2c3d4"),
        ("glm-worker-c3d4e5f6", "glm-worker-03c3d4e5"),
        ("kimi-worker-d4e5f6a7", "kimi-worker-04d4e5f6"),
        ("cursor-worker-e5f6a7b8", "cursor-worker-05e5f6a7"),
        ("logical-bucket-worker-f6a7b8c9", "logical-bucket-worker-06f6a7b8"),
    )
    for old_name, young_name in identities:
        write_projection(
            agents,
            f"{old_name}.json",
            {"agent": old_name, "current_task": None, "last_seen": old_seen},
            40,
        )
        write_projection(
            agents,
            f"{young_name}.json",
            {
                "agent": young_name,
                "current_task": None,
                "last_seen": datetime.now(timezone.utc).isoformat(),
            },
            1,
        )
    for name in (
        "jarvis",
        "link",
        "mero",
        "niobe",
        "seraph",
        "tank",
        "atlas",
    ):
        write_projection(
            agents,
            f"{name}.json",
            {"agent": name, "current_task": None, "last_seen": old_seen},
            40,
        )
    symlink = agents / "linked-worker-07a7b8c9.json"
    symlink.symlink_to(agents / "pi-worker-a1b2c3d4.json")

    assert tool.main([]) == 0
    output = capsys.readouterr().out
    for old_name, young_name in identities:
        assert old_name in output
        assert young_name not in output
    for name in ("jarvis", "link", "mero", "niobe", "seraph", "tank", "atlas"):
        assert f"{name}.json" not in output
    assert symlink.is_symlink()


def test_stale_task_requires_fresh_exact_four_way_mismatch_proof(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    tool = load_tool()
    agents = tmp_path / ".skcapstone" / "coordination" / "agents"
    agents.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    old_seen = (now - timedelta(days=40)).isoformat()
    path = write_projection(
        agents,
        "codex-worker-a1b2c3d4.json",
        {
            "agent": "codex-worker-a1b2c3d4",
            "current_task": "a1b2c3d4",
            "claimed_tasks": ["a1b2c3d4"],
            "_claim_revision": "old-revision",
            "last_seen": old_seen,
        },
        40,
    )
    key = ("codex-worker-a1b2c3d4", "a1b2c3d4", "old-revision")
    proof = {
        "owner": key[0],
        "card_id": key[1],
        "claim_revision": key[2],
        "observed_at": now.isoformat(),
        "process_alive": False,
        "active_worker_unit": False,
        "direct_seat_record": False,
    }

    def mismatch(_card):
        return "new-owner", "new-revision"

    assert tool.assess(path, now, {key: proof}, mismatch)[0] is True
    for field in ("process_alive", "active_worker_unit", "direct_seat_record"):
        ambiguous = dict(proof)
        ambiguous[field] = True
        assert tool.assess(path, now, {key: ambiguous}, mismatch)[0] is False
    assert tool.assess(path, now, {}, mismatch)[0] is False

    def exact(_card):
        return key[0], key[2]

    assert tool.assess(path, now, {key: proof}, exact)[0] is False


def test_stale_task_apply_manifest_is_content_addressed_and_reversible(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    tool = load_tool()
    agents = tmp_path / ".skcapstone" / "coordination" / "agents"
    agents.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    name = "future-worker-a1b2c3d4"
    original = write_projection(
        agents,
        f"{name}.json",
        {
            "agent": name,
            "current_task": "a1b2c3d4",
            "claimed_tasks": ["a1b2c3d4"],
            "_claim_revision": "stale-revision",
            "last_seen": (now - timedelta(days=40)).isoformat(),
        },
        40,
    )
    proof_path = tmp_path / "proof.json"
    proof_path.write_text(
        json.dumps(
            [
                {
                    "owner": name,
                    "card_id": "a1b2c3d4",
                    "claim_revision": "stale-revision",
                    "observed_at": now.isoformat(),
                    "process_alive": False,
                    "active_worker_unit": False,
                    "direct_seat_record": False,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(tool, "folded_claim_identity", lambda _card: (None, "new-revision"))
    original_hash = tool.sha256_of(original)

    assert tool.main(["--apply", "--stale-task-proof", str(proof_path)]) == 0
    record = tool.moved_records()[original.name]
    assert record["sha256"] == original_hash
    assert record["disposition"] == "stale-task-non-exact"
    assert record["observed_at"]
    assert record["rollback"] == f"--restore {original.name}"
    assert tool.main(["--apply", "--stale-task-proof", str(proof_path)]) == 0
    assert tool.main(["--restore", original.name]) == 0
    assert tool.sha256_of(original) == original_hash


def test_stale_task_proof_refuses_stale_or_ambiguous_observations(tmp_path) -> None:
    tool = load_tool()
    now = datetime.now(timezone.utc)
    proof = {
        "owner": "worker-a1b2c3d4",
        "card_id": "a1b2c3d4",
        "claim_revision": "revision",
        "observed_at": (now - timedelta(minutes=6)).isoformat(),
        "process_alive": False,
        "active_worker_unit": False,
        "direct_seat_record": False,
    }
    path = tmp_path / "proof.json"
    path.write_text(json.dumps([proof]), encoding="utf-8")
    try:
        tool.load_stale_task_proofs(path, now)
    except tool.ProofError:
        pass
    else:
        raise AssertionError("stale proof must fail closed")

    proof["observed_at"] = now.isoformat()
    proof.pop("process_alive")
    path.write_text(json.dumps([proof]), encoding="utf-8")
    try:
        tool.load_stale_task_proofs(path, now)
    except tool.ProofError:
        pass
    else:
        raise AssertionError("ambiguous proof must fail closed")
