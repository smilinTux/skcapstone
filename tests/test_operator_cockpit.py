from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from skdashboard.dashboard_operator import get_operator_cockpit

NOW = datetime(2026, 8, 20, 21, 0, tzinfo=timezone.utc)


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_missing_evidence_fails_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "fleet"))
    result = get_operator_cockpit(tmp_path, now=NOW)
    assert result["freeze"]["status"] == "frozen"
    assert result["conditions"] == []
    assert result["watchdog"]["available"] is False
    assert result["cmdb"]["available"] is False
    assert result["skbrain"]["available"] is False


def test_projects_conditions_controls_and_action_lifecycle(tmp_path: Path, monkeypatch) -> None:
    fleet, atlas = tmp_path / "fleet", tmp_path / "fleet" / "atlas"
    monkeypatch.setenv("SKFLEET_ROOT", str(fleet))
    _write(fleet / "objects" / "_freeze.json", {"frozen": False})
    _write(atlas / "brief" / "brief.json", {"generated_at": "2026-08-20T20:58:00+00:00", "conditions": [{"type": "CmdbAuditClean", "status": True, "subject": "fleet", "provenance": "artifact:abc"}]})
    _write(atlas / "state" / "execution-state.json", {"version": 1, "actions": {"abc": {"last_attempt": 4, "circuit_open": True, "consecutive_failures": 3}}})
    intent = "ai-aaaaaaaaaaaaaaaaaaaaaaaa"
    _write(atlas / "action-ledger" / "intents" / f"{intent}.json", {"intent_id": intent, "application": "cmdb", "target_kind": "service", "target_id": "cmdb-reconcile", "action": "apply", "itil_change_id": "chg-1", "verification": {"condition": "CmdbAuditClean"}, "rollback": {}})
    event = atlas / "action-ledger" / "events" / f"{intent}.jsonl"
    event.parent.mkdir(parents=True)
    event.write_text(json.dumps({"state": "verified", "occurred_at": NOW.isoformat()}) + "\n")
    result = get_operator_cockpit(tmp_path, now=NOW)
    assert result["freeze"]["status"] == "active"
    assert result["conditions"][0]["age_seconds"] == 120
    assert result["execution_controls"][0]["circuit_open"] is True
    assert result["actions"][0]["state"] == "verified"
    assert result["actions"][0]["change_id"] == "chg-1"


def test_malformed_action_event_is_reported_not_rendered(tmp_path: Path, monkeypatch) -> None:
    fleet, atlas = tmp_path / "fleet", tmp_path / "fleet" / "atlas"
    monkeypatch.setenv("SKFLEET_ROOT", str(fleet))
    intent = "ai-bbbbbbbbbbbbbbbbbbbbbbbb"
    _write(atlas / "action-ledger" / "intents" / f"{intent}.json", {"intent_id": intent})
    event = atlas / "action-ledger" / "events" / f"{intent}.jsonl"
    event.parent.mkdir(parents=True)
    event.write_text("not-json\n", encoding="utf-8")
    result = get_operator_cockpit(tmp_path, now=NOW)
    assert result["actions"] == []
    assert result["ledger_errors"] and intent in result["ledger_errors"][0]
