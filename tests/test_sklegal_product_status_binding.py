"""Immutable SKLegal Product Status binding for scheduler admission."""

from __future__ import annotations

import json
from pathlib import Path

from skcapstone.fleet_lane_health import MAX_AGE_SECONDS


def test_product_status_binding_preserves_routes_and_freshness_policy() -> None:
    path = Path(__file__).parents[1] / "docs/fleet/sklegal-product-status-binding.json"
    binding = json.loads(path.read_text(encoding="utf-8"))

    assert binding["card"] == "1480a869"
    assert binding["product_status"]["review_verdict"] == "PASS"
    assert binding["routing"] == {
        "ordinary_logical_buckets": ["sk-s", "sk-m", "sk-l", "sk-xl"],
        "provider_neutral": True,
        "qwen_sovereign_exception": True,
    }
    assert binding["scheduler_admission"] == {
        "backend_observation_max_age_seconds": MAX_AGE_SECONDS,
        "excessive_future_observations_denied": True,
        "stale_observations_denied": True,
    }
    assert binding["source_only"] is True
