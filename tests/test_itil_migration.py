"""Lossless, idempotent ITIL migration (docs section 8d).

Builds a legacy single-file ITIL tree in a tmp fixture (never real data),
runs scripts/itil_migrate_events.py, and asserts:
  - every legacy timeline entry (ts, agent, note) is represented after folding,
  - final status is preserved,
  - a duplicate incident cluster collapses to one canonical record + redirect,
  - a sync-conflict file's distinct entry is recovered,
  - a second run changes zero bytes (checksum no-op).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from skcapstone.itil import Change, Incident, ITILManager, Problem

_MIG_PATH = Path(__file__).resolve().parent.parent / "scripts" / "itil_migrate_events.py"


def _load_migration_module():
    spec = importlib.util.spec_from_file_location("itil_migrate_events", _MIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _legacy_write(directory: Path, filename: str, data: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _tree_checksum(root: Path) -> str:
    """Stable checksum of every file under *root* except migration.state.json."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != "migration.state.json":
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


@pytest.fixture()
def legacy_tree(tmp_path: Path) -> Path:
    """Create a legacy ITIL tree and return the shared root."""
    itil = tmp_path / "coordination" / "itil"
    inc_dir = itil / "incidents"
    prb_dir = itil / "problems"
    chg_dir = itil / "changes"

    # A rich auto-detected incident (resolved), with 'still down' churn.
    inc1 = {
        "id": "inc-aaaa1111",
        "type": "incident",
        "title": "skvector (Qdrant) down",
        "severity": "sev3",
        "status": "resolved",
        "source": "service_health",
        "affected_services": ["skvector (Qdrant)"],
        "impact": "unreachable",
        "managed_by": "lumina",
        "created_by": "service_health",
        "detected_at": "2026-07-10T10:00:00+00:00",
        "acknowledged_at": "2026-07-10T10:05:00+00:00",
        "resolved_at": "2026-07-10T12:00:00+00:00",
        "resolution_summary": "came back",
        "timeline": [
            {
                "ts": "2026-07-10T10:00:00+00:00",
                "agent": "service_health",
                "action": "created",
                "note": "Incident detected: skvector (Qdrant) down",
            },
            {
                "ts": "2026-07-10T10:30:00+00:00",
                "agent": "lumina",
                "action": "note",
                "note": "still down",
            },
            {
                "ts": "2026-07-10T10:35:00+00:00",
                "agent": "lumina",
                "action": "note",
                "note": "still down (again)",
            },
            {
                "ts": "2026-07-10T10:40:00+00:00",
                "agent": "lumina",
                "action": "status:detected->acknowledged",
                "note": "ack",
            },
            {
                "ts": "2026-07-10T12:00:00+00:00",
                "agent": "lumina",
                "action": "status:acknowledged->resolved",
                "note": "fixed",
            },
        ],
        "tags": ["auto-detected", "service-health"],
        "gtd_item_ids": [],
    }
    _legacy_write(inc_dir, "inc-aaaa1111-skvector-qdrant-down.json", inc1)

    # A sync-conflict sibling for inc1 carrying one distinct extra note.
    conflict = dict(inc1)
    conflict["timeline"] = inc1["timeline"] + [
        {
            "ts": "2026-07-10T11:00:00+00:00",
            "agent": "fw41",
            "action": "note",
            "note": "distinct-conflict-note",
        },
    ]
    _legacy_write(
        inc_dir,
        "inc-aaaa1111-skvector-qdrant-down.json.sync-conflict-20260710-120000-ABCDEF",
        conflict,
    )

    # Duplicate cluster: two manual incidents, same service + title.
    inc2 = {
        "id": "inc-bbbb2222",
        "type": "incident",
        "title": "skchat daemon down",
        "severity": "sev2",
        "status": "acknowledged",
        "source": "manual",
        "affected_services": ["skchat"],
        "impact": "down",
        "managed_by": "opus",
        "created_by": "opus",
        "detected_at": "2026-07-11T08:00:00+00:00",
        "acknowledged_at": "2026-07-11T08:10:00+00:00",
        "timeline": [
            {
                "ts": "2026-07-11T08:00:00+00:00",
                "agent": "opus",
                "action": "created",
                "note": "Incident detected: skchat daemon down",
            },
            {
                "ts": "2026-07-11T08:10:00+00:00",
                "agent": "opus",
                "action": "status:detected->acknowledged",
                "note": "",
            },
        ],
        "tags": [],
        "gtd_item_ids": [],
    }
    inc3 = {
        "id": "inc-cccc3333",
        "type": "incident",
        "title": "skchat daemon down",
        "severity": "sev2",
        "status": "detected",
        "source": "manual",
        "affected_services": ["skchat"],
        "impact": "down",
        "managed_by": "jarvis",
        "created_by": "jarvis",
        "detected_at": "2026-07-11T09:00:00+00:00",
        "timeline": [
            {
                "ts": "2026-07-11T09:00:00+00:00",
                "agent": "jarvis",
                "action": "created",
                "note": "Incident detected: skchat daemon down",
            },
        ],
        "tags": [],
        "gtd_item_ids": [],
    }
    _legacy_write(inc_dir, "inc-bbbb2222-skchat-daemon-down.json", inc2)
    _legacy_write(inc_dir, "inc-cccc3333-skchat-daemon-down.json", inc3)

    # A problem and a change.
    prb1 = {
        "id": "prb-dddd4444",
        "type": "problem",
        "title": "flaky qdrant",
        "status": "analyzing",
        "root_cause": None,
        "workaround": "restart",
        "managed_by": "lumina",
        "created_by": "lumina",
        "created_at": "2026-07-10T13:00:00+00:00",
        "related_incident_ids": ["inc-aaaa1111"],
        "timeline": [
            {
                "ts": "2026-07-10T13:00:00+00:00",
                "agent": "lumina",
                "action": "created",
                "note": "Problem identified: flaky qdrant",
            },
            {
                "ts": "2026-07-10T13:30:00+00:00",
                "agent": "lumina",
                "action": "status:identified->analyzing",
                "note": "digging",
            },
        ],
        "tags": [],
        "gtd_item_ids": [],
    }
    chg1 = {
        "id": "chg-eeee5555",
        "type": "change",
        "title": "bump qdrant",
        "change_type": "normal",
        "status": "approved",
        "risk": "medium",
        "rollback_plan": "revert",
        "test_plan": "smoke",
        "managed_by": "opus",
        "created_by": "opus",
        "implementer": "lumina",
        "cab_required": True,
        "created_at": "2026-07-11T10:00:00+00:00",
        "timeline": [
            {
                "ts": "2026-07-11T10:00:00+00:00",
                "agent": "opus",
                "action": "proposed",
                "note": "RFC: bump qdrant",
            },
            {
                "ts": "2026-07-11T11:00:00+00:00",
                "agent": "cab-system",
                "action": "status:proposed->approved",
                "note": "Approved by: human",
            },
        ],
        "tags": [],
        "gtd_item_ids": [],
    }
    _legacy_write(prb_dir, "prb-dddd4444-flaky-qdrant.json", prb1)
    _legacy_write(chg_dir, "chg-eeee5555-bump-qdrant.json", chg1)

    return tmp_path


