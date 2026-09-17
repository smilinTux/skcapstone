"""Contract tests for the five lifecycle seats after tank folds into atlas.

ADR-0005 names ATLAS as Operations and tank's real activity (approved-artifact
release, install, behavioral verification, and rollback) is a subset of that
role. ATLAS absorbs tank; the LIFECYCLE_SEATS roster shrinks from six seats to
five and `tank` is no longer a valid seat name anywhere the roster is checked.
"""

from __future__ import annotations

import pytest

from skcapstone import lifecycle_seats
from skcapstone.lifecycle_seats import (
    LIFECYCLE_SEATS,
    load_lifecycle_seat_profiles,
    load_seat_control_plane,
)
from skcapstone.seat_cycle_guard import SeatCycleGuard


def test_lifecycle_seats_is_exactly_five_seats() -> None:
    """tank is folded into atlas; the roster is link, mero, seraph, niobe, atlas."""
    assert LIFECYCLE_SEATS == frozenset({"link", "mero", "seraph", "niobe", "atlas"})
    assert len(LIFECYCLE_SEATS) == 5


def test_tank_is_rejected_as_a_seat_name(tmp_path) -> None:
    """tank is not a lifecycle seat; anything validating a seat name refuses it."""
    with pytest.raises(ValueError, match="unsupported recurring seat"):
        SeatCycleGuard(tmp_path, "tank")


def test_atlas_profile_carries_tanks_ported_on_charter_duties() -> None:
    """Atlas absorbs tank's on-charter release/install/verify/rollback duties.

    tank's own off-charter behaviour (editing source) is not carried forward:
    atlas's denies list keeps the same source-authoring fence tank had.
    """
    seats = load_lifecycle_seat_profiles()["seats"]
    assert "tank" not in seats
    atlas = seats["atlas"]
    for duty in (
        "approved_artifact_release",
        "approved_artifact_install",
        "behavioral_verification",
        "rollback",
    ):
        assert duty in atlas["owns"], f"missing ported duty: {duty}"
    assert "source_authoring" in atlas["denies"]


def test_load_lifecycle_seat_profiles_raises_on_roster_mismatch(monkeypatch) -> None:
    """The profile loader still fails closed on an unexpected roster."""
    monkeypatch.setattr(
        lifecycle_seats,
        "LIFECYCLE_SEATS",
        frozenset({"link", "mero", "seraph", "niobe", "atlas", "ghost"}),
    )
    with pytest.raises(ValueError, match="lifecycle seat set does not match"):
        load_lifecycle_seat_profiles()


def test_load_seat_control_plane_raises_on_roster_mismatch(monkeypatch) -> None:
    """The control-plane loader still fails closed on an unexpected roster."""
    monkeypatch.setattr(
        lifecycle_seats,
        "LIFECYCLE_SEATS",
        frozenset({"link", "mero", "seraph", "niobe", "atlas", "ghost"}),
    )
    with pytest.raises(ValueError, match="seat control plane must contain exactly"):
        load_seat_control_plane("some-host")
