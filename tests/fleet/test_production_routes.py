"""Gateway capabilities and exact Pi identifiers govern native route admission."""

import json
import time
from pathlib import Path

import pytest

from skcapstone.fleet import production_builder
from skcapstone.fleet import production_routes as routes
from skcapstone.fleet_route_preflight import RoutePreflight


@pytest.fixture
def qualification(monkeypatch, tmp_path):
    value = {
        "gateway_url": "http://gateway:18790",
        "lanes": {
            name: {"enabled": True, "provider": "skgateway"}
            for name in ("codex", "glm", "deepseek", "qwen")
        },
    }
    value["lanes"]["kimi"] = {"enabled": False}
    snapshot = {
        "schema_version": 1,
        "error": None,
        "observed_at": time.time(),
        "routes": [
            {
                "logical_route": name + "-exact",
                "model_or_bucket": name + "-exact",
                "provider": domain,
                "capacity_domain": domain,
                "size_class": size,
                "policy_tier": tier,
                "state": "healthy",
                "max": 8,
                "gateway_active": 0,
            }
            for name, domain, size, tier in (
                ("codex", "codex", "XL", "cloud"),
                ("glm", "zai", "M", "cloud"),
                ("deepseek", "deepseek", "M", "cloud"),
                ("qwen", "chiap08-qwen38", "S", "local"),
            )
        ],
    }
    monkeypatch.setattr(routes, "snapshot", lambda policy: snapshot)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    catalog = tmp_path / ".pi/agent/models.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        json.dumps(
            {
                "providers": {
                    "skgateway": {
                        "baseUrl": "http://gateway:18790/v1",
                        "apiKey": "test-placeholder-never-returned",
                        "models": [
                            {"id": name + "-exact"}
                            for name in ("codex", "glm", "deepseek", "qwen")
                        ],
                    }
                }
            }
        )
    )
    routes._PREFLIGHTS.clear()
    return value, snapshot, catalog


def test_large_work_never_hashes_into_medium_family(qualification):
    value, snapshot, _ = qualification
    for size in ("sk-l", "sk-xl"):
        for number in range(20):
            assert (
                production_builder.route_binding(value, f"{number:08x}", size, [])["family"]
                == "codex"
            )
    snapshot["routes"][0]["state"] = "owner-down"
    with pytest.raises(routes.RouteUnavailableError):
        production_builder.route_binding(value, "24b00001", "sk-xl", [])


def test_local_policy_uses_only_qualified_qwen_and_stale_snapshot_refuses(qualification):
    value, snapshot, _ = qualification
    bound = production_builder.route_binding(value, "24b00001", "sk-s", ["local-only"])
    assert bound["family"] == "qwen"
    # A fresh gateway card can qualify a larger local model without a second
    # hard-coded S-only restriction in the worker scheduler.
    snapshot["routes"][-1]["size_class"] = "M"
    assert (
        production_builder.route_binding(value, "24b00001", "sk-m", ["local-only"])["family"]
        == "qwen"
    )
    assert (
        production_builder.route_binding(value, "24b00001", "sk-m", ["qwen-first"])["family"]
        == "qwen"
    )
    snapshot["observed_at"] -= 31
    assert routes.candidates(value, "sk-s", ["local-only"]) == []


@pytest.mark.parametrize("kind", ["capacity", "family", "duplicate"])
def test_exact_route_refuses_full_wrong_family_or_duplicate(qualification, kind):
    value, snapshot, _ = qualification
    if kind == "capacity":
        snapshot["routes"][0]["gateway_active"] = 8
    elif kind == "family":
        snapshot["routes"][0]["capacity_domain"] = "zai"
    else:
        snapshot["routes"].append(dict(snapshot["routes"][0]))
    assert routes.candidates(value, "sk-xl", []) == []


def test_preflight_exact_requested_served_backend_and_short_cache(qualification, monkeypatch):
    value, _, _ = qualification
    bound = production_builder.route_binding(value, "24b00001", "sk-xl", [])
    calls = []

    def probe(gateway, model):
        calls.append((gateway, model))
        return RoutePreflight(model, "actual-qualified-model", "codex")

    monkeypatch.setattr(routes, "resolve_and_preflight", probe)
    first = routes.preflight(value, bound)
    assert routes.preflight(value, bound) == first
    assert len(calls) == 1
    assert first["requested_identity"] == "codex-exact"
    assert first["served_identity"] == "actual-qualified-model"
    assert first["provider"] == "codex"
    routes._PREFLIGHTS.clear()
    monkeypatch.setattr(
        routes,
        "resolve_and_preflight",
        lambda *args: RoutePreflight("codex-exact", "wrong", "zai"),
    )
    with pytest.raises(ValueError):
        routes.preflight(value, bound)


@pytest.mark.parametrize("kind", ["missing", "fuzzy", "duplicate", "endpoint"])
def test_pi_catalog_never_uses_profile_or_fuzzy_fallback(qualification, monkeypatch, kind):
    value, _, path = qualification
    bound = production_builder.route_binding(value, "24b00001", "sk-xl", [])
    catalog = json.loads(path.read_text())
    provider = catalog["providers"]["skgateway"]
    if kind == "missing":
        del catalog["providers"]["skgateway"]
    elif kind == "fuzzy":
        provider["models"][0]["id"] = "codex-exact-suffix"
    elif kind == "duplicate":
        provider["models"].append(dict(provider["models"][0]))
    else:
        provider["baseUrl"] = "https://provider.example.invalid/v1"
    path.write_text(json.dumps(catalog))
    monkeypatch.setattr(
        routes, "resolve_and_preflight", lambda *args: pytest.fail("must not probe")
    )
    with pytest.raises(ValueError, match="Pi exact model catalog"):
        routes.preflight(value, bound)
