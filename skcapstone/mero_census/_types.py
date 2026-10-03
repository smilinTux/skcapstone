"""Typed enums of the Mero blocker census.

Card 8fa7d8eb moved these verbatim from the single-module layout of card
2516480b. The bounded set of finding types is the census contract; the risk
classes order consumer attention.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["RiskClass", "CensusFindingType"]


class RiskClass(StrEnum):
    """Consumer risk classes for census recommendations."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CensusFindingType(StrEnum):
    """The bounded set of census findings card 2516480b requires."""

    DEAD_CLAIM = "dead_claim"
    STALE_CLAIM = "stale_claim"
    COMPLETED_DEPENDENCY = "completed_dependency"
    CONTRADICTORY_VERDICTS = "contradictory_verdicts"
    MALFORMED_BLOCKER_REFERENT = "malformed_blocker_referent"
    VOID_DEPENDENCY_EDGE = "void_dependency_edge"
    SUPERSEDED_LIVE_CARD = "superseded_live_card"
    REVIEW_IDENTITY_GAP = "review_identity_gap"
