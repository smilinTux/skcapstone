"""
Desktop notification support for the sovereign agent.

Sends desktop notifications for incoming messages via:
  - gi.repository.Notify (Linux / libnotify, with GLib action callbacks)
  - notify-send (Linux / libnotify, subprocess fallback)
  - osascript (macOS)

Gracefully no-ops if neither tool is available.
Enforces a 5-second debounce so rapid messages don't flood the desktop, plus a
content-based deduplication cache so an identical notification (same
title/body/urgency, or an explicit ``dedup_key``) is suppressed if it was
already sent within a configurable TTL window.

Click actions (Linux gi.Notify only):
  - open-dashboard: xdg-open the skcapstone dashboard URL (default localhost:7778)
  - open-skchat:    open skchat watch in a terminal session
  Click events are stored in skcapstone memory (layer: short-term).
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import platform
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.notifications")

# Values (case-insensitive) that disable desktop notifications.
_DISABLED_VALUES = frozenset({"0", "false", "no", "off", "silent", "null", "none"})

# Default deduplication window (seconds). Identical notifications sent within
# this window are suppressed. Overridable per-manager and via the
# ``SKCAPSTONE_NOTIFY_DEDUP_TTL`` environment variable.
_DEFAULT_DEDUP_TTL = 120.0

# ---------------------------------------------------------------------------
# Headless out-of-band alerting (daemon-down / agent-dark)
# ---------------------------------------------------------------------------
# The desktop transports above are useless on a headless server or a laptop
# with no login session. These env vars configure a non-desktop, out-of-band
# channel so daemon-down / agent-dark events still reach an operator. Both are
# opt-in: with neither configured, headless alerting is a no-op and current
# behaviour is preserved.
#
#   SKCAPSTONE_ALERT_WEBHOOK   generic webhook URL (HTTP POST of a JSON body)
#   SKCAPSTONE_HEADLESS_ALERT  truthy → also shell out to the ``sk-alert``
#                              Telegram primitive if it is on PATH
_ALERT_WEBHOOK_ENV = "SKCAPSTONE_ALERT_WEBHOOK"
_HEADLESS_ALERT_ENV = "SKCAPSTONE_HEADLESS_ALERT"

# Timeout (seconds) for a headless webhook POST or sk-alert shell-out.
_HEADLESS_TIMEOUT = 10.0

# Map notification urgency onto an sk-alert severity level.
_SK_ALERT_LEVEL = {
    "low": "info",
    "normal": "warn",
    "critical": "error",
}


def _truthy(value: Optional[str]) -> bool:
    """Return whether an env string represents an enabled/true value."""
    if value is None:
        return False
    return value.strip().lower() not in _DISABLED_VALUES and value.strip() != ""


def _alert_webhook_url() -> str:
    """Return the configured headless webhook URL (empty string if unset)."""
    return (os.environ.get(_ALERT_WEBHOOK_ENV) or "").strip()


def _skalert_channel_enabled() -> bool:
    """Return whether the ``sk-alert`` shell-out channel is opted in."""
    return _truthy(os.environ.get(_HEADLESS_ALERT_ENV))


def headless_alerting_enabled() -> bool:
    """Return whether any headless out-of-band channel is configured.

    Headless alerting is disabled by default. It is considered enabled when
    either a webhook URL is set (``SKCAPSTONE_ALERT_WEBHOOK``) or the
    ``sk-alert`` channel is opted in (``SKCAPSTONE_HEADLESS_ALERT`` truthy).
    With neither set, :func:`notify_headless` is a no-op, preserving the
    framework's prior desktop-only behaviour.

    Returns:
        True if at least one headless channel is configured.
    """
    return bool(_alert_webhook_url()) or _skalert_channel_enabled()


def _resolve_sk_alert() -> Optional[str]:
    """Return an executable ``sk-alert`` path, or None if unavailable.

    Mirrors the resolution used elsewhere in the framework (scheduler /
    gfs_backup): prefer a PATH lookup, else the conventional venv location.
    """
    import shutil as _shutil

    found = _shutil.which("sk-alert")
    if found:
        return found
    fallback = os.path.expanduser("~/.skenv/bin/sk-alert")
    if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
        return fallback
    return None


def _default_dedup_ttl() -> float:
    """Return the default dedup TTL, honouring ``SKCAPSTONE_NOTIFY_DEDUP_TTL``.

    Reads the ``SKCAPSTONE_NOTIFY_DEDUP_TTL`` environment variable (seconds).
    A value of ``0`` (or negative) disables content deduplication. Any
    unparseable value falls back to :data:`_DEFAULT_DEDUP_TTL`.

    Returns:
        The dedup TTL in seconds.
    """
    raw = os.environ.get("SKCAPSTONE_NOTIFY_DEDUP_TTL")
    if raw is None:
        return _DEFAULT_DEDUP_TTL
    try:
        return max(0.0, float(raw.strip()))
    except (TypeError, ValueError):
        return _DEFAULT_DEDUP_TTL


def desktop_notifications_enabled() -> bool:
    """Return whether desktop notifications should be dispatched.

    Controlled by the ``SKCAPSTONE_DESKTOP_NOTIFY`` environment variable.
    Defaults to DISABLED (opt-in): background agents must NOT flood the desktop
    tray. Set it to ``1``/``true``/``yes``/``on`` to enable desktop popups
    (``gi.repository.Notify``, ``notify-send`` and ``osascript``) on a machine
    where you actually want them. Any disabled/unset value suppresses them.

    The test suite forces this off (see ``tests/conftest.py``) so running
    tests never floods the live desktop's notification tray.

    Returns:
        True if notifications should be sent, False to suppress them.
    """
    value = os.environ.get("SKCAPSTONE_DESKTOP_NOTIFY", "0").strip().lower()
    return value not in _DISABLED_VALUES


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


def _store_notification_memory(
    title: str,
    body: str,
    urgency: str,
    event_type: str = "notification-sent",
    channels: Optional[list[str]] = None,
) -> None:
    """Log a notification dispatch to the skcomms/notifications/ directory.

    These are transport bookkeeping, not persistent memories, so they
    go to ``~/.skcapstone/agents/{agent}/skcomms/notifications/`` instead
    of polluting the memory/ tree that skmemory indexes.

    Args:
        title: Notification title.
        body: Notification body text.
        urgency: Urgency level.
        event_type: Record type (``notification-sent`` for desktop popups,
            ``headless-alert-sent`` for out-of-band alerts).
        channels: Optional list of channel names an out-of-band alert reached.
    """
    try:
        import json as _json
        import uuid

        from . import AGENT_HOME

        home = Path(AGENT_HOME).expanduser()
        if not home.exists():
            return

        from . import active_agent_name

        agent_name = os.environ.get("SKCAPSTONE_AGENT") or active_agent_name()
        notif_dir = home / "agents" / agent_name / "skcomms" / "notifications"
        notif_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = {
            "id": uuid.uuid4().hex[:12],
            "type": event_type,
            "title": title,
            "body": body,
            "urgency": urgency,
            "timestamp": ts,
        }
        if channels:
            entry["channels"] = channels
        path = notif_dir / f"{entry['id']}.json"
        path.write_text(_json.dumps(entry, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("Failed to store notification log: %s", exc)


def _store_click_event(action: str, detail: str) -> None:
    """Log a notification click event to the skcomms/notifications/ directory."""
    try:
        import json as _json
        import uuid

        from . import AGENT_HOME

        home = Path(AGENT_HOME).expanduser()
        if not home.exists():
            return

        from . import active_agent_name

        agent_name = os.environ.get("SKCAPSTONE_AGENT") or active_agent_name()
        notif_dir = home / "agents" / agent_name / "skcomms" / "notifications"
        notif_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = {
            "id": uuid.uuid4().hex[:12],
            "type": "click-event",
            "action": action,
            "detail": detail,
            "timestamp": ts,
        }
        path = notif_dir / f"{entry['id']}.json"
        path.write_text(_json.dumps(entry, indent=2), encoding="utf-8")
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
    """Send desktop notifications with debounce + deduplication protection.

    Two independent suppression layers guard the desktop tray:

    * **Debounce** (``debounce_seconds``) is a global rate limit: no more than
      one notification of any content per interval.
    * **Deduplication** (``dedup_ttl``) is content-specific: an identical
      notification (same ``title``/``body``/``urgency``, or the same explicit
      ``dedup_key``) is suppressed if it was already dispatched within the TTL
      window. This catches duplicate events delivered repeatedly, retries, and
      multi-path delivery of the same event.

    Args:
        debounce_seconds: Minimum seconds between notifications (default 5).
        dashboard_url:    URL opened by the "Open Dashboard" action button.
        dedup_ttl:        Seconds an identical notification is remembered and
            suppressed for. Defaults to the ``SKCAPSTONE_NOTIFY_DEDUP_TTL``
            environment variable, else 120s. Pass ``0`` to disable dedup.
    """

    def __init__(
        self,
        debounce_seconds: float = 5.0,
        dashboard_url: str = _DEFAULT_DASHBOARD_URL,
        dedup_ttl: Optional[float] = None,
    ) -> None:
        self._debounce_seconds = debounce_seconds
        self._dashboard_url = dashboard_url
        self._dedup_ttl = _default_dedup_ttl() if dedup_ttl is None else max(0.0, dedup_ttl)
        self._last_sent: float = 0.0
        # Maps a stable dedup key -> monotonic timestamp of last dispatch.
        self._dedup_seen: dict[str, float] = {}
        self._system = platform.system()  # "Linux", "Darwin", "Windows"
        # GLib main-loop plumbing (created lazily, shared across notifications)
        self._glib_loop: object | None = None
        self._glib_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Deduplication helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_dedup_key(title: str, body: str, urgency: str) -> str:
        """Compute a stable dedup key from a notification's content.

        Args:
            title: Notification title.
            body: Notification body text.
            urgency: Urgency level.

        Returns:
            A short hex digest uniquely identifying this notification content.
        """
        payload = "\x1f".join((title, body, urgency))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _is_duplicate(self, key: str, now: float) -> bool:
        """Return whether ``key`` was already seen within the dedup TTL.

        Expired entries are pruned as a side effect so the cache stays small.

        Args:
            key: The dedup key to test.
            now: Current monotonic timestamp.

        Returns:
            True if an identical notification is still within the TTL window.
        """
        if self._dedup_ttl <= 0:
            return False
        # Prune expired entries to bound memory growth.
        expired = [k for k, ts in self._dedup_seen.items() if now - ts >= self._dedup_ttl]
        for k in expired:
            del self._dedup_seen[k]
        last = self._dedup_seen.get(key)
        return last is not None and (now - last) < self._dedup_ttl

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def notify(
        self,
        title: str,
        body: str,
        urgency: str = "normal",
        dedup_key: Optional[str] = None,
    ) -> bool:
        """Send a desktop notification.

        Args:
            title: Notification title.
            body: Notification body text.
            urgency: "low", "normal", or "critical".
            dedup_key: Optional explicit key identifying this notification for
                deduplication. When omitted, a stable key is derived from
                ``title``/``body``/``urgency``. An identical key seen within the
                manager's ``dedup_ttl`` window is suppressed.

        Returns:
            True if the notification was dispatched, False if suppressed,
            debounced, deduplicated, or no notification system is available.
        """
        if not desktop_notifications_enabled():
            logger.debug("Desktop notifications disabled via SKCAPSTONE_DESKTOP_NOTIFY")
            return False

        now = time.monotonic()

        # Content deduplication: suppress an identical notification already sent
        # within the TTL window (duplicate events, retries, multi-path delivery).
        key = dedup_key if dedup_key is not None else self._compute_dedup_key(title, body, urgency)
        if self._is_duplicate(key, now):
            logger.debug(
                "Notification deduplicated (key=%s within %.0fs TTL)", key, self._dedup_ttl
            )
            return False

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
            sent_at = time.monotonic()
            self._last_sent = sent_at
            if self._dedup_ttl > 0:
                self._dedup_seen[key] = sent_at
            _store_notification_memory(title, body, urgency)
        return dispatched

    # ------------------------------------------------------------------
    # Headless out-of-band transport (daemon-down / agent-dark)
    # ------------------------------------------------------------------

    def notify_headless(
        self,
        title: str,
        body: str,
        urgency: str = "critical",
        dedup_key: Optional[str] = None,
    ) -> bool:
        """Send an out-of-band alert over a non-desktop, headless channel.

        Unlike :meth:`notify` (which targets a desktop tray and is useless on a
        headless server), this fans an alert out to whatever headless channel
        the operator has configured:

        * ``SKCAPSTONE_ALERT_WEBHOOK``: an HTTP endpoint that receives a JSON
          POST (``{agent, title, body, urgency, level, timestamp, source}``).
        * ``SKCAPSTONE_HEADLESS_ALERT`` truthy: shell out to the ``sk-alert``
          Telegram primitive when it is available on the host.

        Disabled by default: with neither configured this is a no-op and returns
        ``False``, preserving prior behaviour. It reuses the manager's content
        deduplication cache (namespaced from the desktop keys) so a still-true
        condition re-fired every watchdog tick does not spam the channel. It is
        fail-safe: no channel error propagates to the caller (an alert failure
        must never crash the daemon).

        Args:
            title: Alert title.
            body: Alert body text.
            urgency: "low", "normal", or "critical" (default critical, since
                these events are, by nature, faults).
            dedup_key: Optional explicit key. When omitted a stable key is
                derived from ``title``/``body``/``urgency``.

        Returns:
            True if at least one headless channel accepted the alert, else
            False (disabled, deduplicated, or every channel failed/absent).
        """
        if not headless_alerting_enabled():
            logger.debug("Headless alerting disabled (no channel configured)")
            return False

        now = time.monotonic()
        raw_key = (
            dedup_key if dedup_key is not None else self._compute_dedup_key(title, body, urgency)
        )
        # Namespace so a headless alert never collides with a desktop dedup key.
        key = f"hl:{raw_key}"
        if self._is_duplicate(key, now):
            logger.debug("Headless alert deduplicated (key=%s)", raw_key)
            return False

        level = _SK_ALERT_LEVEL.get(urgency, "warn")
        channels: list[str] = []

        webhook = _alert_webhook_url()
        if webhook and self._post_webhook(webhook, title, body, urgency, level):
            channels.append("webhook")

        if _skalert_channel_enabled() and self._send_sk_alert(title, body, level):
            channels.append("sk-alert")

        if channels:
            if self._dedup_ttl > 0:
                self._dedup_seen[key] = time.monotonic()
            _store_notification_memory(
                title,
                body,
                urgency,
                event_type="headless-alert-sent",
                channels=channels,
            )
            logger.info("Headless alert dispatched via %s: %r", channels, title)
            return True

        logger.warning("Headless alert configured but no channel accepted it: %r", title)
        return False

    @staticmethod
    def _post_webhook(url: str, title: str, body: str, urgency: str, level: str) -> bool:
        """POST an alert as JSON to a webhook URL. Never raises.

        Uses only the standard library (``urllib``) so no extra dependency is
        pulled in. Any transport error is swallowed and reported as a failed
        dispatch.

        Args:
            url: Destination webhook URL.
            title: Alert title.
            body: Alert body.
            urgency: Notification urgency.
            level: Mapped sk-alert severity level.

        Returns:
            True on a 2xx response, else False.
        """
        import json as _json
        import urllib.request

        try:
            from . import active_agent_name

            agent = os.environ.get("SKCAPSTONE_AGENT") or active_agent_name()
        except Exception:  # noqa: BLE001 - identity lookup must not break alerting
            agent = os.environ.get("SKCAPSTONE_AGENT") or "unknown"

        payload = {
            "agent": agent,
            "title": title,
            "body": body,
            "urgency": urgency,
            "level": level,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "source": "skcapstone.notifications",
        }
        try:
            data = _json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_HEADLESS_TIMEOUT) as resp:
                code = getattr(resp, "status", None) or resp.getcode()
                ok = 200 <= int(code) < 300
                if not ok:
                    logger.debug("Alert webhook returned HTTP %s", code)
                return ok
        except Exception as exc:  # noqa: BLE001 - alerting must never raise
            logger.debug("Alert webhook POST failed: %s", exc)
            return False

    @staticmethod
    def _send_sk_alert(title: str, body: str, level: str) -> bool:
        """Shell out to the ``sk-alert`` Telegram primitive. Never raises.

        Args:
            title: Alert title.
            body: Alert body.
            level: sk-alert severity level (``info``/``warn``/``error``).

        Returns:
            True if sk-alert was found and invoked without raising, else False.
        """
        alert = _resolve_sk_alert()
        if not alert:
            logger.debug("sk-alert not found; headless sk-alert channel skipped")
            return False
        message = f"{title}\n{body}" if body else title
        try:
            subprocess.run(
                [alert, "-l", level, message],
                timeout=_HEADLESS_TIMEOUT,
                check=False,
                capture_output=True,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - alerting must never raise
            logger.debug("sk-alert dispatch failed: %s", exc)
            return False

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

            def _on_open_dashboard(notification: object, action: str, user_data: object) -> None:
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

            def _on_open_skchat(notification: object, action: str, user_data: object) -> None:
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
            logger.debug("notify-send not found - desktop notifications unavailable")
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
        script = f'display notification "{safe_body}" with title "{safe_title}"'
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
            logger.debug("osascript not found - desktop notifications unavailable")
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


def notify(
    title: str,
    body: str,
    urgency: str = "normal",
    dedup_key: Optional[str] = None,
) -> bool:
    """Convenience wrapper: send a notification via the singleton manager."""
    return get_manager().notify(title, body, urgency, dedup_key=dedup_key)


def notify_headless(
    title: str,
    body: str,
    urgency: str = "critical",
    dedup_key: Optional[str] = None,
) -> bool:
    """Convenience wrapper: send an out-of-band headless alert.

    Delegates to :meth:`NotificationManager.notify_headless` on the shared
    singleton. Disabled by default (no channel configured → no-op) and
    fail-safe (never raises).
    """
    return get_manager().notify_headless(title, body, urgency, dedup_key=dedup_key)
