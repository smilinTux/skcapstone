"""Cross-provider selection over already qualified production review routes."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_ALIASES = {
    "codex": "codex",
    "openai": "codex",
    "skgw-codex": "codex",
    "glm": "zai",
    "zai": "zai",
    "skgw-zai": "zai",
    "deepseek": "deepseek",
    "skgw-deepseek": "deepseek",
    "qwen": "qwen",
    "skgw-qwen": "qwen",
    "chiap01-qwen38": "qwen",
    "chiap08-qwen38": "qwen",
    # Generic gateway size buckets: skgateway chooses the member model, so the
    # fleet routes to the bucket and the gateway config is the on/off switch.
    "skgateway": "gateway",
    "gateway": "gateway",
    "sk-s": "gateway",
    "sk-m": "gateway",
    "sk-l": "gateway",
    "sk-xl": "gateway",
}
_IDENTITY = re.compile(r"pi-(codex|glm|zai|deepseek|qwen|escalate|gateway)-[a-z0-9][a-z0-9._-]*\Z")
_GLM_DISTINCT_REVIEW_LABELS = {"review", "glm-only", "review-distinct-agent"}


def provider_family(provider: object) -> str | None:
    """Normalize typed provider/backend identity, never a transport-only label."""
    return _ALIASES.get(provider.strip().lower()) if isinstance(provider, str) else None


def review_family_allowed(source: str, reviewer: str, labels) -> bool:
    """Allow any known reviewer family; independence is a distinct reviewer agent.

    Same-family review used to require explicit GLM labels, and cross-family was
    the default. With one lane exhausted (z.ai, 2026-10-10) that parked every
    DeepSeek-built review until GLM returned. Chef: drop the family requirement.
    The publisher still refuses producer_reviewer_not_distinct, and an explicit
    glm-only pin on the card is still honored.
    """
    source, reviewer = provider_family(source), provider_family(reviewer)
    if source is None or reviewer is None:
        return False
    return not ("glm-only" in labels and reviewer != "zai")


def producer_family(identity: str, provider: str | None = None) -> str:
    """Resolve source evidence supplied by the authoritative source-card caller.

    The caller must verify this is the source producer's identity and typed
    provider, not a review-card author's assertion. Unknown identities require
    typed provider evidence; contradictory evidence never silently wins.
    """
    identity = identity.strip().lower() if isinstance(identity, str) else ""
    match = _IDENTITY.fullmatch(identity)
    from_identity = None
    if match:
        from_identity = "codex" if match[1] == "escalate" else provider_family(match[1])
    elif re.fullmatch(r"codex-[a-z0-9][a-z0-9._-]*", identity):
        from_identity = "codex"
    from_provider = provider_family(provider)
    if provider is not None and from_provider is None:
        raise ValueError("producer provider family is ambiguous")
    if from_identity and from_provider and from_identity != from_provider:
        raise ValueError("producer identity and provider family disagree")
    family = from_provider or from_identity
    if family is None:
        raise ValueError("producer family requires authoritative source evidence")
    return family


def independent_review_routes(
    routes: Sequence[Mapping[str, Any]],
    *,
    policy: Mapping[str, Any],
    producer_identity: str,
    producer_provider: str | None = None,
    reviewer_identity: str | None = None,
    labels: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Keep qualified review routes for a distinct reviewer principal.

    ``routes`` must already pass the current sealed gateway health, capability,
    capacity and card policy gates. This filter neither creates qualification
    nor changes source, claim, candidate or reviewer-principal authorization.
    Any enabled family may review (Chef, 2026-10-10: the cross-family rule
    parked every DeepSeek build while GLM was the only other lane). This is
    the dispatch-side twin of ``review_family_allowed``: independence is a
    distinct reviewer agent, and an explicit ``glm-only`` pin admits only GLM.
    The producer family must still be known, because acceptance needs it.
    """
    from ..seat_boundaries import canonical_principal
    from .production_dispatch import enabled_family_routes

    normalized_labels = {str(label).strip().lower() for label in labels}
    explicit_glm_review = _GLM_DISTINCT_REVIEW_LABELS <= normalized_labels
    distinct_principals = reviewer_identity is not None and canonical_principal(
        producer_identity
    ) != canonical_principal(reviewer_identity)
    allow_glm = explicit_glm_review and distinct_principals
    try:
        source = producer_family(producer_identity, producer_provider)
    except ValueError:
        if not allow_glm or producer_provider is not None:
            raise
        source = None

    if reviewer_identity is not None and not distinct_principals:
        return []
    glm_only = "glm-only" in normalized_labels or source is None

    def is_independent(route: Mapping[str, Any]) -> bool:
        family = provider_family(route["provider"])
        return family is not None and (not glm_only or family == "zai")

    return [
        route for route in enabled_family_routes(routes, policy=policy) if is_independent(route)
    ]
