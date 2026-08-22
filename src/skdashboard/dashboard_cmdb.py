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


def _artifacts(home: Path, limit: int = 10) -> list[dict[str, Any]]:
    return _verified_run_artifacts(Path(home).expanduser())[:limit]


def _coverage(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    latest = artifacts[0] if artifacts else {}
    completeness = latest.get("completeness") or {}
    targets = (latest.get("collector_health") or {}).get("targets") or []
    expected = int(completeness.get("collectors_expected") or 0)
    complete = int(completeness.get("collectors_complete") or 0)
    nodes = []
    for target in targets:
        node_expected = int(target.get("expected_collectors") or 0)
        node_complete = int(target.get("completed_collectors") or 0)
        nodes.append(
            {
                "node": str(target.get("host") or "unknown"),
                "complete": bool(target.get("complete")),
                "coverage_percent": round(100 * node_complete / node_expected, 1)
                if node_expected
                else 0.0,
                "collectors_expected": node_expected,
                "collectors_complete": node_complete,
                "collectors": target.get("coverage") or [],
                "failures": target.get("failures") or [],
                "provenance": target.get("provenance") or [],
                "findings": int(target.get("findings") or 0),
            }
        )
    return {
        "scan_id": latest.get("scan_id"),
        "coverage_percent": round(100 * complete / expected, 1) if expected else 0.0,
        "collectors_expected": expected,
        "collectors_complete": complete,
        "collectors_unavailable": int(completeness.get("collectors_unavailable") or 0),
        "nodes": sorted(nodes, key=lambda item: item["node"].casefold()),
    }


def _history(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "scan_id": item.get("scan_id"),
            "ended_at": item.get("ended_at"),
            "complete": bool((item.get("completeness") or {}).get("complete")),
            "applied": bool(item.get("applied")),
            "drift": (item.get("drift") or {}).get("count", 0),
            "duration_seconds": item.get("duration_seconds"),
        }
        for item in artifacts
    ]


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
    from skcoord.discovery import ci_observation_state

    freshness = Counter(ci_observation_state(ci).value for ci in cis)
    artifacts = _artifacts(home)
    successful = next(
        (item for item in artifacts if (item.get("completeness") or {}).get("complete")), None
    )
    for lst in groups.values():
        lst.sort(key=lambda c: c["name"])
    return {
        "total": len(cis),
        "health": dict(health),
        "evidence_health": {
            "fresh": freshness["fresh"],
            "stale": freshness["stale"],
            "unknown": freshness["unknown"],
            "unreachable": sum(
                bool((target.get("failures") or []))
                for target in ((artifacts[0].get("collector_health") or {}).get("targets") or [])
            )
            if artifacts
            else 0,
        },
        "coverage": _coverage(artifacts),
        "last_successful_reconciliation": (successful or {}).get("ended_at"),
        "reconciliation_history": _history(artifacts),
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
    relationship_groups = {
        "runs_on": [],
        "hosts": [],
        "depends_on": [],
        "connects_to": [],
        "other": [],
    }
    for r in ci.get("relationships", []):
        target = mgr.get_ci(r["target"])
        relationship = {
            "rel_type": r["rel_type"],
            "target": r["target"],
            "target_name": target.name if target else r["target"],
            "target_type": target.ci_type if target else "unknown",
            "authority": r.get("authority", ""),
        }
        rels.append(relationship)
        relationship_groups.get(r["rel_type"], relationship_groups["other"]).append(relationship)
    events = mgr.migration_preview(ci_id)["events"]
    health_history = [
        {
            "at": event.get("ts"),
            "status": event.get("status"),
            "note": event.get("note", ""),
            "writer": event.get("writer", ""),
        }
        for event in events
        if event.get("action") == "status"
    ][-50:]
    attrs = ci.get("attributes") or {}
    endpoint_keys = ("endpoint", "address", "url", "uri", "port", "socket", "bind")
    endpoints = {
        key: value
        for key, value in attrs.items()
        if any(fragment in key.casefold() for fragment in endpoint_keys)
    }
    provenance = {
        "source": attrs.get("source_authority", "unknown"),
        "scan_id": attrs.get("scan_id"),
        "lifecycle_scope": attrs.get("lifecycle_scope"),
        "canonical_name": attrs.get("canonical_name"),
        "aliases": attrs.get("aliases") or [],
        "tag_authorities": getattr(mgr.get_ci(ci_id), "tag_authorities", {}),
        "event_writers": sorted(
            {str(event.get("writer")) for event in events if event.get("writer")}
        ),
    }
    return {
        "ci": ci,
        "relationships": rels,
        "relationship_groups": relationship_groups,
        "dependents": impact["dependents"],
        "open_incidents": impact["open_incidents"],
        "linked_itil": impact["open_incidents"],
        "owner": ci.get("owner") or "unassigned",
        "endpoints": endpoints,
        "provenance": provenance,
        "last_seen": attrs.get("observed_at") or ci.get("updated_at") or ci.get("created_at"),
        "health_history": health_history,
        "event_history": events[-100:],
    }


def search(
    home: Path,
    query: str,
    limit: int = 50,
    *,
    ci_type: str = "",
    node: str = "",
    status: str = "",
    owner: str = "",
    tag: str = "",
    staleness: str = "",
    source: str = "",
) -> dict[str, Any]:
    """Search the canonical CMDB fold without creating a second write path."""
    needle = query.strip().casefold()[:200]
    try:
        bounded_limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        bounded_limit = 50
    filters = {
        "type": ci_type.strip()[:100],
        "node": node.strip()[:100],
        "status": status.strip()[:100],
        "owner": owner.strip()[:100],
        "tag": tag.strip()[:100],
        "staleness": staleness.strip()[:100],
        "source": source.strip()[:100],
    }
    if not needle and not any(filters.values()):
        return {"query": "", "total": 0, "items": []}

    from skcoord.discovery import ci_observation_state

    matches = []
    for ci in _mgr(home).list_cis():
        observed_state = ci_observation_state(ci).value
        source_authority = str(ci.attributes.get("source_authority", ""))
        checks = (
            (filters["type"], ci.ci_type),
            (filters["node"], ci.node),
            (filters["status"], ci.status),
            (filters["owner"], ci.owner),
            (filters["staleness"], observed_state),
            (filters["source"], source_authority),
        )
        if any(wanted.casefold() != actual.casefold() for wanted, actual in checks if wanted):
            continue
        if filters["tag"] and filters["tag"].casefold() not in {
            item.casefold() for item in ci.tags
        }:
            continue
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
        if not needle or needle in document:
            matches.append(
                {
                    "id": ci.id,
                    "name": ci.name,
                    "type": ci.ci_type,
                    "status": ci.status,
                    "node": ci.node,
                    "description": ci.description,
                    "owner": ci.owner,
                    "tags": ci.tags,
                    "staleness": observed_state,
                    "source": source_authority,
                }
            )
    matches.sort(key=lambda item: (item["name"].casefold(), item["id"]))
    return {
        "query": query.strip()[:200],
        "filters": filters,
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


def plan(home: Path, authorization: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a write-free local discovery reconciliation plan."""
    result = _local_reconcile(home, apply=False)
    result.update(
        {
            "preview": True,
            "execution_state": "not_executed",
            "authorization": authorization
            or {"evaluated": False, "authorized": False, "reason": "not evaluated"},
        }
    )
    return result


def apply(home: Path, authorization: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply validated local discovery through the canonical event store."""
    result = _local_reconcile(home, apply=True)
    if result.get("validation_failures"):
        result["applied"] = False
    result.update(
        {
            "preview": False,
            "execution_state": "applied" if result.get("applied") else "refused",
            "authorization": authorization
            or {"evaluated": False, "authorized": False, "reason": "not evaluated"},
        }
    )
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
