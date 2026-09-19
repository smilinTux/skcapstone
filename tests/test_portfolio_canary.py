"""Adversarial qualification for the simulation-only canary front door."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from skcoord.portfolio import AllocationDecisionV1, PortfolioPlanContentV1, RankedCandidate

from skcapstone.portfolio_canary import (
    CanaryManifestV1,
    ComponentPin,
    PortfolioCanaryFrontDoor,
    digest,
)

NOW = datetime(2026, 8, 29, 11, 0, tzinfo=timezone.utc)
SHA_A = "a" * 64
SHA_B = "b" * 64
CARD_ID = "public-synthetic-canary-096e93e2"
CARD_REVISION = digest(b"public-synthetic-canary-096e93e2:r1")
CHANGED_PATHS = (
    "docs/runbooks/096e93e2-portfolio-canary.md",
    "src/skcapstone/portfolio_canary.py",
    "tests/test_portfolio_canary.py",
)


def manifest(**updates) -> CanaryManifestV1:
    values = {
        "schema_version": "portfolio-canary-manifest.v1",
        "source_commit": "73d5e294ab4b7e5d450375a983978b4e76e1107b",
        "source_tree": "819f3d150f2bc83f4cfc85f518b3748813d2fb72",
        "components": (
            ComponentPin(
                card_id="d5c6f539",
                commit="d5c6f539",
                tree="8fdb056dbeabf31f547dfef78a89975df74011af",
                evidence_sha256="a18596c5768ef7be65b1e46ba90b9a59d9c8b56456a8bdbc4f51b5f6ef18b47e",
            ),
            ComponentPin(
                card_id="2850e05b",
                commit="2850e05b",
                tree="bc8a01a2e5c24019a3dca8d8898702701b8bace3",
                evidence_sha256="35b3066939763ab5745b2d5152d9ce2f45bc6de08562abc742e0f9a2adee08cc",
            ),
            ComponentPin(
                card_id="048c5de2",
                commit="bdec7b6c",
                tree="e2ee55c8425cee25a269315556a9a95b305a4149",
                evidence_sha256="bcf778da9956abadca9118dc84b8202436f6ba2fb4f49b907e2ccd7837ab6a64",
            ),
            ComponentPin(
                card_id="de712b36",
                commit="de712b36",
                tree="855f6afeb990415a46a57fc3757bf393e60b5e19",
                evidence_sha256="66ad9067fbb068363594eb8f41dee2aece96b190faa55d4ffb2995340f6d5d11",
            ),
        ),
        "policy_id": "portfolio-canary-simulation.v1",
        "policy_hash": SHA_A,
        "schema_hash": SHA_B,
        "selected_card_id": CARD_ID,
        "selected_card_revision": CARD_REVISION,
        "selected_card_classification": "public-synthetic",
        "expected_active_wip": 0,
        "wip_limit": 1,
        "lease_generation": 7,
        "lease_seconds": 300,
        "leader_epoch": 11,
        "executor_allowlist": ("portfolio-canary-executor",),
        "reviewer_principal_id": "portfolio-canary-independent-reviewer",
        "stop_conditions": (
            "any CardStore write attempt",
            "any network or provider attempt",
            "any protected-data access",
            "card, revision, policy, WIP, lease, epoch, executor, or reviewer mismatch",
        ),
        "rollback": (
            "discard in-memory replay cache",
            "delete generated evidence only",
            "revert the three closed changed paths",
        ),
        "changed_paths": CHANGED_PATHS,
    }
    values.update(updates)
    return CanaryManifestV1(**values)


def proposal(*, card_id=CARD_ID, expires_at=NOW + timedelta(minutes=5)) -> PortfolioPlanContentV1:
    return PortfolioPlanContentV1(
        status="proposed",
        objective_hash=SHA_A,
        snapshot_id="public-synthetic-snapshot-096e93e2",
        snapshot_hash=SHA_B,
        snapshot_expires_at=expires_at,
        policy_id="portfolio-canary-simulation.v1",
        policy_version="1",
        policy_hash=SHA_A,
        recommendations=(
            RankedCandidate(
                card_id=card_id,
                rank=1,
                repo_id="skcapstone",
                executor_principal_id="portfolio-canary-executor",
                class_of_service="standard",
                ranking_key=(2, 2, 1, "", "2026-08-29T10:00:00Z", 0, card_id),
            ),
        ),
        exclusions=(),
    )


def decision(plan: PortfolioPlanContentV1, **updates) -> AllocationDecisionV1:
    values = {
        "plan_content_hash": digest(plan),
        "card_id": CARD_ID,
        "expected_card_revision": CARD_REVISION,
        "dependency_revision_vector": {},
        "approval_ref": "simulation-envelope:45d97493",
        "approval_hash": SHA_B,
        "approved_card_revision": CARD_REVISION,
        "policy_id": "portfolio-canary-simulation.v1",
        "policy_version": "1",
        "policy_hash": SHA_A,
        "capacity_revision": "public-synthetic-capacity-r1",
        "expected_active_wip": 0,
        "wip_limit": 1,
        "expected_lease_generation": 7,
        "target_executor_principal_id": "portfolio-canary-executor",
        "target_repo_id": "skcapstone",
        "target_repo_revision": "73d5e294ab4b7e5d450375a983978b4e76e1107b",
        "eligible": True,
        "reason_codes": (),
        "ranking_key": plan.recommendations[0].ranking_key,
        "requested_lease_seconds": 300,
        "idempotency_key": "canary-096e93e2-r1",
        "decided_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
    }
    values.update(updates)
    return AllocationDecisionV1.create(**values)


def test_focused_integration_exactly_one_card_and_zero_effects() -> None:
    plan = proposal()
    result = PortfolioCanaryFrontDoor(manifest()).simulate(
        proposal=plan, decision=decision(plan), now=NOW
    )

    assert result.status == "simulated"
    assert result.counters.model_dump() == {
        "selected_cards": 1,
        "live_claims": 0,
        "cardstore_mutations": 0,
        "provider_traffic": 0,
        "protected_data_accesses": 0,
        "external_actions": 0,
    }
    handoff = result.authenticated_handoff.handoff
    assert handoff.card_id == CARD_ID
    assert handoff.card_revision == CARD_REVISION
    assert handoff.executor_principal_id == "portfolio-canary-executor"
    assert handoff.reviewer_principal_id == "portfolio-canary-independent-reviewer"
    assert handoff.claim_event_id == "SIMULATION_NO_EVENT"
    assert handoff.authorization_decision_id == "SIMULATION_DENY_LIVE_MUTATION"
    assert handoff.allowed_paths == CHANGED_PATHS


def test_deterministic_replay_and_concurrency_return_identical_receipt() -> None:
    plan = proposal()
    allocation = decision(plan)
    front = PortfolioCanaryFrontDoor(manifest())
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(
            pool.map(
                lambda _: front.simulate(proposal=plan, decision=allocation, now=NOW), range(40)
            )
        )

    assert len({r.result_hash for r in results}) == 1
    assert len({r.claim_receipt.receipt_hash for r in results}) == 1
    assert all(r.counters.cardstore_mutations == 0 for r in results)


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"target_executor_principal_id": "untrusted"}, "executor_denied"),
        ({"policy_hash": SHA_B}, "policy_mismatch"),
        ({"expected_active_wip": 1}, "wip_mismatch"),
        ({"expected_lease_generation": 6}, "lease_generation_stale"),
        ({"expected_card_revision": SHA_A}, "card_revision_mismatch"),
        ({"eligible": False, "reason_codes": ("capauth_denied",)}, "allocation_ineligible"),
    ],
)
def test_policy_denial_and_stale_state_abstain(updates, reason) -> None:
    plan = proposal()
    result = PortfolioCanaryFrontDoor(manifest()).simulate(
        proposal=plan, decision=decision(plan, **updates), now=NOW
    )
    assert result.status == "abstained"
    assert reason in result.reason_codes
    assert result.authenticated_handoff is None


def test_expired_state_abstains() -> None:
    plan = proposal(expires_at=NOW + timedelta(seconds=1))
    allocation = decision(plan, expires_at=NOW + timedelta(seconds=1))
    result = PortfolioCanaryFrontDoor(manifest()).simulate(
        proposal=plan, decision=allocation, now=NOW + timedelta(seconds=2)
    )
    assert result.reason_codes == ("stale_state",)


def test_multiple_or_substitute_card_abstains() -> None:
    first = proposal()
    raw = first.model_dump(mode="python")
    raw["recommendations"] = (
        *first.recommendations,
        first.recommendations[0].model_copy(update={"card_id": "substitute", "rank": 2}),
    )
    widened = PortfolioPlanContentV1.model_validate(raw)
    result = PortfolioCanaryFrontDoor(manifest()).simulate(
        proposal=widened, decision=decision(widened), now=NOW
    )
    assert result.status == "abstained"
    assert "not_exactly_one_selected_card" in result.reason_codes


def test_replay_conflict_abstains_without_replacing_receipt() -> None:
    plan = proposal()
    front = PortfolioCanaryFrontDoor(manifest())
    original = front.simulate(proposal=plan, decision=decision(plan), now=NOW)
    conflicting = decision(plan, capacity_revision="changed")
    denied = front.simulate(proposal=plan, decision=conflicting, now=NOW)
    replay = front.simulate(proposal=plan, decision=decision(plan), now=NOW)
    assert denied.reason_codes == ("idempotency_conflict",)
    assert replay.result_hash == original.result_hash


def test_manifest_is_closed_and_rollback_is_pinned() -> None:
    with pytest.raises(ValidationError, match="closed and sorted"):
        manifest(changed_paths=tuple(reversed(CHANGED_PATHS)))
    with pytest.raises(ValidationError, match="wip exhausted"):
        manifest(expected_active_wip=1)
    assert manifest().rollback[-1] == "revert the three closed changed paths"
