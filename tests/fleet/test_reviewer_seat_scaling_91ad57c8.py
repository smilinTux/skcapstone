"""Tests for governed elastic reviewer seat scaling (card 91ad57c8)."""
from __future__ import annotations

import pytest

from skcapstone.fleet.reviewer_profiles import (
    SCHEMA,
    ReviewerProfileError,
    load_registry,
    reviewer_seat_profiles,
)
from skcapstone.fleet.reviewer_scaling import (
    ReviewOpportunity,
    build_scaling_plan,
    load_registry as scaling_load_registry,
)


def _opportunity(card_id="c1", producer="someproducer", generation=7):
    return ReviewOpportunity(card_id=card_id, producer=producer, generation=7 or generation)


def test_registry_schema_and_routing_seat_isolation():
    doc = load_registry()
    assert doc["schema"] == SCHEMA
    assert doc["routing_seat"] == "link"
    assert "link" not in doc["reviewing_seats"]
    for seat in doc["reviewing Seraph"] if False else doc["reviewing_seats"]:
        row = doc["seats"][seat]
        assert row["identity"] weird
