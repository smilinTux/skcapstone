"""Tests for headless out-of-band alerting (daemon-down / agent-dark).

The desktop transports in :mod:`skcapstone.notifications` are useless on a
headless server or a laptop with no login session. These tests cover the
headless transport (webhook + sk-alert) and the daemon watchdog wiring that
fires it when a component goes dark or the watchdog gives up.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.notifications import (
    NotificationManager,
    headless_alerting_enabled,
    notify_headless,
)


@pytest.fixture(autouse=True)
def _clear_headless_env(monkeypatch):
    """Ensure headless alerting starts disabled for each test (opt-in)."""
    monkeypatch.delenv("SKCAPSTONE_ALERT_WEBHOOK", raising=False)
    monkeypatch.delenv("SKCAPSTONE_HEADLESS_ALERT", raising=False)


def _mgr() -> NotificationManager:
    """Fresh manager with a live dedup window so dedup tests are deterministic."""
    return NotificationManager(dedup_ttl=120.0)


# ---------------------------------------------------------------------------
# Enable gate
# ---------------------------------------------------------------------------


class TestEnableGate:
    def test_disabled_by_default(self):
        """No channel configured → headless alerting reports disabled."""
        assert headless_alerting_enabled() is False

    def test_enabled_by_webhook(self, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_ALERT_WEBHOOK", "https://hook.example/alert")
        assert headless_alerting_enabled() is True

    def test_enabled_by_sk_alert_flag(self, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_HEADLESS_ALERT", "1")
        assert headless_alerting_enabled() is True

    def test_disabled_value_does_not_enable(self, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_HEADLESS_ALERT", "off")
        assert headless_alerting_enabled() is False


# ---------------------------------------------------------------------------
# Disabled-by-default no-op
# ---------------------------------------------------------------------------


class TestDisabledNoop:
    def test_notify_headless_noops_when_unconfigured(self):
        """With no channel set, notify_headless returns False and touches nothing."""
        mgr = _mgr()
        with (
            patch.object(mgr, "_post_webhook") as mock_hook,
            patch.object(mgr, "_send_sk_alert") as mock_alert,
        ):
            result = mgr.notify_headless("agent-dark: poll", "no heartbeat")

        assert result is False
        mock_hook.assert_not_called()
        mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# Webhook channel
# ---------------------------------------------------------------------------


class TestWebhookChannel:
    def test_webhook_fires_when_configured(self, monkeypatch):
        """A configured webhook receives a JSON POST and the alert dispatches."""
        monkeypatch.setenv("SKCAPSTONE_ALERT_WEBHOOK", "https://hook.example/alert")
        mgr = _mgr()

        resp = MagicMock()
        resp.status = 200
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value = resp
            result = mgr.notify_headless("daemon-down: poll", "gave up", urgency="critical")

        assert result is True
        assert mock_urlopen.call_count == 1
        req = mock_urlopen.call_args.args[0]
        assert req.full_url == "https://hook.example/alert"
        assert req.method == "POST"
        import json

        payload = json.loads(req.data.decode("utf-8"))
        assert payload["title"] == "daemon-down: poll"
        assert payload["level"] == "error"  # critical → error
        assert payload["source"] == "skcapstone.notifications"

    def test_webhook_non_2xx_is_not_dispatched(self, monkeypatch):
        """A non-2xx webhook response counts as a failed dispatch (False)."""
        monkeypatch.setenv("SKCAPSTONE_ALERT_WEBHOOK", "https://hook.example/alert")
        mgr = _mgr()

        resp = MagicMock()
        resp.status = 500
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value = resp
            result = mgr.notify_headless("agent-dark: poll", "no heartbeat")

        assert result is False


# ---------------------------------------------------------------------------
# sk-alert channel
# ---------------------------------------------------------------------------


class TestSkAlertChannel:
    def test_sk_alert_fires_when_enabled(self, monkeypatch):
        """With the flag on and sk-alert present, it is invoked with the message."""
        monkeypatch.setenv("SKCAPSTONE_HEADLESS_ALERT", "1")
        mgr = _mgr()

        with (
            patch("skcapstone.notifications._resolve_sk_alert", return_value="/usr/bin/sk-alert"),
            patch("skcapstone.notifications.subprocess.run") as mock_run,
        ):
            result = mgr.notify_headless("agent-dark: poll", "no heartbeat", urgency="critical")

        assert result is True
        args = mock_run.call_args.args[0]
        assert args[0] == "/usr/bin/sk-alert"
        assert args[1:3] == ["-l", "error"]
        assert "agent-dark: poll" in args[3]

    def test_sk_alert_missing_binary_noops(self, monkeypatch):
        """Flag on but sk-alert absent → that channel does not dispatch."""
        monkeypatch.setenv("SKCAPSTONE_HEADLESS_ALERT", "1")
        mgr = _mgr()

        with (
            patch("skcapstone.notifications._resolve_sk_alert", return_value=None),
            patch("skcapstone.notifications.subprocess.run") as mock_run,
        ):
            result = mgr.notify_headless("agent-dark: poll", "no heartbeat")

        assert result is False
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Fail-safe
# ---------------------------------------------------------------------------


class TestFailSafe:
    def test_webhook_transport_error_swallowed(self, monkeypatch):
        """A raising transport never propagates; returns False."""
        monkeypatch.setenv("SKCAPSTONE_ALERT_WEBHOOK", "https://hook.example/alert")
        mgr = _mgr()

        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            result = mgr.notify_headless("agent-dark: poll", "no heartbeat")

        assert result is False  # no exception raised

    def test_sk_alert_subprocess_error_swallowed(self, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_HEADLESS_ALERT", "1")
        mgr = _mgr()

        with (
            patch("skcapstone.notifications._resolve_sk_alert", return_value="/usr/bin/sk-alert"),
            patch("skcapstone.notifications.subprocess.run", side_effect=OSError("boom")),
        ):
            result = mgr.notify_headless("agent-dark: poll", "no heartbeat")

        assert result is False


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


class TestDedup:
    def test_identical_alert_deduplicated(self, monkeypatch):
        """A repeated alert within the TTL is suppressed (one webhook POST)."""
        monkeypatch.setenv("SKCAPSTONE_ALERT_WEBHOOK", "https://hook.example/alert")
        mgr = _mgr()

        resp = MagicMock()
        resp.status = 200
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value = resp
            first = mgr.notify_headless(
                "agent-dark: poll", "no heartbeat", dedup_key="agent-dark:poll"
            )
            second = mgr.notify_headless(
                "agent-dark: poll", "no heartbeat", dedup_key="agent-dark:poll"
            )

        assert first is True
        assert second is False
        assert mock_urlopen.call_count == 1

    def test_headless_key_independent_of_desktop(self, monkeypatch):
        """A headless dedup entry does not suppress a desktop notify (namespaced)."""
        monkeypatch.setenv("SKCAPSTONE_ALERT_WEBHOOK", "https://hook.example/alert")
        monkeypatch.setenv("SKCAPSTONE_DESKTOP_NOTIFY", "1")
        mgr = NotificationManager(debounce_seconds=0.0, dedup_ttl=120.0)

        resp = MagicMock()
        resp.status = 200
        with (
            patch("urllib.request.urlopen") as mock_urlopen,
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch.object(mgr, "_notify_linux_gi", return_value=False),
            patch("skcapstone.notifications.subprocess.run", return_value=MagicMock(returncode=0)),
        ):
            mock_urlopen.return_value.__enter__.return_value = resp
            hl = mgr.notify_headless("same", "same", urgency="normal")
            desk = mgr.notify("same", "same", urgency="normal")

        assert hl is True
        assert desk is True  # not swallowed by the headless dedup entry


# ---------------------------------------------------------------------------
# Module-level wrapper
# ---------------------------------------------------------------------------


def test_module_wrapper_delegates_to_singleton(monkeypatch):
    """notify_headless() delegates to the singleton manager."""
    fake = MagicMock()
    fake.notify_headless.return_value = True
    with patch("skcapstone.notifications.get_manager", return_value=fake):
        assert notify_headless("t", "b") is True
    fake.notify_headless.assert_called_once()


# ---------------------------------------------------------------------------
# Daemon watchdog wiring (agent-dark / daemon-down)
# ---------------------------------------------------------------------------


class TestDaemonWiring:
    def _manager(self):
        from skcapstone.daemon import ComponentManager

        return ComponentManager(threading.Event())

    def test_heartbeat_timeout_fires_agent_dark_alert(self):
        """A component past its heartbeat timeout fires an agent-dark alert."""
        mgr = self._manager()
        comp = mgr.register("poll", target=lambda: None, heartbeat_timeout=120)
        comp.status = "alive"
        comp.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=300)
        mgr._launch = MagicMock()  # do not spin a real restart thread

        with patch("skcapstone.notifications.notify_headless") as mock_alert:
            mgr._check_components()

        mock_alert.assert_called_once()
        kwargs = mock_alert.call_args.kwargs
        args = mock_alert.call_args.args
        assert args[0].startswith("agent-dark: poll")
        assert kwargs["dedup_key"] == "agent-dark:poll"

    def test_gave_up_fires_daemon_down_alert(self):
        """A dead component out of restart budget fires a daemon-down alert."""
        from skcapstone.daemon import ComponentManager

        mgr = self._manager()
        comp = mgr.register("poll", target=lambda: None)
        comp.status = "dead"
        comp.restart_times = [datetime.now(timezone.utc)] * ComponentManager.MAX_RESTARTS

        with patch("skcapstone.notifications.notify_headless") as mock_alert:
            mgr._check_components()

        mock_alert.assert_called_once()
        args = mock_alert.call_args.args
        kwargs = mock_alert.call_args.kwargs
        assert args[0].startswith("daemon-down: poll")
        assert kwargs["dedup_key"] == "daemon-down:poll"

    def test_healthy_component_fires_no_alert(self):
        """A healthy component with a fresh heartbeat triggers no alert."""
        mgr = self._manager()
        comp = mgr.register("poll", target=lambda: None, heartbeat_timeout=120)
        comp.status = "alive"
        comp.last_heartbeat = datetime.now(timezone.utc)

        with patch("skcapstone.notifications.notify_headless") as mock_alert:
            mgr._check_components()

        mock_alert.assert_not_called()

    def test_alert_failure_does_not_break_watchdog(self):
        """If the headless alert raises, the watchdog pass still completes."""
        from skcapstone.daemon import ComponentManager

        mgr = self._manager()
        comp = mgr.register("poll", target=lambda: None)
        comp.status = "dead"
        comp.restart_times = [datetime.now(timezone.utc)] * ComponentManager.MAX_RESTARTS

        with patch(
            "skcapstone.notifications.notify_headless",
            side_effect=RuntimeError("alert channel down"),
        ):
            # Must not raise despite the alert blowing up.
            mgr._check_components()
