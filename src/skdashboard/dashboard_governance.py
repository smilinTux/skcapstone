"""Protected read-only governance, lineage, policy, and data-quality projection."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .control_plane_adapters import Reader, default_readers, project_estate
from .control_plane_metric_registry import REGISTRY, REGISTRY_VERSION, registry_manifest
from .control_plane_quality import project_data_quality

MAX_CARDS = 2_000
MAX_FINDINGS = 400
MAX_HISTORY = 200
_HISTORY_ACTIONS = frozenset({"amend_criteria", "describe"})
_HISTORY_LINKS = frozenset({"correction", "superseded_by", "supersedes"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _preview(kind: str, target_id: str) -> dict:
    return {
        "kind": kind,
        "target_id": target_id,
        "preview_only": True,
        "dispatch_authorized": False,
        "approval_state": "not_requested",
    }


def _finding(
    category: str,
    target_id: str,
    *,
    owner: str | None,
    severity: str,
    due_state: str = "unknown",
    truth_state: str = "current",
    evidence_refs: list[str],
    safe_detail: str,
    preview_kind: str,
) -> dict:
    return {
        "finding_id": f"governance:{category}:{target_id}",
        "category": category,
        "target_id": target_id,
        "owner": owner,
        "severity": severity,
        "due_state": due_state,
        "truth_state": truth_state,
        "safe_detail": safe_detail,
        "evidence_refs": evidence_refs,
        "remediation_preview": _preview(preview_kind, target_id),
    }


def _source_lineage(observations: list[dict]) -> list[dict]:
    return [
        {
            "adapter_id": item["adapter_id"],
            "adapter_version": item["adapter_version"],
            "authoritative_owner": item["owner"],
            "population": item["population"],
            "classification": item["classification"],
            "truth_state": item["truth_state"],
            "visibility": item["visibility"],
            "observed_at": item["observed_at"],
            "projected_at": item["projected_at"],
            "ttl_seconds": item["ttl_seconds"],
            "age_seconds": item["age_seconds"],
            "coverage": item["coverage"],
            "watermark": item["watermark"],
            "safe_errors": [
                {"code": value["code"], "message": value["message"]}
                for value in item["errors"][:4]
            ],
        }
        for item in observations
    ]


def _metric_lineage(observations: list[dict], decision_ref: str | None) -> list[dict]:
    by_adapter = {item["adapter_id"]: item for item in observations}
    output = []
    for (_metric_id, _version), definition in sorted(REGISTRY.items()):
        source = by_adapter[definition.adapter_id]
        output.append(
            {
                "metric_id": definition.metric_id,
                "definition_version": definition.definition_version,
                "definition_hash": definition.definition_hash,
                "registry_version": REGISTRY_VERSION,
                "family": definition.family,
                "label": definition.label,
                "authoritative_owner": definition.source_owner,
                "adapter_id": definition.adapter_id,
                "classification": definition.classification,
                "classification_policy_decision_ref": (
                    decision_ref
                    if definition.classification in {"confidential", "restricted"}
                    else None
                ),
                "calculation": {
                    "method": definition.method,
                    "expression": definition.expression,
                    "inputs": list(definition.calculation_inputs),
                    "calculation_ref": (
                        f"registry:{REGISTRY_VERSION}:{definition.metric_id}"
                        f"@{definition.definition_version}"
                    ),
                },
                "scope_dimensions": list(definition.scope_dimensions),
                "grain": definition.grain,
                "target": definition.target,
                "source_truth_state": source["truth_state"],
                "source_watermark": source["watermark"],
                "human_review": {
                    "state": "unknown",
                    "review_ref": None,
                    "reason": "No metric-specific human review record is in the registry contract.",
                },
                "history": {
                    "state": "current_definition_recorded",
                    "supersedes": [],
                    "superseded_by": None,
                },
            }
        )
    return output


def _safe_history(store, cards: list) -> list[dict]:
    history = []
    read_events = getattr(store, "_read_events", None)
    if not callable(read_events):
        return history
    for card in cards:
        for event in read_events(card.id):
            action = event.get("action")
            link_key = event.get("link_key")
            if action not in _HISTORY_ACTIONS and not (
                action == "link" and link_key in _HISTORY_LINKS
            ):
                continue
            event_id = event.get("event_id")
            writer = event.get("writer")
            timestamp = event.get("ts")
            if not all(
                isinstance(value, str) and value for value in (event_id, writer, timestamp)
            ):
                continue
            history.append(
                {
                    "record_id": event_id,
                    "target_id": card.id,
                    "kind": link_key if action == "link" else "correction",
                    "action": action,
                    "attributed_to": writer,
                    "recorded_at": timestamp,
                    "append_only_ref": f"skcoord.card_store:{card.id}:event:{event_id}",
                }
            )
    return sorted(history, key=lambda item: (item["recorded_at"], item["record_id"]))[
        -MAX_HISTORY:
    ]


def _card_findings(cards: list, all_ids: set[str]) -> tuple[list[dict], dict[str, dict]]:
    findings = []
    missing_criteria = [card for card in cards if not card.acceptance_criteria]
    orphan_edges = sorted(
        (card, dependency)
        for card in cards
        for dependency in card.dependencies
        if dependency not in all_ids
    )
    active_claims = [card for card in cards if card.owner]
    claim_ttl = [card for card in active_claims if not _iso(card.meta.get("claim_expires_at"))]
    for card in missing_criteria:
        findings.append(
            _finding(
                "missing_criteria",
                card.id,
                owner=card.owner or "SKCapstone coordination",
                severity="medium",
                evidence_refs=[f"skcoord.card_store:{card.id}"],
                safe_detail="The folded card has no acceptance criteria.",
                preview_kind="criteria_amendment_preview",
            )
        )
    for card, dependency in orphan_edges:
        findings.append(
            _finding(
                "orphan_dependency",
                f"{card.id}:{dependency}",
                owner=card.owner or "SKCapstone coordination",
                severity="high",
                evidence_refs=[f"skcoord.card_store:{card.id}", f"missing:{dependency}"],
                safe_detail="A folded dependency identifier is absent from the current CardStore population.",
                preview_kind="dependency_reconciliation_preview",
            )
        )
    for card in claim_ttl:
        findings.append(
            _finding(
                "claim_ttl",
                card.id,
                owner=card.owner,
                severity="medium",
                truth_state="unknown",
                evidence_refs=[f"skcoord.card_store:{card.id}"],
                safe_detail="The active folded claim has no policy-readable expiry evidence.",
                preview_kind="claim_review_preview",
            )
        )
    summary = {
        "missing_criteria": {"count": len(missing_criteria), "truth_state": "current"},
        "orphan_dependency": {"count": len(orphan_edges), "truth_state": "current"},
        "duplicate_card": {
            "count": None,
            "truth_state": "unknown",
            "reason": "Canonical IDs are unique; no approved semantic-duplicate registry is configured.",
        },
        "claim_ttl": {
            "count": len(claim_ttl),
            "truth_state": "unknown" if active_claims else "not_applicable",
            "reason": "Claim expiry is not part of the folded CardStore contract.",
        },
    }
    return findings, summary


def get_governance_projection(
    home: Path,
    query: dict,
    *,
    aggregate_readers: Mapping[str, Reader] | None = None,
    store_factory=None,
    policy_decision: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Project bounded governance metadata without exposing protected content or writes."""
    from skcoord.card_store import CardStore

    instant = (now or _now()).astimezone(timezone.utc)
    readers = aggregate_readers if aggregate_readers is not None else default_readers(home)
    observations = project_estate(readers, now=instant)
    quality = project_data_quality(observations)
    source_lineage = _source_lineage(observations)
    decision_ref = (policy_decision or {}).get("decision_ref")
    metric_lineage = _metric_lineage(observations, decision_ref)

    store = (store_factory or CardStore)(Path(home).expanduser())
    all_cards = sorted(store.list_cards(include_archived=True), key=lambda card: card.id)
    cards = all_cards[:MAX_CARDS]
    all_ids = {card.id for card in all_cards}
    findings, finding_classes = _card_findings(cards, all_ids)
    correction_history = _safe_history(store, cards)

    capauth = next(item for item in observations if item["adapter_id"] == "capauth.policy")
    aggregate = capauth.get("aggregate") or {}
    policy_available = (
        aggregate.get("available")
        if capauth["truth_state"] in {"current", "stale", "partial"}
        else None
    )
    denials = (
        aggregate.get("denials")
        if capauth["truth_state"] in {"current", "stale", "partial"}
        else None
    )
    if policy_available is not True:
        findings.insert(
            0,
            _finding(
                "policy_unavailable",
                "capauth.policy",
                owner="CapAuth",
                severity="critical",
                truth_state=capauth["truth_state"],
                evidence_refs=[capauth["watermark"]["value"]]
                if capauth["watermark"]["value"]
                else [],
                safe_detail="Policy availability is unavailable or not affirmatively true.",
                preview_kind="policy_health_review_preview",
            ),
        )
    if isinstance(denials, int) and not isinstance(denials, bool) and denials > 0:
        findings.insert(
            1,
            _finding(
                "policy_denial",
                "capauth.policy",
                owner="CapAuth",
                severity="high",
                evidence_refs=[capauth["watermark"]["value"]],
                safe_detail=f"The approved aggregate reports {denials} denied decisions; denial detail is excluded.",
                preview_kind="denial_review_preview",
            ),
        )
    finding_classes.update(
        {
            "policy_unavailable": {
                "count": int(policy_available is not True),
                "truth_state": capauth["truth_state"],
            },
            "policy_denial": {
                "count": denials,
                "truth_state": capauth["truth_state"] if denials is not None else "unknown",
            },
            "stale_evidence": {
                "count": quality["state_counts"]["stale"],
                "truth_state": "current",
            },
            "partial_coverage": {
                "count": quality["state_counts"]["partial"],
                "truth_state": "current",
            },
        }
    )
    for issue in quality["issues"]:
        category = {
            "stale": "stale_evidence",
            "partial": "partial_coverage",
        }.get(issue["truth_state"])
        if category:
            findings.append(
                _finding(
                    category,
                    issue["source"]["adapter_id"],
                    owner=issue["owner"],
                    severity="medium",
                    truth_state=issue["truth_state"],
                    evidence_refs=[issue["watermark"]["value"]]
                    if issue.get("watermark") and issue["watermark"].get("value")
                    else [],
                    safe_detail=issue["safe_provenance"][0]["message"],
                    preview_kind="source_refresh_preview",
                )
            )

    source = {
        "registry": registry_manifest(),
        "source_lineage": source_lineage,
        "metric_lineage": metric_lineage,
        "quality": quality,
        "card_ids": [card.id for card in cards],
        "finding_classes": finding_classes,
        "correction_history": correction_history,
    }
    projection_hash = hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    states = {item["truth_state"] for item in observations}
    truth_state = (
        "current" if states == {"current"} and len(cards) == len(all_cards) else "partial"
    )
    return {
        "schema_version": "1.0.0",
        "projection_id": "governance-latest",
        "projection_hash": f"sha256:{projection_hash}",
        "source_owner": "Metric registry, authorized aggregate owners, CapAuth, and SKCoord CardStore",
        "scope": dict(query),
        "projected_at": instant.isoformat(),
        "truth_state": truth_state,
        "visibility": {"state": "visible", "authorization": "authorized"},
        "request_authorization": policy_decision
        or {"state": "unknown", "decision_ref": None, "expires_at": None},
        "registry": registry_manifest(),
        "source_lineage": source_lineage,
        "metric_lineage": metric_lineage,
        "report_registry": {
            "truth_state": "unknown",
            "reports": [],
            "safe_error": "No approved immutable report-snapshot owner reader is configured.",
        },
        "policy": {
            "source_truth_state": capauth["truth_state"],
            "available": policy_available,
            "denials": denials,
            "watermark": capauth["watermark"],
            "access_review": {
                "state": "unknown",
                "review_ref": None,
                "reason": "No approved access-review aggregate is configured.",
            },
        },
        "data_quality": quality,
        "card_population": {
            "observed": len(all_cards),
            "analyzed": len(cards),
            "truncated": len(cards) != len(all_cards),
            "content_fields_excluded": True,
        },
        "finding_classes": finding_classes,
        "findings": findings[:MAX_FINDINGS],
        "correction_history": correction_history,
        "history_contract": {
            "mode": "append_only_projection",
            "rewrites_history": False,
            "payload_fields_excluded": True,
        },
        "actions": {"mode": "preview_only", "dispatch_authorized": False},
        "individual_ranking_prohibited": True,
        "errors": []
        if len(cards) == len(all_cards)
        else [
            {
                "code": "CARD_POPULATION_TRUNCATED",
                "message": "The governance card population exceeded its analysis cap.",
            }
        ],
    }


class GovernanceProjectionProvider:
    """Read governance owner evidence only while the exact CapAuth decision remains current."""

    def __init__(
        self,
        aggregate_readers: Mapping[str, Reader] | None = None,
        *,
        store_factory=None,
    ):
        self.aggregate_readers = aggregate_readers
        self.store_factory = store_factory

    def read(self, context, query, home, *, currentness_verifier):
        if currentness_verifier.check_before_owner_read(context).value != "allow":
            raise PermissionError("control-plane decision is not current")
        projection = get_governance_projection(
            home,
            query,
            aggregate_readers=self.aggregate_readers,
            store_factory=self.store_factory,
            policy_decision={
                "state": "allow",
                "decision_ref": context.capauth_decision.decision_id,
                "expires_at": context.expires_at.isoformat(),
            },
        )
        if currentness_verifier.check_after_owner_read(context).value != "allow":
            raise PermissionError("control-plane decision expired during owner read")
        return projection
