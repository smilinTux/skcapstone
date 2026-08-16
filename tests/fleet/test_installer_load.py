"""Tests for installer.load_drift: applied-profile lookup + cluster-aware diff.

load_drift is the read path that ties the synced fleet store (store.read_spec)
to the pure diff (profile_doctor.diff) via the observed inventory
(nodeinventory). It must never fall back to the repo's shipped
deploy/fleet-objects/ manifests: an applied profile is the fleet's own
agreed-upon state, and silently substituting the shipped default would make
a node's drift report lie about what was actually applied.
"""

from __future__ import annotations

import pytest

from skcapstone.fleet.installer import ProfileNotApplied, load_drift


class _FakePaths:
    """Stand-in for FleetPaths; load_drift never touches it directly."""


def test_load_drift_raises_when_profile_not_applied(monkeypatch):
    monkeypatch.setattr("skcapstone.fleet.installer.store.read_spec", lambda p, k, n: None)
    with pytest.raises(ProfileNotApplied):
        load_drift(_FakePaths(), "control", inventory={"units": {}, "packages": {}})


def test_load_drift_diffs_applied_profile_against_inventory(monkeypatch):
    # store.read_spec returns the full spec-file envelope (kind/name/spec/...);
    # load_drift must pull the "spec" block out, not diff the envelope itself.
    profile = {
        "kind": "Profile",
        "name": "control",
        "generation": 3,
        "spec": {
            "units": {"required": ["sknoded.service"]},
            "packages": {"required": []},
        },
    }
    monkeypatch.setattr("skcapstone.fleet.installer.store.read_spec", lambda p, k, n: profile)
    drift = load_drift(_FakePaths(), "control", inventory={"units": {}, "packages": {}})
    assert "sknoded.service" in drift.missing_required_units


def test_load_drift_reads_the_role_named_profile(monkeypatch):
    # read_spec must be called with kind="profile" and name=role, not a
    # fixed/wrong kind, and never falls back to a different role/name.
    calls = []

    def _fake_read_spec(paths, kind, name):
        calls.append((kind, name))
        return {"spec": {"units": {}, "packages": {}}}

    monkeypatch.setattr("skcapstone.fleet.installer.store.read_spec", _fake_read_spec)
    load_drift(_FakePaths(), "edge", inventory={"units": {}, "packages": {}})
    assert calls == [("profile", "edge")]


def test_load_drift_builds_inventory_via_nodeinventory_when_not_injected(monkeypatch):
    # When no inventory is injected, load_drift must build one via
    # nodeinventory.collect() rather than requiring every caller to do so.
    monkeypatch.setattr(
        "skcapstone.fleet.installer.store.read_spec",
        lambda p, k, n: {"spec": {"units": {"required": ["sknoded.service"]}, "packages": {}}},
    )
    monkeypatch.setattr(
        "skcapstone.fleet.installer.nodeinventory.collect",
        lambda: {"units": {"user": {}}, "packages": {}},
    )
    drift = load_drift(_FakePaths(), "control")
    assert "sknoded.service" in drift.missing_required_units
