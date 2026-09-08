from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from skcapstone.skrsi_registry import (
    AppendOnlyOutbox,
    MetadataRecord,
    SKRSIError,
    SKRSIIntegrityError,
    TargetRegistry,
    TargetRevision,
    canonical_json,
    make_record,
    redact_metadata,
)


def target(**overrides):
    expires = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
    data = {
        "target_id": "review-latency",
        "revision": 1,
        "owner": "link",
        "domain": "capstone",
        "objective": "measure review latency with quality guardrails",
        "scope": {"products": ["skcapstone"], "event_types": ["card.review.finished"]},
        "invariants": ["no protected payload collection", "no authorization regression"],
        "baseline_window": {"duration": "14d", "min_samples": 30, "timezone": "UTC"},
        "metrics": ["reviewer_latency_ms", "first_pass_review_rate"],
        "slos": {"reviewer_latency_ms": {"p95_lte": 3600000}},
        "evaluators": ["deterministic-test-v1", "independent-reviewer"],
        "permitted_interventions": ["metadata-only prompt proposal"],
        "exclusions": ["capability_grants", "production_activation", "protected_data"],
        "risk_class": "low",
        "review_policy": {
            "independent_review": True,
            "min_reviewers": 1,
            "human_approval_for": [],
        },
        "activation": {"expires_at": expires},
        "rollback": {"artifact_ref": "sha256:abc", "trigger": "invariant_failure"},
    }
    data.update(overrides)
    return TargetRevision(**data)


def test_target_canonical_parse_back_and_hash_are_stable():
    value = target()
    parsed = TargetRevision.from_dict(json.loads(value.canonical_bytes()))
    assert parsed.to_dict() == value.to_dict()
    assert parsed.content_hash == value.content_hash
    assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_target_rejects_missing_exclusions_or_expiry():
    with pytest.raises(SKRSIError):
        target(exclusions=["production_activation"])
    with pytest.raises(SKRSIError):
        target(activation={})


def test_all_architecture_record_types_round_trip():
    for record_type in (
        "Observation",
        "Baseline",
        "Hypothesis",
        "Experiment",
        "InterventionProposal",
        "Evaluation",
        "Decision",
        "Rollout",
        "Regression",
        "Recovery",
    ):
        payload = {
            "natural_key": record_type,
            "value": 1,
            "unit": "count",
            "source": "test",
            "cohort": "baseline",
            "sample_id": record_type,
            "collection_quality": "complete",
            "frozen_window": "14d",
            "population": "test",
            "metric_distribution": {"mean": 1},
            "sample_size": 1,
            "expected_delta": 0.1,
            "mechanism": "test",
            "guardrails": ["quality"],
            "prior": 0.5,
            "cohorts": ["baseline", "treatment"],
            "randomization": "matched",
            "target_revision": "review-latency@1",
            "stop_rules": ["guardrail failure"],
            "budget": {"max_events": 1},
            "proposed_change": "metadata-only proposal",
            "artifact_hash": "sha256:artifact",
            "permissions": [],
            "blast_radius": "none",
            "rollback": "sha256:rollback",
            "required_reviewers": ["independent-reviewer"],
            "evaluator_version": "test-v1",
            "deterministic_checks": ["pytest"],
            "independent_reviewer": "seraph",
            "metric_results": {"quality": 1},
            "confidence_interval": [0, 1],
            "power": 0.8,
            "sample_adequacy": True,
            "negative_findings": [],
            "verdict": "PASS",
            "decision_policy": "test-policy",
            "rationale_digest": "sha256:rationale",
            "expires_at": "2099-01-01T00:00:00Z",
            "approver": "link",
            "proposal_approval": "sha256:approval",
            "bounded_observation": {"max_samples": 1},
            "invariant_or_slo": "quality",
            "affected_cohort": "treatment",
            "first_observed": "2099-01-01T00:00:00Z",
            "severity": "low",
            "containment_proposal": "pause proposal",
            "trigger": "test",
            "owner": "link",
            "resulting_health": "healthy",
            "verification_hash": "sha256:verification",
        }
        if record_type != "Observation":
            for key in ("value", "unit", "source", "cohort", "sample_id", "collection_quality"):
                payload.pop(key)
        payload["sample"] = 1
        record = make_record(
            record_type,
            actor="link",
            target_ref="review-latency@1",
            payload=payload,
            event_id=f"event-{record_type.lower()}",
            route_ref="sk-m",
        )
        parsed = MetadataRecord.from_dict(record.to_dict())
        assert parsed.to_dict() == record.to_dict()
        assert parsed.content_hash == record.content_hash


