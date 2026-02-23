"""
Security pillar — SKSecurity integration.

Audit everything. Detect threats. Protect the sovereign.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..models import PillarStatus, SecurityState


def initialize_security(home: Path) -> SecurityState:
    """Initialize security layer for the agent.

    Creates audit log structure and baseline security config.

    Args:
        home: Agent home directory (~/.skcapstone).

    Returns:
        SecurityState after initialization.
    """
    security_dir = home / "security"
    security_dir.mkdir(parents=True, exist_ok=True)

    state = SecurityState()

    try:
        import sksecurity  # type: ignore[import-untyped]

        state.status = PillarStatus.DEGRADED
    except ImportError:
        security_config = {
            "note": "Install sksecurity (pip install sksecurity) for full security",
            "audit_enabled": True,
        }
        (security_dir / "security.json").write_text(json.dumps(security_config, indent=2))
        state.status = PillarStatus.DEGRADED
        _init_audit_log(security_dir)
        return state

    _init_audit_log(security_dir)

    baseline = {
        "threats_detected": 0,
        "last_scan": None,
        "audit_enabled": True,
        "initialized_at": datetime.now(timezone.utc).isoformat(),
    }
    (security_dir / "security.json").write_text(json.dumps(baseline, indent=2))

    return state


def _init_audit_log(security_dir: Path) -> None:
    """Create the audit log file with a header entry."""
    audit_log = security_dir / "audit.log"
    if not audit_log.exists():
        timestamp = datetime.now(timezone.utc).isoformat()
        audit_log.write_text(f"[{timestamp}] INIT — SKCapstone security audit log created\n")


def audit_event(home: Path, event_type: str, detail: str) -> None:
    """Append an event to the audit log.

    Args:
        home: Agent home directory.
        event_type: Event category (INIT, AUTH, MEMORY, TRUST, etc.).
        detail: Human-readable event description.
    """
    security_dir = home / "security"
    security_dir.mkdir(parents=True, exist_ok=True)
    audit_log = security_dir / "audit.log"

    timestamp = datetime.now(timezone.utc).isoformat()
    entry = f"[{timestamp}] {event_type} — {detail}\n"

    with audit_log.open("a") as f:
        f.write(entry)
