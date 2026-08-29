"""Immutable, simulation-only Portfolio Steward canary front door.

This module deliberately has no CardStore, provider, network, credential, or
protected-data adapter. It composes reviewed contract shapes into a pure,
single-card simulation and fails closed on every stale or widened input.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from skcoord.portfolio import AllocationDecisionV1, PortfolioPlanContentV1

_SHA = r"^[0-9a-f]{64}$"


def canonical_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    """Return deterministic UTF-8 JSON bytes for public synthetic evidence."""
    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: BaseModel | dict[str, Any] | bytes) -> str:
    data = value if isinstance(value, bytes) else canonical_bytes(value)
    return hashlib.sha256(data).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ComponentPin(Contract):
    card_id: str
    commit: str = Field(pattern=r"^[0-9a-f]{7,40}$")
    tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    evidence_sha256: str = Field(pattern=_SHA)


class CanaryManifestV1(Contract):
    schema_version: Literal["portfolio-canary-manifest.v1"]
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    components: tuple[ComponentPin, ...]
    policy_id: str
    policy_hash: str = Field(pattern=_SHA)
    schema_hash: str = Field(pattern=_SHA)
    selected_card_id: str
    selected_card_revision: str = Field(pattern=_SHA)
    selected_card_classification: Literal["public-synthetic"]
    expected_active_wip: int = Field(ge=0)
    wip_limit: int = Field(gt=0)
    lease_generation: int = Field(ge=0)
    lease_seconds: int = Field(gt=0)
    leader_epoch: int = Field(gt=0)
    executor_allowlist: tuple[str, ...]
    reviewer_principal_id: str
    stop_conditions: tuple[str, ...]
    rollback: tuple[str, ...]
    changed_paths: tuple[str, ...]

    @model_validator(mode="after")
    def bounded(self) -> "CanaryManifestV1":
        if len(set(self.executor_allowlist)) != len(self.executor_allowlist):
            raise ValueError("duplicate executor")
        if self.expected_active_wip >= self.wip_limit:
            raise ValueError("wip exhausted")
        if not self.changed_paths or tuple(sorted(self.changed_paths)) != self.changed_paths:
            raise ValueError("changed-path manifest must be closed and sorted")
        return self


class WriterFenceSimulationV1(Contract):
    schema_version: Literal["writer-fence-simulation.v1"]
    mutation_service_principal_id: str
    leader_epoch: int = Field(gt=0)
    lease_generation: int = Field(ge=0)
    mode: Literal["simulation"] = "simulation"


class SimulatedClaimReceiptV1(Contract):
    schema_version: Literal["simulated-claim-receipt.v1"]
    decision_id: str = Field(pattern=_SHA)
    idempotency_key: str
    card_id: str
    card_revision: str = Field(pattern=_SHA)
    executor_principal_id: str
    leader_epoch: int
    lease_generation: int
    state: Literal["simulated"] = "simulated"
    receipt_hash: str = Field(pattern=_SHA)


class ExecutionHandoffV1(Contract):
    schema_version: Literal["portfolio-execution-handoff.v1"]
    plan_content_hash: str = Field(pattern=_SHA)
    allocation_decision_id: str = Field(pattern=_SHA)
    claim_event_id: Literal["SIMULATION_NO_EVENT"]
    claim_receipt_hash: str = Field(pattern=_SHA)
    leader_epoch: int
    lease_generation: int
    lease_expires_at: datetime
    authorization_decision_id: Literal["SIMULATION_DENY_LIVE_MUTATION"]
    card_id: str
    card_revision: str = Field(pattern=_SHA)
    repo_id: str
    repo_revision: str
    task_brief_hash: str = Field(pattern=_SHA)
    acceptance_hash: str = Field(pattern=_SHA)
    allowed_paths: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    executor_principal_id: str
    reviewer_policy_ref: str
    reviewer_principal_id: str
    idempotency_key: str
    expires_at: datetime


class AuthenticatedHandoffV1(Contract):
    schema_version: Literal["authenticated-handoff.v1"]
    producer_principal_id: Literal["portfolio-handoff-producer"]
    transport: Literal["in-process-public-synthetic"]
    envelope_hash: str = Field(pattern=_SHA)
    authentication_receipt: str = Field(pattern=_SHA)
    handoff: ExecutionHandoffV1


class CanaryCountersV1(Contract):
    selected_cards: Literal[1] = 1
    live_claims: Literal[0] = 0
    cardstore_mutations: Literal[0] = 0
    provider_traffic: Literal[0] = 0
    protected_data_accesses: Literal[0] = 0
    external_actions: Literal[0] = 0


class CanaryResultV1(Contract):
    schema_version: Literal["portfolio-canary-result.v1"]
    status: Literal["simulated", "abstained"]
    reason_codes: tuple[str, ...]
    manifest_hash: str = Field(pattern=_SHA)
    proposal_hash: str = Field(pattern=_SHA)
    allocation_decision: AllocationDecisionV1 | None
    fence: WriterFenceSimulationV1 | None
    claim_receipt: SimulatedClaimReceiptV1 | None
    authenticated_handoff: AuthenticatedHandoffV1 | None
    counters: CanaryCountersV1
    result_hash: str = Field(pattern=_SHA)


class PortfolioCanaryFrontDoor:
    """Thread-safe deterministic replay boundary with no effectful dependencies."""

    def __init__(self, manifest: CanaryManifestV1):
        self.manifest = manifest
        self._lock = threading.Lock()
        self._results: dict[str, CanaryResultV1] = {}

    def simulate(
        self,
        *,
        proposal: PortfolioPlanContentV1,
        decision: AllocationDecisionV1,
        now: datetime,
    ) -> CanaryResultV1:
        key = decision.idempotency_key
        with self._lock:
            cached = self._results.get(key)
            if cached is not None:
                if cached.allocation_decision == decision:
                    return cached
                return self._abstain(proposal, "idempotency_conflict")
            reasons = self._validate(proposal, decision, now)
            if reasons:
                return self._abstain(proposal, *reasons)
            result = self._compose(proposal, decision)
            self._results[key] = result
            return result

    def _validate(
        self, proposal: PortfolioPlanContentV1, decision: AllocationDecisionV1, now: datetime
    ) -> tuple[str, ...]:
        m = self.manifest
        reasons: list[str] = []
        selected = [x for x in proposal.recommendations if x.card_id == m.selected_card_id]
        if len(proposal.recommendations) != 1 or len(selected) != 1:
            reasons.append("not_exactly_one_selected_card")
        if proposal.status != "proposed" or proposal.claims or proposal.mutations:
            reasons.append("proposal_not_advisory")
        if digest(proposal) != decision.plan_content_hash:
            reasons.append("proposal_hash_mismatch")
        if (
            decision.card_id != m.selected_card_id
            or decision.expected_card_revision != m.selected_card_revision
        ):
            reasons.append("card_revision_mismatch")
        if decision.policy_hash != m.policy_hash:
            reasons.append("policy_mismatch")
        if decision.target_executor_principal_id not in m.executor_allowlist:
            reasons.append("executor_denied")
        if (
            decision.expected_active_wip != m.expected_active_wip
            or decision.wip_limit != m.wip_limit
        ):
            reasons.append("wip_mismatch")
        if decision.expected_lease_generation != m.lease_generation:
            reasons.append("lease_generation_stale")
        if not decision.eligible or decision.authorization_state != "pending":
            reasons.append("allocation_ineligible")
        aware_now = now.astimezone(timezone.utc)
        if decision.expires_at <= aware_now or proposal.snapshot_expires_at <= aware_now:
            reasons.append("stale_state")
        return tuple(sorted(set(reasons)))

    def _compose(
        self, proposal: PortfolioPlanContentV1, decision: AllocationDecisionV1
    ) -> CanaryResultV1:
        m = self.manifest
        proposal_hash = digest(proposal)
        fence = WriterFenceSimulationV1(
            schema_version="writer-fence-simulation.v1",
            mutation_service_principal_id="portfolio-mutation-simulator",
            leader_epoch=m.leader_epoch,
            lease_generation=m.lease_generation,
        )
        receipt_body = {
            "decision_id": decision.decision_id,
            "idempotency_key": decision.idempotency_key,
            "card_id": decision.card_id,
            "card_revision": decision.expected_card_revision,
            "executor_principal_id": decision.target_executor_principal_id,
            "leader_epoch": m.leader_epoch,
            "lease_generation": m.lease_generation,
            "state": "simulated",
        }
        receipt = SimulatedClaimReceiptV1(
            schema_version="simulated-claim-receipt.v1",
            **receipt_body,
            receipt_hash=digest(receipt_body),
        )
        handoff = ExecutionHandoffV1(
            schema_version="portfolio-execution-handoff.v1",
            plan_content_hash=proposal_hash,
            allocation_decision_id=decision.decision_id,
            claim_event_id="SIMULATION_NO_EVENT",
            claim_receipt_hash=receipt.receipt_hash,
            leader_epoch=m.leader_epoch,
            lease_generation=m.lease_generation,
            lease_expires_at=decision.expires_at,
            authorization_decision_id="SIMULATION_DENY_LIVE_MUTATION",
            card_id=decision.card_id,
            card_revision=decision.expected_card_revision,
            repo_id=decision.target_repo_id,
            repo_revision=decision.target_repo_revision,
            task_brief_hash=digest(b"public-synthetic bounded canary"),
            acceptance_hash=digest(b"simulation only; zero effects"),
            allowed_paths=m.changed_paths,
            allowed_tools=("read", "simulate"),
            executor_principal_id=decision.target_executor_principal_id,
            reviewer_policy_ref="portfolio-independent-review.v1",
            reviewer_principal_id=m.reviewer_principal_id,
            idempotency_key=decision.idempotency_key,
            expires_at=decision.expires_at,
        )
        envelope_hash = digest(handoff)
        authenticated = AuthenticatedHandoffV1(
            schema_version="authenticated-handoff.v1",
            producer_principal_id="portfolio-handoff-producer",
            transport="in-process-public-synthetic",
            envelope_hash=envelope_hash,
            authentication_receipt=digest(
                b"portfolio-handoff-producer\x00in-process-public-synthetic\x00"
                + envelope_hash.encode()
            ),
            handoff=handoff,
        )
        body = {
            "status": "simulated",
            "reason_codes": [],
            "manifest_hash": digest(m),
            "proposal_hash": proposal_hash,
            "allocation_decision": decision.model_dump(mode="json"),
            "fence": fence.model_dump(mode="json"),
            "claim_receipt": receipt.model_dump(mode="json"),
            "authenticated_handoff": authenticated.model_dump(mode="json"),
            "counters": CanaryCountersV1().model_dump(mode="json"),
        }
        return CanaryResultV1(
            schema_version="portfolio-canary-result.v1", **body, result_hash=digest(body)
        )

    def _abstain(self, proposal: PortfolioPlanContentV1, *reasons: str) -> CanaryResultV1:
        body = {
            "status": "abstained",
            "reason_codes": sorted(set(reasons)),
            "manifest_hash": digest(self.manifest),
            "proposal_hash": digest(proposal),
            "allocation_decision": None,
            "fence": None,
            "claim_receipt": None,
            "authenticated_handoff": None,
            "counters": CanaryCountersV1().model_dump(mode="json"),
        }
        return CanaryResultV1(
            schema_version="portfolio-canary-result.v1", **body, result_hash=digest(body)
        )
