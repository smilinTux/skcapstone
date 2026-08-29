#!/usr/bin/env python3
"""Tests for lane affinity enforcement module.

Tests codex-only, glm-only, escalation-only, conflicting labels,
and unlabeled cards across all scenarios.
"""

import sys
from pathlib import Path

import pytest

# Import the lanes module from scripts/fleet/
# ruff: noqa: E402 - sys.path.insert must come before import
_repo_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_repo_root / "scripts" / "fleet"))
from lanes import (
    count_compatible_slots,
    filter_compatible_lanes,
    is_lane_compatible,
    lane_affinity,
    lane_preference_order,
)


class TestLaneAffinity:
    """Test lane_affinity function."""

    def test_no_labels_returns_none(self):
        """Unlabeled cards return None (use default preference order)."""
        assert lane_affinity([]) is None
        assert lane_affinity(None) is None

    def test_codex_only_label(self):
        """Cards with codex-only label require codex lane."""
        assert lane_affinity(["codex-only"]) == "codex"
        assert lane_affinity(["codex-only", "size-m"]) == "codex"
        assert lane_affinity(["size-m", "codex-only"]) == "codex"

    def test_glm_only_label(self):
        """Cards with glm-only label require glm lane."""
        assert lane_affinity(["glm-only"]) == "glm"
        assert lane_affinity(["glm-only", "size-m"]) == "glm"

    def test_escalation_required_label(self):
        """Cards with escalation-required label require escalation lane."""
        assert lane_affinity(["escalation-required"]) == "escalation"
        assert lane_affinity(["escalation-required", "priority"]) == "escalation"

    def test_conflicting_labels_raises(self):
        """Conflicting lane-only labels raise ValueError."""
        with pytest.raises(ValueError, match="Conflicting lane-only labels"):
            lane_affinity(["codex-only", "glm-only"])

        with pytest.raises(ValueError, match="Conflicting lane-only labels"):
            lane_affinity(["codex-only", "escalation-required"])

        with pytest.raises(ValueError, match="Conflicting lane-only labels"):
            lane_affinity(["glm-only", "escalation-required"])

        with pytest.raises(ValueError, match="Conflicting lane-only labels"):
            lane_affinity(["codex-only", "glm-only", "escalation-required"])

    def test_non_lane_labels_ignored(self):
        """Non-lane labels don't affect affinity."""
        assert lane_affinity(["size-m", "priority"]) is None
        assert lane_affinity(["review", "fleet"]) is None


class TestIsLaneCompatible:
    """Test is_lane_compatible function."""

    @pytest.fixture
    def lanes(self):
        """Sample lane configurations."""
        return [
            {"name": "codex", "prefix": "codex-auto-", "free": 2},
            {"name": "glm", "prefix": "glm-auto-", "free": 1},
        ]

    def test_unlabeled_card_compatible_with_any_lane(self, lanes):
        """Unlabeled cards can use any lane."""
        assert is_lane_compatible(None, lanes[0]) is True
        assert is_lane_compatible(None, lanes[1]) is True

    def test_codex_only_card_compatible_only_with_codex(self, lanes):
        """codex-only cards can only use codex lane."""
        assert is_lane_compatible("codex", lanes[0]) is True
        assert is_lane_compatible("codex", lanes[1]) is False

    def test_glm_only_card_compatible_only_with_glm(self, lanes):
        """glm-only cards can only use glm lane."""
        assert is_lane_compatible("glm", lanes[0]) is False
        assert is_lane_compatible("glm", lanes[1]) is True


class TestCountCompatibleSlots:
    """Test count_compatible_slots function."""

    @pytest.fixture
    def lanes(self):
        """Sample lane configurations with varying free slots."""
        return [
            {"name": "codex", "prefix": "codex-auto-", "free": 3},
            {"name": "glm", "prefix": "glm-auto-", "free": 2},
        ]

    def test_unlabeled_card_counts_all_slots(self, lanes):
        """Unlabeled cards count all free slots."""
        assert count_compatible_slots([], lanes) == 5
        assert count_compatible_slots(["size-m"], lanes) == 5

    def test_codex_only_card_counts_only_codex_slots(self, lanes):
        """codex-only cards count only codex free slots."""
        assert count_compatible_slots(["codex-only"], lanes) == 3

    def test_glm_only_card_counts_only_glm_slots(self, lanes):
        """glm-only cards count only glm free slots."""
        assert count_compatible_slots(["glm-only"], lanes) == 2

    def test_incompatible_lane_returns_zero(self, lanes):
        """Card requiring unavailable lane returns 0."""
        # escalation-required lane not in lanes list
        assert count_compatible_slots(["escalation-required"], lanes) == 0


