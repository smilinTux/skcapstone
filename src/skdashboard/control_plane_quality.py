"""Fail-closed data-quality projection for control-plane observations."""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Mapping

from .control_plane_adapters import SPECS
from .control_plane_metric_registry import TRUTH_STATES, registry_manifest

_ISSUE_STATES = frozenset(TRUTH_STATES - {"current", "not_applicable"})
_STATE_ORDER = {
    "unavailable": 0,
    "unreachable": 1,
    "unknown": 2,
    "partial": 3,
    "stale": 4,
    "not_applicable": 5,
    "current": 6,
}


def _coverage(item: Mapping[str, object]) -> dict:
    raw = item.get("coverage")
    expected = raw.get("expected") if isinstance(raw, dict) else None
    reporting = raw.get("reporting") if isinstance(raw, dict) else None
    percent = None
    if (
        isinstance(expected, int)
        and not isinstance(expected, bool)
        and expected > 0
        and isinstance(reporting, int)
        and not isinstance(reporting, bool)
        and 0 <= reporting <= expected
    ):
        percent = round(100 * reporting / expected, 1)
    return {"reporting": reporting, "expected": expected, "percent": percent}


def _safe_provenance(item: Mapping[str, object]) -> list[dict]:
    state = str(item["truth_state"])
    if state == "stale":
        return [{"code": "EVIDENCE_STALE", "message": "The last observation exceeded its freshness TTL"}]
    if state == "partial":
        return [{"code": "COVERAGE_PARTIAL", "message": "Only part of the declared population reported"}]
    if state == "unknown":
        return [{"code": "EVIDENCE_UNKNOWN", "message": "No usable observation is available"}]
    return [{"code": "EVIDENCE_UNAVAILABLE", "message": "The source did not provide usable evidence"}]


def _issue(item: Mapping[str, object]) -> dict:
    adapter_id = str(item["adapter_id"])
    errors = item.get("errors") if isinstance(item.get("errors"), list) else []
    provenance = [
        {"code": error.get("code"), "message": error.get("message")}
        for error in errors[:4]
        if isinstance(error, dict)
        and isinstance(error.get("code"), str)
        and isinstance(error.get("message"), str)
    ]
    visibility = item.get("visibility")
    if isinstance(visibility, dict) and visibility.get("reason"):
        provenance.append(
            {"code": "VISIBILITY", "message": str(visibility["reason"])[:256]}
        )
    watermark = item.get("watermark")
    return {
        "issue_id": f"quality:{adapter_id}",
        "truth_state": item["truth_state"],
        "owner": item["owner"],
        "source": {
            "adapter_id": adapter_id,
            "adapter_version": item["adapter_version"],
        },
        "watermark": dict(watermark) if isinstance(watermark, dict) else None,
        "last_observation": item.get("observed_at"),
        "coverage": _coverage(item),
        "safe_provenance": provenance or _safe_provenance(item),
        "safe_next_step": {
            "kind": "refresh_preview",
            "label": "Preview refresh",
            "preview_only": True,
            "dispatch_authorized": False,
        },
    }


def project_data_quality(observations: Iterable[Mapping[str, object]]) -> dict:
    """Summarize adapter truth without inventing values for missing evidence."""
    items = list(observations)
    declared = {spec.adapter_id for spec in SPECS}
    seen = [item.get("adapter_id") for item in items]
    if (
        len(items) != len(SPECS)
        or set(seen) != declared
        or len(seen) != len(set(seen))
        or any(item.get("truth_state") not in TRUTH_STATES for item in items)
    ):
        raise ValueError("quality projection requires one valid observation per adapter")

    state_counts = Counter(str(item["truth_state"]) for item in items)
    issues = [_issue(item) for item in items if item["truth_state"] in _ISSUE_STATES]
    observed_sources = sum(
        item["truth_state"] not in {"unavailable", "unreachable", "unknown"}
        and bool(item.get("observed_at"))
        and isinstance(item.get("watermark"), dict)
        and bool(item["watermark"].get("value"))
        for item in items
    )
    expected_sources = len(items)
    manifest = registry_manifest()
    overall = min((str(item["truth_state"]) for item in items), key=_STATE_ORDER.get)
    return {
        "projection_type": "data_quality",
        "schema_version": "1.1.0",
        "truth_state": overall,
        "state_counts": {state: state_counts.get(state, 0) for state in sorted(TRUTH_STATES)},
        "coverage": {
            "reporting": observed_sources,
            "expected": expected_sources,
            "percent": round(100 * observed_sources / expected_sources, 1),
            "population": "declared_sources",
        },
        "source_count": len(items),
        "issue_count": len(issues),
        "issues": issues,
        "metric_registry": {
            "registry_version": manifest["registry_version"],
            "registry_hash": manifest["registry_hash"],
            "definition_count": len(manifest["definition_hashes"]),
        },
        "actions": {
            "mode": "preview_only",
            "dispatch_authorized": False,
        },
    }
