"""Shared primitives for the Mero blocker census package.

Card 8fa7d8eb decomposes the single 1027-line ``mero_census`` module of card
2516480b into focused submodules without changing any behavior. This module
holds the pure constants: census bounds, SLA defaults, the recommendation
event name and schema, the risk ordering, and the board-vocabulary regexes.
No behavior lives here.
"""

from __future__ import annotations

import re
from datetime import timedelta

__all__ = [
    "MAX_CARDS_EXAMINED",
    "MAX_FINDINGS_PER_RUN",
    "DEFAULT_STALE_CLAIM_SLA",
    "DEFAULT_RECOMMENDATION_SLA",
    "RECOMMENDATION_EVENT",
    "RECOMMENDATION_SCHEMA",
    "RISK_ORDER",
    "_BLOCKED_RE",
    "_PROVISIONAL_PASS_RE",
    "_OUTCOME_KEY_RE",
    "_BLOCKER_ACTIONS",
    "_SUCCESSOR_KEY_RE",
    "_BLOCKED_ON_VALUES",
]

#: Census bounds. Bounded input, bounded output.
MAX_CARDS_EXAMINED = 4000
MAX_FINDINGS_PER_RUN = 200

#: Default age after which an unchanged DOING claim is reported stale.
DEFAULT_STALE_CLAIM_SLA = timedelta(hours=24)

#: Default age after which an unanswered emitted recommendation is re-emitted.
DEFAULT_RECOMMENDATION_SLA = timedelta(hours=48)

#: The event action and schema of a census recommendation.
RECOMMENDATION_EVENT = "mero_blocker_recommendation"
RECOMMENDATION_SCHEMA = "skfleet.mero-blocker-recommendation/v1"

#: Deterministic findings digest under this prefix; bump to re-key findings.
_GENERATION_VERSION = "mrc1"

#: Risk classes, highest first. Findings sort by this order, then card id.
RISK_ORDER = ("high", "medium", "low", "info")

#: A verdict text whose leading token declares BLOCKED.
_BLOCKED_RE = re.compile(r"^\s*BLOCKED\b", re.IGNORECASE)

#: A PASS that only declares work ready for review. It has not cleared its own
#: independent review, so it is not a completed pass.
_PROVISIONAL_PASS_RE = re.compile(r"^PASS[_-](FOR|READY)", re.IGNORECASE)

#: Recognised outcome keys, matching the spellings the store actually writes.
_OUTCOME_KEY_RE = re.compile(
    r"(verdict|outcome|result|disposition|review_decision)", re.IGNORECASE
)

#: Event actions that carry blocker attributes on some cards.
_BLOCKER_ACTIONS = frozenset({"blocked_on", "block", "blocked", "blocked_verdict"})

#: Link keys that name a card's repair, re-review, or supersession successor.
_SUCCESSOR_KEY_RE = re.compile(
    r"(repair|rereview|re_review|reviewed_by|successor|superseded_by)", re.IGNORECASE
)

#: Referent values the BLOCKED verdict contract allows.
_BLOCKED_ON_VALUES = ("dependency", "human", "capability", "card")
