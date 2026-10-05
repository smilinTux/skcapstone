"""Shared fixtures for fleet tests."""

from __future__ import annotations

import sys

import pytest

from skcapstone.fleet.paths import FleetPaths


@pytest.fixture
def qualified_runtime(tmp_path, monkeypatch):
    """Fingerprint synthetic installed bytes, never the operator's real .skenv.

    These plan/dispatch tests inspect runtime custody without executing it.
    Keep the real fingerprint implementation so changed bytes still invalidate
    a qualified plan. Each test owns its tree and can mutate it independently.
    """
    from skcapstone.fleet import production_test_plan as plan
    from skcapstone.fleet import production_test_worker as worker
    from skcapstone.fleet import production_tests as native

    prefix = tmp_path / "qualified-runtime"
    (prefix / "bin").mkdir(parents=True)
    for executable in ("python", "ruff"):
        (prefix / "bin" / executable).write_bytes(b"synthetic runtime bytes\n")
    site = (
        prefix
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    for package in plan.TOOL_PACKAGES:
        directory = site / package
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text("# Synthetic qualified dependency.\n")
    harness = tmp_path / "harness"
    harness.mkdir()
    for name in plan.HARNESS_MODULES:
        (harness / name).write_text("# Synthetic trusted executor.\n")
    monkeypatch.setattr(plan, "HARNESS_ROOT", harness)
    for module in (plan, native, worker):
        monkeypatch.setattr(module, "PREFIX", prefix)
    monkeypatch.setattr(plan, "_RUNTIME_CACHE", {})
    return prefix


@pytest.fixture(autouse=True)
def _hermetic_node_inventory(monkeypatch):
    """No fleet test observes the real host's systemd or site-packages.

    sknoded publishes an inventory block into node.json, and its collector
    is cached process wide. Without this, node.json content would depend on
    whichever machine ran the suite and would leak between tests through the
    cache. Tests that care about inventory content re-patch the same seam,
    which wins because the test body runs after its fixtures.
    """
    from skcapstone.fleet import sknoded

    sknoded.reset_inventory_cache()
    monkeypatch.setattr(
        sknoded,
        "_collect_inventory",
        lambda: {"units": {"user": {}}, "packages": {}, "collectedAt": "2026-08-15T00:00:00Z"},
    )
    yield
    sknoded.reset_inventory_cache()


@pytest.fixture
def paths(tmp_path) -> FleetPaths:
    """A throwaway fleet tree root."""
    return FleetPaths(root=tmp_path / "fleet")


@pytest.fixture
def operator():
    """The operator seat writer (spec owner)."""
    from skcapstone.fleet.store import Writer

    return Writer(role="operator", node="node-158", identity="capauth:chef@skworld.io")


@pytest.fixture
def noded41():
    """sknoded writer on node-41 (status owner for node-41 only)."""
    from skcapstone.fleet.store import Writer

    return Writer(role="sknoded", node="node-41", identity="")


@pytest.fixture
def scheduler_writer():
    """The scheduler seat (placement owner, runs on the control-plane node)."""
    from skcapstone.fleet.store import Writer

    return Writer(role="scheduler", node="node-158", identity="")


@pytest.fixture
def sandbox_prerequisites(monkeypatch):
    """Observer unit tests stub system prerequisites; sandbox tests exercise them."""
    from skcapstone.fleet import sandbox_tools

    monkeypatch.setattr(sandbox_tools, "readiness", lambda *args, **kwargs: [])