class TestFilterCompatibleLanes:
    """Test filter_compatible_lanes function."""

    @pytest.fixture
    def lanes(self):
        """Sample lane configurations."""
        return [
            {"name": "codex", "prefix": "codex-auto-", "free": 2},
            {"name": "glm", "prefix": "glm-auto-", "free": 1},
        ]

    def test_unlabeled_card_returns_all_lanes(self, lanes):
        """Unlabeled cards can use all lanes."""
        result = filter_compatible_lanes([], lanes)
        assert result == lanes

    def test_codex_only_returns_only_codex(self, lanes):
        """codex-only cards filter to only codex lane."""
        result = filter_compatible_lanes(["codex-only"], lanes)
        assert len(result) == 1
        assert result[0]["name"] == "codex"

    def test_glm_only_returns_only_glm(self, lanes):
        """glm-only cards filter to only glm lane."""
        result = filter_compatible_lanes(["glm-only"], lanes)
        assert len(result) == 1
        assert result[0]["name"] == "glm"


class TestLanePreferenceOrder:
    """Test lane_preference_order function."""

    @pytest.fixture
    def lanes(self):
        """Sample lane configurations."""
        return [
            {"name": "codex", "prefix": "codex-auto-", "free": 2},
            {"name": "glm", "prefix": "glm-auto-", "free": 1},
        ]

    def test_glm_first_then_codex(self, lanes):
        """GLM lane comes before Codex for unlabeled cards."""
        result = lane_preference_order(lanes)
        assert len(result) == 2
        assert result[0]["name"] == "glm"
        assert result[1]["name"] == "codex"


class TestIntegrationScenarios:
    """Integration tests for real-world scenarios."""

    @pytest.fixture
    def lanes(self):
        """Sample lane configurations for integration tests."""
        return [
            {"name": "codex", "prefix": "codex-auto-", "free": 2},
            {"name": "glm", "prefix": "glm-auto-", "free": 1},
        ]

    def test_card_12eaed95_codex_only_scenario(self, lanes):
        """Reproduce card 12eaed95: codex-only label should enforce codex lane."""
        labels = ["skcoord", "graph-truth", "codex", "independent-review",
                  "review", "size-m", "codex-only", "no-runtime-mutation"]

        card_lane = lane_affinity(labels)
        assert card_lane == "codex", "codex-only should require codex lane"

        compatible = filter_compatible_lanes(labels, lanes)
        assert len(compatible) == 1, "Should have exactly 1 compatible lane"
        assert compatible[0]["name"] == "codex", "Should be codex lane"

        # Not compatible with glm lane
        assert not is_lane_compatible(card_lane, lanes[1]), "codex-only not compatible with glm"

    def test_card_ac8592fc_codex_only_scenario(self, lanes):
        """Reproduce card ac8592fc: codex-only label should enforce codex lane."""
        labels = ["skcapstone", "skcoord", "graph-truth", "cli", "mcp",
                  "packaging", "codex", "independent-review", "rereview",
                  "size-m", "codex-only", "no-runtime-mutation"]

        card_lane = lane_affinity(labels)
        assert card_lane == "codex", "codex-only should require codex lane"

        compatible = filter_compatible_lanes(labels, lanes)
        assert len(compatible) == 1, "Should have exactly 1 compatible lane"
        assert compatible[0]["name"] == "codex", "Should be codex lane"

    def test_glm_only_card_scenario(self, lanes):
        """Test glm-only card enforces glm lane."""
        labels = ["some-task", "glm-only", "size-m"]

        card_lane = lane_affinity(labels)
        assert card_lane == "glm", "glm-only should require glm lane"

        compatible = filter_compatible_lanes(labels, lanes)
        assert len(compatible) == 1, "Should have exactly 1 compatible lane"
        assert compatible[0]["name"] == "glm", "Should be glm lane"

    def test_unlabeled_card_uses_preference_order(self, lanes):
        """Unlabeled cards use default GLM-first preference order."""
        labels = ["some-task", "size-m", "fleet"]

        card_lane = lane_affinity(labels)
        assert card_lane is None, "Unlabeled card should have no lane constraint"

        compatible = filter_compatible_lanes(labels, lanes)
        assert len(compatible) == 2, "Should have all lanes available"

        ordered = lane_preference_order(compatible)
        assert ordered[0]["name"] == "glm", "GLM should come first"
        assert ordered[1]["name"] == "codex", "Codex should come second"

    def test_no_compatible_free_slot_scenario(self, lanes):
        """Card with lane constraint but no free slots in that lane."""
        labels = ["codex-only"]

        # Set codex free slots to 0
        lanes[0]["free"] = 0

        compatible_slots = count_compatible_slots(labels, lanes)
        assert compatible_slots == 0, "Should have 0 compatible free slots"

        compatible_lanes = filter_compatible_lanes(labels, lanes)
        assert len(compatible_lanes) == 1, "Should still find the compatible lane"
        assert compatible_lanes[0]["free"] == 0, "But it has 0 free slots"

    def test_conflicting_labels_excluded_from_pool(self):
        """Cards with conflicting lane labels should be excluded."""
        labels = ["codex-only", "glm-only"]

        with pytest.raises(ValueError, match="Conflicting lane-only labels"):
            lane_affinity(labels)


