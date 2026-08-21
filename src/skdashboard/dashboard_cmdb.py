"""Dashboard CMDB view: Configuration Items by type + health, CI detail + impact."""

from __future__ import annotations

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


def seed(home: Path) -> dict:
    return _mgr(home).seed_from_inventory()
