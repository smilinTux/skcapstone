"""Resolve card size and semantic intent to provider-neutral SKGateway routes."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Protocol

_SIZE_PATTERN = re.compile(r"\[(S|M|XL|L)\]")
_LITERAL_MODEL_PATTERN = re.compile(r"(?:[/]|\d)")
_SIZE_ROUTES: dict[str, tuple[str, ...]] = {
    "S": ("sk-s", "sk-m", "sk-l"),
    "M": ("sk-m", "sk-l"),
    "L": ("sk-l",),
    "XL": ("sk-l",),
}
_SOVEREIGN_MARKERS = {"qwen-first", "semantic-lane:sovereign-corpus"}
_SOVEREIGN_ROUTE = "sk-default"


class _PreflightResult(Protocol):
    served_identity: str
    provider: str


class RouteSelectionError(ValueError):
    """A card cannot be routed without guessing or weakening its contract."""

    def __init__(self, code: str, message: str) -> None:
        """Create a machine-readable routing refusal.

        Args:
            code: Stable refusal classification.
            message: Bounded operator-readable detail.
        """

        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkerRoutePlan:
    """Exact routing facts included in one worker brief."""

    card_size: str
    semantic_lane: str
    route_candidates: tuple[str, ...]
    selected_route: str
    fallback_reason: str
    served_model: str
    served_provider: str
    attribution_phase: str


def _semantic_lane(labels: Sequence[str]) -> str:
    """Return the explicitly selected semantic lane.

    Args:
        labels: Folded card labels.

    Returns:
        ``general`` or ``sovereign-corpus``.

    Raises:
        RouteSelectionError: The labels name an unknown or implicit lane.
    """

    normalized = {str(label).strip().lower() for label in labels}
    declared = {label for label in normalized if label.startswith("semantic-lane:")}
    if "semantic-lane:general" in declared:
        declared.remove("semantic-lane:general")
        if declared or normalized & _SOVEREIGN_MARKERS:
            raise RouteSelectionError(
                "conflicting_semantic_lane", "card declares conflicting semantic lanes"
            )
        return "general"
    unknown = declared - {"semantic-lane:sovereign-corpus"}
    if unknown:
        raise RouteSelectionError(
            "invalid_semantic_lane",
            "unknown semantic lane: " + ", ".join(sorted(unknown)),
        )
    if "sovereign-corpus" in normalized:
        raise RouteSelectionError(
            "semantic_lane_marker_required",
            "sovereign corpus work requires an explicit semantic-lane marker",
        )
    if normalized & _SOVEREIGN_MARKERS:
        return "sovereign-corpus"
    return "general"


def logical_route_candidates(size: str, labels: Sequence[str] = ()) -> tuple[str, ...]:
    """Resolve a size and explicit semantic marker to ordered logical routes.

    A larger general route may satisfy smaller work. A smaller route is never a
    fallback for larger work. XL folds onto the reviewed large route because the
    fleet exposes exactly three T-shirt route classes.

    Args:
        size: Card size, one of S, M, L, or XL.
        labels: Folded card labels carrying any semantic-lane marker.

    Returns:
        Ordered, provider-neutral logical route candidates.

    Raises:
        RouteSelectionError: The size or semantic marker cannot be resolved.
    """

    value = str(size or "").strip()
    if _LITERAL_MODEL_PATTERN.search(value):
        raise RouteSelectionError(
            "literal_model_refused", f'refusing literal model name "{value}" as card size'
        )
    normalized = value.upper()
    if normalized not in _SIZE_ROUTES:
        raise RouteSelectionError(
            "invalid_card_size", f'unknown card size "{value}"; use S, M, L, or XL'
        )
    if _semantic_lane(labels) == "sovereign-corpus":
        return (_SOVEREIGN_ROUTE,)
    return _SIZE_ROUTES[normalized]


def _card_size(title: str) -> str:
    """Extract exactly one T-shirt size from a card title.

    Args:
        title: Folded card title.

    Returns:
        The canonical uppercase size.

    Raises:
        RouteSelectionError: The title has no size or more than one size.
    """

    matches = _SIZE_PATTERN.findall(str(title or ""))
    if len(matches) != 1:
        raise RouteSelectionError(
            "ambiguous_card_size", "card title must contain exactly one S, M, L, or XL marker"
        )
    return matches[0]


def build_worker_route_plan(
    title: str,
    labels: Sequence[str],
    *,
    preflight: Callable[[str], _PreflightResult] | None = None,
) -> WorkerRoutePlan:
    """Choose the first available logical route and retain exact attribution.

    Args:
        title: Folded card title.
        labels: Folded card labels.
        preflight: Optional SKGateway preflight called once per candidate.

    Returns:
        A complete route plan suitable for serialized worker-brief inclusion.

    Raises:
        RouteSelectionError: No route can be selected without guessing.
    """

    size = _card_size(title)
    semantic_lane = _semantic_lane(labels)
    candidates = logical_route_candidates(size, labels)
    if preflight is None:
        return WorkerRoutePlan(
            card_size=size,
            semantic_lane=semantic_lane,
            route_candidates=candidates,
            selected_route=candidates[0],
            fallback_reason="preflight pending",
            served_model="pending runtime attribution",
            served_provider="pending runtime attribution",
            attribution_phase="pending",
        )

    failures: list[str] = []
    for route in candidates:
        try:
            result = preflight(route)
        except ValueError as exc:
            failures.append(f"{route} unavailable: {str(exc)[:120]}")
            continue
        served = str(result.served_identity or "").strip()
        if not served:
            failures.append(f"{route} unavailable: preflight omitted served identity")
            continue
        return WorkerRoutePlan(
            card_size=size,
            semantic_lane=semantic_lane,
            route_candidates=candidates,
            selected_route=route,
            fallback_reason="; ".join(failures) or "primary route available",
            served_model=served,
            served_provider=str(getattr(result, "provider", "") or "unknown"),
            attribution_phase="preflight",
        )
    raise RouteSelectionError(
        "route_unavailable",
        "no logical route available in order "
        + ", ".join(candidates)
        + ": "
        + "; ".join(failures),
    )


def format_worker_route_brief(plan: WorkerRoutePlan) -> str:
    """Serialize a route plan as one stable worker-brief section.

    Args:
        plan: Exact plan returned by :func:`build_worker_route_plan`.

    Returns:
        A newline-terminated JSON routing section.
    """

    payload = asdict(plan)
    payload["route_candidates"] = list(plan.route_candidates)
    return "WORKER ROUTING:\n" + json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