def _all_legacy_entries(*records: dict) -> list[tuple]:
    entries = []
    for rec in records:
        for e in rec.get("timeline", []):
            entries.append((e["ts"], e["agent"], e.get("note", "")))
    return entries


def test_migration_is_lossless_and_idempotent(legacy_tree: Path):
    mod = _load_migration_module()
    root = legacy_tree
    itil_dir = root / "coordination" / "itil"

    summary = mod.migrate(root, dry_run=False)
    assert summary["exploded"] == 5
    assert summary["merged_redirects"] == 1  # inc2/inc3 cluster -> 1 redirect

    mgr = ITILManager(root)

    # (d) cluster collapse: 3 legacy incidents -> 2 folded records.
    incidents = mgr.list_incidents()
    assert len(incidents) == 2

    # Final status preserved for the surviving records.
    by_service = {tuple(i.affected_services): i for i in incidents}
    skvector = by_service[("skvector (Qdrant)",)]
    skchat = by_service[("skchat",)]
    assert skvector.status.value == "resolved"
    # Merged skchat cluster keeps the most-advanced status (acknowledged).
    assert skchat.status.value == "acknowledged"

    # Sync-conflict distinct entry recovered into the folded timeline.
    notes = {r["note"] for r in skvector.timeline}
    assert "distinct-conflict-note" in notes
    assert "still down" in notes
    assert "still down (again)" in notes

    # Redirect: the losing duplicate id resolves to the canonical record.
    canonical_id = skchat.id
    redirected = mgr._fold_record(mgr.incidents_dir, "inc-cccc3333", Incident)
    assert redirected is not None and redirected.id == canonical_id

    # Problem + change migrated with status preserved.
    problems = mgr.list_problems()
    changes = mgr.list_changes()
    assert len(problems) == 1 and problems[0].status.value == "analyzing"
    assert len(changes) == 1 and changes[0].status.value == "approved"

    # Every legacy timeline entry (ts, agent, note) is represented after fold.
    folded_entries = set()
    for rec in incidents + problems + changes:
        for r in rec.timeline:
            folded_entries.add((r["ts"], r["agent"], r.get("note", "")))
    # Reconstruct the exact legacy entries we wrote and assert superset.
    legacy_inc = json.loads(
        (itil_dir / "_legacy" / "incidents" / "inc-aaaa1111-skvector-qdrant-down.json").read_text()
    )
    for ts, agent, note in _all_legacy_entries(legacy_inc):
        assert (ts, agent, note) in folded_entries, (ts, agent, note)

    # (d) re-run changes zero bytes (checksum no-op, ignoring migration.state.json).
    before = _tree_checksum(itil_dir)
    mod.migrate(root, dry_run=False)
    after = _tree_checksum(itil_dir)
    assert before == after


def test_migration_dry_run_writes_nothing(legacy_tree: Path):
    mod = _load_migration_module()
    root = legacy_tree
    itil_dir = root / "coordination" / "itil"
    before = _tree_checksum(itil_dir)
    mod.migrate(root, dry_run=True)
    after = _tree_checksum(itil_dir)
    assert before == after
    # No new record directories were created.
    assert not (itil_dir / "incidents" / "inc-aaaa1111").exists()
