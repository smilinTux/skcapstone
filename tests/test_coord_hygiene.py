"""Governed migration of legacy reaper rows and quarantine of Syncthing conflicts."""

from __future__ import annotations

import json
from pathlib import Path

from skcoord.card import CardEventLog

from skcapstone import coord_hygiene as hygiene

LEGACY = (
    b'{"action":"verdict","card_id":"57764450","event_id":"aa","ts":"2026-09-01T05:50:46+00:00",'
    b'"verdict":"WORKER_DIED","writer":"fleet-liveness-reaper"}\n'
)
LINK = (
    b'{"action":"link","card_id":"57764450","event_id":"bb","link_key":"worker_died",'
    b'"link_value":"owner=x claim_revision=y","ts":"2026-09-01T05:50:46+00:00",'
    b'"writer":"fleet-liveness-reaper"}\n'
)


def _live(home: Path, writer: str, data: bytes) -> Path:
    directory = home / "coordination" / "card_events"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{writer}.jsonl"
    path.write_bytes(data)
    return path


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    live = _live(tmp_path, "fleet-liveness-reaper", LEGACY + LINK)
    report = hygiene.archive_legacy_verdicts(tmp_path)
    assert report.moved == {"fleet-liveness-reaper": 1}
    assert live.read_bytes() == LEGACY + LINK
    assert not (tmp_path / "coordination" / "recovery").exists()


def test_apply_moves_only_rejected_rows_byte_for_byte(tmp_path: Path) -> None:
    live = _live(tmp_path, "fleet-liveness-reaper", LEGACY + LINK + LEGACY.replace(b"aa", b"cc"))
    before = CardEventLog(tmp_path).read_all()
    report = hygiene.archive_legacy_verdicts(tmp_path, apply=True)
    assert report.moved == {"fleet-liveness-reaper": 2}
    assert live.read_bytes() == LINK
    moved = Path(report.archive["fleet-liveness-reaper"]).read_bytes()
    assert moved == LEGACY + LEGACY.replace(b"aa", b"cc")
    # The fold sees exactly the same events, and no rejected lines remain.
    log = CardEventLog(tmp_path)
    after = log.read_all()
    assert [e.event_id for e in after] == [e.event_id for e in before] == ["bb"]
    assert log.rejected == []
    # A full pre-migration copy and a manifest exist.
    out = Path(report.archive["fleet-liveness-reaper"]).parent
    backup = next(out.glob("*.pre-migration.jsonl"))
    assert backup.read_bytes() == LEGACY + LINK + LEGACY.replace(b"aa", b"cc")
    assert json.loads((out / "manifest.jsonl").read_text().splitlines()[0])["moved_rows"] == 2


def test_migration_is_idempotent_and_leaves_valid_verdicts(tmp_path: Path) -> None:
    valid = (
        b'{"action":"verdict","card_id":"c","link_key":"verdict","link_value":"PASS",'
        b'"ts":"2026-09-01T05:50:46+00:00","writer":"fleet-liveness-reaper"}\n'
    )
    live = _live(tmp_path, "fleet-liveness-reaper", LEGACY + valid)
    hygiene.archive_legacy_verdicts(tmp_path, apply=True)
    assert live.read_bytes() == valid
    again = hygiene.archive_legacy_verdicts(tmp_path, apply=True)
    assert again.moved == {}
    assert live.read_bytes() == valid


def test_quarantine_conflicts_moves_never_deletes(tmp_path: Path) -> None:
    coordination = tmp_path / "coordination"
    for name in ("agents", "tasks", "card_events"):
        (coordination / name).mkdir(parents=True)
    keep = coordination / "agents" / "bob.json"
    keep.write_text("{}")
    conflict = coordination / "agents" / "bob.sync-conflict-20261011-184807-ANXXYPM.json"
    conflict.write_text('{"agent": "other"}')
    task = coordination / "tasks" / "t.sync-conflict-20261011-184807-ANXXYPM.json"
    task.write_text("{}")
    append_only = coordination / "card_events" / "h.sync-conflict-20261011-184807-ANXXYPM.jsonl"
    append_only.write_text("x\n")

    dry = hygiene.quarantine_conflicts(tmp_path)
    assert len(dry.found) == 2 and conflict.exists()

    done = hygiene.quarantine_conflicts(tmp_path, apply=True)
    assert len(done.moved) == 2
    assert keep.exists() and not conflict.exists() and not task.exists()
    assert append_only.exists()  # opt-in only
    moved = [Path(p) for p in done.moved]
    assert sorted(p.read_text() for p in moved) == ['{"agent": "other"}', "{}"]
    assert all("sync-conflict-bulk" in str(p) for p in moved)

    opted = hygiene.quarantine_conflicts(tmp_path, apply=True, include_append_only=True)
    assert len(opted.moved) == 1 and not append_only.exists()


def test_cli_dry_run(tmp_path: Path, capsys) -> None:
    _live(tmp_path, "fleet-wedge-reaper", LEGACY.replace(b"liveness", b"wedge"))
    assert hygiene.main(["legacy-verdicts", "--home", str(tmp_path)]) == 0
    assert "DRY-RUN" in capsys.readouterr().out
