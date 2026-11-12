"""Converge must refuse to overwrite a synced election it disagrees with.

`converge_lifecycle_seats` derives `active_host` from the LOCAL machine and
writes it into both the seat control plane and the generated placement
manifest. Before the placement generator was wired in
(``tests/test_seat_placement_wiring.py``), a stale hand-maintained placement
file was an accidental brake: a converge run on a non-elected host produced a
pin conflict and refused. Wiring the generator removed that brake, so running
the documented runbook command on the wrong host now silently re-elects that
host. During the Syncthing replication window the old elected host and the
new one each read a record naming themselves, and both dispatch: the exact
double-dispatch this estate has no other defence against
(``active_host`` is the only cross-host exclusion it has, per
``skcapstone.estate``).

This proves converge refuses when a synced control-plane record already
names a different ``active_host`` than the one about to be written, unless
an explicit ``force`` override is passed, and that the refusal names both
hosts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.lifecycle_seats import LIFECYCLE_SEATS, converge_lifecycle_seats


def _seed_identities(home: Path) -> None:
    for seat in LIFECYCLE_SEATS:
        identity = home / "agents" / seat / "identity/identity.json"
        identity.parent.mkdir(parents=True)
        identity.write_text(json.dumps({"name": seat.title()}))


def _seed_existing_election(home: Path, active_host: str) -> None:
    control = home / "coordination/seat-control-plane.json"
    control.parent.mkdir(parents=True, exist_ok=True)
    control.write_text(json.dumps({"schema_version": 1, "active_host": active_host, "seats": {}}))


def test_converge_refuses_when_synced_record_elects_a_different_host(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_identities(home)
    _seed_existing_election(home, "chiap01")

    with pytest.raises(ValueError) as excinfo:
        converge_lifecycle_seats(home, tmp_path / "rollback", active_host="chiap02")

    message = str(excinfo.value)
    assert "chiap01" in message
    assert "chiap02" in message


def test_converge_does_not_touch_control_plane_or_placement_on_refusal(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_identities(home)
    _seed_existing_election(home, "chiap01")
    control_path = home / "coordination/seat-control-plane.json"
    before = control_path.read_bytes()
    placement_path = home / "coordination/seat-placement.json"

    with pytest.raises(ValueError):
        converge_lifecycle_seats(home, tmp_path / "rollback", active_host="chiap02")

    assert control_path.read_bytes() == before
    assert not placement_path.exists()


def test_converge_allows_reconverging_the_same_elected_host(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_identities(home)
    _seed_existing_election(home, "chiap08")

    # No refusal: the host being written matches the existing election.
    converge_lifecycle_seats(home, tmp_path / "rollback", active_host="chiap08")

    control = json.loads((home / "coordination/seat-control-plane.json").read_text())
    assert control["active_host"] == "chiap08"


def test_converge_allows_first_ever_convergence_with_no_prior_election(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_identities(home)

    # No prior seat-control-plane.json at all: nothing to disagree with.
    converge_lifecycle_seats(home, tmp_path / "rollback", active_host="chiap08")

    control = json.loads((home / "coordination/seat-control-plane.json").read_text())
    assert control["active_host"] == "chiap08"


def test_converge_force_overrides_the_election_refusal(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_identities(home)
    _seed_existing_election(home, "chiap01")

    converge_lifecycle_seats(home, tmp_path / "rollback", active_host="chiap02", force=True)

    control = json.loads((home / "coordination/seat-control-plane.json").read_text())
    assert control["active_host"] == "chiap02"
    placement = json.loads((home / "coordination/seat-placement.json").read_text())
    assert all(hosts == ["chiap02"] for hosts in placement["seats"].values())
