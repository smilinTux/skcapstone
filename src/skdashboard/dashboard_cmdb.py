"""Dashboard CMDB view: Configuration Items by type + health, CI detail + impact."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _mgr(home: Path):
    from skcoord.cmdb import CMDBManager

    return CMDBManager(Path(home).expanduser())


def get_overview(home: Path) -> dict:
    """CIs grouped by type, with health counts."""
    from collections import Counter

    mgr = _mgr(home)
    cis = mgr.list_cis()
    groups: dict[str, list] = {}
    for ci in cis:
        groups.setdefault(ci.ci_type, []).append(
            {
                "id": ci.id,
                "name": ci.name,
                "status": ci.status,
                "node": ci.node,
                "rels": len(ci.relationships),
            }
        )
    health = Counter(ci.status for ci in cis)
    for lst in groups.values():
        lst.sort(key=lambda c: c["name"])
    return {
        "total": len(cis),
        "health": dict(health),
        "types": [{"type": t, "items": groups[t]} for t in sorted(groups)],
    }


def get_ci(home: Path, ci_id: str) -> dict:
    """A CI's full detail: attributes, relationships, dependents, open incidents."""
    mgr = _mgr(home)
    impact = mgr.impact_analysis(ci_id)
    if "error" in impact:
        return impact
    ci = impact["ci"]
    # resolve relationship target names for display
    rels = []
    for r in ci.get("relationships", []):
        target = mgr.get_ci(r["target"])
        rels.append(
            {
                "rel_type": r["rel_type"],
                "target": r["target"],
                "target_name": target.name if target else r["target"],
            }
        )
    return {
        "ci": ci,
        "relationships": rels,
        "dependents": impact["dependents"],
        "open_incidents": impact["open_incidents"],
    }


def search(home: Path, query: str, limit: int = 50) -> dict[str, Any]:
    """Search the canonical CMDB fold without creating a second write path."""
    needle = query.strip().casefold()[:200]
    try:
        bounded_limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        bounded_limit = 50
    if not needle:
        return {"query": "", "total": 0, "items": []}

    matches = []
    for ci in _mgr(home).list_cis():
        document = " ".join(
            (
                ci.id,
                ci.name,
                ci.ci_type,
                ci.status,
                ci.description,
                ci.owner,
                ci.node,
                " ".join(ci.tags),
                " ".join(f"{key} {value}" for key, value in ci.attributes.items()),
            )
        ).casefold()
        if needle in document:
            matches.append(
                {
                    "id": ci.id,
                    "name": ci.name,
                    "type": ci.ci_type,
                    "status": ci.status,
                    "node": ci.node,
                    "description": ci.description,
                }
            )
    matches.sort(key=lambda item: (item["name"].casefold(), item["id"]))
    return {
        "query": query.strip()[:200],
        "total": len(matches),
        "items": matches[:bounded_limit],
    }


def _local_reconcile(home: Path, *, apply: bool) -> dict[str, Any]:
    """Plan or apply the declared-plus-local discovery scope."""
    from skcoord.discovery import LocalRunner, reconcile, scan

    root = Path(home).expanduser()
    discovered = scan(root, runners=[LocalRunner()], include_declared=True)
    report = reconcile(_mgr(root), discovered, apply=apply, scan_complete=True)
    result = report.as_dict()
    for key in ("relationships", "validation_failures", "secret_redaction_findings"):
        result.setdefault(key, [])
        result.setdefault("counts", {}).setdefault(key, len(result[key]))
    result["scope"] = "declared+local"
    return result


def plan(home: Path) -> dict[str, Any]:
    """Return a write-free local discovery reconciliation plan."""
    return _local_reconcile(home, apply=False)


def apply(home: Path) -> dict[str, Any]:
    """Apply validated local discovery through the canonical event store."""
    result = _local_reconcile(home, apply=True)
    if result.get("validation_failures"):
        result["applied"] = False
    return result


def _verified_run_artifacts(home: Path) -> list[dict]:
    """Compatibility reader for a consumer deployed before the skcoord release."""
    try:
        from skcoord.cmdb_reconcile import read_verified_run_artifacts
    except ImportError:
        read_verified_run_artifacts = None
    if read_verified_run_artifacts is not None:
        return read_verified_run_artifacts(home)

    verified: list[tuple[float, dict]] = []
    for path in (home / "cmdb" / "reconcile-runs").glob("*.json"):
        try:
            payload = path.read_bytes()
            expected = path.with_suffix(".sha256").read_text().split()[0]
            value = json.loads(payload)
            if hashlib.sha256(payload).hexdigest() == expected and isinstance(value, dict):
                verified.append((path.stat().st_mtime, value))
        except (OSError, ValueError, IndexError, json.JSONDecodeError):
            continue
    return [value for _, value in sorted(verified, key=lambda item: item[0], reverse=True)]


def status(home: Path) -> dict[str, Any]:
    """Return inventory and checksum-verified reconcile status."""
    from skcoord.cmdb_reconcile import operator_summary

    root = Path(home).expanduser()
    mgr = _mgr(root)
    result = operator_summary(
        _verified_run_artifacts(root),
        datetime.now(timezone.utc),
        timedelta(hours=4),
    )
    cis = mgr.list_cis()
    result["inventory"] = {
        "total": len(cis),
        "discovered": sum("discovered" in (ci.tags or []) for ci in cis),
        "retired": sum(ci.status == "retired" for ci in cis),
    }
    findings = mgr.audit_relationships()
    result["relationship_audit"] = {"clean": not findings, "findings": findings}
    return result


def drift(home: Path) -> dict[str, Any]:
    """Return declared-versus-local drift without changing the store."""
    from skcoord.discovery import LocalRunner, scan
    from skcoord.discovery import drift as find_drift

    root = Path(home).expanduser()
    discovered = scan(root, runners=[LocalRunner()], include_declared=True)
    findings = [item.as_dict() for item in find_drift(discovered, _mgr(root))]
    return {"scope": "declared+local", "count": len(findings), "findings": findings}


def seed(home: Path) -> dict:
    """Versioned compatibility alias for clients predating ``/apply``."""
    result = apply(home)
    result.update(
        {
            "schema": "skdashboard.cmdb.compat-seed/v1",
            "deprecated": True,
            "cis": len(_mgr(home).list_cis()),
        }
    )
    return result
