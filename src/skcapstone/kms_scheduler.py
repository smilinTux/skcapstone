"""
KMS Auto-Rotation Scheduler.

Background daemon thread that checks daily for KMS keys whose
``next_rotation_at`` timestamp is in the past and rotates them
automatically.

Rotation policy (stored in key metadata):
    service keys — every 30 days
    team keys    — every 90 days

On each rotation the scheduler:
    1. Calls ``KeyStore.rotate_key`` with reason='scheduled-auto-rotation'
    2. Sends a desktop notification via notify-send (best-effort)
    3. Stores a memory entry tagged ``key-rotation`` in the agent memory

The scheduler thread is a daemon thread and exits when the MCP server
process terminates. Call ``start()`` once from ``mcp_server._run_server()``.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger("skcapstone.kms_scheduler")

# How often to check for due keys (86400 s = 24 h).
_CHECK_INTERVAL = 86_400


class KMSRotationScheduler:
    """Daemon thread that auto-rotates KMS service/team keys on schedule.

    Args:
        home: Agent home directory (~/.skcapstone).
    """

    def __init__(self, home: Path) -> None:
        self._home = home
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the scheduler background thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="kms-rotation-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("KMS rotation scheduler started (check interval: %ds)", _CHECK_INTERVAL)

    def stop(self) -> None:
        """Signal the scheduler to stop after the current sleep."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main scheduler loop — runs until stop() is called."""
        while not self._stop_event.is_set():
            try:
                self._check_and_rotate()
            except Exception:
                logger.exception("Unhandled error during KMS rotation check")
            # Sleep for the check interval, but wake immediately if stopped.
            self._stop_event.wait(timeout=_CHECK_INTERVAL)

    def _check_and_rotate(self) -> None:
        """Rotate all keys whose next_rotation_at is in the past."""
        from .kms import KeyStore

        store = KeyStore(self._home)
        store.initialize()

        due = store.get_due_for_rotation()
        if not due:
            logger.debug("KMS rotation check: no keys due")
            return

        logger.info("KMS rotation check: %d key(s) due for rotation", len(due))
        for key in due:
            try:
                new_key = store.rotate_key(key.key_id, reason="scheduled-auto-rotation")
                logger.info(
                    "Auto-rotated %s key '%s' → v%d",
                    key.key_type.value,
                    key.label,
                    new_key.version,
                )
                self._send_notification(key.label, key.key_type.value, new_key.version)
                self._store_memory(key.label, key.key_type.value, new_key.version)
            except Exception:
                logger.exception(
                    "Failed to auto-rotate key '%s' (%s)", key.label, key.key_id
                )

    def _send_notification(self, label: str, key_type: str, new_version: int) -> None:
        """Send a desktop notification (best-effort, never raises)."""
        try:
            subprocess.run(
                [
                    "notify-send",
                    "--urgency", "normal",
                    "KMS Key Auto-Rotated",
                    f"{key_type} key '{label}' rotated to v{new_version}",
                ],
                check=False,
                timeout=5,
                capture_output=True,
            )
        except Exception:
            pass  # Notification failure must never interrupt rotation

    def _store_memory(self, label: str, key_type: str, new_version: int) -> None:
        """Persist a memory entry tagged key-rotation (best-effort)."""
        try:
            from .memory_engine import store as memory_store

            content = (
                f"KMS auto-rotation: {key_type} key '{label}' rotated to "
                f"v{new_version} via scheduled rotation policy "
                f"({30 if key_type == 'service' else 90}-day interval)."
            )
            memory_store(
                home=self._home,
                content=content,
                tags=["key-rotation", "kms", "security"],
                source="kms-scheduler",
                importance=0.6,
            )
        except Exception:
            logger.warning(
                "Failed to store key-rotation memory for '%s'", label, exc_info=True
            )
