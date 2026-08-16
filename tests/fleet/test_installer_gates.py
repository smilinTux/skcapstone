"""Tests for installer.run_install(): freeze/opt-in gates + inventory refresh."""
from __future__ import annotations

import json

import pytest

from skcapstone.fleet import store
from skcapstone.fleet.installer import ActuationNotAllowed, Frozen, run_install


class _P:
    ...


def test_apply_refuses_when_frozen(monkeypatch):
    monkeypatch.setattr("skcapstone.fleet.installer.store.is_frozen", lambda p: True)
    with pytest.raises(Frozen):
        run_install(
            _P(), "control", mode="apply", dry_run=False, enable=False, start=False,
            only=None, backends={},
        )


def test_check_mode_allowed_even_when_frozen(monkeypatch):
    monkeypatch.setattr("skcapstone.fleet.installer.store.is_frozen", lambda p: True)
    monkeypatch.setattr(
        "skcapstone.fleet.installer.load_drift",
        lambda p, r, **k: __import__(
            "skcapstone.fleet.profile_doctor", fromlist=["DriftReport"]
        ).DriftReport(),
    )
    out = run_install(
        _P(), "control", mode="check", dry_run=False, enable=False, start=False,
        only=None, backends={},
    )
    assert out["mode"] == "check" and out["ok"] is True


# ------------------------------------------------------------- extra coverage ---


def test_apply_refuses_when_not_actuation_allowed(monkeypatch):
    """is_frozen() can be False while actuation_allowed() is still False
    (e.g. a future per-node opt-in gate); both guards must be checked."""
    monkeypatch.setattr("skcapstone.fleet.installer.store.is_frozen", lambda p: False)
    monkeypatch.setattr("skcapstone.fleet.installer.store.actuation_allowed", lambda p: False)
    with pytest.raises(ActuationNotAllowed):
        run_install(
            _P(), "control", mode="apply", dry_run=False, enable=False, start=False,
            only=None, backends={},
        )


def test_check_mode_reports_not_ok_when_missing_required(monkeypatch):
    from skcapstone.fleet.profile_doctor import DriftReport

    monkeypatch.setattr(
        "skcapstone.fleet.installer.load_drift",
        lambda p, r, **k: DriftReport(missing_required_units=["sknoded.service"]),
    )
    out = run_install(
        _P(), "control", mode="check", dry_run=False, enable=False, start=False,
        only=None, backends={},
    )
    assert out["ok"] is False
    assert out["mode"] == "check"
    assert any(row["name"] == "sknoded.service" for row in out["results"])
    json.dumps(out)  # must be JSON-able


def test_apply_builds_and_runs_plan_ok(monkeypatch):
    from skcapstone.fleet.profile_doctor import DriftReport

    monkeypatch.setattr("skcapstone.fleet.installer.store.is_frozen", lambda p: False)
    monkeypatch.setattr("skcapstone.fleet.installer.store.actuation_allowed", lambda p: True)
    monkeypatch.setattr(
        "skcapstone.fleet.installer.load_drift",
        lambda p, r, **k: DriftReport(missing_required_units=["sknoded.service"]),
    )
    refreshed = []
    monkeypatch.setattr(
        "skcapstone.fleet.installer._refresh_inventory", lambda p: refreshed.append(p)
    )
    backends = {"core": lambda names, **kw: ("ok", "")}
    out = run_install(
        _P(), "control", mode="apply", dry_run=False, enable=False, start=False,
        only=None, backends=backends,
    )
    assert out["mode"] == "apply"
    assert out["ok"] is True
    assert out["results"][0]["status"] == "ok"
    assert len(refreshed) == 1  # refresh called exactly once, with the paths object
    json.dumps(out)  # must be JSON-able (InstallResult/InstallStep are dataclasses, not dicts)


def test_apply_does_not_refresh_inventory_on_dry_run(monkeypatch):
    from skcapstone.fleet.profile_doctor import DriftReport

    monkeypatch.setattr("skcapstone.fleet.installer.store.is_frozen", lambda p: False)
    monkeypatch.setattr("skcapstone.fleet.installer.store.actuation_allowed", lambda p: True)
    monkeypatch.setattr(
        "skcapstone.fleet.installer.load_drift",
        lambda p, r, **k: DriftReport(missing_required_units=["sknoded.service"]),
    )
    refreshed = []
    monkeypatch.setattr(
        "skcapstone.fleet.installer._refresh_inventory", lambda p: refreshed.append(p)
    )
    backends = {"core": lambda names, **kw: ("would-write", "cmd")}
    out = run_install(
        _P(), "control", mode="apply", dry_run=True, enable=False, start=False,
        only=None, backends=backends,
    )
    assert out["ok"] is True
    assert refreshed == []  # dry-run never touches the published inventory


def test_apply_does_not_refresh_inventory_when_a_step_failed(monkeypatch):
    from skcapstone.fleet.profile_doctor import DriftReport

    monkeypatch.setattr("skcapstone.fleet.installer.store.is_frozen", lambda p: False)
    monkeypatch.setattr("skcapstone.fleet.installer.store.actuation_allowed", lambda p: True)
    monkeypatch.setattr(
        "skcapstone.fleet.installer.load_drift",
        lambda p, r, **k: DriftReport(missing_required_units=["sknoded.service"]),
    )
    refreshed = []
    monkeypatch.setattr(
        "skcapstone.fleet.installer._refresh_inventory", lambda p: refreshed.append(p)
    )
    backends = {"core": lambda names, **kw: ("failed", "boom")}
    out = run_install(
        _P(), "control", mode="apply", dry_run=False, enable=False, start=False,
        only=None, backends=backends,
    )
    assert out["ok"] is False
    assert refreshed == []


def test_refresh_inventory_publishes_via_sknoded_run_once(paths, monkeypatch):
    """End-to-end (no mocking of _refresh_inventory itself): a real, non-dry
    apply against a real FleetPaths tree republishes node.json."""
    from skcapstone.fleet.profile_doctor import DriftReport
    from skcapstone.fleet.paths import self_node_name

    monkeypatch.setattr("skcapstone.fleet.installer.store.is_frozen", lambda p: False)
    monkeypatch.setattr("skcapstone.fleet.installer.store.actuation_allowed", lambda p: True)
    monkeypatch.setattr(
        "skcapstone.fleet.installer.load_drift",
        lambda p, r, **k: DriftReport(missing_required_units=["sknoded.service"]),
    )
    backends = {"core": lambda names, **kw: ("ok", "")}
    out = run_install(
        paths, "control", mode="apply", dry_run=False, enable=False, start=False,
        only=None, backends=backends,
    )
    assert out["ok"] is True
    node = self_node_name()
    report = store.read_node_file(paths, node, "node.json")
    assert report is not None
    assert "inventory" in report["status"]
