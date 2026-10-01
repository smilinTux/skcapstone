"""Production policy is current routing input, never activation authorization."""

import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from skcapstone.fleet.production_policy import (
    LEGACY_CEILING_VARIABLES,
    load_production_policy,
)
from skcapstone.niobe_live_entrypoint import run_live


@pytest.fixture
def policy():
    return {
        "schema": "skfleet.production/v1",
        "authority_host": "chiap08",
        "capacity_authority": "skgateway",
        "gateway_url": "http://chiap01:18790",
        "lanes": {
            lane: {"enabled": True, "provider": "skgateway", "model": model}
            for lane, model in {
                "codex": "gpt-5.6-sol",
                "glm": "sk-zai-m",
                "deepseek": "deepseek-flash",
                "qwen": "qwen3.8-27b-huihui-abliterated-q4_k_m",
            }.items()
        }
        | {"kimi": {"enabled": False}},
        "node_quotas": {
            "chiap03": {
                "cpu_quota_percent": 200,
                "memory_max_bytes": 2147483648,
                "tasks_max": 256,
                "runtime_max_seconds": 1800,
            }
        },
        "cycle_budget_seconds": 240,
        "scan_budget": 100,
    }


def test_policy_fresh_read_and_independent_result(tmp_path, policy):
    path = tmp_path / "production.json"
    path.write_text(json.dumps(policy))
    first = load_production_policy(path, host="chiap08")
    assert first == policy
    first["lanes"]["codex"]["model"] = "caller-corruption"
    assert load_production_policy(path, host="chiap08") == policy
    policy["lanes"]["codex"]["model"] = "future-qualified-model"
    path.write_text(json.dumps(policy))
    assert load_production_policy(path, host="chiap08") == policy


@pytest.mark.parametrize(
    "change",
    [
        {"schema": "old"},
        {"capacity_authority": "local"},
        {"authority_host": "chiap03"},
        {"worker_count": 12},
        {"scan_budget": True},
        {"cycle_budget_seconds": 0},
        {"cycle_budget_seconds": 251},
        {"node_quotas": {"chiap03": {"workers": 4}}},
        {"lanes": {}},
    ],
)
def test_policy_rejects_invalid_authority_or_local_ceiling(tmp_path, policy, change):
    path = tmp_path / "production.json"
    path.write_text(json.dumps(policy | change))
    with pytest.raises(ValueError):
        load_production_policy(path, host="chiap08")


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.invalid",
        "http://@chiap01:18790",
        "file:///tmp/gateway",
        "http://chiap01:18790/v1",
        "http://chiap01:18790?key=x",
        "http://chiap01:18790#x",
        "http://chiap01:0",
        "http://chiap01:999999",
        "http://chiap01:\n18790",
    ],
)
def test_policy_rejects_non_origin_or_credentialed_url(tmp_path, policy, url):
    path = tmp_path / "production.json"
    policy["gateway_url"] = url
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="URL"):
        load_production_policy(path, host="chiap08")


@pytest.mark.parametrize(
    "lane,change",
    [
        ("codex", {"provider": "direct-provider"}),
        ("glm", {"enabled": 1}),
        ("deepseek", {"model": ""}),
        ("qwen", {"model": "model with spaces"}),
        ("codex", {"workers": 3}),
        ("kimi", {"enabled": True}),
    ],
)
def test_policy_rejects_ambiguous_routes(tmp_path, policy, lane, change):
    path = tmp_path / "production.json"
    policy["lanes"][lane].update(change)
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError):
        load_production_policy(path, host="chiap08")


@pytest.mark.parametrize(
    "field,value",
    [
        ("cpu_quota_percent", True),
        ("memory_max_bytes", 0),
        ("tasks_max", -1),
        ("runtime_max_seconds", "1800"),
    ],
)
def test_policy_rejects_invalid_per_worker_resources(tmp_path, policy, field, value):
    path = tmp_path / "production.json"
    policy["node_quotas"]["chiap03"][field] = value
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="worker resources"):
        load_production_policy(path, host="chiap08")


@pytest.mark.parametrize(
    "kind", ["missing", "directory", "symlink", "fifo", "oversized", "duplicates"]
)
def test_policy_refuses_unsafe_or_ambiguous_input(tmp_path, policy, kind):
    path = tmp_path / "production.json"
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target.json"
        target.write_text(json.dumps(policy))
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "oversized":
        path.write_bytes(b" " * (1024 * 1024 + 1))
    elif kind == "duplicates":
        path.write_text('{"schema": 1, "schema": 2}')
    with pytest.raises((OSError, ValueError)):
        load_production_policy(path, host="chiap08")