def test_decision_and_intervention_fail_closed():
    with pytest.raises(SKRSIError):
        make_record(
            "Decision", actor="link", target_ref="review-latency@1", payload={"verdict": "BLOCKED"}
        )
    with pytest.raises(SKRSIError):
        make_record(
            "InterventionProposal",
            actor="link",
            target_ref="review-latency@1",
            payload={"execute": True},
        )
    with pytest.raises(SKRSIError):
        make_record(
            "Observation", actor="link", target_ref="review-latency@1", payload={"x": float("nan")}
        )


def test_redaction_and_outbox_idempotency_and_retention(tmp_path):
    assert redact_metadata({"token": "never", "nested": {"body": "never"}}) == {
        "token": "[REDACTED]",
        "nested": {"body": "[REDACTED]"},
    }
    outbox = AppendOnlyOutbox(tmp_path / "outbox.jsonl")
    record = make_record(
        "Observation",
        actor="link",
        target_ref="review-latency@1",
        payload={
            "natural_key": "one",
            "value": 3,
            "unit": "count",
            "source": "test",
            "cohort": "baseline",
            "sample_id": "one",
            "collection_quality": "complete",
        },
    )
    first = outbox.append(record)
    second = outbox.append(record)
    assert first == second
    assert len(outbox.entries()) == 1
    assert len(outbox.pending()) == 1
    outbox.acknowledge(first)
    assert not outbox.pending()
    reloaded = AppendOnlyOutbox(tmp_path / "outbox.jsonl")
    assert not reloaded.pending()
    with pytest.raises(SKRSIIntegrityError):
        MetadataRecord.from_dict({**record.to_dict(), "payload_digest": "0" * 64})
    altered = make_record(
        "Observation",
        actor="link",
        target_ref="review-latency@1",
        payload={
            "natural_key": "one",
            "value": 4,
            "unit": "count",
            "source": "test",
            "cohort": "baseline",
            "sample_id": "one",
            "collection_quality": "complete",
        },
        event_id=record.event_id,
    )
    with pytest.raises(SKRSIIntegrityError):
        outbox.append(altered)


def test_outbox_reload_rejects_hash_or_ack_tampering(tmp_path):
    path = tmp_path / "outbox.jsonl"
    record = make_record(
        "Observation",
        actor="link",
        target_ref="review-latency@1",
        payload={
            "natural_key": "tamper",
            "value": 1,
            "unit": "count",
            "source": "test",
            "cohort": "baseline",
            "sample_id": "tamper",
            "collection_quality": "complete",
        },
    )
    outbox = AppendOnlyOutbox(path)
    outbox.append(record)

    row = json.loads(path.read_text())
    row["record_hash"] = "0" * 64
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises(SKRSIIntegrityError):
        AppendOnlyOutbox(path)

    outbox = AppendOnlyOutbox(tmp_path / "second.jsonl")
    outbox.append(record)
    second_path = tmp_path / "second.jsonl"
    row = json.loads(second_path.read_text())
    row["sent"] = True
    second_path.write_text(json.dumps(row) + "\n")
    with pytest.raises(SKRSIIntegrityError):
        AppendOnlyOutbox(second_path)


def test_registry_rejects_revision_reuse_and_discovers_only_active_unexpired():
    registry = TargetRegistry()
    draft = target()
    registry.register(draft, idempotency_key="register-1")
    with pytest.raises(SKRSIIntegrityError):
        registry.register(target(status="active"), idempotency_key="register-1-reused")
    active = target(revision=2, status="active")
    registry.register(active, idempotency_key="register-2")
    assert registry.discover(
        product="skcapstone", domain="capstone", event_type="card.review.finished"
    ) == (active,)
