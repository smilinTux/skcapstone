from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet.workspace_retention import archive_workspaces

NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=8)).isoformat()
RECENT = (NOW - timedelta(days=2)).isoformat()


def _card(home: Path, card_id: str, action: str | None, timestamp: str = OLD) -> None:
    store = CardStore(home)
    store.create(CardCore(id=card_id, title=card_id, created_by="test"))
    if action:
        store.append_event(card_id, action, "test", ts=timestamp)


def _workspace(home: Path, card_id: str, content: bytes = b"payload") -> Path:
    path = home / "fleet" / "workspaces" / f"pi-test-chiap08-{card_id}"
    path.mkdir(parents=True)
    (path / "artifact.bin").write_bytes(content)
    return path


def test_dry_run_reports_only_old_terminal_and_changes_nothing(tmp_path: Path) -> None:
    _card(tmp_path, "11111111", "complete")
    _card(tmp_path, "22222222", "void")
    _card(tmp_path, "33333333", "complete", RECENT)
    _card(tmp_path, "44444444", None)
    paths = [
        _workspace(tmp_path, card_id)
        for card_id in ("11111111", "22222222", "33333333", "44444444")
    ]

    results = archive_workspaces(tmp_path, dry_run=True, now=NOW)

    assert {result.card_id for result in results if result.disposition == "would_archive"} == {
        "11111111",
        "22222222",
    }
    assert all(path.exists() for path in paths)
    assert not (tmp_path / "fleet" / "workspaces-archive").exists()


def test_archive_creates_verified_tar_zst_then_removes_workspace(tmp_path: Path) -> None:
    _card(tmp_path, "aaaaaaaa", "complete")
    workspace = _workspace(tmp_path, "aaaaaaaa")

    result = archive_workspaces(tmp_path, now=NOW)[0]

    assert result.disposition == "archived"
    assert result.archive is not None and result.archive.exists()
    assert not workspace.exists()
    members = subprocess.run(
        ["tar", "--zstd", "-tf", str(result.archive)], check=True, capture_output=True, text=True
    ).stdout
    assert "artifact.bin" in members


def test_hashed_evidence_and_reopened_terminal_card_are_never_touched(tmp_path: Path) -> None:
    evidence = b"review me"
    _card(tmp_path, "bbbbbbbb", "complete")
    protected = _workspace(tmp_path, "bbbbbbbb", evidence)
    store = CardStore(tmp_path)
    store.append_event(
        "bbbbbbbb",
        "link",
        "test",
        link_key="evidence",
        link_value=f"sha256:{hashlib.sha256(evidence).hexdigest()}",
    )

    _card(tmp_path, "cccccccc", "complete")
    reopened = _workspace(tmp_path, "cccccccc")
    store.append_event("cccccccc", "move", "test", column="doing", ts=RECENT)

    results = archive_workspaces(tmp_path, now=NOW)

    reasons = {result.card_id: result.reason for result in results}
    assert reasons["bbbbbbbb"] == "contains hashed evidence"
    assert reasons["cccccccc"] == "card is live or unreadable"
    assert protected.exists() and reopened.exists()


def test_void_remains_terminal_despite_later_ignored_lifecycle_event(tmp_path: Path) -> None:
    _card(tmp_path, "dddddddd", "void")
    workspace = _workspace(tmp_path, "dddddddd")
    store = CardStore(tmp_path)
    events = store._read_events("dddddddd")  # noqa: SLF001
    events.append(
        {
            "event_id": "synthetic-after-void",
            "ts": RECENT,
            "writer": "test",
            "seq": 99,
            "action": "move",
            "column": "doing",
        }
    )
    event_path = next((tmp_path / "cards" / "dddddddd" / "events").glob("*.jsonl"))
    event_path.write_text("".join(f"{json.dumps(event)}\n" for event in events))

    result = archive_workspaces(tmp_path, dry_run=True, now=NOW)[0]

    assert result.disposition == "would_archive"
    assert workspace.exists()
