"""
Desktop notification support for the sovereign agent.

Sends desktop notifications for incoming messages via:
  - notify-send (Linux / libnotify)
  - osascript (macOS)

Gracefully no-ops if neither tool is available.
Enforces a 5-second debounce so rapid messages don't flood the desktop.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import time
from typing import Optional

logger = logging.getLogger("skcapstone.notifications")

# Urgency map for notify-send
_NOTIFY_SEND_URGENCY = {
    "low": "low",
    "normal": "normal",
    "critical": "critical",
}


class NotificationManager:
    """Send desktop notifications with debounce protection.

    Args:
        debounce_seconds: Minimum seconds between notifications (default 5).
    """

    def __init__(self, debounce_seconds: float = 5.0) -> None:
        self._debounce_seconds = debounce_seconds
        self._last_sent: float = 0.0
        self._system = platform.system()  # "Linux", "Darwin", "Windows"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def notify(
        self,
        title: str,
        body: str,
        urgency: str = "normal",
    ) -> bool:
        """Send a desktop notification.

        Args:
            title: Notification title.
            body: Notification body text.
            urgency: "low", "normal", or "critical".

        Returns:
            True if the notification was dispatched, False if debounced
            or no notification system is available.
        """
        now = time.monotonic()
        if now - self._last_sent < self._debounce_seconds:
            logger.debug(
                "Notification debounced (%.1fs since last send)",
                now - self._last_sent,
            )
            return False

        dispatched = False
        if self._system == "Linux":
            dispatched = self._notify_linux(title, body, urgency)
        elif self._system == "Darwin":
            dispatched = self._notify_macos(title, body)
        else:
            logger.debug("Desktop notifications not supported on %s", self._system)
            return False

        if dispatched:
            self._last_sent = time.monotonic()
        return dispatched

    # ------------------------------------------------------------------
    # Platform implementations
    # ------------------------------------------------------------------

    def _notify_linux(self, title: str, body: str, urgency: str) -> bool:
        """Send via notify-send (libnotify)."""
        urgency_arg = _NOTIFY_SEND_URGENCY.get(urgency, "normal")
        try:
            subprocess.run(
                ["notify-send", "--urgency", urgency_arg, title, body],
                check=True,
                capture_output=True,
                timeout=5,
            )
            logger.debug("notify-send dispatched: %r / %r", title, body)
            return True
        except FileNotFoundError:
            logger.debug("notify-send not found — desktop notifications unavailable")
            return False
        except subprocess.CalledProcessError as exc:
            logger.debug("notify-send failed (rc=%d): %s", exc.returncode, exc.stderr)
            return False
        except subprocess.TimeoutExpired:
            logger.debug("notify-send timed out")
            return False
        except Exception as exc:
            logger.debug("notify-send unexpected error: %s", exc)
            return False

    def _notify_macos(self, title: str, body: str) -> bool:
        """Send via osascript (macOS Notification Center)."""
        # Escape single quotes to prevent injection through osascript
        safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
        safe_body = body.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            f'display notification "{safe_body}" with title "{safe_title}"'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                check=True,
                capture_output=True,
                timeout=5,
            )
            logger.debug("osascript dispatched: %r / %r", title, body)
            return True
        except FileNotFoundError:
            logger.debug("osascript not found — desktop notifications unavailable")
            return False
        except subprocess.CalledProcessError as exc:
            logger.debug("osascript failed (rc=%d): %s", exc.returncode, exc.stderr)
            return False
        except subprocess.TimeoutExpired:
            logger.debug("osascript timed out")
            return False
        except Exception as exc:
            logger.debug("osascript unexpected error: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Module-level singleton (lazy, shared across the process)
# ---------------------------------------------------------------------------

_manager: Optional[NotificationManager] = None


def get_manager() -> NotificationManager:
    """Return the module-level NotificationManager singleton."""
    global _manager
    if _manager is None:
        _manager = NotificationManager()
    return _manager


def notify(title: str, body: str, urgency: str = "normal") -> bool:
    """Convenience wrapper — send a notification via the singleton manager."""
    return get_manager().notify(title, body, urgency)
