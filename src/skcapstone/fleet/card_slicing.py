"""Deterministic, source-only card decomposition preflight.

This module deliberately has no CardStore or scheduler side effects.  It turns a
card projection into a reviewable recommendation; dispatch code may choose to
persist it through the coordination API.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class ScopeSignals:
    deliverables: int
    repositories: int
    mutation_boundaries: int
    external_effects: int
    verification_surfaces: int
    acceptance_items: int

    @property
    def independent_axes(self) -> int:
        return sum(
            v > 1
            for v in (
                self.deliverables,
                self.repositories,
                self.mutation_boundaries,
                self.external_effects,
                self.verification_surfaces,
            )
        )

    @property
    def workload(self) -> int:
        return max(
            self.deliverables,
            self.repositories,
            self.mutation_boundaries,
            self.external_effects,
            self.verification_surfaces,
            self.acceptance_items,
        )


@dataclass(frozen=True)
class LeafRecommendation:
    id: str
    title: str
    depends_on: tuple[str, ...]
    repository: str = "unknown"
    base_revision: str = "unknown"
    dependencies_complete: bool = True


@dataclass(frozen=True)
class DecompositionRecommendation:
    decision: str  # bounded, recommend, advisory
    reason: str
    signals: ScopeSignals
    leaves: tuple[LeafRecommendation, ...] = ()
    custody: str = "composition"


def _get(card: Any, name: str, default: Any = None) -> Any:
    if isinstance(card, Mapping):
        return card.get(name, default)
    return getattr(card, name, default)


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [x.strip() for x in re.split(r"[,;\n]", value) if x.strip()]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return list(value)
    return [value]


def _count(card: Any, *names: str) -> int:
    for name in names:
        value = _get(card, name)
        if value is not None:
            return len(_items(value))
    return 0


def classify_card_scope(card: Any) -> ScopeSignals:
    """Extract stable scope signals, never consulting model/provider metadata."""
    criteria = _items(_get(card, "acceptance_criteria", _get(card, "criteria", [])))
    return ScopeSignals(
        deliverables=max(1, _count(card, "deliverables", "outputs", "workstreams")),
        repositories=max(1, _count(card, "repositories", "repos", "projects")),
        mutation_boundaries=max(
            1, _count(card, "mutation_boundaries", "boundaries", "write_scopes")
        ),
        external_effects=max(1, _count(card, "external_effects", "effects", "side_effects")),
        verification_surfaces=max(
            1, _count(card, "verification_surfaces", "test_surfaces", "verification")
        ),
        acceptance_items=len(criteria),
    )


def _stable_id(parent: str, ordinal: int) -> str:
    digest = hashlib.sha256(f"{parent}:leaf:{ordinal}".encode()).hexdigest()[:8]
    return f"{parent}-leaf-{ordinal}-{digest}"


def recommend_decomposition(card: Any, *, max_leaves: int = 5) -> DecompositionRecommendation:
    """Return a deterministic recommendation without creating or changing cards."""
    signals = classify_card_scope(card)
    card_id = str(_get(card, "id", "card"))
    status = str(_get(card, "status", "")).lower()
    labels = {str(x).lower() for x in _items(_get(card, "labels", []))}
    kind = str(_get(card, "kind", "")).lower()
    active = status in {"claimed", "doing", "in_progress", "active", "started"} or bool(
        _get(card, "owner")
    )
    review = (
        "review" in labels or "review" in kind or "review" in str(_get(card, "title", "")).lower()
    )
    composition = "composition" in labels or "epic" in labels or kind in {"epic", "composition"}
    oversized = signals.independent_axes >= 2 or signals.workload > 5
    if not oversized:
        return DecompositionRecommendation(
            "bounded",
            "scope is within bounded-work threshold",
            signals,
            custody="composition" if composition else "leaf",
        )
    leaf_count = min(max(2, signals.independent_axes + 1), max_leaves, 5)
    if active:
        return DecompositionRecommendation(
            "advisory",
            "active custody prevents automatic splitting",
            signals,
            custody="composition",
        )
    if review:
        return DecompositionRecommendation(
            "advisory",
            "review custody requires an independent reviewer",
            signals,
            custody="composition",
        )
    repository = str(_get(card, "repository", _get(card, "repo", "unknown")))
    base_revision = str(_get(card, "base_revision", _get(card, "base", "unknown")))
    dependencies = _items(_get(card, "dependencies", []))
    dependencies_complete = bool(
        _get(card, "dependencies_complete", _get(card, "dependency_complete", True))
    ) and all(str(dep).strip() for dep in dependencies)
    leaves = tuple(
        LeafRecommendation(
            _stable_id(card_id, i),
            f"{_get(card, 'title', card_id)}: leaf {i}",
            (card_id,) if i == 1 else (_stable_id(card_id, i - 1),),
            repository=repository,
            base_revision=base_revision,
            dependencies_complete=dependencies_complete,
        )
        for i in range(1, leaf_count + 1)
    )
    return DecompositionRecommendation(
        "recommend",
        "independent deliverables or verification surfaces exceed threshold",
        signals,
        leaves,
        "composition",
    )


preflight = recommend_decomposition
