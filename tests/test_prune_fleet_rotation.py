"""Tests for safe fleet rotation report retention."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "prune-fleet-rotation.py"
SPEC = importlib.util.spec_from_file_location("prune_fleet_rotation", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def write_event(path: Path, event: dict[str, object]) -> None:
    """Append one serializer-produced event to a synthetic CardStore."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")


def age(path: Path, epoch: float) -> None:
    """Set one synthetic report directory's modification time."""
    os.utime(path, (epoch, epoch))


def test_dry_run_and_prune_preserve_live_card_reference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Old unreferenced reports prune while recent and live references remain."""
    home = tmp_path / ".skcapstone"
    root = home / "evidence" / "fleet-rotation"
    old = root / "20260101T000000Z"
    protected = root / "20260102T000000Z"
    recent = root / "20260120T000000Z"
    for report in (old, protected, recent):
        report.mkdir(parents=True)
        (report / "actions.log").write_text("synthetic\n", encoding="utf-8")
    age(old, 100)
    age(protected, 100)
    age(recent, 900)
    write_event(
        home / "cards" / "live1234" / "events" / "writer.jsonl",
        {"action": "evidence", "artifact_path": str(protected / "actions.log")},
    )

    assert MODULE.main(["--home", str(home), "--days", "0", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "protected=1" in output
    assert "eligible=2" in output
    assert "deleted=0" in output
    assert old.exists()

    counts = MODULE.prune_root(root, MODULE.live_card_references(home / "cards"), 800, False)
    assert counts == MODULE.PruneCounts(scanned=3, recent=1, protected=1, eligible=1, deleted=1)
    assert not old.exists()
    assert protected.exists()
    assert recent.exists()


def test_terminal_card_reference_does_not_protect_report(tmp_path: Path) -> None:
    """A completed card no longer exempts its referenced old report."""
    home = tmp_path / ".skcapstone"
    report = home / "evidence" / "fleet-rotation" / "old-report"
    report.mkdir(parents=True)
    stream = home / "cards" / "done1234" / "events" / "writer.jsonl"
    write_event(stream, {"action": "evidence", "artifact_path": str(report)})
    write_event(stream, {"action": "complete"})

    counts = MODULE.prune_root(
        report.parent,
        MODULE.live_card_references(home / "cards"),
        report.stat().st_mtime + 1,
        False,
    )
    assert counts.deleted == 1
    assert not report.exists()


def test_malformed_card_event_fails_closed(tmp_path: Path) -> None:
    """Malformed CardStore input aborts before deletion can be authorized."""
    home = tmp_path / ".skcapstone"
    report = home / "evidence" / "fleet-rotation" / "old-report"
    report.mkdir(parents=True)
    stream = home / "cards" / "live1234" / "events" / "writer.jsonl"
    stream.parent.mkdir(parents=True)
    stream.write_text("{not-json}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="malformed CardStore JSON"):
        MODULE.main(["--home", str(home)])
    assert report.exists()


def test_authority_host_fence_precedes_scan(tmp_path: Path) -> None:
    """The service fence refuses mutation when invoked away from chiap08."""
    with pytest.raises(SystemExit, match="outside authority host definitely-not-this-host"):
        MODULE.main(["--home", str(tmp_path), "--authority-host", "definitely-not-this-host"])
