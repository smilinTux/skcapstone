"""skcode adapter: contract-conformant, session/registry logic, act gated by freeze."""

from __future__ import annotations

import json

import pytest

from skcapstone.fleet import store
from skcapstone.fleet.paths import FleetPaths
from skcapstone.operator_seat import adapter, policy, skcode_adapter


def _paths(tmp_path):
    return FleetPaths(root=tmp_path / "fleet")


def _provision_ready(paths):
    writer = store.Writer(role="operator", node="test-node", identity="test")
    store.set_frozen(paths, False, writer=writer, reason="test fixture provisioning")


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


class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(self.body).encode()


def test_atlas_activity_replay_carries_bearer_cursor_and_filters(monkeypatch):
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        return _Response({"events": [], "next_cursor": 7})

    monkeypatch.setenv("SKCODE_HOSTD_URL", "http://node41:9394/")
    result = skcode_adapter.skcode_activity(
        token="wire-token",
        after=7,
        filters={
            "agent_id": "scout-1",
            "card_id": "card-1",
            "contract_id": "contract-1",
            "lease_id": "lease-1",
            "role": "scout",
        },
        opener=opener,
    )
    request = calls[0][0]
    assert result["next_cursor"] == 7
    assert request.full_url.startswith("http://node41:9394/api/v1/activity?")
    assert "after=7" in request.full_url and "agent_id=scout-1" in request.full_url
    assert "contract_id=contract-1" in request.full_url
    assert request.headers["Authorization"] == "Bearer wire-token"
    assert request.method == "GET"


def test_atlas_control_submits_exact_command_and_reads_receipt(monkeypatch):
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if request.method == "POST":
            return _Response({"command": {"command_id": "cmd-1"}, "receipt": {"status": "queued"}})
        return _Response({"command": {"command_id": "cmd-1"}, "receipt": {"status": "applied"}})

    monkeypatch.setenv("SKCODE_HOSTD_URL", "https://node41.example")
    queued = skcode_adapter.skcode_control(
        token="wire-token",
        idempotency_key="atlas-1",
        target_kind="agent",
        target_id="scout-1",
        action="cancel",
        payload={"reason": "operator stop"},
        opener=opener,
    )
    sent = json.loads(calls[0].data)
    assert queued["receipt"]["status"] == "queued"
    assert sent["target_id"] == "scout-1" and sent["action"] == "cancel"
    assert calls[0].headers["Authorization"] == "Bearer wire-token"
    applied = skcode_adapter.skcode_control_receipt("cmd-1", token="wire-token", opener=opener)
    assert applied["receipt"]["status"] == "applied"
    assert calls[1].full_url.endswith("/api/v1/control/cmd-1")


def test_live_contract_exposes_separate_monitor_and_control_scopes(monkeypatch):
    monkeypatch.setenv("SKCODE_HOSTD_URL", "https://node41.example")
    contract = skcode_adapter.skcode_live_contract()
    assert contract["activity_stream"].startswith("wss://")
    assert contract["monitor_scope"] == "skcode.stream"
    assert contract["message_scope"] == "skcode.inject"
    assert contract["action_scope"] == "skcode.dispatch"
    assert "parent_agent_id" in contract["lineage_fields"]
    assert "contract_hash" in contract["lineage_fields"]


def test_atlas_control_rejects_unknown_targets_before_network():
    with pytest.raises(ValueError, match="unknown"):
        skcode_adapter.skcode_control(
            token="wire-token",
            idempotency_key="atlas-1",
            target_kind="fleet-root",
            target_id="all",
            action="cancel",
        )


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


def test_default_probe_failure_is_unknown(monkeypatch):
    def _boom(*a, **k):
        raise OSError("hostd down")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    st = skcode_adapter._probe_hostd()
    assert st == {"_probe_error": "OSError"}
    assert all(
        item["status"] == "Unknown"
        for item in skcode_adapter.skcode_observe(lambda: st)["conditions"]
    )


# --- act verb ----------------------------------------------------------------


def test_act_restart_hostd_calls_systemd(tmp_path):
    paths = _paths(tmp_path)
    _provision_ready(paths)
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
