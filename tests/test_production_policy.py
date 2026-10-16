"""Production policy is current routing input, never activation authorization."""

import json
import os

import pytest

from skcapstone.fleet.production_policy import (
    load_production_policy,
)


@pytest.fixture
def policy():
    return {
        "schema": "skfleet.production/v1",
        "authority_host": "chiap08",
        "capacity_authority": "skgateway",
        "gateway_url": "http://chiap01:18790",
        "lanes": {
            lane: {"enabled": True, "provider": "skgateway"}
            for lane in ("codex", "glm", "deepseek", "qwen")
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
    first["lanes"]["codex"]["enabled"] = False
    assert load_production_policy(path, host="chiap08") == policy
    policy["lanes"]["codex"]["enabled"] = False
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
    "runtime_limits",
    [{"other": 5400}, {"glm": True}, {"glm": 5401}, []],
)
def test_policy_validates_bounded_lane_runtime_limits(tmp_path, policy, runtime_limits):
    path = tmp_path / "production.json"
    path.write_text(json.dumps(policy | {"lane_runtime_max_seconds": runtime_limits}))
    with pytest.raises(ValueError, match="lane runtime limits"):
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
