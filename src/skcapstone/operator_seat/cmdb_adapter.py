"""CMDB operator facet for Atlas.

Atlas observes the CMDB through verified reconcile artifacts and the append-only
store audit.  Physical reconcile is deliberately not wired into the autonomous
HONOR catalog: the safe shadow action and the apply action remain explicit
operator-facet verbs until the rollout gate and human ratification are complete.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from ..fleet import actuation, store

PROBLEM_WHEN_TRUE = frozenset()
_MAX_ARTIFACT_AGE = timedelta(hours=4)
_SHADOW_UNIT = "skcapstone-cmdb-reconcile-shadow.service"
_APPLY_UNIT = "skcapstone-cmdb-reconcile-network.service"

_ACTIONS = [
    {
        "name": "run-cmdb-shadow",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "run one credentialed, write-free CMDB network reconcile",
        "kedb_refs": ["ke-cmdb-reconcile-stale"],
    },
    {
        "name": "apply-cmdb-reconcile",
        "standard": False,
        "reversible": False,
        "blast_radius": "medium",
        "runbook": "apply CMDB reconciliation after the three-shadow-run gate",
        "kedb_refs": ["ke-cmdb-reconcile-stale"],
    },
]


def cmdb_explain() -> dict:
    """Return the CMDB operator contract."""
    return {
        "kinds": ["cmdb"],
        "conditions": ["CmdbReconcileFresh", "CmdbLastScanComplete", "CmdbAuditClean"],
        "actions": list(_ACTIONS),
    }


def _verified_latest_artifact(home: Path) -> dict | None:
    """Return the newest checksum-verified artifact, or ``None``.

    Invalid, missing, or unchecksummed artifacts are not trusted as operational
    evidence.  This reader never modifies the artifact directory.
    """
    directory = home / "cmdb" / "reconcile-runs"
    candidates = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime)
    for path in reversed(candidates):
        try:
            payload = path.read_bytes()
            expected = path.with_suffix(".sha256").read_text().strip().split()[0]
            if hashlib.sha256(payload).hexdigest() != expected:
                continue
            value = json.loads(payload)
            if isinstance(value, dict):
                return value
        except (OSError, ValueError, IndexError, json.JSONDecodeError):
            continue
    return None


def _verified_artifacts(home: Path) -> list[dict]:
    """Return checksum-valid run artifacts, newest first."""
    directory = home / "cmdb" / "reconcile-runs"
    verified: list[tuple[float, dict]] = []
    for path in directory.glob("*.json"):
        try:
            payload = path.read_bytes()
            expected = path.with_suffix(".sha256").read_text().strip().split()[0]
            if hashlib.sha256(payload).hexdigest() != expected:
                continue
            value = json.loads(payload)
            if isinstance(value, dict):
                verified.append((path.stat().st_mtime, value))
        except (OSError, ValueError, IndexError, json.JSONDecodeError):
            continue
    return [value for _, value in sorted(verified, key=lambda item: item[0], reverse=True)]


def _apply_gate(
    home: Path,
    change_id: str,
    *,
    itil_factory=None,
    manager_factory=None,
) -> str | None:
    """Return a refusal reason unless the governed network-apply gate passes."""
    if not change_id.startswith("chg-"):
        return "a valid --change-id is required"
    if itil_factory is None:
        from skcoord.itil import ITILManager

        itil_factory = ITILManager
    itil = itil_factory(home)
    change = next((item for item in itil.list_changes() if item.id == change_id), None)
    if change is None:
        return f"unknown ITIL change: {change_id}"
    if change.status.value not in {"approved", "scheduled"}:
        return f"ITIL change is {change.status.value}, not approved or scheduled"
    approvals = [
        vote
        for vote in itil.get_cab_votes(change_id)
        if vote.decision.value == "approved"
        and (
            vote.agent == "human"
            or (
                getattr(vote, "subject_role", "") in {"owner", "approver"}
                and bool(getattr(vote, "subject_fingerprint", ""))
                and bool(getattr(vote, "authorization_id", ""))
            )
        )
    ]
    if not approvals:
        return "independent authenticated human CAB approval is required"

    artifacts = _verified_artifacts(home)
    complete = [item for item in artifacts if item.get("completeness", {}).get("complete") is True]
    if len(complete) < 3:
        return "three checksum-valid complete shadow artifacts are required"
    latest = complete[:3]
    scopes = {item.get("scope_fingerprint") for item in latest}
    scans = {item.get("scan_id") for item in latest}
    if None in scopes or "" in scopes or len(scopes) != 1:
        return "three complete shadow artifacts must have one non-empty scope"
    if None in scans or "" in scans or len(scans) != 3:
        return "three distinct complete shadow runs are required"

    try:
        if manager_factory is None:
            from skcoord.cmdb import CMDBManager

            manager_factory = CMDBManager
        if manager_factory(home).audit_relationships():
            return "CMDB relationship audit is not clean"
    except Exception:
        return "CMDB relationship audit could not be verified"
    return None


def _parse_time(value: object) -> datetime | None:
    """Parse one UTC timestamp without accepting naive values."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def observe(paths=None, now_iso: str | None = None, *, manager_factory=None) -> dict:
    """Observe reconcile freshness, completeness, and store integrity.

    Missing evidence reports ``Unknown`` rather than inventing health.  Atlas
    files Unknown conditions as stale, keeping the gap visible without turning
    an unreadable probe into a false positive.
    """
    home = Path.home() / ".skcapstone"
    artifact = _verified_latest_artifact(home)
    freshness = completeness = "Unknown"
    if artifact is not None:
        ended = _parse_time(artifact.get("ended_at"))
        now = _parse_time(now_iso) if now_iso else datetime.now(timezone.utc)
        if ended is not None and now is not None:
            freshness = "True" if now - ended <= _MAX_ARTIFACT_AGE else "False"
        completeness = (
            "True" if artifact.get("completeness", {}).get("complete") is True else "False"
        )

    audit = "Unknown"
    try:
        if manager_factory is None:
            from skcoord.cmdb import CMDBManager

            manager_factory = CMDBManager
        findings = manager_factory(home).audit_relationships()
        audit = "True" if not findings else "False"
    except Exception:
        audit = "Unknown"

    return {
        "conditions": [
            {"type": "CmdbReconcileFresh", "status": freshness},
            {"type": "CmdbLastScanComplete", "status": completeness},
            {"type": "CmdbAuditClean", "status": audit},
        ]
    }