class TestAllHostPartitions:
    """Test lane affinity works across all five host partitions."""

    @pytest.fixture
    def all_lanes(self):
        """Lanes with all host partitions."""
        return [
            {"name": "codex", "prefix": "codex-auto-", "free": 2},
            {"name": "glm", "prefix": "glm-auto-", "free": 1},
        ]

    def test_chiap01_partition(self, all_lanes):
        """Test chiap01 partition (offset 0)."""
        labels = ["codex-only"]
        card_lane = lane_affinity(labels)
        assert card_lane == "codex"
        compatible = filter_compatible_lanes(labels, all_lanes)
        assert len(compatible) == 1
        assert compatible[0]["name"] == "codex"

    def test_chiap02_partition(self, all_lanes):
        """Test chiap02 partition (offset 1)."""
        labels = ["glm-only"]
        card_lane = lane_affinity(labels)
        assert card_lane == "glm"
        compatible = filter_compatible_lanes(labels, all_lanes)
        assert len(compatible) == 1
        assert compatible[0]["name"] == "glm"

    def test_chiap03_partition(self, all_lanes):
        """Test chiap03 partition (offset 2)."""
        labels = ["codex-only", "review"]
        card_lane = lane_affinity(labels)
        assert card_lane == "codex"
        compatible = filter_compatible_lanes(labels, all_lanes)
        assert len(compatible) == 1
        assert compatible[0]["name"] == "codex"

    def test_chiap08_partition(self, all_lanes):
        """Test chiap08 partition (offset 3)."""
        labels = ["glm-only", "size-m"]
        card_lane = lane_affinity(labels)
        assert card_lane == "glm"
        compatible = filter_compatible_lanes(labels, all_lanes)
        assert len(compatible) == 1
        assert compatible[0]["name"] == "glm"

    def test_chiap04_partition(self, all_lanes):
        """Test chiap04 partition (offset 4)."""
        labels = ["codex-only"]
        card_lane = lane_affinity(labels)
        assert card_lane == "codex"
        compatible = filter_compatible_lanes(labels, all_lanes)
        assert len(compatible) == 1
        assert compatible[0]["name"] == "codex"


class TestFoldedLabelEvents:
    """Test lane affinity handles folded add and remove label events."""

    def test_add_codex_only(self):
        """Adding codex-only label changes lane affinity."""
        labels = []
        assert lane_affinity(labels) is None

        labels.append("codex-only")
        assert lane_affinity(labels) == "codex"

    def test_remove_codex_only(self):
        """Removing codex-only label removes lane constraint."""
        labels = ["codex-only", "size-m"]
        assert lane_affinity(labels) == "codex"

        labels.remove("codex-only")
        assert lane_affinity(labels) is None

    def test_replace_codex_only_with_glm_only(self):
        """Replacing codex-only with glm-only changes lane."""
        labels = ["codex-only", "size-m"]
        assert lane_affinity(labels) == "codex"

        labels.remove("codex-only")
        labels.append("glm-only")
        assert lane_affinity(labels) == "glm"

    def test_multiple_non_lane_labels_ignored(self):
        """Multiple non-lane labels don't create conflicts."""
        labels = ["size-m", "priority", "review", "fleet", "scheduler"]
        assert lane_affinity(labels) is None

        labels.append("codex-only")
        assert lane_affinity(labels) == "codex"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
