"""skcode adapter: contract-conformant, session/registry logic, act gated by freeze."""

from __future__ import annotations

import pytest

from skcapstone.fleet import store
from skcapstone.fleet.paths import FleetPaths
from skcapstone.operator_seat import adapter, policy, skcode_adapter


def _paths(tmp_path):
    return FleetPaths(root=tmp_path / "fleet")


def _ok_runner(cmd):
    import subprocess

    return subprocess.CompletedProcess(cmd, 0, "", "")


# --- contract + observe ------------------------------------------------------


def test_skcode_explain_is_contract_conformant():
    assert adapter.validate_explain(skcode_adapter.skcode_explain()) == []


def test_explain_lists_the_four_conditions():
    assert skcode_adapter.skcode_explain()["conditions"] == [
        "HostdReady",
        "SessionsHealthy",
        "RegistryConsistent",
        "AuthEnforced",
    ]


def test_skcode_observe_is_contract_conformant():
    obs = skcode_adapter.skcode_observe(probe=lambda: {})
    assert adapter.validate_observe(obs) == []


def test_healthy_hostd_is_all_true():
    obs = skcode_adapter.skcode_observe(
        probe=lambda: {
            "hostd_ready": True,
            "sessions_healthy": True,
            "registry_consistent": True,
            "auth_enforced": True,
        }
    )
    by_type = {c["type"]: c["status"] for c in obs["conditions"]}
    assert set(by_type.values()) == {"True"}


def test_each_bad_signal_fires_its_condition():
    obs = skcode_adapter.skcode_observe(
        probe=lambda: {
            "hostd_ready": False,
            "sessions_healthy": False,
            "registry_consistent": False,
            "auth_enforced": False,
        }
    )
    by_type = {c["type"]: c["status"] for c in obs["conditions"]}
    assert by_type == {
        "HostdReady": "False",
        "SessionsHealthy": "False",
        "RegistryConsistent": "False",
        "AuthEnforced": "False",
    }


# --- pure probe logic --------------------------------------------------------


def test_sessions_healthy_flags_stale_running_session():
    fresh = [{"state": "running", "last_event_age_s": 10}]
    stale = [{"state": "running", "last_event_age_s": 5000}]
    done = [{"state": "exited", "last_event_age_s": 99999}]
    unknown = [{"state": "running", "last_event_age_s": None}]
    assert skcode_adapter._sessions_healthy(fresh) is True
    assert skcode_adapter._sessions_healthy(stale) is False  # runaway/wedge
    assert skcode_adapter._sessions_healthy(done) is True  # not running
    assert skcode_adapter._sessions_healthy(unknown) is True  # unknown age fails safe
    assert skcode_adapter._sessions_healthy([]) is True


def test_registry_consistent_detects_orphans():
    # every registered id backed by a live id = consistent.
    assert skcode_adapter._registry_consistent(["a", "b"], ["a", "b", "c"]) is True
    # a registry entry with no live backing = orphan = inconsistent.
    assert skcode_adapter._registry_consistent(["a", "orphan"], ["a"]) is False
    assert skcode_adapter._registry_consistent([], []) is True


def test_default_probe_fails_safe(monkeypatch):
    def _boom(*a, **k):
        raise OSError("hostd down")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    st = skcode_adapter._probe_hostd()
    assert st == {
        "hostd_ready": True,
        "sessions_healthy": True,
        "registry_consistent": True,
        "auth_enforced": True,
    }


# --- act verb ----------------------------------------------------------------


def test_act_restart_hostd_calls_systemd(tmp_path):
    paths = _paths(tmp_path)
    calls = []
    res = skcode_adapter.skcode_act(
        paths,
        {"action": "restart-hostd", "ts": "t1"},
        {},
        runner=lambda c: calls.append(c) or _ok_runner(c),
    )
    assert res["performed"] is True
    assert any("restart" in c and "skcode-hostd.service" in c for c in calls)


def test_act_archive_stale_session_uses_cli_runner(tmp_path):
    paths = _paths(tmp_path)
    calls = []
    res = skcode_adapter.skcode_act(
        paths,
        {"action": "archive-stale-session", "object": "sess-42", "ts": "t2"},
        {},
        cli_runner=lambda c: calls.append(c) or _ok_runner(c),
    )
    assert res["performed"] is True
    assert calls[0][:5] == [
        "skcode-hostd",
        "operator",
        "act",
        "archive-stale-session",
        "--session",
    ]
    assert "sess-42" in calls[0]


def test_act_archive_without_cli_runner_raises(tmp_path):
    paths = _paths(tmp_path)
    with pytest.raises(ValueError):
        skcode_adapter.skcode_act(
            paths, {"action": "archive-stale-session", "object": "s1", "ts": "t"}, {}
        )


def test_act_refuses_when_frozen(tmp_path):
    paths = _paths(tmp_path)
    human = store.Writer(role="operator", node="n", identity="chef")
    store.set_frozen(paths, True, writer=human)
    with pytest.raises(RuntimeError):
        skcode_adapter.skcode_act(
            paths, {"action": "restart-hostd", "ts": "t3"}, {}, runner=_ok_runner
        )


def test_act_refuses_unmapped_action(tmp_path):
    paths = _paths(tmp_path)
    with pytest.raises(ValueError):
        skcode_adapter.skcode_act(
            paths, {"action": "kill-runaway-session", "ts": "t4"}, {}, runner=_ok_runner
        )


def test_kill_runaway_classifies_major_by_construction():
    kill = next(a for a in skcode_adapter._ACTIONS if a["name"] == "kill-runaway-session")
    verdict = policy.classify_change(kill)
    assert verdict["change_class"] == "major"
    assert verdict["auto_approvable"] is False
