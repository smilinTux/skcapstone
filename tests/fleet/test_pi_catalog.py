"""Real private-file regression tests for gateway-derived Pi catalogs."""

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from skcapstone.fleet import pi_catalog
from skcapstone.fleet.review_capacity import seal_review_capacity_truth


def policy():
    return {
        "gateway_url": "http://gateway.test:18790",
        "lanes": {
            family: {
                "enabled": family != "kimi",
                **({"provider": "skgateway"} if family != "kimi" else {}),
            }
            for family in ("codex", "glm", "deepseek", "qwen", "kimi")
        },
    }


def snapshot(model="new-gateway-id", **changes):
    row = {
        "id": model,
        "advertised": True,
        "stale": False,
        "tools": True,
        "provider": "deepseek",
        "vision": False,
        "card": {
            "display_name": "Gateway title",
            "reasoning": True,
            "generation_default_tokens": 12345,
            "context_length": 54321,
        },
    }
    value = {
        "schema_version": 1,
        "cycle_id": "fixture",
        "observed_at": time.time(),
        "endpoint": policy()["gateway_url"],
        "error": None,
        "routes": [
            {
                "logical_route": model,
                "model_or_bucket": model,
                "provider": "deepseek",
                "capacity_domain": "deepseek",
                "size_class": "M",
                "state": "healthy",
                "gateway_model": row,
            }
        ],
    }
    value.update(changes)
    return seal_review_capacity_truth(value, {})


def existing(tmp_path, generic=True):
    path = tmp_path / ".pi/agent/models.json"
    path.parent.mkdir(parents=True)
    gateway = {
        "baseUrl": policy()["gateway_url"] + "/v1",
        "apiKey": "PRIVATE_TEST_SENTINEL",
        "api": "openai-completions",
        "models": [{"id": "old-id"}],
    }
    value = {
        "other_config": True,
        "providers": {
            "unrelated": {"sentinel": True},
            "skgateway" if generic else "existing-gateway-alias": gateway,
        },
    }
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path, value


def test_exact_gateway_metadata_and_private_credentials_preserved(tmp_path):
    path, before = existing(tmp_path)
    result = pi_catalog.materialize_gateway_catalog(tmp_path, policy(), snapshot())
    after = json.loads(path.read_text())
    assert after["providers"]["unrelated"] == before["providers"]["unrelated"]
    provider = after["providers"]["skgateway"]
    assert provider["apiKey"] == "PRIVATE_TEST_SENTINEL"
    assert provider["models"] == [
        {
            "id": "new-gateway-id",
            "name": "Gateway title",
            "reasoning": True,
            "input": ["text"],
            "contextWindow": 54321,
            "maxTokens": 12345,
            "compat": {"thinkingFormat": "deepseek"},
        }
    ]
    assert path.stat().st_mode & 0o777 == 0o600
    assert result["changed"] and result["models"] == ["new-gateway-id"]
    assert "PRIVATE_TEST_SENTINEL" not in json.dumps(result)
    backup = path.parent / result["backup_name"]
    assert json.loads(backup.read_text()) == before
    assert backup.stat().st_mode & 0o777 == 0o600


def test_gateway_alias_credential_used_without_copying_unrelated_provider(tmp_path):
    path, before = existing(tmp_path, generic=False)
    pi_catalog.materialize_gateway_catalog(tmp_path, policy(), snapshot())
    after = json.loads(path.read_text())
    assert (
        after["providers"]["existing-gateway-alias"]
        == before["providers"]["existing-gateway-alias"]
    )
    assert after["providers"]["skgateway"]["apiKey"] == "PRIVATE_TEST_SENTINEL"


def test_repeated_snapshot_is_noop_without_another_backup(tmp_path):
    path, _ = existing(tmp_path)
    snap = snapshot()
    first = pi_catalog.materialize_gateway_catalog(tmp_path, policy(), snap)
    second = pi_catalog.materialize_gateway_catalog(tmp_path, policy(), snap)
    assert first["changed"] and not second["changed"]
    assert len(list(path.parent.glob("models.json.before-*"))) == 1


@pytest.mark.parametrize(
    "failure",
    ["stale", "wrong-endpoint", "unsealed", "unhealthy", "wrong-id", "unqualified", "no-routes"],
)
def test_bad_observation_never_changes_catalog(tmp_path, failure):
    path, _ = existing(tmp_path)
    snap = snapshot()
    if failure == "stale":
        snap["observed_at"] -= 1000
    if failure == "wrong-endpoint":
        snap["endpoint"] = "http://other.test"
    if failure == "unsealed":
        snap["routes"][0]["gateway_model"]["id"] = "tampered"
    if failure == "unhealthy":
        snap["routes"][0]["state"] = "unknown"
    if failure == "wrong-id":
        snap["routes"][0]["gateway_model"]["id"] = "other-id"
    if failure == "unqualified":
        snap["routes"][0]["gateway_model"]["tools"] = False
    if failure == "no-routes":
        snap["routes"] = []
    if failure != "unsealed":
        snap = seal_review_capacity_truth(snap, {})
    before = path.read_bytes()
    with pytest.raises(ValueError):
        pi_catalog.materialize_gateway_catalog(tmp_path, policy(), snap)
    assert path.read_bytes() == before
    assert not list(path.parent.glob("models.json.before-*"))


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_unsafe_catalog_rejected_without_opening_or_clobber(tmp_path, kind):
    path, _ = existing(tmp_path)
    original = path.read_bytes()
    path.unlink()
    target = tmp_path / "untouched"
    target.write_bytes(original)
    if kind == "symlink":
        path.symlink_to(target)
    else:
        os.mkfifo(path)
    with pytest.raises((OSError, ValueError)):
        pi_catalog.materialize_gateway_catalog(tmp_path, policy(), snapshot())
    assert target.read_bytes() == original


def test_optional_gateway_limits_not_invented(tmp_path):
    path, _ = existing(tmp_path)
    snap = snapshot()
    snap["routes"][0]["gateway_model"]["card"] = {"reasoning": True}
    snap = seal_review_capacity_truth(snap, {})
    pi_catalog.materialize_gateway_catalog(tmp_path, policy(), snap)
    model = json.loads(path.read_text())["providers"]["skgateway"]["models"][0]
    assert "maxTokens" not in model and "contextWindow" not in model


def test_concurrent_writers_leave_complete_private_json(tmp_path):
    path, _ = existing(tmp_path)
    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(
                lambda _: pi_catalog.materialize_gateway_catalog(tmp_path, policy(), snapshot()),
                range(2),
            )
        )
    assert sum(row["changed"] for row in results) == 1
    assert (
        json.loads(path.read_text())["providers"]["skgateway"]["models"][0]["id"]
        == "new-gateway-id"
    )
    assert hashlib.sha256(path.read_bytes()).hexdigest() in {row["sha256"] for row in results}
