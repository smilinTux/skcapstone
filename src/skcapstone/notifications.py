"""
Desktop notification support for the sovereign agent.

Sends desktop notifications for incoming messages via:
  - gi.repository.Notify (Linux / libnotify, with GLib action callbacks)
  - notify-send (Linux / libnotify, subprocess fallback)
  - osascript (macOS)

Gracefully no-ops if neither tool is available.
Enforces a 5-second debounce so rapid messages don't flood the desktop.

Click actions (Linux gi.Notify only):
  - open-dashboard: xdg-open the skcapstone dashboard URL (default localhost:7778)
  - open-skchat:    open skchat watch in a terminal session
  Click events are stored in skcapstone memory (layer: short-term).
"""

from __future__ import annotations

import datetime
import logging
import platform
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.notifications")

# Default dashboard URL (skcapstone dashboard default port)
_DEFAULT_DASHBOARD_URL = "http://localhost:7778"

# Terminal emulators tried in order when opening skchat watch
_TERMINAL_CMDS: list[list[str]] = [
    ["konsole", "--new-tab", "-e"],
    ["gnome-terminal", "--"],
    ["xfce4-terminal", "-x"],
    ["alacritty", "-e"],
    ["kitty"],
    ["xterm", "-e"],
]


def _store_notification_memory(title: str, body: str, urgency: str) -> None:
    """Persist a short-term memory entry for every dispatched notification."""
    try:
        from . import AGENT_HOME
        from .memory_engine import store as mem_store
        from .models import MemoryLayer

        home = Path(AGENT_HOME).expanduser()
        if not home.exists():
            return

        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        content = f"[{ts}] Notification sent — title={title!r} body={body!r} urgency={urgency}"
        mem_store(
            home=home,
            content=content,
            tags=["notification"],
            source="notifications",
            importance=0.3,
            layer=MemoryLayer.SHORT_TERM,
        )
    except Exception as exc:
        logger.debug("Failed to store notification memory: %s", exc)


def _store_click_event(action: str, detail: str) -> None:
    """Persist a short-term memory entry for a notification click action."""
    try:
        from . import AGENT_HOME
        from .memory_engine import store as mem_store
        from .models import MemoryLayer

        home = Path(AGENT_HOME).expanduser()
        if not home.exists():
            return

        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        content = (
            f"[{ts}] Notification click — action={action!r} detail={detail!r}"
        )
        mem_store(
            home=home,
            content=content,
            tags=["notification", "click-event"],
            source="notifications",
            importance=0.3,
            layer=MemoryLayer.SHORT_TERM,
        )
        logger.debug("Stored notification click event: %s → %s", action, detail)
    except Exception as exc:
        logger.debug("Failed to store click event in memory: %s", exc)


def _open_skchat_terminal() -> None:
    """Open ``skchat watch`` in a terminal emulator (best-effort)."""
    skchat_cmd = ["skchat", "watch"]
    for term_prefix in _TERMINAL_CMDS:
        cmd = term_prefix + skchat_cmd
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.debug("Opened skchat terminal with: %s", cmd)
            return
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.debug("Terminal launch error (%s): %s", cmd[0], exc)
            continue
    logger.debug("No terminal emulator found; cannot open skchat session")


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
        dashboard_url:    URL opened by the "Open Dashboard" action button.
    """

    def __init__(
        self,
        debounce_seconds: float = 5.0,
        dashboard_url: str = _DEFAULT_DASHBOARD_URL,
    ) -> None:
        self._debounce_seconds = debounce_seconds
        self._dashboard_url = dashboard_url
        self._last_sent: float = 0.0
        self._system = platform.system()  # "Linux", "Darwin", "Windows"
        # GLib main-loop plumbing (created lazily, shared across notifications)
        self._glib_loop: object | None = None
        self._glib_thread: threading.Thread | None = None

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
            dispatched = self._notify_linux_gi(title, body, urgency)
            if not dispatched:
                dispatched = self._notify_linux(title, body, urgency)
        elif self._system == "Darwin":
            dispatched = self._notify_macos(title, body)
        else:
            logger.debug("Desktop notifications not supported on %s", self._system)
            return False

        if dispatched:
            self._last_sent = time.monotonic()
            _store_notification_memory(title, body, urgency)
        return dispatched

    # ------------------------------------------------------------------
    # GLib main loop (needed for gi.Notify action callbacks)
    # ------------------------------------------------------------------

    def _ensure_glib_loop(self) -> None:
        """Start a GLib main loop in a daemon thread (idempotent)."""
        if (
            self._glib_loop is not None
            and self._glib_thread is not None
            and self._glib_thread.is_alive()
        ):
            return
        try:
            from gi.repository import GLib  # type: ignore[import-untyped]

            loop = GLib.MainLoop()
            self._glib_loop = loop

            def _run() -> None:
                loop.run()

            t = threading.Thread(
                target=_run,
                daemon=True,
                name="skcapstone-glib-loop",
            )
            t.start()
            self._glib_thread = t
            logger.debug("GLib main loop started in daemon thread")
        except Exception as exc:
            logger.debug("Could not start GLib main loop: %s", exc)

    # ------------------------------------------------------------------
    # Platform implementations
    # ------------------------------------------------------------------

    def _notify_linux_gi(self, title: str, body: str, urgency: str) -> bool:
        """Send via gi.repository.Notify with GLib action callbacks.

        Adds two action buttons:
          - "Open Dashboard" → xdg-open dashboard URL + stores click event
          - "Open SKChat"    → open skchat watch in terminal + stores click event

        Falls back gracefully (returns False) if gi is not importable.
        """
        try:
            import gi  # type: ignore[import-untyped]

            gi.require_version("Notify", "0.7")
            from gi.repository import Notify  # type: ignore[import-untyped]
        except (ImportError, ValueError) as exc:
            logger.debug("gi.repository.Notify unavailable: %s", exc)
            return False

        try:
            if not Notify.is_initted():
                Notify.init("skcapstone")

            _urgency_map = {
                "low": Notify.Urgency.LOW,
                "normal": Notify.Urgency.NORMAL,
                "critical": Notify.Urgency.CRITICAL,
            }

            n = Notify.Notification.new(title, body, "dialog-information")
            n.set_urgency(_urgency_map.get(urgency, Notify.Urgency.NORMAL))

            dashboard_url = self._dashboard_url

            def _on_open_dashboard(
                notification: object, action: str, user_data: object
            ) -> None:
                logger.debug("Notification action invoked: open-dashboard")
                _store_click_event("open-dashboard", dashboard_url)
                try:
                    subprocess.Popen(
                        ["xdg-open", dashboard_url],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception as exc:
                    logger.debug("xdg-open failed: %s", exc)

            def _on_open_skchat(
                notification: object, action: str, user_data: object
            ) -> None:
                logger.debug("Notification action invoked: open-skchat")
                _store_click_event("open-skchat", "skchat watch")
                _open_skchat_terminal()

            n.add_action(
                "open-dashboard",
                "Open Dashboard",
                _on_open_dashboard,
                None,
            )
            n.add_action(
                "open-skchat",
                "Open SKChat",
                _on_open_skchat,
                None,
            )

            # GLib main loop must be running to deliver action callbacks
            self._ensure_glib_loop()

            n.show()
            logger.debug("gi.Notify dispatched: %r / %r", title, body)
            return True

        except Exception as exc:
            logger.debug("gi.Notify error: %s", exc)
            return False

    def _notify_linux(self, title: str, body: str, urgency: str) -> bool:
        """Send via notify-send (libnotify subprocess fallback)."""
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
