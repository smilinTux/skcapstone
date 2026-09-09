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
    repository: str
    base_ref: str
    deliverables: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    verification_scope: tuple[str, ...]
    focused_gates: tuple[str, ...]
    mutation_boundaries: tuple[str, ...] = ()
    external_effects: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompositionVerificationContract:
    """Proof obligation retained by the parent after all leaves complete."""

    leaf_ids: tuple[str, ...]
    deliverables: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    verification_scope: tuple[str, ...]
    focused_gates: tuple[str, ...]
    coverage_sha256: str
    checks: tuple[str, ...] = (
        "every parent item is assigned to exactly one leaf",
        "no leaf partition overlaps another leaf partition",
        "every leaf focused gate passes",
        "parent composition and full-suite verification pass",
    )


@dataclass(frozen=True)
class DecompositionRecommendation:
    decision: str  # bounded, reject, advisory
    reason: str
    signals: ScopeSignals
    leaves: tuple[LeafRecommendation, ...] = ()
    custody: str = "composition"
    parent_dependencies: tuple[str, ...] = ()
    composition_verification: CompositionVerificationContract | None = None


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


def _linked(card: Any, *names: str) -> Any:
    """Return the first direct or linked card value for ``names``."""
    links = _get(card, "links", {})
    for name in names:
        value = _get(card, name)
        if value is None and isinstance(links, Mapping):
            value = links.get(name)
        if value is not None:
            return value
    return None


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


def _strings(card: Any, *names: str) -> tuple[str, ...]:
    value = _linked(card, *names)
    if value is None:
        return ()
    items = _items(value)
    if any(not isinstance(item, str) or not item.strip() for item in items):
        return ()
    return tuple(item.strip() for item in items)


def _partition(items: tuple[str, ...], count: int) -> tuple[tuple[str, ...], ...]:
    """Assign every ordered item exactly once using stable round-robin buckets."""
    buckets: list[list[str]] = [[] for _ in range(count)]
    for index, item in enumerate(items):
        buckets[index % count].append(item)
    return tuple(tuple(bucket) for bucket in buckets)


def _coverage_digest(*groups: tuple[str, ...]) -> str:
    payload = "\n".join(
        f"{group_index}:{item_index}:{item}"
        for group_index, group in enumerate(groups)
        for item_index, item in enumerate(group)
    )
    return hashlib.sha256(payload.encode()).hexdigest()


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
    leaf_limit = min(5, max(2, max_leaves))
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
    raw_repository = _linked(card, "repository", "repo")
    raw_base_ref = _linked(card, "base_ref", "base_reference", "base")
    repository = raw_repository.strip() if isinstance(raw_repository, str) else ""
    base_ref = raw_base_ref.strip() if isinstance(raw_base_ref, str) else ""
    if not repository or not base_ref:
        return DecompositionRecommendation(
            "advisory",
            "repository and base_ref are required for leaf recommendations",
            signals,
            custody="composition",
        )
    raw_dependencies = _items(_get(card, "dependencies", []))
    if any(not isinstance(value, str) or not value.strip() for value in raw_dependencies):
        return DecompositionRecommendation(
            "advisory",
            "dependencies must be non-empty card ids",
            signals,
            custody="composition",
        )
    dependencies = tuple(sorted({value.strip() for value in raw_dependencies}))
    deliverables = _strings(card, "deliverables", "outputs", "workstreams")
    criteria = _strings(card, "acceptance_criteria", "criteria")
    verification = _strings(card, "verification_surfaces", "test_surfaces", "verification")
    gates = _strings(card, "focused_gates", "test_commands", "gates")
    if min(len(deliverables), len(criteria), len(verification), len(gates)) < 2:
        return DecompositionRecommendation(
            "advisory",
            "card is unsliceable without at least two concrete deliverables, "
            "acceptance criteria, verification scopes, and focused gates",
            signals,
            custody="composition",
        )
    leaf_count = min(
        max(2, signals.independent_axes + 1, (signals.workload + 1) // 2),
        leaf_limit,
        len(deliverables),
        len(criteria),
        len(verification),
        len(gates),
    )
    deliverable_parts = _partition(deliverables, leaf_count)
    criteria_parts = _partition(criteria, leaf_count)
    verification_parts = _partition(verification, leaf_count)
    gate_parts = _partition(gates, leaf_count)
    mutation_parts = _partition(
        _strings(card, "mutation_boundaries", "boundaries", "write_scopes"), leaf_count
    )
    effect_parts = _partition(
        _strings(card, "external_effects", "effects", "side_effects"), leaf_count
    )
    existing_successors = {
        str(value) for value in _items(_linked(card, "successors", "successor") or [])
    }
    leaf_ids = tuple(_stable_id(card_id, i) for i in range(1, leaf_count + 1))
    leaves = tuple(
        LeafRecommendation(
            leaf_ids[i - 1],
            f"{_get(card, 'title', card_id)}: leaf {i}",
            dependencies,
            repository,
            base_ref,
            deliverable_parts[i - 1],
            criteria_parts[i - 1],
            verification_parts[i - 1],
            gate_parts[i - 1],
            mutation_parts[i - 1],
            effect_parts[i - 1],
        )
        for i in range(1, leaf_count + 1)
        if leaf_ids[i - 1] not in existing_successors
    )
    return DecompositionRecommendation(
        "reject",
        "oversized unclaimed card requires bounded decomposition before dispatch",
        signals,
        leaves,
        "composition",
        leaf_ids,
        CompositionVerificationContract(
            leaf_ids=leaf_ids,
            deliverables=deliverables,
            acceptance_criteria=criteria,
            verification_scope=verification,
            focused_gates=gates,
            coverage_sha256=_coverage_digest(deliverables, criteria, verification, gates),
        ),
    )


preflight = recommend_decomposition
