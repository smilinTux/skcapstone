"""Protected read-only DORA, architecture, CMDB, capacity, and drift projection."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .control_plane_adapters import Reader, project_estate

MAX_CIS = 200
DORA_VERSION = "dora-2024-five-metrics"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: object, now: datetime) -> int | None:
    parsed = _parse(value)
    return max(0, int((now - parsed).total_seconds())) if parsed else None


def _metric(
    metric_id: str,
    label: str,
    *,
    value=None,
    unit: str,
    truth_state: str = "unknown",
    definition: str,
    definition_version: str,
    numerator=None,
    denominator=None,
    sample_size: int = 0,
    target=None,
    baseline=None,
    balancing_context: str,
    uncertainty: str,
    exclusions: list[str],
    evidence_refs: list[str],
) -> dict:
    return {
        "metric_id": metric_id,
        "label": label,
        "value": value,
        "unit": unit,
        "truth_state": truth_state,
        "definition": definition,
        "definition_version": definition_version,
        "numerator": numerator,
        "denominator": denominator,
        "sample_size": sample_size,
        "target": target,
        "baseline": baseline,
        "balancing_context": balancing_context,
        "uncertainty": uncertainty,
        "exclusions": exclusions,
        "evidence_refs": evidence_refs,
    }


def _unknown_metrics() -> list[dict]:
    missing_delivery = "No approved service-scoped deployment event source is configured."
    dora = [
        (
            "dora.deployment_frequency",
            "Deployment frequency",
            "Count of production deployments per service in the selected window.",
            "deployments_per_day",
            "Review change failure, rework, and service reliability beside frequency.",
        ),
        (
            "dora.lead_time_for_changes",
            "Lead time for changes",
            "Elapsed time from committed change to successful production deployment at service scope.",
            "hours",
            "Review release quality and rework beside lead time.",
        ),
        (
            "dora.change_failure_rate",
            "Change failure rate",
            "Percent of production deployments that cause degraded service or require remediation.",
            "percent",
            "Review deployment volume and failed-deployment recovery time.",
        ),
        (
            "dora.failed_deployment_recovery_time",
            "Failed deployment recovery time",
            "Elapsed time to restore service after a failed production deployment.",
            "hours",
            "Review incident severity and user-facing service impact.",
        ),
        (
            "dora.deployment_rework_rate",
            "Deployment rework rate",
            "Percent of deployed work spent correcting avoidable deployment defects.",
            "percent",
            "Review quality evidence and change scope beside rework.",
        ),
    ]
    metrics = [
        _metric(
            metric_id,
            label,
            unit=unit,
            definition=definition,
            definition_version=DORA_VERSION,
            balancing_context=balancing,
            uncertainty=missing_delivery,
            exclusions=[
                "Repository activity, card movement, and individual activity are not deployment evidence."
            ],
            evidence_refs=[],
        )
        for metric_id, label, definition, unit, balancing in dora
    ]
    metrics.extend(
        [
            _metric(
                "engineering.release_quality",
                "Release quality",
                unit="percent",
                definition="Approved service-scoped releases meeting their recorded quality gate.",
                definition_version="release-quality@1.0.0",
                balancing_context="Review deployment volume, change failure, and rework together.",
                uncertainty="No approved release quality gate results are configured.",
                exclusions=["Release counts alone do not establish quality."],
                evidence_refs=[],
            ),
            _metric(
                "architecture.adr_freshness",
                "ADR freshness",
                unit="percent",
                definition="Applicable architecture decisions reviewed within their approved review interval.",
                definition_version="adr-freshness@1.0.0",
                balancing_context="Review applicability, supersession, and exception evidence.",
                uncertainty="No approved ADR registry and review target are configured.",
                exclusions=["Repository file modification time is not an ADR review decision."],
                evidence_refs=[],
            ),
            _metric(
                "architecture.technical_debt_exposure",
                "Technical-debt exposure",
                unit="items",
                definition="Approved technical-debt records with current impact and remediation evidence.",
                definition_version="technical-debt-exposure@1.0.0",
                balancing_context="Review service impact, lifecycle risk, and capacity evidence.",
                uncertainty="No approved technical-debt registry is configured.",
                exclusions=[
                    "Drift, age, and unsupported tags are not silently relabeled as debt."
                ],
                evidence_refs=[],
            ),
        ]
    )
    return metrics


def _approved_aggregates(readers: Mapping[str, Reader] | None, now: datetime) -> dict[str, dict]:
    items = project_estate(readers or {}, now=now)
    return {
        item["adapter_id"]: item
        for item in items
        if item["adapter_id"] in {"skcapstone.service_release", "skperf.aggregate"}
    }


def _aggregate_metrics(items: dict[str, dict]) -> list[dict]:
    skperf = items["skperf.aggregate"]
    aggregate = skperf.get("aggregate") or {}
    available = skperf["truth_state"] in {"current", "stale", "partial"}
    evidence = [skperf["watermark"]["value"]] if skperf["watermark"]["value"] else []
    uncertainty = (
        "The approved aggregate does not expose its target or regression baseline identifier."
        if available
        else "No authorized approved SKPerf aggregate is configured."
    )
    return [
        _metric(
            "skperf.regressions",
            "Approved benchmark regressions",
            value=aggregate.get("regressions") if available else None,
            unit="regressions",
            truth_state=skperf["truth_state"] if available else "unknown",
            definition="Regression count from the approved SKPerf aggregate only.",
            definition_version="skperf-approved-aggregate@1.0.0",
            numerator=aggregate.get("regressions") if available else None,
            denominator=None,
            sample_size=skperf["coverage"].get("reporting") or 0,
            target=None,
            baseline=None,
            balancing_context="Review benchmark coverage, workload identity, and service impact.",
            uncertainty=uncertainty,
            exclusions=[
                "Raw corpus targets, benchmark paths, samples, and unapproved runs are excluded."
            ],
            evidence_refs=evidence,
        ),
        _metric(
            "architecture.capacity_pressure",
            "Approved aggregate capacity pressure",
            value=aggregate.get("capacity_pressure") if available else None,
            unit="ratio",
            truth_state=skperf["truth_state"] if available else "unknown",
            definition="Capacity pressure supplied by the approved SKPerf aggregate.",
            definition_version="skperf-approved-aggregate@1.0.0",
            numerator=None,
            denominator=None,
            sample_size=skperf["coverage"].get("reporting") or 0,
            target=None,
            baseline=None,
            balancing_context="Review saturation, latency, errors, and workload coverage together.",
            uncertainty=uncertainty,
            exclusions=["No host capacity or target is inferred from CMDB attributes."],
            evidence_refs=evidence,
        ),
    ]


def get_architecture_projection(
    home: Path,
    query: dict,
    *,
    aggregate_readers: Mapping[str, Reader] | None = None,
    now: datetime | None = None,
) -> dict:
    """Project bounded CMDB topology and approved aggregate evidence without writes."""
    from skcoord.cmdb import CMDBManager
    from skcoord.discovery import ci_observation_state

    from .dashboard_cmdb import _verified_run_artifacts

    instant = (now or _now()).astimezone(timezone.utc)
    manager = CMDBManager(Path(home).expanduser())
    all_cis = sorted(manager.list_cis(), key=lambda item: item.id)
    cis = all_cis[:MAX_CIS]
    ids = {item.id for item in cis}
    freshness = {item.id: ci_observation_state(item).value for item in cis}
    owners = sum(bool(item.owner) for item in cis)
    unsupported = [item for item in cis if "unsupported" in item.tags]
    lifecycle_risk = [
        item for item in cis if item.status == "retired" or "unsupported" in item.tags
    ]
    relationship_findings = manager.audit_relationships()
    artifacts = _verified_run_artifacts(Path(home).expanduser())
    latest = artifacts[0] if artifacts else {}
    latest_drift = (latest.get("drift") or {}).get("count")
    reconcile_at = latest.get("ended_at")
    cmdb_ref = (
        f"cmdb-fold:{hashlib.sha256('|'.join(item.id for item in all_cis).encode()).hexdigest()}"
    )

    nodes = []
    for ci in cis:
        graph = manager.impact_graph(ci.id, max_depth=8, max_nodes=200)
        dependents = graph.get("dependents", [])
        impacted_services = sorted(
            {item["id"] for item in dependents if item.get("ci_type") == "service"}
            | ({ci.id} if ci.ci_type == "service" else set())
        )
        observed_at = ci.attributes.get("observed_at") or ci.updated_at or ci.created_at
        nodes.append(
            {
                "ci_id": ci.id,
                "name": ci.name,
                "ci_type": ci.ci_type,
                "status": ci.status,
                "owner": ci.owner or None,
                "environment": ci.attributes.get("environment") or None,
                "node": ci.node or None,
                "freshness": freshness[ci.id],
                "observed_at": observed_at or None,
                "evidence_age_seconds": _age_seconds(observed_at, instant),
                "source_authority": ci.attributes.get("source_authority") or None,
                "scan_id": ci.attributes.get("scan_id") or None,
                "reconciliation_state": "recorded" if ci.attributes.get("scan_id") else "unknown",
                "unsupported": "unsupported" in ci.tags,
                "lifecycle_state": "retired" if ci.status == "retired" else "unknown",
                "blast_radius": {
                    "dependent_count": len(dependents),
                    "impacted_service_ids": impacted_services,
                    "cycles": graph.get("cycles", []),
                    "truncated": bool(graph.get("truncated")),
                },
                "evidence_refs": [f"cmdb:{ci.id}"],
            }
        )

    edges = [
        {
            "source_ci_id": ci.id,
            "target_ci_id": relationship.target,
            "relationship": relationship.rel_type,
            "authority": relationship.authority or None,
            "target_visible": relationship.target in ids,
            "evidence_refs": [
                f"cmdb:{ci.id}:relationship:{relationship.rel_type}:{relationship.target}"
            ],
        }
        for ci in cis
        for relationship in ci.relationships
    ][:1000]

    metrics = _unknown_metrics()
    metrics.extend(
        [
            _metric(
                "cmdb.owner_coverage",
                "Service and CI owner coverage",
                value=round(100 * owners / len(cis), 2) if cis else None,
                unit="percent",
                truth_state="current" if cis else "unknown",
                definition="Visible CIs with an explicit folded owner divided by visible CIs.",
                definition_version="cmdb-owner-coverage@1.0.0",
                numerator=owners if cis else None,
                denominator=len(cis) if cis else None,
                sample_size=len(cis),
                target=None,
                baseline=None,
                balancing_context="Review source coverage and policy visibility before acting on gaps.",
                uncertainty="No approved owner-coverage target is configured.",
                exclusions=[
                    "Profile metadata and discovery source are not authorization or ownership."
                ],
                evidence_refs=[cmdb_ref] if cis else [],
            ),
            _metric(
                "cmdb.relationship_integrity_findings",
                "Relationship integrity findings",
                value=len(relationship_findings) if all_cis else None,
                unit="findings",
                truth_state="current" if all_cis else "unknown",
                definition="Deterministic dangling, self, unknown, and invalid-target relationship findings.",
                definition_version="cmdb-relationship-audit@1.0.0",
                numerator=len(relationship_findings) if all_cis else None,
                denominator=sum(len(item.relationships) for item in all_cis) if all_cis else None,
                sample_size=len(all_cis),
                target=0,
                baseline=None,
                balancing_context="Review reconciliation coverage and source authority.",
                uncertainty="The visible topology is capped at 200 CIs, while the audit covers the full folded CMDB.",
                exclusions=["No relationship is inferred from names, ports, or co-location."],
                evidence_refs=[cmdb_ref] if all_cis else [],
            ),
            _metric(
                "cmdb.configuration_drift",
                "Latest verified configuration drift",
                value=latest_drift if isinstance(latest_drift, int) else None,
                unit="findings",
                truth_state="current" if isinstance(latest_drift, int) else "unknown",
                definition="Drift count from the latest checksum-verified reconciliation artifact.",
                definition_version="cmdb-verified-reconciliation@1.0.0",
                numerator=latest_drift if isinstance(latest_drift, int) else None,
                denominator=None,
                sample_size=int(
                    (latest.get("completeness") or {}).get("collectors_complete") or 0
                ),
                target=0,
                baseline=None,
                balancing_context="Review collector completeness and unavailable targets.",
                uncertainty="No comparison baseline is inferred when the artifact omits one.",
                exclusions=["Live discovery is not executed by this workspace."],
                evidence_refs=[f"cmdb-reconcile:{latest.get('scan_id')}"]
                if latest.get("scan_id")
                else [],
            ),
            _metric(
                "architecture.unsupported_components",
                "Explicit unsupported components",
                value=len(unsupported) if cis else None,
                unit="components",
                truth_state="current" if cis else "unknown",
                definition="Visible folded CIs carrying the exact explicit unsupported tag.",
                definition_version="cmdb-explicit-unsupported@1.0.0",
                numerator=len(unsupported) if cis else None,
                denominator=len(cis) if cis else None,
                sample_size=len(cis),
                target=0,
                baseline=None,
                balancing_context="Review tag authority, lifecycle owner, and service impact.",
                uncertainty="Components without an explicit tag remain unclassified, not supported.",
                exclusions=[
                    "Age, version strings, and model inference do not create unsupported status."
                ],
                evidence_refs=[f"cmdb:{item.id}" for item in unsupported],
            ),
            _metric(
                "architecture.lifecycle_risk",
                "Explicit lifecycle risk",
                value=len(lifecycle_risk) if cis else None,
                unit="components",
                truth_state="current" if cis else "unknown",
                definition="Visible CIs explicitly retired or tagged unsupported.",
                definition_version="cmdb-explicit-lifecycle-risk@1.0.0",
                numerator=len(lifecycle_risk) if cis else None,
                denominator=len(cis) if cis else None,
                sample_size=len(cis),
                target=0,
                baseline=None,
                balancing_context="Review owner, impacted services, and replacement decision evidence.",
                uncertainty="No approved lifecycle date or vendor-support registry is configured.",
                exclusions=["Version age and repository age are not lifecycle decisions."],
                evidence_refs=[f"cmdb:{item.id}" for item in lifecycle_risk],
            ),
        ]
    )
    aggregate_items = _approved_aggregates(aggregate_readers, instant)
    metrics.extend(_aggregate_metrics(aggregate_items))

    exceptions = []
    for node in nodes:
        reasons = []
        if node["status"] in {"degraded", "down", "retired"}:
            reasons.append(f"status:{node['status']}")
        if node["freshness"] != "fresh":
            reasons.append(f"freshness:{node['freshness']}")
        if node["owner"] is None:
            reasons.append("owner:unknown")
        if node["unsupported"]:
            reasons.append("lifecycle:unsupported")
        if reasons:
            exceptions.append(
                {
                    "exception_id": f"architecture:{node['ci_id']}",
                    "ci_id": node["ci_id"],
                    "service_ids": node["blast_radius"]["impacted_service_ids"],
                    "reasons": reasons,
                    "decision_refs": [],
                    "decision_state": "unknown",
                    "evidence_refs": node["evidence_refs"],
                }
            )
    exceptions.extend(
        {
            "exception_id": f"relationship:{index}",
            "ci_id": finding.get("source"),
            "service_ids": [],
            "reasons": [finding["kind"]],
            "decision_refs": [],
            "decision_state": "unknown",
            "evidence_refs": [cmdb_ref],
        }
        for index, finding in enumerate(relationship_findings)
    )

    source = {
        "nodes": nodes,
        "edges": edges,
        "metrics": metrics,
        "reconciliation": {"scan_id": latest.get("scan_id"), "ended_at": reconcile_at},
    }
    projection_hash = hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    states = {metric["truth_state"] for metric in metrics}
    truth_state = "unknown" if not all_cis else ("current" if states == {"current"} else "partial")
    return {
        "schema_version": "1.0.0",
        "projection_id": "architecture-latest",
        "projection_hash": f"sha256:{projection_hash}",
        "source_owner": "CMDB, approved service-release owners, and SKPerf approved aggregates",
        "scope": dict(query),
        "observed_at": reconcile_at,
        "projected_at": instant.isoformat(),
        "truth_state": truth_state,
        "visibility": {"state": "visible", "authorization": "authorized"},
        "source_watermarks": [
            {"source": "cmdb.configuration", "value": cmdb_ref},
            *[
                {"source": item["adapter_id"], "value": item["watermark"]["value"]}
                for item in aggregate_items.values()
                if item["watermark"]["value"]
            ],
        ],
        "metrics": metrics,
        "topology": {
            "nodes": nodes,
            "edges": edges,
            "total_cis": len(all_cis),
            "visible_cis": len(nodes),
            "truncated": len(all_cis) > MAX_CIS or len(edges) == 1000,
        },
        "exceptions": exceptions[:400],
        "reconciliation": {
            "scan_id": latest.get("scan_id"),
            "observed_at": reconcile_at,
            "complete": (latest.get("completeness") or {}).get("complete"),
            "relationship_findings": relationship_findings,
        },
        "individual_ranking_prohibited": True,
        "errors": []
        if all_cis
        else [{"code": "CMDB_UNKNOWN", "message": "No folded CMDB evidence is available."}],
    }


class ArchitectureProjectionProvider:
    """Read owner evidence only while the exact CapAuth decision remains current."""

    def __init__(self, aggregate_readers: Mapping[str, Reader] | None = None):
        self.aggregate_readers = aggregate_readers

    def read(self, context, query, home, *, currentness_verifier):
        if currentness_verifier.check_before_owner_read(context).value != "allow":
            raise PermissionError("control-plane decision is not current")
        projection = get_architecture_projection(
            home,
            query,
            aggregate_readers=self.aggregate_readers,
        )
        if currentness_verifier.check_after_owner_read(context).value != "allow":
            raise PermissionError("control-plane decision expired during owner read")
        return projection
