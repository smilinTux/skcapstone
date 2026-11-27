"""Send Atlas's report and parked escalations to the human (Telegram).

When Atlas escalates, the human's phone gets a report-only card: what was seen, why
ATLAS will not act on it, what (if anything) would let it, and how to acknowledge it.
There is nothing to approve (card f8dd9441).
The sender is injectable so tests never send a real message; the default shells
to sk-alert (the Telegram alert path).
"""

from __future__ import annotations

import subprocess
from typing import Callable

from ..sk_alert_bin import sk_alert_bin


def default_sender(text: str) -> bool:
    """Send one Telegram message via sk-alert. Never raises; returns success."""
    try:
        r = subprocess.run([sk_alert_bin(), text], capture_output=True, text=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def format_escalation(decision: dict) -> str:
    """Human-readable report-only escalation card: what, why, the hint, and how to acknowledge."""
    did = decision.get("id", "?")
    lines = [f"Atlas escalation, report-only (ATLAS will not act) [{did}]:"]
    for i, opt in enumerate(decision.get("options", [])):
        obj = opt.get("object", "?")
        lines.append(f"  [{i}] {opt.get('action')} on {obj}: {opt.get('rationale', '')}")
        reason = opt.get("report_only_reason")
        if reason:
            lines.append(f"      why: {reason}")
    lines.append(f"acknowledge: skoperator decide {did} --reject")
    return "\n".join(lines)


def notify_report(report: str, *, sender: Callable[[str], bool] = default_sender) -> bool:
    """Send the operator report."""
    return sender(f"Atlas report:\n{report}")


def notify_escalation(decision: dict, *, sender: Callable[[str], bool] = default_sender) -> bool:
    """Send one parked-decision escalation card."""
    return sender(format_escalation(decision))
