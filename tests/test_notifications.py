"""Tests for desktop notification support."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.notifications import (
    NotificationManager,
    desktop_notifications_enabled,
    get_manager,
    notify,
)


@pytest.fixture(autouse=True)
def _enable_desktop_notifications(monkeypatch):
    """Re-enable the desktop-notification guard for this module.

    The session-wide conftest fixture disables notifications so test runs
    don't flood the live desktop.  Every test here mocks ``subprocess.run`` /
    ``osascript``, so nothing real is dispatched - they just need the guard
    on to exercise the dispatch logic.  Guard-specific tests override this.
    """
    monkeypatch.setenv("SKCAPSTONE_DESKTOP_NOTIFY", "1")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mgr(debounce: float = 0.0) -> NotificationManager:
    """Return a fresh NotificationManager with zero debounce by default."""
    return NotificationManager(debounce_seconds=debounce)


# ---------------------------------------------------------------------------
# notify-send (Linux)
# ---------------------------------------------------------------------------


class TestNotifyLinux:
    """Tests for Linux notify-send path."""

    def test_notify_send_called_with_correct_args(self):
        """notify-send is invoked with urgency, title, body."""
        mgr = _make_mgr()
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            result = mgr.notify("Hello", "World", urgency="normal")

        assert result is True
        mock_run.assert_called_once_with(
            ["notify-send", "--urgency", "normal", "Hello", "World"],
            check=True,
            capture_output=True,
            timeout=5,
        )

    def test_notify_send_urgency_low(self):
        """Low urgency maps to notify-send --urgency low."""
        mgr = _make_mgr()
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            mgr.notify("T", "B", urgency="low")

        args = mock_run.call_args[0][0]
        assert "--urgency" in args
        assert args[args.index("--urgency") + 1] == "low"

    def test_notify_send_urgency_critical(self):
        """Critical urgency maps to notify-send --urgency critical."""
        mgr = _make_mgr()
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            mgr.notify("T", "B", urgency="critical")

        args = mock_run.call_args[0][0]
        assert args[args.index("--urgency") + 1] == "critical"

    def test_notify_send_not_found_returns_false(self):
        """Returns False gracefully when notify-send binary is missing."""
        mgr = _make_mgr()
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run", side_effect=FileNotFoundError),
        ):
            result = mgr.notify("T", "B")

        assert result is False

    def test_notify_send_nonzero_exit_returns_false(self):
        """Returns False when notify-send exits non-zero."""
        import subprocess

        mgr = _make_mgr()
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch(
                "skcapstone.notifications.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "notify-send", stderr=b"err"),
            ),
        ):
            result = mgr.notify("T", "B")

        assert result is False

    def test_notify_send_timeout_returns_false(self):
        """Returns False when notify-send times out."""
        import subprocess

        mgr = _make_mgr()
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch(
                "skcapstone.notifications.subprocess.run",
                side_effect=subprocess.TimeoutExpired("notify-send", 5),
            ),
        ):
            result = mgr.notify("T", "B")

        assert result is False


# ---------------------------------------------------------------------------
# osascript (macOS)
# ---------------------------------------------------------------------------


class TestNotifyMacOS:
    """Tests for macOS osascript path."""

    def test_osascript_called(self):
        """osascript is invoked with a display notification command."""
        mgr = _make_mgr()
        mgr._system = "Darwin"
        with patch("skcapstone.notifications.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = mgr.notify("Hello", "World")

        assert result is True
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "osascript"
        # The script is passed as cmd[-1] (osascript -e <script>)
        assert "Hello" in cmd[-1]
        assert "World" in cmd[-1]

    def test_osascript_not_found_returns_false(self):
        """Returns False gracefully when osascript is missing."""
        mgr = _make_mgr()
        mgr._system = "Darwin"
        with patch("skcapstone.notifications.subprocess.run", side_effect=FileNotFoundError):
            result = mgr.notify("T", "B")

        assert result is False

    def test_osascript_escapes_double_quotes_in_title(self):
        """Double quotes in title are escaped to prevent osascript injection."""
        mgr = _make_mgr()
        mgr._system = "Darwin"
        with patch("skcapstone.notifications.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            mgr.notify('Say "hi"', "body")

        script = mock_run.call_args[0][0][-1]  # osascript -e <script>
        assert '\\"hi\\"' in script

    def test_osascript_escapes_double_quotes_in_body(self):
        """Double quotes in body are escaped."""
        mgr = _make_mgr()
        mgr._system = "Darwin"
        with patch("skcapstone.notifications.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            mgr.notify("title", 'body "here"')

        script = mock_run.call_args[0][0][-1]
        assert '\\"here\\"' in script


# ---------------------------------------------------------------------------
# Unsupported platform
# ---------------------------------------------------------------------------


class TestNotifyUnsupportedPlatform:
    """Windows and unknown platforms return False without error."""

    def test_windows_returns_false(self):
        mgr = _make_mgr()
        mgr._system = "Windows"
        with patch("skcapstone.notifications.subprocess.run") as mock_run:
            result = mgr.notify("T", "B")

        assert result is False
        mock_run.assert_not_called()

    def test_unknown_platform_returns_false(self):
        mgr = _make_mgr()
        mgr._system = "FreeBSD"
        with patch("skcapstone.notifications.subprocess.run") as mock_run:
            result = mgr.notify("T", "B")

        assert result is False
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Debounce logic
# ---------------------------------------------------------------------------


class TestDebounce:
    """Debounce prevents more than one notification per interval."""

    def test_second_call_within_window_is_debounced(self):
        """A second notify() within the debounce window returns False."""
        mgr = NotificationManager(debounce_seconds=5.0)
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            first = mgr.notify("T", "B")
            second = mgr.notify("T", "B")

        assert first is True
        assert second is False
        assert mock_run.call_count == 1

    def test_call_after_window_is_allowed(self):
        """A notify() after the debounce window passes through."""
        mgr = NotificationManager(debounce_seconds=0.05)
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            mgr.notify("T", "B")
            time.sleep(0.1)
            second = mgr.notify("T2", "B2")

        assert second is True
        assert mock_run.call_count == 2

    def test_debounce_does_not_update_timestamp_on_failed_dispatch(self):
        """Failed dispatch (notify-send missing) does not reset the debounce clock."""
        mgr = NotificationManager(debounce_seconds=5.0)
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run", side_effect=FileNotFoundError),
        ):
            mgr.notify("T", "B")  # fails → _last_sent stays 0

        # Now try again immediately - should not be debounced
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run") as mock_run2,
        ):
            mock_run2.return_value = MagicMock(returncode=0)
            result = mgr.notify("T", "B")

        assert result is True

    def test_zero_debounce_allows_rapid_calls(self):
        """debounce_seconds=0 means every call is dispatched.

        Dedup is disabled (dedup_ttl=0) so this isolates the debounce layer:
        identical rapid calls would otherwise be caught by the dedup cache.
        """
        mgr = NotificationManager(debounce_seconds=0, dedup_ttl=0)
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            for _ in range(3):
                mgr.notify("T", "B")

        assert mock_run.call_count == 3


# ---------------------------------------------------------------------------
# Deduplication cache
# ---------------------------------------------------------------------------


class TestDedup:
    """Content deduplication suppresses identical notifications within the TTL."""

    def _linux_manager(self, dedup_ttl: float) -> NotificationManager:
        """Return a fresh Linux manager with no debounce and the given dedup TTL."""
        mgr = NotificationManager(debounce_seconds=0, dedup_ttl=dedup_ttl)
        mgr._system = "Linux"
        return mgr

    def test_duplicate_within_ttl_is_suppressed(self):
        """A second identical notify() within the dedup TTL returns False."""
        mgr = self._linux_manager(dedup_ttl=300)
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            first = mgr.notify("Same", "Body", urgency="normal")
            second = mgr.notify("Same", "Body", urgency="normal")

        assert first is True
        assert second is False
        assert mock_run.call_count == 1

    def test_distinct_notifications_pass_through(self):
        """Notifications with different content are not deduplicated."""
        mgr = self._linux_manager(dedup_ttl=300)
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            a = mgr.notify("Title A", "Body A")
            b = mgr.notify("Title B", "Body B")
            c = mgr.notify("Title A", "Body A", urgency="critical")  # urgency differs

        assert a is True
        assert b is True
        assert c is True
        assert mock_run.call_count == 3

    def test_dedup_expires_after_ttl(self):
        """After the TTL elapses, an identical notification is dispatched again."""
        mgr = self._linux_manager(dedup_ttl=0.05)
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            first = mgr.notify("Same", "Body")
            time.sleep(0.1)
            second = mgr.notify("Same", "Body")

        assert first is True
        assert second is True
        assert mock_run.call_count == 2

    def test_explicit_dedup_key_suppresses_different_content(self):
        """An explicit dedup_key groups notifications regardless of title/body."""
        mgr = self._linux_manager(dedup_ttl=300)
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            first = mgr.notify("Title 1", "Body 1", dedup_key="event-42")
            second = mgr.notify("Title 2", "Body 2", dedup_key="event-42")

        assert first is True
        assert second is False
        assert mock_run.call_count == 1

    def test_dedup_ttl_zero_disables_dedup(self):
        """dedup_ttl=0 dispatches every identical notification."""
        mgr = self._linux_manager(dedup_ttl=0)
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            for _ in range(3):
                mgr.notify("Same", "Body")

        assert mock_run.call_count == 3

    def test_failed_dispatch_not_cached_for_dedup(self):
        """A notification that failed to dispatch is not remembered for dedup."""
        mgr = self._linux_manager(dedup_ttl=300)
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run", side_effect=FileNotFoundError),
        ):
            first = mgr.notify("Same", "Body")  # fails -> not cached

        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run") as mock_run2,
        ):
            mock_run2.return_value = MagicMock(returncode=0)
            second = mgr.notify("Same", "Body")

        assert first is False
        assert second is True

    def test_env_var_sets_default_dedup_ttl(self, monkeypatch):
        """SKCAPSTONE_NOTIFY_DEDUP_TTL sets the default when dedup_ttl is unset."""
        monkeypatch.setenv("SKCAPSTONE_NOTIFY_DEDUP_TTL", "42")
        mgr = NotificationManager(debounce_seconds=0)
        assert mgr._dedup_ttl == 42.0

    def test_env_var_zero_disables_dedup_by_default(self, monkeypatch):
        """SKCAPSTONE_NOTIFY_DEDUP_TTL=0 disables dedup for a default manager."""
        monkeypatch.setenv("SKCAPSTONE_NOTIFY_DEDUP_TTL", "0")
        mgr = NotificationManager(debounce_seconds=0)
        mgr._system = "Linux"
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            mgr.notify("Same", "Body")
            mgr.notify("Same", "Body")

        assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class TestModuleLevelHelpers:
    """notify() convenience function and get_manager() singleton."""

    def test_notify_convenience_function(self):
        """Module-level notify() delegates to the singleton manager."""
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run") as mock_run,
            patch("skcapstone.notifications._manager", None),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            # Reset singleton so debounce is fresh
            import skcapstone.notifications as _notif_mod

            _notif_mod._manager = None
            result = notify("Hello", "World")

        assert result is True

    def test_get_manager_returns_singleton(self):
        """get_manager() returns the same instance on repeated calls."""
        import skcapstone.notifications as _notif_mod

        _notif_mod._manager = None  # reset
        m1 = get_manager()
        m2 = get_manager()
        assert m1 is m2


# ---------------------------------------------------------------------------
# Desktop-notification guard (SKCAPSTONE_DESKTOP_NOTIFY)
# ---------------------------------------------------------------------------


class TestDesktopNotificationGuard:
    """SKCAPSTONE_DESKTOP_NOTIFY suppresses every dispatch path."""

    def test_disabled_by_default(self, monkeypatch):
        """Unset env var means notifications are DISABLED (opt-in): background
        agents must not flood the desktop tray unless explicitly enabled."""
        monkeypatch.delenv("SKCAPSTONE_DESKTOP_NOTIFY", raising=False)
        assert desktop_notifications_enabled() is False

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "silent", "null", "none"])
    def test_disabled_values(self, monkeypatch, value):
        """Recognised falsy values disable notifications (case-insensitive)."""
        monkeypatch.setenv("SKCAPSTONE_DESKTOP_NOTIFY", value.upper())
        assert desktop_notifications_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
    def test_enabled_values(self, monkeypatch, value):
        """Other values keep notifications enabled."""
        monkeypatch.setenv("SKCAPSTONE_DESKTOP_NOTIFY", value)
        assert desktop_notifications_enabled() is True

    def test_notify_short_circuits_when_disabled(self, monkeypatch):
        """notify() returns False and never shells out when disabled."""
        monkeypatch.setenv("SKCAPSTONE_DESKTOP_NOTIFY", "0")
        mgr = _make_mgr()
        with (
            patch("skcapstone.notifications.platform.system", return_value="Linux"),
            patch("skcapstone.notifications.subprocess.run") as mock_run,
        ):
            result = mgr.notify("Hello", "World")

        assert result is False
        mock_run.assert_not_called()
