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
}
_IDENTITY = re.compile(r"pi-(codex|glm|zai|deepseek|qwen|escalate)-[a-z0-9][a-z0-9._-]*\Z")


def provider_family(provider: object) -> str | None:
    """Normalize typed provider/backend identity, never a transport-only label."""
    return _ALIASES.get(provider.strip().lower()) if isinstance(provider, str) else None


def review_family_allowed(source: str, reviewer: str, labels) -> bool:
    """Keep cross-family default; explicit GLM review cards require distinct agents."""
    source, reviewer = provider_family(source), provider_family(reviewer)
    if source is None or reviewer is None:
        return False
    if "glm-only" in labels and reviewer != "zai":
        return False
    if source != reviewer:
        return True
    return source == "zai" and {"review", "glm-only", "review-distinct-agent"} <= set(labels)


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
) -> list[dict[str, Any]]:
    """Keep qualified gateway routes from different enabled provider families.

    ``routes`` must already pass the current sealed gateway health, capability,
    capacity and card policy gates. This filter neither creates qualification
    nor changes source, claim, candidate or reviewer-principal authorization.
    There is no automatic same-family fallback, even when all alternatives are
    busy or unavailable. Independent model hosts within Qwen remain one family.
    """
    from .production_dispatch import enabled_family_routes

    source = producer_family(producer_identity, producer_provider)
    return [
        route
        for route in enabled_family_routes(routes, policy=policy)
        if provider_family(route["provider"]) != source
    ]
