"""Telegram notify: escalation cards and report, with an injectable sender."""

from __future__ import annotations

from skcapstone.operator_seat import notify


def test_format_escalation_lists_options_and_decide_commands():
    d = {
        "id": "abc123",
        "options": [
            {"action": "restart_service", "object": "web", "rationale": "Ready False"},
            {"action": "replace_workload", "object": "web", "rationale": "move it"},
        ],
    }
    text = notify.format_escalation(d)
    assert "abc123" in text
    assert "[0] restart_service on web" in text
    assert "[1] replace_workload on web" in text
    assert "skoperator decide abc123 --approve" in text
    assert "skoperator decide abc123 --reject" in text
    assert "—" not in text and "–" not in text  # no em/en dashes


def test_notify_report_calls_sender():
    sent = []
    ok = notify.notify_report("all quiet", sender=lambda t: sent.append(t) or True)
    assert ok is True
    assert sent and sent[0].startswith("Atlas report:")


def test_notify_escalation_calls_sender():
    sent = []
    notify.notify_escalation(
        {"id": "d1", "options": [{"action": "delete_object"}]},
        sender=lambda t: sent.append(t) or True,
    )
    assert sent and "d1" in sent[0]
