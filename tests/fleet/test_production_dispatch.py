"""Native production routing and actual worker resource boundary tests."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet.production_dispatch import (
    authoritative_owner_state,
    cycle_budget_seconds,
    production_lanes,
    production_policy_from_environment,
    resolve_production_routes,
    routes_for_lane,
    worker_resource_properties,
)
from skcapstone.fleet.production_review import independent_review_routes
from skcapstone.fleet.review_capacity import aggregate_review_capacity, review_physical_free

ROTATE = Path(__file__).parents[2] / "scripts/fleet/skfleet-rotate.py"


def policy():
    return {
        "schema": "skfleet.production/v1",
        "authority_host": "controller",
        "capacity_authority": "skgateway",
        "gateway_url": "http://gateway:18790",
        "lanes": {
            **{
                name: {"enabled": True, "provider": "skgateway"}
                for name in ("codex", "glm", "deepseek", "qwen")
            },
            "kimi": {"enabled": False},
        },
        "node_quotas": {
            "controller": {
                "cpu_quota_percent": 200,
                "memory_max_bytes": 3 * 1024**3,
                "tasks_max": 256,
                "runtime_max_seconds": 3600,
            }
        },
    }


def helpers(*names, policy_value=None):
    tree = ast.parse(ROTATE.read_text())
    namespace = {
        "PRODUCTION_POLICY": policy_value,
        "HOST": "controller",
        "worker_resource_properties": worker_resource_properties,
        "_LANE_ONLY_LABELS": {"deepseek-only": "deepseek", "codex-only": "codex"},
    }
    nodes = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace


def test_controller_requires_current_policy_and_own_resource_quota(tmp_path):
    path = tmp_path / "policy.json"
    value = policy()
    path.write_text(json.dumps(value))
    env = {"SKFLEET_PRODUCTION_POLICY": str(path)}
    assert production_policy_from_environment(env, "controller") == value
    value["node_quotas"] = {}
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="resource quotas"):
        production_policy_from_environment(env, "controller")
    assert production_policy_from_environment({}, "controller") is None


def test_actual_launch_keeps_child_argv_and_enforces_node_quota(monkeypatch):
    monkeypatch.setenv("SKFLEET_PRODUCTION_POLICY", "/private/production.json")
    ns = helpers("_worker_launch_command", policy_value=policy())
    ns["os"] = __import__("os")
    launch = ns["_worker_launch_command"]
    argv = ["python", "wrapper.py", "--", "bash", "-lc", "printf '%s' '$HOME'"]
    command = launch("worker.service", "/workspace", argv)
    assert command[-len(argv) :] == argv
    assert "--setenv=SKFLEET_PRODUCTION_POLICY=/private/production.json" in command
    for expected in (
        "CPUQuota=200%",
        "MemoryMax=3221225472",
        "TasksMax=256",
        "RuntimeMaxSec=3600",
    ):
        assert "--property=" + expected in command


def test_gateway_capacity_does_not_subtract_existing_worker_count():
    lanes = [{"busy": list(range(1000)), "capacity_domains": ["zai"]}]
    routes = [
        {"capacity_domain": "zai", "free": 7},
        {"capacity_domain": "zai", "free": 7},
        {"capacity_domain": "deepseek", "free": 5},
    ]
    assert review_physical_free(lanes, routes, {"zai": 2}, None) == 10
    assert review_physical_free(lanes, routes, {}, 3) == 0
    assert review_physical_free(lanes, [], {}, None) == 0


def test_lane_uses_qualified_family_not_a_static_model():
    lane = next(row for row in production_lanes(policy(), 100) if row["name"] == "deepseek")
    exact = {
        "logical_route": "deepseek-test",
        "capacity_domain": "deepseek",
        "provider": "deepseek",
    }
    future = {**exact, "logical_route": "future-qualified-deepseek"}
    rows = [
        exact,
        {"logical_route": "gpt-test", "capacity_domain": "codex"},
        future,
        {"logical_route": "deepseek-test", "capacity_domain": "codex"},
    ]
    assert routes_for_lane(rows, lane) == [exact, future]


def test_pi_model_is_selected_from_current_card_routes_not_lane_defaults():
    ns = helpers("_lane_model", policy_value=policy())
    row = {"model_or_bucket": "gateway-qualified-new-id", "capacity_domain": "deepseek", "free": 1}
    calls = []
    ns["_production_card_routes"] = lambda core, labels, lane: calls.append(
        (core, labels, lane)
    ) or [row]
    ns["choose_review_route"] = lambda rows, reservations: rows[0] if rows else None
    ns["_review_route_reservations"] = {}
    core = {"title": "[M] Implement bounded card"}
    lane = {"name": "deepseek", "model": "obsolete-static-id"}
    assert ns["_lane_model"](lane, core, ["deepseek-only"]) == row["model_or_bucket"]
    assert calls == [(core, ["deepseek-only"], "deepseek")]
    ns["_production_card_routes"] = lambda *args: []
    assert ns["_lane_model"](lane, core, []) is None


def test_actual_review_capacity_gate_requires_an_independent_family():
    ns = helpers("evaluate_review_capacity", policy_value=policy())
    rows = [
        {
            "logical_route": "gpt-test",
            "model_or_bucket": "gpt-test",
            "capacity_domain": "codex",
            "provider": "codex",
            "free": 30,
        },
        {
            "logical_route": "deepseek-test",
            "model_or_bucket": "deepseek-test",
            "capacity_domain": "deepseek",
            "provider": "deepseek",
            "free": 20,
        },
    ]
    ns.update(
        {
            "_evaluate_review_capacity": lambda *args, **kwargs: {
                "reason": "eligible",
                "routes": rows,
                "available": 50,
            },
            "aggregate_review_capacity": aggregate_review_capacity,
            "independent_review_routes": independent_review_routes,
        }
    )
    result = ns["evaluate_review_capacity"]({}, "M", [], "pi-codex-node-deadbeef", "reviewer")
    assert result["available"] == 20
    assert result["routes"] == [rows[1]]
    unknown = ns["evaluate_review_capacity"]({}, "M", [], "unknown-source", "reviewer")
    assert unknown["available"] == 0
    assert unknown["reason"] == "source-provider-evidence-required"


def test_old_seat_pin_does_not_poison_other_production_seats(tmp_path):
    path = tmp_path / "seats.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "seats": {
                    "codex": ["old-host"],
                    "niobe": ["controller"],
                    "seraph": ["controller"],
                },
            }
        )
    )
    ns = helpers("_load_seat_placement", policy_value=policy())
    ns.update(
        {
            "json": json,
            "Path": Path,
            "re": __import__("re"),
            "_SEAT_RE": __import__("re").compile(r"[a-z][a-z0-9-]*"),
            "ROTATION_HOSTS": ("controller",),
        }
    )
    placement, error = ns["_load_seat_placement"](path)
    assert error is None
    assert placement["codex"] == ("old-host",)
    assert placement["seraph"] == ("controller",)


def test_policy_has_no_model_table_and_disables_kimi():
    lanes = production_lanes(policy(), 100)
    assert all("model" not in lane for lane in lanes)
    assert next(lane for lane in lanes if lane["name"] == "deepseek")["capacity_domains"] == (
        "deepseek",
    )
    assert next(lane for lane in lanes if lane["name"] == "kimi")["target"] == 0


def test_production_spreads_work_without_breaking_provider_pins():
    ns = helpers("lane_compatibility", "select_compatible_lane", policy_value=policy())
    choose = ns["select_compatible_lane"]
    remaining = {"glm": 9, "deepseek": 10, "codex": 10}
    assert choose([], False, ["glm", "deepseek", "codex"], remaining, False)[0] == "deepseek"
    assert (
        choose(["codex-only"], False, ["glm", "deepseek", "codex"], remaining, False)[0] == "codex"
    )
    assert (
        choose(
            ["deepseek-only"],
            False,
            ["glm", "deepseek", "codex"],
            remaining,
            False,
            lane_health_by_name={"deepseek": (False, "unhealthy")},
        )[0]
        is None
    )


def test_builder_offers_are_bounded_and_do_not_stop_after_first_success():
    source = ROTATE.read_text()
    tree = ast.parse(source)
    block = next(
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "not DRY and _is_niobe_builder_host(HOST)"
    )
    offers = []

    class Builder:
        def offer(self, paths, core, labels, **kwargs):
            offers.append(core["id"])
            return {"node": "node", "request_id": core["id"]}

    ns = {
        "DRY": False,
        "HOST": "controller",
        "_is_niobe_builder_host": lambda host: True,
        "_builder_candidates": [(0, 0, str(i), {}, []) for i in range(5)],
        "MAX_CANDIDATE_SCAN": 3,
        "owned": [(0, 0, str(i), {}, []) for i in range(5)],
        "PRODUCTION_POLICY": policy(),
        "_cycle_started": 0,
        "_production_cycle_budget": 250,
        "time": type("Clock", (), {"monotonic": staticmethod(lambda: 1)}),
        "builder_dispatch": Builder(),
        "fleet_store": SimpleNamespace(Writer=lambda **kwargs: kwargs),
        "default_fleet_paths": lambda: None,
        "log": lambda *args: None,
        "d": None,
    }
    exec(compile(ast.Module(body=[block], type_ignores=[]), str(ROTATE), "exec"), ns)
    assert offers == ["0", "1", "2"]
    assert [row[2] for row in ns["owned"]] == ["3", "4"]


def test_serial_seat_budgets_reserve_cleanup_inside_generation():
    assert cycle_budget_seconds(policy(), "atlas") == 125
    assert cycle_budget_seconds(policy(), "seraph") == 125
    assert cycle_budget_seconds(policy(), "niobe") == 250
    assert (
        sum(cycle_budget_seconds(policy(), seat) + 25 for seat in ("atlas", "seraph", "niobe"))
        < 600
    )
    assert cycle_budget_seconds({**policy(), "cycle_budget_seconds": 60}, "atlas") == 60


def test_native_claim_custody_overrides_stale_raw_overlay_state():
    raw = {"owner": None, "status": "ready", "claim_revision": None, "labels": ["source-only"]}
    card = SimpleNamespace(
        id="deadbeef",
        owner="jarvis",
        status=SimpleNamespace(value="doing"),
        meta={"_claim_revision": "current-generation"},
        archived=False,
    )
    store = SimpleNamespace(fold=lambda card_id: card)
    state = authoritative_owner_state(store, "deadbeef", raw)
    assert state["owner"] == "jarvis"
    assert state["status"] == "doing"
    assert state["claim_revision"] == "current-generation"
    assert raw["owner"] is None


def gateway_route(model="catalog-version-one", provider="deepseek", size="M", **changes):
    return {
        "logical_route": model,
        "model_or_bucket": model,
        "provider": provider,
        "capacity_domain": provider,
        "size_class": size,
        "policy_tier": "paid-cloud",
        "state": "healthy",
        "max": 20,
        "gateway_active": 2,
        **changes,
    }


def test_actual_fresh_gateway_ids_replace_old_ids_without_policy_change():
    p = policy()
    first = resolve_production_routes([gateway_route()], policy=p, required_size="M", labels=[])
    next_rows = [gateway_route("catalog-version-two", gateway_model={"id": "catalog-version-two"})]
    second = resolve_production_routes(next_rows, policy=p, required_size="M", labels=[])
    assert first[0]["model_or_bucket"] == "catalog-version-one"
    assert second[0]["model_or_bucket"] == "catalog-version-two"
    assert second[0]["family"] == second[0]["lane"] == "deepseek"
    assert second[0]["free"] == 18
    second[0]["gateway_model"]["id"] = "poisoned"
    assert next_rows[0]["gateway_model"]["id"] == "catalog-version-two"
    assert all("model" not in row for row in p["lanes"].values())


@pytest.mark.parametrize(
    "change",
    [
        {"state": "unavailable"},
        {"size_class": "S"},
        {"size_class": "unknown"},
        {"max": 2},
        {"max": True},
        {"gateway_active": -1},
        {"max": "20"},
        {"provider": "skgateway"},
        {"capacity_domain": "zai"},
        {"logical_route": ""},
        {"model_or_bucket": None},
    ],
)
def test_unqualified_gateway_route_cannot_supply_card(change):
    assert (
        resolve_production_routes(
            [gateway_route(**change)], policy=policy(), required_size="M", labels=[]
        )
        == []
    )


def test_size_privacy_and_family_constraints_share_existing_gateway_eligibility():
    rows = [
        gateway_route(),
        gateway_route("local-l", "chiap08-qwen38", "L", policy_tier="local"),
        gateway_route("cloud-xl", "codex", "XL"),
    ]

    def resolve(**kw):
        return resolve_production_routes(rows, policy=policy(), **kw)

    assert [row["size_class"] for row in resolve(required_size="L", labels=[])] == ["L", "XL"]
    assert [row["lane"] for row in resolve(required_size="M", labels=["no-egress"])] == ["qwen"]
    assert resolve(required_size="XL", labels=["local-only"]) == []
    assert [row["lane"] for row in resolve(required_size="M", labels=["deepseek-only"])] == [
        "deepseek"
    ]
    assert resolve(required_size="M", labels=["deepseek-only", "codex-only"]) == []
    assert resolve(required_size="M", labels=["kimi-only"]) == []
    assert resolve(required_size="M", labels=["deepseek-only"], lane="qwen") == []
    assert resolve(required_size="unknown", labels=[]) == []


def test_explicit_exact_pin_preserves_bucket_and_does_not_create_a_route():
    row = gateway_route("served-id", "zai", logical_route="card-m-bucket")
    result = resolve_production_routes(
        [row], policy=policy(), required_size="M", labels=[], lane="glm", model_pin="served-id"
    )
    assert result[0]["logical_route"] == "card-m-bucket"
    assert result[0]["model_or_bucket"] == "served-id"
    assert result[0]["lane"] == "glm" and result[0]["family"] == "zai"
    assert (
        resolve_production_routes(
            [row], policy=policy(), required_size="M", labels=[], model_pin="absent-id"
        )
        == []
    )
    p = policy()
    p["lanes"]["glm"]["enabled"] = False
    assert resolve_production_routes([row], policy=p, required_size="M", labels=[]) == []
