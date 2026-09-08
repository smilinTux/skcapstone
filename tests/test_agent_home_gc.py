"""Tests for conservative one-shot agent-home garbage collection."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skcoord.card_store import CardCore, CardStore

from skcapstone.agent_home_gc import collect, is_worker_name

NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


def _touch_tree(path: Path, when: datetime) -> None:
    """Set all timestamps in a synthetic home to the same instant."""
    timestamp = when.timestamp()
    for child in path.rglob("*"):
        os.utime(child, (timestamp, timestamp), follow_symlinks=False)
    os.utime(path, (timestamp, timestamp), follow_symlinks=False)


def _home(root: Path, name: str, age_days: int) -> Path:
    """Create a synthetic agent home with a cache file."""
    path = root / "agents" / name
    path.mkdir(parents=True)
    (path / "cache.bin").write_bytes(b"cache")
    _touch_tree(path, NOW - timedelta(days=age_days))
    return path


def _live_claim(root: Path, owner: str) -> None:
    """Create a synthetic live CardStore claim."""
    store = CardStore(root)
    store.create(CardCore(id="1234abcd", title="Synthetic live claim"))
    store.append_event("1234abcd", "claim", "test", owner=owner)
    store.append_event("1234abcd", "move", "test", column="doing")


def test_worker_name_allowlist_and_reserved_seats() -> None:
    """Only known prefixes qualify, and reserved or template names do not."""
    assert is_worker_name("pi-card-worker")
    assert is_worker_name("codex-card-worker")
    assert is_worker_name("pi-glm-card-worker")
    assert is_worker_name("pi-qwen-card-worker")
    assert not is_worker_name("jarvis")
    assert not is_worker_name("pi-template")
    assert not is_worker_name("worker-without-known-prefix")


def test_dry_run_reports_without_deleting_and_preserves_evidence(tmp_path: Path) -> None:
    """Dry-run is default and never touches the evidence tree."""
    stale = _home(tmp_path, "pi-old-worker", 31)
    _home(tmp_path, "pi-young-worker", 30)
    evidence = tmp_path / "evidence" / "work" / "card" / "artifact.txt"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("keep", encoding="utf-8")
    report_path = tmp_path / "reports" / "dry-run.json"

    result = collect(tmp_path, report_path, now=NOW)

    assert stale.exists()
    assert evidence.read_text(encoding="utf-8") == "keep"
    assert [item["name"] for item in result["candidates"]] == ["pi-old-worker"]
    assert result["deleted"] == []
    on_disk = json.loads(report_path.read_text(encoding="utf-8"))
    assert on_disk["mode"] == "dry-run"
    assert on_disk["evidence_root_untouched"] == str(tmp_path / "evidence" / "work")
    assert len(result["report_sha256"]) == 64


def test_apply_deletes_only_stale_unclaimed_workers(tmp_path: Path) -> None:
    """Apply retains live claims, reserved names, unknown names, and symlinks."""
    removable = _home(tmp_path, "codex-old-worker", 45)
    claimed = _home(tmp_path, "pi-glm-live-worker", 45)
    reserved = _home(tmp_path, "pi-template", 45)
    unknown = _home(tmp_path, "other-old-worker", 45)
    target = _home(tmp_path, "pi-young-target", 1)
    symlink = tmp_path / "agents" / "pi-old-symlink"
    symlink.symlink_to(target, target_is_directory=True)
    _live_claim(tmp_path, claimed.name)

    result = collect(tmp_path, tmp_path / "apply.json", apply=True, now=NOW)

    assert result["deleted"] == [removable.name]
    assert not removable.exists()
    assert claimed.exists()
    assert reserved.exists()
    assert unknown.exists()
    assert symlink.is_symlink()
    assert target.exists()


def test_unreadable_cardstore_fails_closed(tmp_path: Path) -> None:
    """Malformed CardStore input prevents both reporting and deletion."""
    stale = _home(tmp_path, "pi-old-worker", 45)
    card = tmp_path / "cards" / "1234abcd"
    card.mkdir(parents=True)
    (card / "core.json").write_text("{not json}\n", encoding="utf-8")

    try:
        collect(tmp_path, tmp_path / "report.json", apply=True, now=NOW)
    except Exception:
        pass
    else:
        raise AssertionError("malformed CardStore unexpectedly allowed collection")

    assert stale.exists()
    assert not (tmp_path / "report.json").exists()
