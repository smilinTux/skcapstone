"""Provider-neutral routing classification for SKLegal fleet cards."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

LOGICAL_BUCKETS = frozenset({"sk-s", "sk-m", "sk-l", "sk-xl"})
EXACT_GLM_BUCKETS = frozenset({"sk-glm-s", "sk-glm-m", "sk-glm-l"})
PROVIDER_FAMILY_LABELS = frozenset(
    f"{provider}{suffix}"
    for provider in ("astra", "claude", "codex", "fable", "glm", "kimi")
    for suffix in ("", "-lane", "-only", "-specific", "-suitable")
)
QWEN_SOVEREIGN_LABELS = frozenset({"qwen", "qwen-first", "qwen-only", "sk-qwen", "sovereign-qwen"})
SKLEGAL_LABELS = frozenset({"sklegal", "repo:sklegal"})


@dataclass(frozen=True)
class RoutingClassification:
    """One deterministic classification result for creation and dispatch."""

    labels: tuple[str, ...]
    violations: tuple[str, ...]
    normalized: bool = False

    @property
    def valid(self) -> bool:
        return not self.violations

    @property
    def diagnostic(self) -> str:
        return "valid" if self.valid else ";".join(self.violations)


def classify_card_routing(
    labels: Iterable[str], *, normalize_missing: bool = False
) -> RoutingClassification:
    """Classify SKLegal labels, optionally adding the creation-time default.

    Non-SKLegal cards are outside this policy. Explicit sovereign Qwen cards
    do not require a frontier bucket. Provider-family compatibility labels are
    never valid. The caller owns persistence of the returned labels.
    """
    ordered = tuple(dict.fromkeys(str(label).strip() for label in labels if str(label).strip()))
    normalized = {label.lower() for label in ordered}
    if not normalized & SKLEGAL_LABELS:
        return RoutingClassification(ordered, ())

    provider_labels = sorted(normalized & PROVIDER_FAMILY_LABELS)
    buckets = sorted(normalized & (LOGICAL_BUCKETS | EXACT_GLM_BUCKETS))
    sovereign = bool(normalized & QWEN_SOVEREIGN_LABELS)
    output = ordered
    did_normalize = False
    if not buckets and not sovereign and normalize_missing:
        output = (*ordered, "sk-m")
        buckets = ["sk-m"]
        did_normalize = True

    violations: list[str] = []
    if provider_labels:
        violations.append("provider-family-label:" + ",".join(provider_labels))
    if len(buckets) > 1:
        violations.append("multiple-logical-buckets:" + ",".join(buckets))
    elif not buckets and not sovereign:
        violations.append(
            "missing-logical-bucket:add-one-of-sk-s,sk-m,sk-l,sk-xl,sk-glm-s,sk-glm-m,sk-glm-l"
        )
    return RoutingClassification(output, tuple(violations), did_normalize)


def routing_transition_allowed(before: Iterable[str], after: Iterable[str]) -> bool:
    """Allow valid changes and monotonic cleanup of malformed legacy labels."""
    old = classify_card_routing(before)
    new = classify_card_routing(after)
    return new.valid or set(new.violations) < set(old.violations)


def validate_label_transition(
    home: Path, card_id: str, label: str, *, remove: bool
) -> tuple[bool, RoutingClassification]:
    """Validate one folded label amendment and report whether it changes state."""
    from .card_store import CardStore

    card = CardStore(home).fold(card_id)
    if card is None:
        raise ValueError(f"card {card_id} does not exist")
    before = list(card.labels)
    present = label in before
    if present == (not remove):
        return False, classify_card_routing(before)
    after = [value for value in before if value != label] if remove else [*before, label]
    routing = classify_card_routing(after)
    if not routing_transition_allowed(before, after):
        raise ValueError("routing labels rejected: " + routing.diagnostic)
    return True, routing
