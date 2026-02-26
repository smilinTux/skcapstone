"""
Private helpers for TrusteeOps — audit, snapshot, and log utilities.

Not part of the public API; imported only by trustee_ops.py.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .team_engine import AgentStatus, TeamDeployment

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

_AUDIT_DIR = Path("~/.skcapstone/coordination")
_AUDIT_FILE = _AUDIT_DIR / "audit.log"


def write_audit(
    action: str,
    deployment_id: str,
    details: Dict[str, Any],
    home: Optional[Path] = None,
) -> None:
    """Append a structured audit entry to the trustee audit log.

    Args:
        action: Short action name (e.g. "restart_agent").
        deployment_id: The affected deployment.
        details: Extra key/value context for the entry.
        home: Agent home directory override.
    """
    audit_dir = (home / "coordination") if home else _AUDIT_DIR.expanduser()
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "audit.log"

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "deployment_id": deployment_id,
        **details,
    }
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Deployment status refresh
# ---------------------------------------------------------------------------


def refresh_deployment_status(deployment: TeamDeployment) -> None:
    """Update the overall deployment.status based on agent states.

    Args:
        deployment: The deployment to update in-place.
    """
    statuses = {a.status for a in deployment.agents.values()}
    if not statuses:
        deployment.status = "empty"
    elif statuses == {AgentStatus.RUNNING}:
        deployment.status = "running"
    elif AgentStatus.FAILED in statuses:
        deployment.status = "degraded"
    else:
        deployment.status = "partial"


# ---------------------------------------------------------------------------
# Context snapshot
# ---------------------------------------------------------------------------


def snapshot_agent_context(home: Path, agent_name: str) -> Path:
    """Copy agent memory/scratch to a timestamped snapshot directory.

    Args:
        home: Agent home directory.
        agent_name: Name of the agent to snapshot.

    Returns:
        Path to the snapshot directory (parent created even if source absent).
    """
    import shutil

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    src = home / "agents" / "local" / agent_name
    dst = home / "snapshots" / f"{agent_name}-{ts}"

    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
        logger.info("Snapshotted %s → %s", agent_name, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        logger.warning("No source dir to snapshot for %s", agent_name)

    return dst


# ---------------------------------------------------------------------------
# Audit-based log fallback
# ---------------------------------------------------------------------------


def audit_lines_for_agent(
    home: Path,
    deployment_id: str,
    agent_name: str,
    tail: int = 50,
) -> List[str]:
    """Extract audit log lines referencing a specific agent/deployment.

    Args:
        home: Agent home directory.
        deployment_id: Deployment ID to filter on.
        agent_name: Agent name to filter on.
        tail: Maximum lines to return.

    Returns:
        List of formatted audit log strings.
    """
    audit_path = home / "coordination" / "audit.log"
    if not audit_path.exists():
        return []

    matching: List[str] = []
    for raw in audit_path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(raw)
            if entry.get("deployment_id") == deployment_id and (
                entry.get("agent_name") in (agent_name, "ALL", None)
            ):
                ts = entry.get("ts", "")
                action = entry.get("action", "")
                matching.append(f"[{ts}] {action}: {json.dumps(entry)}")
        except (json.JSONDecodeError, KeyError):
            continue

    return matching[-tail:]


# ---------------------------------------------------------------------------
# Stub spec for rotation re-provisioning
# ---------------------------------------------------------------------------


def stub_spec() -> Any:
    """Return a minimal stub AgentSpec for rotation re-provisioning.

    Returns:
        A minimal AgentSpec with default values.
    """
    from .blueprints.schema import AgentRole, AgentSpec, ModelTier

    return AgentSpec(role=AgentRole.WORKER, model=ModelTier.FAST, skills=[])
