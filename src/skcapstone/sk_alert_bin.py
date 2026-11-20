"""Resolve the sk-alert binary the same way everywhere.

``$SK_ALERT_BIN`` wins (the convention sk-cron-run and skfleet-auto-rollout.sh
already use), then ``sk-alert`` on PATH, then the conventional venv copy. The
override is what lets the test suite point every alert at ``/bin/true``: test
runs on noroc2027 were sending real "REFUSED actuation" alerts to Telegram
(2026-11-13).
"""

from __future__ import annotations

import os
import shutil

VENV_FALLBACK = "~/.skenv/bin/sk-alert"


def sk_alert_bin() -> str:
    """Return the sk-alert path to run. Never raises; the path may not exist."""
    override = os.environ.get("SK_ALERT_BIN", "").strip()
    if override:
        return override
    return shutil.which("sk-alert") or os.path.expanduser(VENV_FALLBACK)
