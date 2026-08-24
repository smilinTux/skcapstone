"""Read-only adapter for the pure SKCoord portfolio evaluator."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Mapping, TextIO

from pydantic import BaseModel, ConfigDict
from skcoord.portfolio import (
    AgentCapacity,
    CanonicalSourceRef,
    PlanDataQuality,
    PortfolioPlanContentV1,
    PortfolioPolicy,
    WorkCandidate,
    evaluate_portfolio,
)


class PortfolioPlanInputV1(BaseModel):
    """One frozen, caller-supplied input for a shadow evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["portfolio-plan-input.v1"]
    candidates: tuple[WorkCandidate, ...]
    capacities: Mapping[str, AgentCapacity]
    quality: PlanDataQuality
    policy: PortfolioPolicy
    objective_hash: str
    as_of: datetime
    source_refs: tuple[CanonicalSourceRef, ...] = ()


def evaluate_input(stream: TextIO) -> PortfolioPlanContentV1:
    """Validate and evaluate one JSON input without reading or changing the board."""
    request = PortfolioPlanInputV1.model_validate_json(stream.read())
    return evaluate_portfolio(
        candidates=request.candidates,
        capacities=request.capacities,
        quality=request.quality,
        policy=request.policy,
        objective_hash=request.objective_hash,
        as_of=request.as_of,
        source_refs=request.source_refs,
    )
