"""skchat adapter: contract-conformant, real probes mapped, act gated by freeze."""

from __future__ import annotations

import pytest

from skcapstone.fleet import store
from skcapstone.fleet.paths import FleetPaths
from skcapstone.operator_seat import adapter, policy, skchat_adapter


def _paths(tmp_path):
    return FleetPaths(root=tmp_path / "fleet")


def _ok_runner(cmd):
    import subprocess

    return subprocess.CompletedProcess(cmd, 0, "", "")


# --- contract + observe ------------------------------------------------------


def test_skchat_explain_is_contract_conformant():
    assert adapter.validate_explain(skchat_adapter.skchat_explain()) == []


def test_explain_lists_the_five_conditions():
    assert skchat_adapter.skchat_explain()["conditions"] == [
        "DaemonReady",
        "BridgeAlive",
        "OutboxBounded",
        "AuthEnforced",
        "CallingReady",
    ]


def test_skchat_observe_is_contract_conformant():
    obs = skchat_adapter.skchat_observe(probe=lambda: {})
    assert adapter.validate_observe(obs) == []


def test_healthy_skchat_is_all_true():
    obs = skchat_adapter.skchat_observe(
        probe=lambda: {
            "daemon_ready": True,
            "bridge_alive": True,
            "outbox_depth": 5,
            "outbox_limit": 1000,
            "auth_enforced": True,
            "calling_ready": True,
        }
    )
    by_type = {c["type"]: c["status"] for c in obs["conditions"]}
    assert by_type == {
        "DaemonReady": "True",
        "BridgeAlive": "True",
        "OutboxBounded": "True",
        "AuthEnforced": "True",
        "CallingReady": "True",
    }


def test_each_bad_signal_fires_its_condition():
    obs = skchat_adapter.skchat_observe(
        probe=lambda: {
            "daemon_ready": False,
            "bridge_alive": False,
            "outbox_depth": 5000,
            "outbox_limit": 1000,
            "auth_enforced": False,
            "calling_ready": False,
        }
    )
    by_type = {c["type"]: c["status"] for c in obs["conditions"]}
    assert by_type["DaemonReady"] == "False"
    assert by_type["BridgeAlive"] == "False"  # health condition fires when False
    assert by_type["OutboxBounded"] == "False"  # over the bound
    assert by_type["AuthEnforced"] == "False"
    assert by_type["CallingReady"] == "False"  # calling backend down


def test_calling_ready_rule():
    # "down" is the only value that reads not-ready; ok/degraded/None fail safe.
    assert skchat_adapter._calling_ready("down") is False
    assert skchat_adapter._calling_ready("DOWN") is False
    assert skchat_adapter._calling_ready("ok") is True
    assert skchat_adapter._calling_ready("degraded") is True
    assert skchat_adapter._calling_ready(None) is True


# --- pure probe logic --------------------------------------------------------


def test_bridge_wedge_rule():
    # daemon up + poll stale beyond threshold = wedged.
    assert skchat_adapter._bridge_alive(601, daemon_up=True) is False
    # fresh poll = alive.
    assert skchat_adapter._bridge_alive(10, daemon_up=True) is True
    # daemon down: the wedge detector defers to DaemonReady (not-wedged).
    assert skchat_adapter._bridge_alive(9999, daemon_up=False) is True
    # unknown age fails safe (alive).
    assert skchat_adapter._bridge_alive(None, daemon_up=True) is True


def test_count_outbox_counts_files(tmp_path):
    box = tmp_path / "outbox"
    box.mkdir()
    (box / "a.msg").write_text("x")
    (box / "b.msg").write_text("y")
    (box / "sub").mkdir()  # dirs are not counted
    assert skchat_adapter._count_outbox(box) == 2


def test_count_outbox_missing_dir_is_zero(tmp_path):
    assert skchat_adapter._count_outbox(tmp_path / "nope") == 0


def test_default_probe_fails_safe(monkeypatch, tmp_path):
    # An unreachable skchat must not raise and must report healthy.
    def _boom_urlopen(*a, **k):
        raise OSError("skchat down")

    monkeypatch.setattr("urllib.request.urlopen", _boom_urlopen)
    monkeypatch.delenv("SKCHAT_DATAPLANE_AUTH", raising=False)
    monkeypatch.setenv("SKCHAT_BRIDGE_HEARTBEAT", str(tmp_path / "no-heartbeat.ts"))
    monkeypatch.setenv("SKCOMMS_OUTBOX", str(tmp_path / "empty-outbox"))
    st = skchat_adapter._default_probe()
    assert st["daemon_ready"] is True
    assert st["bridge_alive"] is True
    assert st["auth_enforced"] is True
    assert st["outbox_depth"] == 0
    assert st["calling_ready"] is True


# --- act verb ----------------------------------------------------------------


def test_act_restart_daemon_calls_systemd(tmp_path):
    paths = _paths(tmp_path)
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return _ok_runner(cmd)

    res = skchat_adapter.skchat_act(
        paths, {"action": "restart-daemon", "ts": "t1"}, {}, runner=runner
    )
    assert res["performed"] is True
    assert any("restart" in c and "skchat-daemon.service" in c for c in calls)


def test_act_restart_bridge_uses_per_agent_unit(tmp_path):
    paths = _paths(tmp_path)
    calls = []
    res = skchat_adapter.skchat_act(
        paths,
        {"action": "restart-telegram-bridge", "ts": "t2"},
        {},
        runner=lambda c: calls.append(c) or _ok_runner(c),
        agent="opus",
    )
    assert res["performed"] is True
    assert any("skchat-telegram-opus.service" in c for c in calls)


def test_act_refuses_when_frozen(tmp_path):
    paths = _paths(tmp_path)
    human = store.Writer(role="operator", node="n", identity="chef")
    store.set_frozen(paths, True, writer=human)
    with pytest.raises(RuntimeError):
        skchat_adapter.skchat_act(
            paths, {"action": "restart-daemon", "ts": "t3"}, {}, runner=_ok_runner
        )


def test_act_refuses_unmapped_action(tmp_path):
    paths = _paths(tmp_path)
    with pytest.raises(ValueError):
        skchat_adapter.skchat_act(
            paths, {"action": "purge-outbox", "ts": "t4"}, {}, runner=_ok_runner
        )


def test_purge_outbox_classifies_major_by_construction():
    # The irreversible action escalates: policy forces MAJOR, never auto.
    purge = next(a for a in skchat_adapter._ACTIONS if a["name"] == "purge-outbox")
    verdict = policy.classify_change(purge)
    assert verdict["change_class"] == "major"
    assert verdict["auto_approvable"] is False
