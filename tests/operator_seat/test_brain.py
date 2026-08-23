"""Tests for the O4b hybrid-brain router and report formatter (pure)."""

from __future__ import annotations

from skcapstone.operator_seat.brain import format_report, route_brain


def _condition(app: str, type_: str, status: str) -> dict:
    return {"app": app, "type": type_, "status": status}


def test_quiet_brief_routes_to_ornith():
    brief = {"quiet": True, "conditions": []}
    assert route_brain(brief) == "ornith"


def test_firing_brief_routes_to_claude():
    brief = {
        "quiet": False,
        "conditions": [_condition("payments", "cpu", "True")],
    }
    assert route_brain(brief) == "claude"


def test_stale_only_brief_routes_to_claude():
    brief = {
        "quiet": False,
        "conditions": [_condition("payments", "cpu", "Unknown")],
    }
    assert route_brain(brief) == "claude"


def test_report_lists_firing_conditions_and_proposals():
    brief = {
        "quiet": False,
        "conditions": [
            _condition("payments", "cpu", "True"),
            _condition("payments", "disk", "False"),
        ],
    }
    proposals = [
        {
            "change_class": "standard",
            "action": "restart_service",
            "rationale": "cpu condition is firing on payments",
        }
    ]
    report = format_report(brief, proposals)
    assert "payments: cpu=True" in report
    assert "payments: disk=True" not in report
    assert "standard" in report
    assert "restart_service" in report
    assert "cpu condition is firing on payments" in report


def test_quiet_with_no_proposals_reports_all_quiet():
    brief = {"quiet": True, "conditions": []}
    assert format_report(brief, []) == "all quiet, no action"


def test_report_contains_no_em_or_en_dashes():
    brief = {
        "quiet": False,
        "conditions": [_condition("payments", "cpu", "True")],
    }
    proposals = [
        {
            "change_class": "normal",
            "action": "scale_replica",
            "rationale": "sustained cpu pressure, needs headroom",
        }
    ]
    report = format_report(brief, proposals)
    assert "—" not in report
    assert "–" not in report