def cmdb_act(
    paths,
    action: str,
    *,
    change_id: str | None = None,
    runner: Callable | None = None,
    itil_factory=None,
    manager_factory=None,
    before_start: Callable[[], None] | None = None,
) -> dict:
    """Start one reviewed CMDB oneshot; freeze always wins.

    The apply action exists for human-governed execution but is intentionally
    non-standard and irreversible, ensuring Atlas policy classifies it MAJOR.
    """
    if store.is_frozen(paths):
        return {"performed": False, "reason": "frozen", "action": action}
    units = {"run-cmdb-shadow": _SHADOW_UNIT, "apply-cmdb-reconcile": _APPLY_UNIT}
    if action not in units:
        raise ValueError(f"unknown CMDB action: {action!r}")
    if action == "apply-cmdb-reconcile":
        if not change_id:
            return {"performed": False, "reason": "--change-id is required", "action": action}
        home = paths.root.parent if paths.root.name == "fleet" else paths.root
        refusal = _apply_gate(
            home,
            change_id,
            itil_factory=itil_factory,
            manager_factory=manager_factory,
        )
        if refusal:
            return {"performed": False, "reason": refusal, "action": action}
    if before_start is not None:
        before_start()
    # Close the check/use window: an operator may freeze while evidence is read.
    if store.is_frozen(paths):
        return {"performed": False, "reason": "frozen", "action": action}
    run = runner or actuation.default_runner
    ok = run(["systemctl", "--user", "start", units[action]])
    if hasattr(ok, "returncode"):
        ok = ok.returncode == 0
    return {"performed": bool(ok), "action": action, "unit": units[action]}


__all__ = ["PROBLEM_WHEN_TRUE", "cmdb_act", "cmdb_explain", "observe"]
