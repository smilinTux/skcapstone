"""
Security pillar — SKSecurity integration.

Audit everything. Detect threats. Protect the sovereign.

Audit log format is JSONL (one JSON object per line), making entries
both machine-parseable and append-only safe. Each entry includes a
timestamp, event type, detail, and the hostname that generated it.
"""

from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from ..models import PillarStatus, SecurityState

AUDIT_LOG_NAME = "audit.log"


class AuditEntry(BaseModel):
    """A single structured audit log entry.

    Each entry is serialised as one JSON line in the append-only log.
    """

    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    event_type: str
    detail: str
    host: str = Field(default_factory=socket.gethostname)
    agent: Optional[str] = None
    metadata: Optional[dict] = None


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
    """Create the audit log file with a structured INIT entry."""
    audit_log = security_dir / AUDIT_LOG_NAME
    if not audit_log.exists():
        entry = AuditEntry(
            event_type="INIT",
            detail="SKCapstone security audit log created",
        )
        audit_log.write_text(entry.model_dump_json() + "\n")


def audit_event(
    home: Path,
    event_type: str,
    detail: str,
    agent: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> AuditEntry:
    """Append a structured event to the audit log.

    Each event is written as a single JSON line (JSONL format),
    keeping the log append-only and machine-parseable.

    Args:
        home: Agent home directory.
        event_type: Event category (INIT, AUTH, MEMORY, TRUST, SYNC,
            TOKEN_ISSUE, TOKEN_REVOKE, SECURITY, etc.).
        detail: Human-readable event description.
        agent: Optional agent name that triggered the event.
        metadata: Optional dict of extra structured data.

    Returns:
        AuditEntry: The entry that was written.
    """
    security_dir = home / "security"
    security_dir.mkdir(parents=True, exist_ok=True)
    audit_log = security_dir / AUDIT_LOG_NAME

    entry = AuditEntry(
        event_type=event_type,
        detail=detail,
        agent=agent,
        metadata=metadata,
    )

    with audit_log.open("a") as f:
        f.write(entry.model_dump_json() + "\n")

    return entry


def read_audit_log(home: Path, limit: int = 0) -> list[AuditEntry]:
    """Read and parse the audit log.

    Handles both legacy plain-text entries and new JSONL entries
    gracefully — old lines are wrapped in an AuditEntry with
    event_type="LEGACY".

    Args:
        home: Agent home directory.
        limit: Maximum entries to return (0 = all, newest first).

    Returns:
        list[AuditEntry]: Parsed audit entries.
    """
    audit_log = home / "security" / AUDIT_LOG_NAME
    if not audit_log.exists():
        return []

    entries: list[AuditEntry] = []
    for line in audit_log.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            entries.append(AuditEntry.model_validate(data))
        except (json.JSONDecodeError, Exception):
            # Reason: gracefully handle legacy plain-text log lines
            entries.append(AuditEntry(event_type="LEGACY", detail=line))

    if limit > 0:
        entries = entries[-limit:]

    return entries
