"""The control profile must account for the lifecycle seat units.

They were in no profile's allowed or required list at all, so a brand new
estate running none of them reported ``ok=True`` from ``fleet install
--check`` while the entire workflow layer was absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.fleet import install_backends, installer, profile_doctor, profiles

SEATS = ("atlas", "link", "mero", "niobe", "seraph", "tank")
SEAT_TIMERS = tuple(f"skfleet-{seat}.timer" for seat in SEATS)
PROFILE_DIR = Path(__file__).resolve().parents[1] / "deploy" / "fleet-objects" / "profile"


def _control_spec() -> dict:
    return profiles.normalize_profile_spec(
        json.loads((PROFILE_DIR / "control.json").read_text(encoding="utf-8"))["spec"]
    )


def test_control_profile_requires_every_seat_timer() -> None:
    """Happy path: the six bounded seat timers are a control-node baseline."""
    spec = _control_spec()
    assert set(SEAT_TIMERS) <= set(spec["units"]["required"])
    assert set(SEAT_TIMERS) <= set(spec["units"]["allowed"])
    for seat in SEATS:
        assert f"skfleet-{seat}.service" in spec["units"]["allowed"]


def test_control_profile_allows_but_never_requires_live_dispatch() -> None:
    """Edge case: niobe-live launches real agent runs, so it is a decision."""
    spec = _control_spec()
    assert "skfleet-niobe-live.timer" in spec["units"]["allowed"]
    assert "skfleet-niobe-live.timer" not in spec["units"]["required"]


def test_an_estate_with_no_seat_units_no_longer_reports_ok() -> None:
    """Failure mode that shipped: ok=True with the workflow layer absent."""
    spec = _control_spec()
    inventory = {
        "units": {
            "user": {name: {} for name in spec["units"]["required"] if "skfleet-" not in name}
        },
        "packages": {name: {} for name in spec["packages"]["required"]},
    }
    drift = profile_doctor.diff(inventory, spec)
    assert set(SEAT_TIMERS) <= set(drift.missing_required_units)
    assert bool(drift.missing_required_units or drift.missing_required_packages)


def test_seat_units_resolve_to_the_backend_that_installs_them() -> None:
    """Every required skfleet unit used to resolve to UNSUPPORTED."""
    for name in SEAT_TIMERS + ("skfleet-link-producer.timer", "skfleet-niobe.service"):
        assert install_backends.resolve(name, "unit") == "core"


@pytest.mark.parametrize("name", SEAT_TIMERS)
def test_seat_units_are_planned_not_shrugged_at(name: str) -> None:
    """A planned step must name a real backend, never needs_manual."""
    drift = profile_doctor.DriftReport(missing_required_units=[name])
    plan = installer.plan(drift)
    assert [step.backend_id for step in plan.steps] == ["core"]
    assert plan.steps[0].tier == install_backends.tier_of("core")


def test_every_shipped_profile_still_validates() -> None:
    """Edit safety: a profile that fails validation must never be converged."""
    for path in sorted(PROFILE_DIR.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))["spec"]
        assert profiles.normalize_profile_spec(spec)["stateTier"]
