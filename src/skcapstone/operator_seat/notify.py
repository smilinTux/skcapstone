"""Send Atlas's report and parked escalations to the human (Telegram).

Closes the human-in-the-loop: when Atlas parks a decision that needs a call, it
reaches the human's phone with the options and the exact approve/reject command.
The sender is injectable so tests never send a real message; the default shells
to sk-alert (the Telegram alert path).
"""

from __future__ import annotations

import subprocess
from typing import Callable


def default_sender(text: str) -> bool:
    """Send one Telegram message via sk-alert. Never raises; returns success."""
    try:
        r = subprocess.run(["sk-alert", text], capture_output=True, text=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def format_escalation(decision: dict) -> str:
    """Human-readable escalation card: the options and how to decide."""
    did = decision.get("id", "?")
    lines = [f"Atlas needs your call [{did}]:"]
    for i, opt in enumerate(decision.get("options", [])):
        obj = opt.get("object", "?")
        lines.append(f"  [{i}] {opt.get('action')} on {obj}: {opt.get('rationale', '')}")
    lines.append(f"approve: skoperator decide {did} --approve --choice <i>")
    lines.append(f"reject:  skoperator decide {did} --reject")
    return "\n".join(lines)


def notify_report(report: str, *, sender: Callable[[str], bool] = default_sender) -> bool:
    """Send the operator report."""
    return sender(f"Atlas report:\n{report}")


def notify_escalation(decision: dict, *, sender: Callable[[str], bool] = default_sender) -> bool:
    """Send one parked-decision escalation card."""
    return sender(format_escalation(decision))
