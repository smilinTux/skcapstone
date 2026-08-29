#!/usr/bin/env python3
"""Lane affinity enforcement module for fleet rotation.

Adds deterministic lane compatibility from folded labels:
- codex-only cards may use only Codex
- glm-only cards may use only GLM
- escalation-required cards may use only the escalation lane
- ordinary unlabeled cards retain the configured preference order
- Conflicting lane-only labels fail closed with an explicit reason
"""

from typing import Dict, List, Optional

# Known lane-only labels
LANE_LABELS = {
    "codex": "codex-only",
    "glm": "glm-only",
    "escalation": "escalation-required",
}


def lane_affinity(labels: List[str]) -> Optional[str]:
    """Determine the required lane for a card based on its folded labels.

    Args:
        labels: Folded labels list from the card.

    Returns:
        Lane name ("codex", "glm", "escalation") if a lane constraint exists,
        None for unlabeled cards (use default preference order).

    Raises:
        ValueError: If conflicting lane-only labels are present.
    """
    if not labels:
        return None

    # Find all lane-only labels present
    lane_names = []
    for lane_name, label_name in LANE_LABELS.items():
        if label_name in labels:
            lane_names.append(lane_name)

    # No lane constraints
    if not lane_names:
        return None

    # Single lane constraint
    if len(lane_names) == 1:
        return lane_names[0]

    # Conflicting lane constraints - fail closed
    raise ValueError(
        f"Conflicting lane-only labels: {', '.join(lane_names)}. "
        f"A card may have at most one lane-only label."
    )


def is_lane_compatible(card_lane: Optional[str], lane: Dict) -> bool:
    """Check if a card's lane requirement matches a given lane.

    Args:
        card_lane: Lane name required by the card, or None for any lane.
        lane: Lane dictionary with "name" key.

    Returns:
        True if the card can use this lane.
    """
    # Unlabeled cards can use any lane (subject to preference order)
    if card_lane is None:
        return True
    # Lane-constrained cards can only use their specific lane
    return lane["name"] == card_lane


def count_compatible_slots(labels: List[str], lanes: List[Dict]) -> int:
    """Count total free slots across all lanes compatible with this card.

    Args:
        labels: Card's folded labels.
        lanes: List of lane dicts with "name" and "free" keys.

    Returns:
        Number of compatible free slots.
    """
    card_lane = lane_affinity(labels)
    if card_lane is None:
        # Unlabeled: sum all free slots
        return sum(lane["free"] for lane in lanes)
    # Lane-constrained: only count that lane
    for lane in lanes:
        if lane["name"] == card_lane:
            return lane["free"]
    return 0


def filter_compatible_lanes(
    labels: List[str], lanes: List[Dict]
) -> List[Dict]:
    """Filter lanes to only those compatible with the card.

    Args:
        labels: Card's folded labels.
        lanes: List of lane dicts.

    Returns:
        Filtered list of compatible lanes, in original order.
    """
    card_lane = lane_affinity(labels)
    if card_lane is None:
        # Unlabeled: all lanes are compatible
        return lanes
    # Lane-constrained: only that lane
    return [lane for lane in lanes if lane["name"] == card_lane]


def lane_preference_order(lanes: List[Dict]) -> List[Dict]:
    """Return lanes in configured preference order for unlabeled cards.

    Current preference: GLM first, then Codex. Escalation-only cards have
    their own lane handled by lane_affinity().

    Args:
        lanes: List of lane dicts.

    Returns:
        Sorted list of lanes in preference order.
    """
    return sorted(lanes, key=lambda lane: 0 if lane["name"] == "glm" else 1)


__all__ = [
    "LANE_LABELS",
    "lane_affinity",
    "is_lane_compatible",
    "count_compatible_slots",
    "filter_compatible_lanes",
    "lane_preference_order",
]
