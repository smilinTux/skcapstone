"""`skfleet install` materializes this node's placed Service objects.

Task 0.3 of the 2026-09-04 install-converge plan. The rendering grammar
(test_fleet_materialize.py) and the write/reload half
(test_fleet_materialize_apply.py) are covered elsewhere; this file proves
the CLI wiring only: that --check plans without writing, that --apply
writes, that a second apply is a no-op, that a deleted object removes its
units, and that the exit-code contract did not move.

systemd is never invoked: `_systemctl` is stubbed and the unit directory is
a tmp_path, so the suite can run on a box with no user manager at all.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from skcapstone.fleet import materialize_apply, store
from skcapstone.fleet.cli import fleet
from skcapstone.fleet.paths import FleetPaths

NODE = "node-cli"

DEMO_SPEC = {
    "unit": "demo.service",
    "runtime": "systemd-user",
    "exec": "%h/.skenv/bin/python3 -c pass",
    "type": "oneshot",
    "description": "install materialize demo",
    "schedule": "5m",
}


@pytest.fixture
def paths(tmp_path) -> FleetPaths:
    return FleetPaths(root=tmp_path / "fleet")


@pytest.fixture
def unit_dir(tmp_path, monkeypatch):
    """A throwaway unit directory, with systemctl stubbed out."""
    target = tmp_path / "units"
    target.mkdir()
    monkeypatch.setattr(materialize_apply, "default_unit_dir", lambda: target)
    monkeypatch.setattr(materialize_apply, "_systemctl", lambda args, runner: None)
    return target


def _env(paths: FleetPaths) -> dict:
    return {"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": NODE}


def _seed(paths: FleetPaths, name: str, spec: dict, *, node: str = NODE) -> None:
    """An installable node with one Service spec placed on it."""
    operator = store.Writer(role="operator", node=NODE, identity="capauth:test@skworld.io")
    scheduler = store.Writer(role="scheduler", node=NODE, identity="capauth:test@skworld.io")
    store.write_spec(paths, "node", NODE, {"role": "control", "actuate": True}, writer=operator)
    store.write_spec(paths, "profile", "control", {}, writer=operator)
    store.write_spec(paths, "service", name, spec, writer=operator)
    store.write_placement(paths, "service", name, node=node, reason="test", writer=scheduler)


def _install(paths: FleetPaths, *args: str):
    return CliRunner().invoke(fleet, ["install", *args], env=_env(paths))


# ------------------------------------------------------------------ check ---


def test_check_plans_the_write_and_touches_nothing(paths, unit_dir) -> None:
    _seed(paths, "skfleet-demo", DEMO_SPEC)
    result = _install(paths, "--check")
    assert result.exit_code == 0, result.output
    assert "write skfleet-demo.service" in result.output
    assert "write skfleet-demo.timer" in result.output
    assert list(unit_dir.iterdir()) == []


def test_check_is_still_the_default_mode(paths, unit_dir) -> None:
    _seed(paths, "skfleet-demo", DEMO_SPEC)
    result = _install(paths)
    assert result.exit_code == 0, result.output
    assert "write skfleet-demo.service" in result.output
    assert list(unit_dir.iterdir()) == []


# ------------------------------------------------------------------ apply ---


def test_apply_writes_the_unit_files(paths, unit_dir) -> None:
    _seed(paths, "skfleet-demo", DEMO_SPEC)
    result = _install(paths, "--apply")
    assert result.exit_code == 0, result.output
    assert "write skfleet-demo.service" in result.output
    assert (unit_dir / "skfleet-demo.service").is_file()
    assert (unit_dir / "skfleet-demo.timer").is_file()
    assert (
        "ExecStart=%h/.skenv/bin/python3 -c pass"
        in (unit_dir / "skfleet-demo.service").read_text()
    )


def test_second_apply_is_a_no_op(paths, unit_dir) -> None:
    _seed(paths, "skfleet-demo", DEMO_SPEC)
    assert _install(paths, "--apply").exit_code == 0
    before = {p.name: p.stat().st_mtime_ns for p in unit_dir.iterdir()}
    result = _install(paths, "--apply")
    assert result.exit_code == 0, result.output
    assert "ok skfleet-demo (unchanged)" in result.output
    assert "write" not in result.output
    assert {p.name: p.stat().st_mtime_ns for p in unit_dir.iterdir()} == before


def test_apply_with_dry_run_writes_nothing(paths, unit_dir) -> None:
    _seed(paths, "skfleet-demo", DEMO_SPEC)
    result = _install(paths, "--apply", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "write skfleet-demo.service" in result.output
    assert list(unit_dir.iterdir()) == []


# ---------------------------------------------------------------- removal ---


def test_deleted_object_removes_its_units(paths, unit_dir) -> None:
    _seed(paths, "skfleet-demo", DEMO_SPEC)
    assert _install(paths, "--apply").exit_code == 0
    operator = store.Writer(role="operator", node=NODE, identity="capauth:test@skworld.io")
    store.write_spec(
        paths, "service", "skfleet-demo", {**DEMO_SPEC, "deleted": True}, writer=operator
    )

    plan = _install(paths, "--check")
    assert "remove skfleet-demo.service" in plan.output
    assert (unit_dir / "skfleet-demo.service").is_file()  # check changed nothing

    result = _install(paths, "--apply")
    assert result.exit_code == 0, result.output
    assert "remove skfleet-demo.service" in result.output
    assert not (unit_dir / "skfleet-demo.service").exists()
    assert not (unit_dir / "skfleet-demo.timer").exists()


# ----------------------------------------------------------------- skips ---


def test_unplaced_service_is_not_this_node_s_business(paths, unit_dir) -> None:
    _seed(paths, "skfleet-demo", DEMO_SPEC, node="node-other")
    result = _install(paths, "--apply")
    assert result.exit_code == 0, result.output
    assert "skfleet-demo" not in result.output
    assert list(unit_dir.iterdir()) == []


def test_service_without_exec_is_skipped_not_overwritten(paths, unit_dir) -> None:
    """The live estate's Service objects reference units they did not author."""
    _seed(paths, "skgateway", {"unit": "skgateway.service", "runtime": "systemd-user"})
    (unit_dir / "skgateway.service").write_text("# hand written, not ours\n")
    result = _install(paths, "--apply")
    assert result.exit_code == 0, result.output
    assert "skip skgateway (no spec.exec" in result.output
    assert (unit_dir / "skgateway.service").read_text() == "# hand written, not ours\n"


