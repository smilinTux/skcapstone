"""Converge must write the placement manifest, not merely be able to.

Task 6 built ``generate_seat_placement_manifest`` and
``write_seat_placement_manifest`` with ten passing tests and zero production
callers. The dispatcher (``scripts/fleet/skfleet-rotate.py::_load_seat_placement``)
fails closed on a missing manifest, so an unwired generator fixes nothing: the
live ``seat-placement.json`` stays exactly as stale as it was measured. This
file proves the generator is now part of ``converge_lifecycle_seats``'s write
set, gets the same rollback treatment as the control plane, is idempotent, and
still maps every seat to exactly one host.
"""

from __future__ import annotations

import json
from pathlib import Path

from skcapstone.lifecycle_seats import (
    LIFECYCLE_SEATS,
    converge_lifecycle_seats,
    generate_seat_placement_manifest,
    rollback_lifecycle_seats,
)

#: chiap08 is in the chi fleet default rotation hosts, so these tests need no
#: estate.json fixture to satisfy the generator's rotation-host check.
_ACTIVE_HOST = "chiap08"


def _seed_identities(home: Path) -> None:
    for seat in LIFECYCLE_SEATS:
        identity = home / "agents" / seat / "identity/identity.json"
        identity.parent.mkdir(parents=True)
        identity.write_text(json.dumps({"name": seat.title()}))


def test_converge_writes_the_placement_manifest(tmp_path: Path) -> None:
    home = tmp_path / "home"
    rollback = tmp_path / "rollback"
    _seed_identities(home)

    converge_lifecycle_seats(home, rollback, active_host=_ACTIVE_HOST)

    placement_path = home / "coordination/seat-placement.json"
    assert placement_path.is_file()
    written = json.loads(placement_path.read_text())
    assert written == generate_seat_placement_manifest(active_host=_ACTIVE_HOST, home=home)


def test_converge_placement_manifest_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_identities(home)

    converge_lifecycle_seats(home, tmp_path / "rollback-1", active_host=_ACTIVE_HOST)
    first = (home / "coordination/seat-placement.json").read_bytes()
    converge_lifecycle_seats(home, tmp_path / "rollback-2", active_host=_ACTIVE_HOST)
    second = (home / "coordination/seat-placement.json").read_bytes()

    assert first == second


def test_every_seat_maps_to_exactly_one_host_after_converge(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_identities(home)

    converge_lifecycle_seats(home, tmp_path / "rollback", active_host=_ACTIVE_HOST)

    written = json.loads((home / "coordination/seat-placement.json").read_text())
    assert set(written["seats"]) == LIFECYCLE_SEATS
    for seat, hosts in written["seats"].items():
        assert hosts == [_ACTIVE_HOST], f"seat {seat} must map to exactly one host"


def test_rollback_removes_a_newly_created_placement_manifest(tmp_path: Path) -> None:
    home = tmp_path / "home"
    rollback = tmp_path / "rollback"
    _seed_identities(home)

    converge_lifecycle_seats(home, rollback, active_host=_ACTIVE_HOST)
    placement_path = home / "coordination/seat-placement.json"
    assert placement_path.is_file()

    rollback_lifecycle_seats(home, rollback)

    assert not placement_path.exists()


def test_rollback_restores_a_prior_placement_manifest_coherently_with_control_plane(
    tmp_path: Path,
) -> None:
    """Rollback must not restore one file of the pair and drop the other.

    A rolled-back host with a fresh control plane but a stale placement
    manifest (or vice versa) is a mismatched pair, worse than either state
    alone: the dispatcher would read placement data that no longer agrees
    with who the estate elected.
    """
    home = tmp_path / "home"
    rollback = tmp_path / "rollback"
    _seed_identities(home)

    placement_path = home / "coordination/seat-placement.json"
    placement_path.parent.mkdir(parents=True)
    placement_path.write_text('{"schema_version": 1, "seats": {}}\n')
    control_path = home / "coordination/seat-control-plane.json"
    control_path.write_text('{"prior": "control"}\n')

    converge_lifecycle_seats(home, rollback, active_host=_ACTIVE_HOST)
    rollback_lifecycle_seats(home, rollback)

    assert placement_path.read_text() == '{"schema_version": 1, "seats": {}}\n'
    assert control_path.read_text() == '{"prior": "control"}\n'
