"""Tests for sknoded v1: self-report + join request."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.fleet import sknoded, store

CAP = {"cores": 4, "ram_gb": 8.0, "disk_gb": 50.0, "gpu": None, "vram_gb": None}

#: Real inventories collected off the fleet, checked in for exactly this.
INVENTORY_DIR = Path(__file__).resolve().parents[2] / "docs" / "fleet" / "inventories"

#: The biggest node in the fleet: 80 enabled user units, 25 SK packages.
CONTROL_NODE = "node-noroc2027"


@pytest.fixture(autouse=True)
def _fixed_capacity(monkeypatch):
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity", lambda: dict(CAP))


def test_first_run_writes_all_three(paths) -> None:
    result = sknoded.run_once(paths, "node-41")
    assert result == {"heartbeat": True, "node": True, "join": True}
    hb = store.read_node_file(paths, "node-41", "heartbeat.json")
    assert hb["name"] == "node-41" and "ts" in hb
    report = store.read_node_file(paths, "node-41", "node.json")
    assert report["status"]["capacity"]["cores"] == 4
    assert report["observedGeneration"] == 0  # unadmitted
    join = store.read_node_file(paths, "node-41", "join.json")
    assert join["name"] == "node-41" and join["capacity"]["ram_gb"] == 8.0


def test_second_run_is_write_on_change(paths) -> None:
    sknoded.run_once(paths, "node-41")
    result = sknoded.run_once(paths, "node-41")
    assert result["heartbeat"] is True  # heartbeat always beats
    assert result["node"] is False  # unchanged report skipped
    assert result["join"] is False  # join written once


def test_admitted_node_reports_generation_and_stops_joining(paths, operator) -> None:
    sknoded.run_once(paths, "node-41")
    store.write_spec(paths, "node", "node-41", {"cordoned": False}, writer=operator)
    result = sknoded.run_once(paths, "node-41")
    assert result["node"] is True  # observedGeneration 0 -> 1 changed
    assert store.read_node_file(paths, "node-41", "node.json")["observedGeneration"] == 1


def test_never_writes_outside_own_subtree(paths) -> None:
    sknoded.run_once(paths, "node-41")
    written = [p for p in paths.root.rglob("*") if p.is_file()]
    assert written and all(
        str(p).startswith(str(paths.node_status_dir("node-41"))) for p in written
    )


def test_main_loop_once_runs_a_single_pass_without_sleeping(paths, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(sknoded, "run_once", lambda p, n: calls.append(n))
    monkeypatch.setattr(sknoded.time, "sleep", lambda s: pytest.fail("once=True must not sleep"))
    sknoded.main_loop(paths, "node-41", once=True)
    assert calls == ["node-41"]


def test_main_loop_repeats_and_sleeps_the_actuation_interval(paths, monkeypatch) -> None:
    calls = []
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise RuntimeError("stop after first cycle")

    monkeypatch.setattr(sknoded, "run_once", lambda p, n: calls.append(n))
    monkeypatch.setattr(sknoded.time, "sleep", fake_sleep)
    with pytest.raises(RuntimeError, match="stop after first cycle"):
        sknoded.main_loop(paths, "node-41", interval=5, actuation_interval=5)
    assert calls == ["node-41"]
    assert sleeps == [5]


def test_main_loop_default_sleep_is_the_30s_converge_interval(paths, monkeypatch) -> None:
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise RuntimeError("stop after first cycle")

    monkeypatch.setattr(sknoded, "run_once", lambda p, n: None)
    monkeypatch.setattr(sknoded.time, "sleep", fake_sleep)
    with pytest.raises(RuntimeError, match="stop after first cycle"):
        sknoded.main_loop(paths, "node-41")
    assert sleeps == [30]


# ------------------------------------------------------ published inventory ---


def _real_inventory(node: str = CONTROL_NODE, *, collected_at: str = "2026-08-15T00:00:00Z"):
    """A collect()-shaped inventory built from a real fleet node's dumps."""
    units = json.loads((INVENTORY_DIR / f"{node}-user-units.json").read_text())["units"]
    packages = json.loads((INVENTORY_DIR / f"{node}-packages.json").read_text())["packages"]
    return {"units": {"user": units}, "packages": packages, "collectedAt": collected_at}


def _stub_collect(monkeypatch, inventories) -> None:
    """Serve one inventory per collection, so churn is observable."""
    queue = list(inventories)
    sknoded.reset_inventory_cache()
    monkeypatch.setattr(sknoded, "INVENTORY_INTERVAL_S", 0)  # re-observe every pass
    monkeypatch.setattr(sknoded, "_collect_inventory", lambda: queue.pop(0))


def test_report_carries_the_inventory_doctor_all_reads(paths, monkeypatch) -> None:
    _stub_collect(monkeypatch, [_real_inventory()])
    sknoded.run_once(paths, CONTROL_NODE)
    published = store.read_node_file(paths, CONTROL_NODE, "node.json")["status"]["inventory"]
    assert published["units"]["user"]["capauth-authz.service"] == "enabled"
    assert published["packages"]["skcapstone"]
    assert published["truncated"] == {}  # complete
    assert "collectedAt" not in published  # would churn the file every 60s


def test_unchanged_inventory_does_not_rewrite_node_json(paths, monkeypatch) -> None:
    """The churn guard: a fresh collection every pass, same node, no write.

    collectedAt moves on every collect(), and store._changed() only ignores
    the top level updatedAt, so publishing the raw timestamp would rewrite
    node.json on every heartbeat and flood the control-bus Syncthing folder.
    """
    _stub_collect(
        monkeypatch,
        [
            _real_inventory(collected_at="2026-08-15T00:00:00Z"),
            _real_inventory(collected_at="2026-08-15T00:01:00Z"),
        ],
    )
    assert sknoded.run_once(paths, CONTROL_NODE)["node"] is True
    assert sknoded.run_once(paths, CONTROL_NODE)["node"] is False