def test_docker_service_is_skipped(paths, unit_dir) -> None:
    _seed(paths, "skfleet-demo", {"unit": "demo", "runtime": "docker", "exec": "true"})
    result = _install(paths, "--apply")
    assert result.exit_code == 0, result.output
    assert "skip skfleet-demo (runtime docker" in result.output
    assert list(unit_dir.iterdir()) == []


# ------------------------------------------------------------- contracts ---


def test_units_appear_in_json_output(paths, unit_dir) -> None:
    _seed(paths, "skfleet-demo", DEMO_SPEC)
    result = _install(paths, "--check", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "write skfleet-demo.service" in payload["units"]


def test_exit_code_still_follows_the_drift_summary(paths, unit_dir, monkeypatch) -> None:
    """A clean materialize must not rescue a failing drift report."""
    _seed(paths, "skfleet-demo", DEMO_SPEC)
    monkeypatch.setattr(
        "skcapstone.fleet.cli.installer.run_install",
        lambda *a, **k: {"role": "control", "mode": "check", "results": [], "ok": False},
    )
    result = _install(paths, "--check")
    assert result.exit_code == 1, result.output
    assert "write skfleet-demo.service" in result.output


def test_frozen_fleet_refuses_apply_before_any_unit_is_written(paths, unit_dir) -> None:
    """The freeze gate lives in run_install, above materialization."""
    _seed(paths, "skfleet-demo", DEMO_SPEC)
    operator = store.Writer(role="operator", node=NODE, identity="capauth:test@skworld.io")
    store.set_frozen(paths, True, writer=operator, reason="test")
    result = _install(paths, "--apply")
    assert result.exit_code != 0
    assert "FROZEN" in result.output
    assert list(unit_dir.iterdir()) == []
