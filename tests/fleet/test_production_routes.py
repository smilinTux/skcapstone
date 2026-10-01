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
    routes._CATALOGS.clear()
    monkeypatch.setattr(routes, "materialize_gateway_catalog", lambda *args: {})
    return value, snapshot, catalog


def test_catalog_materializes_same_snapshot_once_before_probe(qualification, monkeypatch):
    value, observed, _ = qualification
    bound = production_builder.route_binding(value, "24b00001", "sk-xl", [])
    calls = []
    monkeypatch.setattr(
        routes,
        "materialize_gateway_catalog",
        lambda home, policy, snap: calls.append((home, policy, snap)),
    )
    monkeypatch.setattr(
        routes,
        "resolve_and_preflight",
        lambda *args: RoutePreflight("codex-exact", "served", "codex"),
    )
    routes.preflight(value, bound)
    routes.preflight(value, bound)
    assert len(calls) == 1 and calls[0] == (Path.home(), value, observed)
    observed["routes"][0]["provider"] = "zai"
    with pytest.raises(ValueError):
        routes.preflight(value, bound)


def test_real_catalog_adapter_projects_gateway_capabilities_before_launch(tmp_path, monkeypatch):
    from skcapstone.fleet.pi_catalog import materialize_gateway_catalog
    from tests.fleet.test_pi_catalog import existing, policy, snapshot

    path, _ = existing(tmp_path)
    observed = snapshot()
    observed["routes"][0].update(policy_tier="cloud", max=4, gateway_active=0)
    from skcapstone.fleet.review_capacity import seal_review_capacity_truth

    observed = seal_review_capacity_truth(observed, {})
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(routes, "snapshot", lambda value: observed)
    monkeypatch.setattr(routes, "materialize_gateway_catalog", materialize_gateway_catalog)
    monkeypatch.setattr(
        routes,
        "resolve_and_preflight",
        lambda *args: RoutePreflight("new-gateway-id", "served", "deepseek"),
    )
    routes._CATALOGS.clear()
    routes._PREFLIGHTS.clear()
    bound = production_builder.route_binding(policy(), "24b00001", "sk-m", [])
    routes.preflight(policy(), bound)
    model = json.loads(path.read_text())["providers"]["skgateway"]["models"][0]
    assert model["id"] == "new-gateway-id"
    assert model["contextWindow"] == 54321 and model["maxTokens"] == 12345


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


def test_family_exclusive_eligibility_does_not_authorize_wrong_privacy_route(qualification):
    value, _, _ = qualification
    assert routes.candidates(value, "sk-m", ["codex-only", "local-only"]) == []


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