@pytest.fixture
def live(tmp_path, monkeypatch, policy):
    """Use real activation validation and child-process dispatch with private data."""
    home = tmp_path / "home"
    core = home / "cards/testcard/core.json"
    core.parent.mkdir(parents=True)
    core.write_text('{"id":"testcard"}')
    estate = home / "config/estate.json"
    estate.parent.mkdir(parents=True)
    estate.write_text(
        json.dumps(
            {
                "schema": "sk.estate-authority/v1",
                "operator": "casey",
                "realm": "skworld.io",
                "product_scope": ["skcapstone"],
            }
        )
    )
    activation = home / "coordination/niobe-activation.json"
    activation.parent.mkdir(parents=True)
    activation.write_text(
        json.dumps(
            {
                "schema": "skfleet.niobe-activation/v1",
                "state": "active",
                "seat": "niobe",
                "card_label": "seat-niobe",
                "decision_id": "test-decision",
                "authorized_by": "casey",
                "card_id": "testcard",
                "card_revision": hashlib.sha256(core.read_bytes()).hexdigest(),
                "host": "chiap08",
                "live_unit": "skfleet-niobe-live.timer",
                "product_scope": ["skcapstone"],
                "allowed_actions": ["claim", "release", "launch", "stop", "reassign"],
                "denied_actions": [
                    "merge",
                    "deploy",
                    "application_actuation",
                    "external_dispatch",
                ],
                "expires_at": "2099-01-01T00:00:00+00:00",
                "rollback": {"owner": "casey", "action": "disable_skfleet-niobe-live.timer"},
            }
        )
    )
    path = home / "fleet/production.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(policy))
    dispatcher = tmp_path / "dispatcher.py"
    dispatcher.write_text("SKFLEET_PRODUCTION_POLICY_V1 = True\n")
    for key in LEGACY_CEILING_VARIABLES:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SKFLEET_PRODUCTION_POLICY", str(path))
    calls = []
    monkeypatch.setattr(
        "skcapstone.niobe_live_entrypoint.startup_hello", lambda *a, **k: calls.append("hello")
    )
    monkeypatch.setattr(
        "skcapstone.niobe_live_entrypoint.poll_mail",
        lambda *a, **k: SimpleNamespace(as_dict=lambda: {"mailbox_ok": True}),
    )
    return home, activation, dispatcher, path, calls


def test_gateway_policy_reaches_actual_dispatcher_and_preserves_retry_guard(live, monkeypatch):
    home, activation, dispatcher, path, calls = live
    monkeypatch.setenv("SKFLEET_MAX_CLAIMS", "7")
    monkeypatch.setenv("SKFLEET_GATEWAY_URL", "http://stale.invalid")
    dispatcher.write_text("""SKFLEET_PRODUCTION_POLICY_V1 = True
import json, os
from pathlib import Path
policy = json.loads(Path(os.environ['SKFLEET_PRODUCTION_POLICY']).read_text())
assert os.environ['SKFLEET_GATEWAY_URL'] == policy['gateway_url']
assert os.environ['SKFLEET_MAX_CLAIMS'] == '7'
assert all(policy['lanes'][lane]['enabled'] for lane in ('codex','glm','deepseek','qwen'))
assert 'SKFLEET_TARGET' not in os.environ
home = Path(os.environ['SKFLEET_NIOBE_ACTIVATION']).parent.parent
evidence = home/'evidence/fleet-rotation'/os.environ['SKFLEET_ROTATION_ID']/'actions.log'
evidence.parent.mkdir(parents=True)
evidence.write_text('NOOP_RECEIPT|chiap08|reason=synthetic-qualified\n')
""".replace("synthetic-qualified\n'", "synthetic-qualified\\n'"))
    assert run_live(activation_path=activation, dispatcher=dispatcher, local_host="chiap08") == 0
    receipt = json.loads((home / "coordination/seat-cycles/niobe.health.jsonl").read_text())
    assert receipt["result"] == "bounded_noop"
    assert receipt["dispatcher_returncode"] == 0
    assert calls == ["hello"]


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "invalid",
        "legacy_target",
        "old_dispatcher",
        "comment_marker",
        "false_marker",
        "duplicate_marker",
        "stale_activation",
    ],
)
def test_policy_refusal_has_no_mail_or_dispatch_side_effect(live, monkeypatch, problem):
    home, activation, dispatcher, path, calls = live
    if problem == "missing":
        path.unlink()
    elif problem == "invalid":
        path.write_text("{}")
    elif problem == "legacy_target":
        monkeypatch.setenv("SKFLEET_TARGET", "100")
    elif problem == "old_dispatcher":
        dispatcher.write_text("raise RuntimeError('must not execute')\n")
    elif problem == "comment_marker":
        dispatcher.write_text("# SKFLEET_PRODUCTION_POLICY_V1 = True\n")
    elif problem == "false_marker":
        dispatcher.write_text("SKFLEET_PRODUCTION_POLICY_V1 = False\n")
    elif problem == "duplicate_marker":
        dispatcher.write_text(
            "SKFLEET_PRODUCTION_POLICY_V1 = True\nSKFLEET_PRODUCTION_POLICY_V1 = False\n"
        )
    elif problem == "stale_activation":
        (home / "cards/testcard/core.json").write_text('{"id":"changed"}')
    with pytest.raises((OSError, ValueError)):
        run_live(
            activation_path=activation,
            dispatcher=dispatcher,
            local_host="chiap08",
            runner=lambda *a, **k: calls.append("dispatcher"),
        )
    assert calls == []
    assert not (home / "coordination/seat-cycles/niobe.health.jsonl").exists()
