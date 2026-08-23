"""Best-effort sk-alert notifications for fleet components.

Same discipline as scheduled_tasks._maybe_notify: locate the sk-alert
CLI, bounded subprocess, never raise into a control loop. Callers gate
alerts on events.emit() returning True, so the event dedupe window is
also the alert rate cap (R2).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

logger = logging.getLogger("skcapstone.fleet.alerts")


def send_alert(message: str, *, level: str = "warn") -> bool:
    """Fire one sk-alert. Returns False (never raises) on any failure."""
    alert = shutil.which("sk-alert") or os.path.expanduser("~/.skenv/bin/sk-alert")
    try:
        out = subprocess.run([alert, "-l", level, message], timeout=30, check=False)
        return out.returncode == 0
    except Exception as exc:
        logger.warning("sk-alert failed: %s", exc)
        return False
