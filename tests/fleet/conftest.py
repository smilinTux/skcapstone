"""Shared fixtures for fleet tests."""

from __future__ import annotations

import pytest

from skcapstone.fleet.paths import FleetPaths


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
