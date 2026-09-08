from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts/fleet/skfleet-projection-audit.py"


def load_module():
    spec = importlib.util.spec_from_file_location("skfleet_projection_audit", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_classifies_without_mutating_and_is_deterministic(tmp_path: Path) -> None:
    module = load_module()
    write(
        tmp_path / "pi-codex-chiap08-live.json",
        {
            "agent": "pi-codex-chiap08-live",
            "current_task": "a1b2c3d4",
            "last_seen": "2026-09-04T12:00:00Z",
        },
    )
    write(
        tmp_path / "pi-codex-chiap08-bad.json",
        {"agent": "other", "last_seen": "2026-09-04T12:00:00Z"},
    )
    write(
        tmp_path / "pi-codex-chiap08-null.json",
        {"agent": "pi-codex-chiap08-null", "last_seen": None},
    )
    conflict = tmp_path / "pi-codex-chiap08-live.sync-conflict-20260904.json"
    # Both matching and mismatching bytes are quarantined before parsing.
    write(conflict, {"agent": "pi-codex-chiap08-live", "last_seen": "bad"})
    mismatch = tmp_path / "pi-codex-chiap08-bad.sync-conflict-20260904.json"
    write(mismatch, {"agent": "unrelated", "last_seen": "bad"})
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    now = datetime(2026, 9, 4, 12, 1, tzinfo=timezone.utc)
    report = module.build_report(tmp_path, now=now, stale_after=900)
    assert report["counts"] == {
        "conflict-copy": 2,
        "identity-mismatch": 1,
        "malformed": 1,
        "canonical": 1,
    }
    assert report["capacity"] == 1
    conflict_record = next(r for r in report["records"] if r["filename"] == conflict.name)
    assert conflict_record["canonical_path"].endswith("pi-codex-chiap08-live.json")
    assert conflict_record["sha256"]
    assert conflict_record["first_seen"]
    mismatch_record = next(r for r in report["records"] if r["filename"] == mismatch.name)
    assert mismatch_record["disposition"] == "conflict-copy"
    assert mismatch_record["canonical_path"].endswith("pi-codex-chiap08-bad.json")
    assert mismatch_record["sha256"]
    assert report["source_mutated"] is False
    assert before == {p: p.read_bytes() for p in tmp_path.iterdir()}
    repeated = module.build_report(tmp_path, now=now, stale_after=900)
    assert report == repeated
    assert before == {p: p.read_bytes() for p in tmp_path.iterdir()}


def test_stale_and_invalid_json_are_diagnostic_only(tmp_path: Path) -> None:
    module = load_module()
    stale = tmp_path / "pi-glm-chiap01-old.json"
    write(
        stale,
        {"agent": stale.stem, "current_task": "deadbeef", "last_seen": "2026-09-04T11:00:00Z"},
    )
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    report = module.build_report(
        tmp_path, now=datetime(2026, 9, 4, 12, tzinfo=timezone.utc), stale_after=900
    )
    assert report["counts"] == {"malformed": 1, "stale": 1}
    assert report["capacity"] == 1
    assert all(record["disposition"] in {"malformed", "stale"} for record in report["records"])