def test_changed_inventory_does_rewrite_node_json(paths, monkeypatch) -> None:
    """Negative control: the guard above must not be a guard against writing."""
    second = _real_inventory()
    second["units"]["user"]["newly-enabled.service"] = "enabled"
    _stub_collect(monkeypatch, [_real_inventory(), second])
    assert sknoded.run_once(paths, CONTROL_NODE)["node"] is True
    assert sknoded.run_once(paths, CONTROL_NODE)["node"] is True
    published = store.read_node_file(paths, CONTROL_NODE, "node.json")["status"]["inventory"]
    assert "newly-enabled.service" in published["units"]["user"]


def test_control_node_sized_node_json_stays_under_64kb(paths, monkeypatch) -> None:
    """The size guard, measured on the serialized file, not on an estimate."""
    inventory = _real_inventory()
    assert len(inventory["units"]["user"]) >= 80
    assert len(inventory["packages"]) >= 25
    _stub_collect(monkeypatch, [inventory])
    sknoded.run_once(paths, CONTROL_NODE)
    written = paths.node_status_dir(CONTROL_NODE) / "node.json"
    assert written.stat().st_size < 64 * 1024


def test_oversized_inventory_is_capped_and_says_so(paths, monkeypatch) -> None:
    """A truncated inventory must never read as a complete one."""
    units = {f"unit-{i:04d}.service": "enabled" for i in range(sknoded.MAX_INVENTORY_UNITS + 25)}
    packages = {f"sk-{i:04d}": "1.0.0" for i in range(sknoded.MAX_INVENTORY_PACKAGES + 7)}
    _stub_collect(
        monkeypatch,
        [{"units": {"user": units}, "packages": packages, "collectedAt": "2026-08-15T00:00:00Z"}],
    )
    sknoded.run_once(paths, CONTROL_NODE)
    published = store.read_node_file(paths, CONTROL_NODE, "node.json")["status"]["inventory"]
    assert len(published["units"]["user"]) == sknoded.MAX_INVENTORY_UNITS
    assert len(published["packages"]) == sknoded.MAX_INVENTORY_PACKAGES
    assert published["truncated"] == {
        "units.user": {"kept": sknoded.MAX_INVENTORY_UNITS, "total": len(units)},
        "packages": {"kept": sknoded.MAX_INVENTORY_PACKAGES, "total": len(packages)},
    }
    # Sorted-then-cut, so the surviving subset is the same on every pass.
    assert list(published["units"]["user"]) == sorted(units)[: sknoded.MAX_INVENTORY_UNITS]


def test_capped_inventory_is_still_write_on_change(paths, monkeypatch) -> None:
    """Truncation must be stable, or the cap itself becomes the churn source."""
    units = {f"unit-{i:04d}.service": "enabled" for i in range(sknoded.MAX_INVENTORY_UNITS + 25)}
    _stub_collect(
        monkeypatch,
        [
            {"units": {"user": dict(units)}, "packages": {}, "collectedAt": "a"},
            {
                "units": {"user": dict(reversed(list(units.items())))},
                "packages": {},
                "collectedAt": "b",
            },
        ],
    )
    assert sknoded.run_once(paths, CONTROL_NODE)["node"] is True
    assert sknoded.run_once(paths, CONTROL_NODE)["node"] is False


def test_inventory_is_collected_on_a_slower_cadence_than_the_heartbeat(paths, monkeypatch) -> None:
    """15 minute cadence: heartbeats must not each cost a systemctl exec."""
    assert sknoded.INVENTORY_INTERVAL_S == 900
    assert sknoded.INVENTORY_INTERVAL_S > sknoded.HEARTBEAT_INTERVAL_S
    calls = []

    sknoded.reset_inventory_cache()
    monkeypatch.setattr(
        sknoded, "_collect_inventory", lambda: calls.append(1) or _real_inventory()
    )
    clock = [1000.0]
    monkeypatch.setattr(sknoded.time, "monotonic", lambda: clock[0])

    sknoded.run_once(paths, CONTROL_NODE)
    clock[0] += sknoded.INVENTORY_INTERVAL_S - 1
    sknoded.run_once(paths, CONTROL_NODE)
    assert len(calls) == 1  # still inside the window, cache served it
    clock[0] += 2
    sknoded.run_once(paths, CONTROL_NODE)
    assert len(calls) == 2


def test_inventory_publishes_names_only_never_unit_file_contents() -> None:
    """Unit bodies are unbounded and the drift diff never reads them."""
    raw = {
        "units": {"user": {"a.service": "enabled"}},
        "packages": {"skcapstone": "1.2.3"},
        "collectedAt": "2026-08-15T00:00:00Z",
    }
    published = sknoded.publishable_inventory(raw)
    assert published == {
        "units": {"user": {"a.service": "enabled"}},
        "packages": {"skcapstone": "1.2.3"},
        "truncated": {},
    }


def test_missing_inventory_degrades_to_an_empty_block(paths, monkeypatch) -> None:
    """A node that cannot be inventoried publishes empty, never partial junk."""
    _stub_collect(monkeypatch, [{"units": {}, "packages": {}, "collectedAt": "z"}])
    sknoded.run_once(paths, CONTROL_NODE)
    published = store.read_node_file(paths, CONTROL_NODE, "node.json")["status"]["inventory"]
    assert published == {"units": {}, "packages": {}, "truncated": {}}
