"""Deterministic, source-only card decomposition preflight.

This module deliberately has no CardStore or scheduler side effects.  It turns a
card projection into a reviewable recommendation; dispatch code may choose to
persist it through the coordination API.
"""

from __future__ import annotations

import hashlib
import json
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
    repositories: tuple[str, ...] = ()
    base_identities: tuple[str, ...] = ()
    composition_sha256: str = ""


@dataclass(frozen=True)
class CompositionVerificationContract:
    """Proof obligation retained by the parent after all leaves complete."""

    leaf_ids: tuple[str, ...]
    repositories: tuple[str, ...]
    base_identities: tuple[str, ...]
    deliverables: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    verification_scope: tuple[str, ...]
    focused_gates: tuple[str, ...]
    mutation_boundaries: tuple[str, ...]
    external_effects: tuple[str, ...]
    dependencies: tuple[str, ...]
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
    successor_mismatches: tuple[str, ...] = ()


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


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _leaf_manifest(leaf: LeafRecommendation) -> dict[str, Any]:
    """Return the complete, stable assignment contract for one leaf."""
    return {
        "id": leaf.id,
        "title": leaf.title,
        "repositories": list(leaf.repositories),
        "base_identities": list(leaf.base_identities),
        "deliverables": list(leaf.deliverables),
        "acceptance_criteria": list(leaf.acceptance_criteria),
        "verification_scope": list(leaf.verification_scope),
        "focused_gates": list(leaf.focused_gates),
        "mutation_boundaries": list(leaf.mutation_boundaries),
        "external_effects": list(leaf.external_effects),
        "dependencies": list(leaf.depends_on),
    }


def _composition_digest(leaves: Sequence[LeafRecommendation]) -> str:
    return hashlib.sha256(_canonical_json([_leaf_manifest(leaf) for leaf in leaves])).hexdigest()


def _successor_matches(
    successor: Any, expected: LeafRecommendation, composition_sha256: str
) -> bool:
    """Bare IDs never prove idempotency; exact canonical content and digest do."""
    if not isinstance(successor, Mapping):
        return False
    actual = {key: successor.get(key) for key in _leaf_manifest(expected)}
    return (
        actual == _leaf_manifest(expected)
        and successor.get("composition_sha256") == composition_sha256
    )


def _repository_identities(card: Any) -> tuple[tuple[str, str], ...]:
    repositories = _strings(card, "repositories", "repos", "projects", "repository", "repo")
    bases = _strings(
        card, "base_refs", "base_references", "bases", "base_ref", "base_reference", "base"
    )
    if not repositories or not bases:
        return ()
    if len(bases) == 1:
        bases = bases * len(repositories)
    if len(repositories) != len(bases):
        return ()
    return tuple(zip(repositories, bases, strict=True))


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
    identities = _repository_identities(card)
    if not identities:
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
            "reject",
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
    if len(identities) > 1:
        leaf_count = min(leaf_count, len(identities))
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
    if len(identities) == 1:
        identity_parts = tuple((identities[0],) for _ in range(leaf_count))
    else:
        identity_parts = _partition(identities, leaf_count)
    leaf_ids = tuple(_stable_id(card_id, i) for i in range(1, leaf_count + 1))
    assigned_leaves = tuple(
        LeafRecommendation(
            leaf_ids[i - 1],
            f"{_get(card, 'title', card_id)}: leaf {i}",
            dependencies,
            identity_parts[i - 1][0][0],
            identity_parts[i - 1][0][1],
            deliverable_parts[i - 1],
            criteria_parts[i - 1],
            verification_parts[i - 1],
            gate_parts[i - 1],
            mutation_parts[i - 1],
            effect_parts[i - 1],
            tuple(repository for repository, _ in identity_parts[i - 1]),
            tuple(f"{repository}@{base}" for repository, base in identity_parts[i - 1]),
        )
        for i in range(1, leaf_count + 1)
    )
    coverage_sha256 = _composition_digest(assigned_leaves)
    assigned_leaves = tuple(
        LeafRecommendation(
            **{
                **leaf.__dict__,
                "composition_sha256": coverage_sha256,
            }
        )
        for leaf in assigned_leaves
    )
    existing_successors = _items(_linked(card, "successor_cards", "successors", "successor") or [])
    existing_by_id: dict[str, list[Mapping[str, Any]]] = {}
    for successor in existing_successors:
        if isinstance(successor, Mapping) and successor.get("id"):
            existing_by_id.setdefault(str(successor["id"]), []).append(successor)
    bare_ids = {
        str(successor) for successor in existing_successors if not isinstance(successor, Mapping)
    }
    mismatches = tuple(
        leaf.id
        for leaf in assigned_leaves
        if leaf.id in bare_ids
        or (
            leaf.id in existing_by_id
            and (
                len(existing_by_id[leaf.id]) != 1
                or not _successor_matches(existing_by_id[leaf.id][0], leaf, coverage_sha256)
            )
        )
    )
    leaves = tuple(
        leaf
        for leaf in assigned_leaves
        if len(existing_by_id.get(leaf.id, ())) != 1
        or not _successor_matches(existing_by_id[leaf.id][0], leaf, coverage_sha256)
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
            repositories=tuple(repository for repository, _ in identities),
            base_identities=tuple(f"{repository}@{base}" for repository, base in identities),
            deliverables=deliverables,
            acceptance_criteria=criteria,
            verification_scope=verification,
            focused_gates=gates,
            mutation_boundaries=_strings(
                card, "mutation_boundaries", "boundaries", "write_scopes"
            ),
            external_effects=_strings(card, "external_effects", "effects", "side_effects"),
            dependencies=dependencies,
            coverage_sha256=coverage_sha256,
        ),
        mismatches,
    )


preflight = recommend_decomposition
