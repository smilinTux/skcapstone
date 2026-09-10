"""Production composition of first-wave SKRSI metadata boundaries.

Use this facade for runtime work. Component classes remain pure library
primitives. Authority adapters are injected by the owning service, never inferred
from an evaluation outcome. This module has no merge, deployment, or route API.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .skrsi_collector import BoundedCollector
from .skrsi_evaluator import evaluate_cohorts
from .skrsi_experiment_controller import ExperimentController
from .skrsi_handoffs import HandoffError, HandoffRuntime
from .skrsi_registry import AppendOnlyOutbox, TargetRegistry, canonical_json, redact_metadata


class SKRSIRuntime:
    """One governed facade for collection, evaluation, projection, and review."""

    def __init__(
        self,
        handoffs: HandoffRuntime,
        *,
        authorize: Callable[[], bool],
        quality: Callable[[], bool],
        authority: Callable[[], str],
    ):
        self.handoffs = handoffs
        self.authorize = authorize
        self.quality = quality
        self.authority = authority

    def _run(self, boundary, key, metadata, operation, revision, *, fresh=lambda: True):
        metadata = json.loads(canonical_json(metadata))
        if redact_metadata(metadata) != metadata:
            raise HandoffError("protected metadata is not a runtime input")
        digest = hashlib.sha256(canonical_json(metadata)).hexdigest()
        return self.handoffs.execute(
            boundary,
            key,
            digest,
            operation,
            authorize=self.authorize,
            quality=lambda: self.quality() is True and fresh() is True,
            authority=self.authority,
            expected_revision=revision,
        )

    def register(self, target, *, revision: str):
        """Register an immutable target for the collector with a replay receipt."""

        def fresh():
            return datetime.fromisoformat(
                target.to_dict()["activation"]["expires_at"].replace("Z", "+00:00")
            ) > datetime.now(timezone.utc)

        def operation():
            return TargetRegistry().register(target, idempotency_key=target.target_ref).to_dict()

        return self._run(
            "registry-to-collector",
            target.target_ref,
            target.to_dict(),
            operation,
            revision,
            fresh=fresh,
        )

    def collect(self, source, envelopes, target, *, revision: str, now=None):
        """Apply the source contract before bounded metadata collection."""
        boundary = {
            "cardstore": "cardstore-to-skrsi",
            "skfleet": "fleet-to-skrsi",
            "skmail": "mail-to-skrsi",
        }[source]
        policy = self.handoffs.contracts[boundary]
        values = tuple(envelopes)
        if not values or len(values) > policy.queue_bound:
            raise HandoffError("collection batch exceeds handoff queue bound")
        material = {"source": source, "target": target.target_ref, "events": list(values)}
        key = hashlib.sha256(canonical_json(material)).hexdigest()
        self.register(target, revision=revision)

        def operation():
            collector = BoundedCollector(
                AppendOnlyOutbox(),
                source=source,
                target_ref=target.target_ref,
                authority=policy.producer,
                handoff_owner=policy.recovery_owner,
                queue_size=policy.queue_bound,
                timeout_seconds=policy.timeout_seconds,
                retry_attempts=policy.retry_attempts,
            )
            for envelope in values:
                if not collector.submit(envelope):
                    raise HandoffError("collector saturated")
            result = collector.drain(now=now)
            if result.rejected:
                raise HandoffError("collector rejected unsafe or malformed metadata")
            return {
                "accepted": result.accepted,
                "duplicates": result.duplicates,
                "cursor": result.cursor,
                "observation_hashes": [e.record_hash for e in collector.outbox.entries()],
            }

        return self._run(boundary, key, material, operation, revision)

    def evaluate(
        self,
        baseline,
        treatment,
        policy,
        guardrails,
        *,
        evaluator: str,
        independent_reviewer: str,
        revision: str,
    ):
        """Run the existing evaluator, preserving all negative and missing results."""
        material = {
            "baseline": asdict(baseline),
            "treatment": asdict(treatment),
            "policy": asdict(policy),
            "guardrails": [asdict(g) for g in guardrails],
            "evaluator": evaluator,
            "reviewer": independent_reviewer,
        }
        key = hashlib.sha256(canonical_json(material)).hexdigest()
        return self._run(
            "collector-to-evaluator",
            key,
            material,
            lambda: evaluate_cohorts(
                baseline,
                treatment,
                policy,
                guardrails,
                evaluator_version="skrsi.runtime.v1",
                evaluator=evaluator,
                independent_reviewer=independent_reviewer,
            ).to_payload(),
            revision,
        )

    def transition(
        self,
        store,
        experiment_id,
        state,
        *,
        agent,
        revision,
        transition_id,
        evaluation,
        authority_revision,
    ):
        """Use the existing CardStore revision fence and proposal-only controller."""
        supplied = dict(evaluation)
        expected = supplied.pop("content_hash", None)
        if expected != hashlib.sha256(canonical_json(supplied)).hexdigest():
            raise HandoffError("evaluation hash mismatch")
        if evaluation.get("authority") != "proposal-only":
            raise HandoffError("evaluation cannot grant authority")
        material = {
            "experiment": experiment_id,
            "state": state,
            "revision": revision,
            "evaluation": evaluation,
            "agent": agent,
            "transition_id": transition_id,
        }
        return self._run(
            "evaluator-to-controller",
            experiment_id + ":" + transition_id,
            material,
            lambda: asdict(
                ExperimentController(store, agent=agent).transition(
                    experiment_id,
                    state,
                    revision=revision,
                    transition_id=transition_id,
                    evidence={"evaluation_sha256": expected},
                    payload={"proposed_decision": evaluation["proposed_decision"]},
                )
            ),
            authority_revision,
        )

    def project(self, store, experiment_id, *, revision, expected_experiment_revision):
        """Project only the controller's proposal metadata, never a claimed verdict."""

        def fresh():
            _, number, _ = ExperimentController(store, agent="tank")._current(experiment_id)
            return number == expected_experiment_revision

        def operation():
            state, number, _ = ExperimentController(store, agent="tank")._current(experiment_id)
            return {
                "experiment_id": experiment_id,
                "state": state,
                "revision": number,
                "authority": "proposal-only",
            }

        return self._run(
            "controller-to-dashboard",
            experiment_id + ":" + revision + ":" + str(expected_experiment_revision),
            {
                "experiment": experiment_id,
                "authority_revision": revision,
                "experiment_revision": expected_experiment_revision,
            },
            operation,
            revision,
            fresh=fresh,
        )

    def deliver(self, query_id, projection_hash, provider, *, revision):
        """Bound a projection provider; the HTTP layer still owns user authorization."""

        def operation():
            result = provider()
            if redact_metadata(result) != result:
                raise HandoffError("protected projection")
            if hashlib.sha256(canonical_json(result)).hexdigest() != projection_hash:
                raise HandoffError("stale projection")
            return result

        return self._run(
            "dashboard-query-delivery",
            query_id + ":" + projection_hash,
            {"query": query_id, "projection_hash": projection_hash},
            operation,
            revision,
        )

    def canary_review(self, evidence: dict, *, revision):
        """Hand a hashed evaluation to Link, including negative evaluations."""
        material = dict(evidence)
        digest = material.pop("content_hash", None)
        if digest != hashlib.sha256(canonical_json(material)).hexdigest():
            raise HandoffError("canary evidence hash mismatch")
        if evidence.get("authority") != "proposal-only":
            raise HandoffError("canary cannot grant authority")
        return self._run(
            "canary-to-review",
            digest,
            evidence,
            lambda: {
                "evidence_sha256": digest,
                "verdict": "PENDING_INDEPENDENT_REVIEW",
                "proposed_decision": evidence["proposed_decision"],
            },
            revision,
        )

    def review(self, home: Path, item: dict, evidence_sha256: str, *, revision: str):
        """Consume Link's real canonical materialization and independent preflight."""
        from skcoord.card_store import CardStore

        from .link_review_work import (
            _source_workspace_binding,
            card_generation,
            reconcile_review_work,
            review_card_id,
        )

        candidates = item.get("reviewer_candidates", [])
        if (
            len(candidates) != 1
            or candidates[0].get("eligible") is not True
            or candidates[0].get("seat") not in {"seraph", "link"}
            or not candidates[0].get("identity")
            or candidates[0]["identity"].casefold() == str(item.get("source_owner", "")).casefold()
        ):
            raise HandoffError("exactly one independent reviewer is required")
        material = {"item": item, "evidence_sha256": evidence_sha256, "home": str(home.resolve())}
        key = item["source_card"] + ":" + item["head_revision"]
        store = CardStore(home)
        card_id = review_card_id(
            item["source_card"], item["head_revision"], item["card_generation"], evidence_sha256
        )

        def fresh():
            source = store.fold(item["source_card"])
            if source is None or card_generation(source) != item["card_generation"]:
                return False
            repository, base_ref = _source_workspace_binding(source, item)
            card = store.fold(card_id)
            return card is None or (
                card.owner is None
                and not card.archived
                and card.status.value in {"backlog", "review"}
                and card.links.get("repository") == repository
                and card.links.get("base_ref") == base_ref
            )

        def operation():
            result = reconcile_review_work(home, item, evidence_sha256=evidence_sha256)
            if not result.launchable:
                raise HandoffError("review preflight rejected")
            return {
                "review_card_id": result.review_card_id,
                "source_card": result.source_card,
                "head_revision": result.head_revision,
                "handoff_state": "materialized",
                "launch_authority": False,
            }

        return self._run("evidence-to-review", key, material, operation, revision, fresh=fresh)
