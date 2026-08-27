"""CR-9.1 honor dispatch: STANDARD-catalog physical actuation (fleet + skchat).

The honor path is OFF by default; these prove that WHEN it is on, an auto action
records an ITIL change and physically actuates through the tested systemd path,
routed to the right adapter, refusing when frozen and refusing unmapped actions.
"""

from __future__ import annotations

import subprocess

import pytest

from skcapstone.fleet import store
from skcapstone.fleet.paths import FleetPaths
from skcapstone.operator_seat import act_dispatch


def _paths(tmp_path):
    return FleetPaths(root=tmp_path / "fleet")


def _provision_ready(paths):
    writer = store.Writer(role="operator", node="test-node", identity="test")
    store.set_frozen(paths, False, writer=writer, reason="test fixture provisioning")


def _capture_runner(calls):
    def runner(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return runner


class _FakeITIL:
    """Records propose_change calls; returns an object with an .id."""

    def __init__(self):
        self.changes = []

    def propose_change(self, **kwargs):
        self.changes.append(kwargs)
        return type("Chg", (), {"id": f"chg-{len(self.changes)}"})()


# --- routing + merged catalog ------------------------------------------------


def test_route_action_splits_fleet_and_skchat():
    assert act_dispatch.route_action("restart_service") == "fleet"
    assert act_dispatch.route_action("restart-telegram-bridge") == "skchat"
    assert act_dispatch.route_action("restart-daemon") == "skchat"
    assert act_dispatch.route_action("nonsense") is None


def test_merged_explain_unions_both_action_catalogs():
    m = act_dispatch.merged_explain()
    names = {a["name"] for a in m["actions"]}
    assert "restart_service" in names  # fleet
    assert "restart-telegram-bridge" in names  # skchat
    assert "restart-daemon" in names  # skchat
    # conditions merge too, so the planner/brief see both apps' health vocab.
    assert "BridgeAlive" in m["conditions"]
    assert "MissedRun" in m["conditions"]


# --- honor apply_fn ----------------------------------------------------------


def test_honor_skchat_records_itil_and_actuates(tmp_path):
    paths = _paths(tmp_path)
    _provision_ready(paths)
    calls = []
    itil = _FakeITIL()
    apply_fn = act_dispatch.build_apply_fn(
        paths, "2026-08-07T00:00:00Z", runner=_capture_runner(calls), itil=itil
    )
    prop = {"action": "restart-telegram-bridge", "object": "telegram-bridge", "ts": "t1"}
    cls = {"change_class": "normal", "risk": "low", "auto_approvable": True}
    out = apply_fn(prop, cls)

    assert out["adapter"] == "skchat"
    assert out["actuation"]["performed"] is True
    # physically actuates the per-agent bridge unit via systemctl --user restart.
    assert any("restart" in c and "skchat-telegram-" in " ".join(c) for c in calls)
    # governance: an ITIL change was recorded first.
    assert len(itil.changes) == 1
    assert itil.changes[0]["change_type"] == "normal"
    assert "auto-normal" in itil.changes[0]["tags"]


def test_honor_fleet_annotates_then_actuates(tmp_path):
    paths = _paths(tmp_path)
    _provision_ready(paths)
    # Seed a fleet Service object so fleet_act can annotate its spec.
    seat = store.Writer(role="operator", node="n", identity="operator", agent_seat=True)
    store.write_spec(paths, "service", "svc-x", {"unit": "svc-x.service"}, writer=seat)

    calls = []
    apply_fn = act_dispatch.build_apply_fn(
        paths, "2026-08-07T00:00:00Z", runner=_capture_runner(calls), itil=None
    )
    prop = {"action": "restart_service", "object": "svc-x", "ts": "t2"}
    cls = {"change_class": "standard", "risk": "low", "auto_approvable": True}
    out = apply_fn(prop, cls)

    assert out["adapter"] == "fleet"
    # the signed intent annotation landed on the object spec.
    spec = store.read_spec(paths, "service", "svc-x")["spec"]
    assert spec["operatorActions"][-1]["action"] == "restart_service"
    # and the physical restart was computed.
    assert any("restart" in c and "svc-x" in " ".join(c) for c in calls)


def test_honor_refuses_unmapped_action(tmp_path):
    paths = _paths(tmp_path)
    apply_fn = act_dispatch.build_apply_fn(paths, "t", runner=_capture_runner([]))
    with pytest.raises(ValueError):
        apply_fn({"action": "nonsense", "object": "x"}, {"change_class": "normal"})


def test_honor_refuses_when_frozen(tmp_path):
    paths = _paths(tmp_path)
    human = store.Writer(role="operator", node="n", identity="chef")
    store.set_frozen(paths, True, writer=human)
    apply_fn = act_dispatch.build_apply_fn(paths, "t", runner=_capture_runner([]))
    with pytest.raises(RuntimeError):
        apply_fn(
            {"action": "restart-telegram-bridge", "object": "telegram-bridge", "ts": "t"},
            {"change_class": "normal", "risk": "low", "auto_approvable": True},
        )
