"""Tests for ``skcapstone alerts`` command.

Covers:
- help text exposes all options
- --once with no messages prints "No alerts found"
- --once with matching messages prints formatted panels
- messages outside subscribed topics are silently skipped
- --notify flag triggers NotificationManager.notify()
- --topic adds an extra subscription
- helper: _style_for_topic
- helper: _format_payload
- helper: _make_alert_panel
- helper: _resolve_home (agent vs home)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from skcapstone.cli import main
from skcapstone.cli.alerts import (
    DEFAULT_TOPICS,
    _format_payload,
    _make_alert_panel,
    _resolve_home,
    _style_for_topic,
)
from skcapstone.pubsub import PubSub, TopicMessage

runner = CliRunner()


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    """Minimal agent home with an initialised pubsub bus."""
    bus = PubSub(tmp_path, agent_name="test-agent")
    bus.initialize()
    return tmp_path


def _make_msg(topic: str, payload: dict | None = None, sender: str = "jarvis") -> TopicMessage:
    """Build a TopicMessage fixture for a given topic."""
    return TopicMessage(
        topic=topic,
        sender=sender,
        payload=payload if payload is not None else {"detail": "something went wrong"},
    )


# ---------------------------------------------------------------------------
# Unit: _style_for_topic
# ---------------------------------------------------------------------------


class TestStyleForTopic:
    def test_agent_critical_is_bold_red(self):
        assert _style_for_topic("agent.critical") == "bold red"

    def test_coord_task_failed_is_red(self):
        assert _style_for_topic("coord.task_failed") == "red"

    def test_consciousness_error_is_bold_magenta(self):
        assert _style_for_topic("consciousness.error") == "bold magenta"

    def test_pillar_degraded_is_yellow(self):
        assert _style_for_topic("pillar.degraded") == "yellow"

    def test_unknown_topic_returns_dim(self):
        assert _style_for_topic("some.unknown.topic") == "dim"


# ---------------------------------------------------------------------------
# Unit: _format_payload
# ---------------------------------------------------------------------------


class TestFormatPayload:
    def test_produces_valid_json(self):
        result = _format_payload({"key": "value", "n": 42})
        data = json.loads(result)
        assert data["key"] == "value"
        assert data["n"] == 42

    def test_pretty_printed_with_indent(self):
        result = _format_payload({"a": 1})
        assert "\n" in result  # multi-line == indented

    def test_empty_payload(self):
        result = _format_payload({})
        assert result == "{}"

    def test_non_serialisable_uses_str(self):
        class Obj:
            def __str__(self):
                return "my-obj"

        result = _format_payload({"x": Obj()})
        assert "my-obj" in result


# ---------------------------------------------------------------------------
# Unit: _make_alert_panel
# ---------------------------------------------------------------------------


class TestMakeAlertPanel:
    def test_returns_panel(self):
        from rich.panel import Panel

        msg = _make_msg("agent.critical")
        panel = _make_alert_panel(msg)
        assert isinstance(panel, Panel)

    def test_panel_contains_sender(self):
        msg = _make_msg("agent.critical", sender="opus")
        panel = _make_alert_panel(msg)
        rendered = str(panel.renderable)
        assert "opus" in rendered

    def test_panel_contains_message_id(self):
        msg = _make_msg("coord.task_failed")
        panel = _make_alert_panel(msg)
        assert msg.message_id in str(panel.renderable)

    def test_panel_omits_payload_section_when_empty(self):
        msg = _make_msg("pillar.degraded", payload={})
        panel = _make_alert_panel(msg)
        # "payload:" label must not appear when dict is empty
        assert "payload" not in str(panel.renderable)


# ---------------------------------------------------------------------------
# Unit: _resolve_home
# ---------------------------------------------------------------------------


class TestResolveHome:
    def test_uses_home_when_agent_none(self, tmp_path):
        result = _resolve_home(None, str(tmp_path))
        assert result == tmp_path

    def test_uses_agent_subdir_when_agent_given(self, tmp_path):
        with patch("skcapstone.cli.alerts.SKCAPSTONE_ROOT", str(tmp_path)):
            result = _resolve_home("opus", str(tmp_path / "ignored"))
        assert result == (tmp_path / "agents" / "opus").expanduser()


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestAlertsCLI:
    def test_help_shows_all_options(self):
        result = runner.invoke(main, ["alerts", "--help"])
        assert result.exit_code == 0
        assert "--notify" in result.output
        assert "--once" in result.output
        assert "--interval" in result.output
        assert "--topic" in result.output

    def test_once_no_messages_prints_no_alerts(self, agent_home):
        result = runner.invoke(
            main, ["alerts", "--home", str(agent_home), "--once"]
        )
        assert result.exit_code == 0
        assert "No alerts found" in result.output

    def test_once_with_published_message_shows_panel(self, agent_home):
        bus = PubSub(agent_home, agent_name="test-agent")
        bus.publish("agent.critical", {"reason": "disk full"})

        result = runner.invoke(
            main, ["alerts", "--home", str(agent_home), "--once"]
        )
        assert result.exit_code == 0
        assert "agent.critical" in result.output
        assert "disk full" in result.output

    def test_once_multiple_topics_shown(self, agent_home):
        bus = PubSub(agent_home, agent_name="test-agent")
        bus.publish("coord.task_failed", {"task": "abc"})
        bus.publish("pillar.degraded", {"pillar": "memory"})

        result = runner.invoke(
            main, ["alerts", "--home", str(agent_home), "--once"]
        )
        assert result.exit_code == 0
        assert "coord.task_failed" in result.output
        assert "pillar.degraded" in result.output

    def test_messages_outside_default_topics_not_shown(self, agent_home):
        bus = PubSub(agent_home, agent_name="test-agent")
        bus.publish("unrelated.topic", {"noise": True})
        # also publish a real alert so we know the command ran
        bus.publish("agent.critical", {"signal": True})

        result = runner.invoke(
            main, ["alerts", "--home", str(agent_home), "--once"]
        )
        assert result.exit_code == 0
        assert "unrelated.topic" not in result.output
        assert "agent.critical" in result.output

    def test_extra_topic_via_option(self, agent_home):
        bus = PubSub(agent_home, agent_name="test-agent")
        bus.publish("custom.event", {"x": 1})

        result = runner.invoke(
            main,
            ["alerts", "--home", str(agent_home), "--once", "--topic", "custom.event"],
        )
        assert result.exit_code == 0
        assert "custom.event" in result.output

    def test_notify_flag_calls_notifier(self, agent_home):
        bus = PubSub(agent_home, agent_name="test-agent")
        bus.publish("agent.critical", {"msg": "test notification"})

        mock_notifier = MagicMock()
        mock_notifier.notify.return_value = True

        with patch(
            "skcapstone.cli.alerts.NotificationManager",
            return_value=mock_notifier,
        ):
            result = runner.invoke(
                main,
                ["alerts", "--home", str(agent_home), "--once", "--notify"],
            )

        assert result.exit_code == 0
        mock_notifier.notify.assert_called_once()
        args, kwargs = mock_notifier.notify.call_args
        title = kwargs.get("title", args[0] if args else "")
        assert "AGENT.CRITICAL" in title

    def test_notify_unavailable_prints_warning(self, agent_home):
        # Simulate instantiation failure (e.g. no display / no gi)
        with patch(
            "skcapstone.cli.alerts.NotificationManager",
            side_effect=Exception("no gi"),
        ):
            result = runner.invoke(
                main,
                ["alerts", "--home", str(agent_home), "--once", "--notify"],
            )
        assert result.exit_code == 0
        assert "unavailable" in result.output.lower()

    def test_notify_module_none_prints_warning(self, agent_home):
        """When NotificationManager is None (import failed), warn and continue."""
        with patch("skcapstone.cli.alerts.NotificationManager", None):
            result = runner.invoke(
                main,
                ["alerts", "--home", str(agent_home), "--once", "--notify"],
            )
        assert result.exit_code == 0
        assert "unavailable" in result.output.lower()

    def test_ctrl_c_exits_gracefully(self, agent_home):
        """KeyboardInterrupt during streaming prints a stop message and exits 0."""

        def fake_sleep(_n: float) -> None:
            raise KeyboardInterrupt()

        with patch("skcapstone.cli.alerts.time.sleep", fake_sleep):
            result = runner.invoke(
                main, ["alerts", "--home", str(agent_home)]
            )

        assert result.exit_code == 0
        assert "Stopped" in result.output

    def test_monitors_header_printed(self, agent_home):
        result = runner.invoke(
            main, ["alerts", "--home", str(agent_home), "--once"]
        )
        assert result.exit_code == 0
        assert "Monitoring" in result.output
