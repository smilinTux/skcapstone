"""Tests for the O1a change-class / risk policy classifier (pure)."""

from __future__ import annotations

from skcapstone.operator_seat.policy import classify_change


def test_ratified_standard_action_is_auto_approvable():
    action = {
        "name": "restart_service",
        "standard": True,
        "blast_radius": "single_service",
        "risk": "low",
    }
    result = classify_change(action)
    assert result == {"change_class": "standard", "risk": "low", "auto_approvable": True}


def test_irreversible_blast_radius_forces_major():
    action = {
        "name": "drop_node",
        "blast_radius": "delete",
        "risk": "low",
        "rollback_plan": "restore from backup",
        "author": "operator",
    }
    result = classify_change(action)
    assert result["change_class"] == "major"
    assert result["auto_approvable"] is False


def test_high_risk_forces_major_not_auto():
    action = {
        "name": "rotate_credential",
        "blast_radius": "single_service",
        "risk": "high",
        "rollback_plan": "revert credential",
        "author": "operator",
    }
    result = classify_change(action)
    assert result["change_class"] == "major"
    assert result["risk"] == "high"
    assert result["auto_approvable"] is False


def test_freeze_action_is_emergency():
    action = {
        "name": "freeze_fleet",
        "freeze": True,
        "risk": "medium",
    }
    result = classify_change(action)
    assert result["change_class"] == "emergency"
    assert result["auto_approvable"] is False


def test_normal_auto_approvable_with_rollback_and_operator_author():
    action = {
        "name": "scale_replica",
        "blast_radius": "single_service",
        "risk": "low",
        "rollback_plan": "scale back down",
        "author": "operator",
    }
    result = classify_change(action)
    assert result == {"change_class": "normal", "risk": "low", "auto_approvable": True}


def test_normal_not_auto_approvable_without_rollback_plan():
    action = {
        "name": "scale_replica",
        "blast_radius": "single_service",
        "risk": "low",
        "author": "operator",
    }
    result = classify_change(action)
    assert result["change_class"] == "normal"
    assert result["auto_approvable"] is False


def test_standard_claim_cannot_bypass_irreversible_major():
    action = {
        "name": "rotate_credential",
        "standard": True,
        "blast_radius": "delete",
        "risk": "low",
    }
    result = classify_change(action)
    assert result["change_class"] == "major"
    assert result["auto_approvable"] is False


def test_standard_claim_cannot_bypass_high_risk_major():
    action = {
        "name": "rotate_credential",
        "standard": True,
        "blast_radius": "single_service",
        "risk": "high",
    }
    result = classify_change(action)
    assert result["change_class"] == "major"
    assert result["auto_approvable"] is False


def test_normal_not_auto_approvable_when_author_is_not_operator():
    action = {
        "name": "scale_replica",
        "blast_radius": "single_service",
        "risk": "low",
        "rollback_plan": "scale back down",
        "author": "human",
    }
    result = classify_change(action)
    assert result["change_class"] == "normal"
    assert result["auto_approvable"] is False
