"""Tests for the O1b ITIL change-record intent builder (pure)."""

from __future__ import annotations

from skcapstone.operator_seat.itil_intent import build_change_record


def test_standard_change_payload():
    action = {"name": "restart_service", "standard": True, "blast_radius": "single_service"}
    classification = {"change_class": "standard", "risk": "low", "auto_approvable": True}
    result = build_change_record(action, classification, "true", "restart again")
    assert result == {
        "title": "Operator change: restart_service",
        "description": (
            "Operator-initiated standard change 'restart_service' " "(risk: low, dry_run: true)."
        ),
        "change_class": "standard",
        "risk": "low",
        "dry_run": "true",
        "rollback_plan": "restart again",
        "tags": ["operator"],
        "author": "operator",
        "requires_human": False,
    }


def test_normal_auto_approvable_gets_auto_normal_tag():
    action = {"name": "scale_replica"}
    classification = {"change_class": "normal", "risk": "low", "auto_approvable": True}
    result = build_change_record(action, classification, "false", "scale back down")
    assert result["tags"] == ["operator", "auto-normal"]
    assert result["requires_human"] is False


def test_normal_not_auto_approvable_omits_auto_normal_tag():
    action = {"name": "scale_replica"}
    classification = {"change_class": "normal", "risk": "low", "auto_approvable": False}
    result = build_change_record(action, classification, "false", None)
    assert result["tags"] == ["operator"]
    assert result["requires_human"] is True
    assert result["rollback_plan"] is None


def test_major_change_requires_human_and_no_auto_normal_tag():
    action = {"name": "drop_node", "blast_radius": "delete"}
    classification = {"change_class": "major", "risk": "high", "auto_approvable": False}
    result = build_change_record(action, classification, "true", "restore from backup")
    assert result["change_class"] == "major"
    assert result["tags"] == ["operator"]
    assert result["requires_human"] is True


def test_dry_run_and_rollback_plan_carried_through_unmodified():
    action = {"name": "rotate_log"}
    classification = {"change_class": "standard", "risk": "low", "auto_approvable": True}
    result = build_change_record(action, classification, "dry-run-mode-x", "some rollback text")
    assert result["dry_run"] == "dry-run-mode-x"
    assert result["rollback_plan"] == "some rollback text"
