"""Tests for desktop notification support."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, call, patch

import pytest

from skcapstone.notifications import NotificationManager, notify, get_manager


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
        with patch("skcapstone.notifications.platform.system", return_value="Linux"), \
             patch("skcapstone.notifications.subprocess.run") as mock_run:
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
        with patch("skcapstone.notifications.platform.system", return_value="Linux"), \
             patch("skcapstone.notifications.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            mgr.notify("T", "B", urgency="low")

        args = mock_run.call_args[0][0]
        assert "--urgency" in args
        assert args[args.index("--urgency") + 1] == "low"

    def test_notify_send_urgency_critical(self):
        """Critical urgency maps to notify-send --urgency critical."""
        mgr = _make_mgr()
        with patch("skcapstone.notifications.platform.system", return_value="Linux"), \
             patch("skcapstone.notifications.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            mgr.notify("T", "B", urgency="critical")

        args = mock_run.call_args[0][0]
        assert args[args.index("--urgency") + 1] == "critical"

    def test_notify_send_not_found_returns_false(self):
        """Returns False gracefully when notify-send binary is missing."""
        mgr = _make_mgr()
        with patch("skcapstone.notifications.platform.system", return_value="Linux"), \
             patch("skcapstone.notifications.subprocess.run", side_effect=FileNotFoundError):
            result = mgr.notify("T", "B")

        assert result is False

    def test_notify_send_nonzero_exit_returns_false(self):
        """Returns False when notify-send exits non-zero."""
        import subprocess
        mgr = _make_mgr()
        with patch("skcapstone.notifications.platform.system", return_value="Linux"), \
             patch(
                 "skcapstone.notifications.subprocess.run",
                 side_effect=subprocess.CalledProcessError(1, "notify-send", stderr=b"err"),
             ):
            result = mgr.notify("T", "B")

        assert result is False

    def test_notify_send_timeout_returns_false(self):
        """Returns False when notify-send times out."""
        import subprocess
        mgr = _make_mgr()
        with patch("skcapstone.notifications.platform.system", return_value="Linux"), \
             patch(
                 "skcapstone.notifications.subprocess.run",
                 side_effect=subprocess.TimeoutExpired("notify-send", 5),
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
        with patch("skcapstone.notifications.platform.system", return_value="Linux"), \
             patch("skcapstone.notifications.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            first = mgr.notify("T", "B")
            second = mgr.notify("T", "B")

        assert first is True
        assert second is False
        assert mock_run.call_count == 1

    def test_call_after_window_is_allowed(self):
        """A notify() after the debounce window passes through."""
        mgr = NotificationManager(debounce_seconds=0.05)
        with patch("skcapstone.notifications.platform.system", return_value="Linux"), \
             patch("skcapstone.notifications.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            mgr.notify("T", "B")
            time.sleep(0.1)
            second = mgr.notify("T2", "B2")

        assert second is True
        assert mock_run.call_count == 2

    def test_debounce_does_not_update_timestamp_on_failed_dispatch(self):
        """Failed dispatch (notify-send missing) does not reset the debounce clock."""
        mgr = NotificationManager(debounce_seconds=5.0)
        with patch("skcapstone.notifications.platform.system", return_value="Linux"), \
             patch("skcapstone.notifications.subprocess.run", side_effect=FileNotFoundError):
            mgr.notify("T", "B")  # fails → _last_sent stays 0

        # Now try again immediately — should not be debounced
        with patch("skcapstone.notifications.platform.system", return_value="Linux"), \
             patch("skcapstone.notifications.subprocess.run") as mock_run2:
            mock_run2.return_value = MagicMock(returncode=0)
            result = mgr.notify("T", "B")

        assert result is True

    def test_zero_debounce_allows_rapid_calls(self):
        """debounce_seconds=0 means every call is dispatched."""
        mgr = NotificationManager(debounce_seconds=0)
        with patch("skcapstone.notifications.platform.system", return_value="Linux"), \
             patch("skcapstone.notifications.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            for _ in range(3):
                mgr.notify("T", "B")

        assert mock_run.call_count == 3


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

class TestModuleLevelHelpers:
    """notify() convenience function and get_manager() singleton."""

    def test_notify_convenience_function(self):
        """Module-level notify() delegates to the singleton manager."""
        with patch("skcapstone.notifications.platform.system", return_value="Linux"), \
             patch("skcapstone.notifications.subprocess.run") as mock_run, \
             patch("skcapstone.notifications._manager", None):
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
