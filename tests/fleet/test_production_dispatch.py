"""Native production routing and actual worker resource boundary tests."""

import ast
import json
import time
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
from skcapstone.fleet.review_capacity import (
    _review_capacity_truth_is_current,
    aggregate_review_capacity,
    review_physical_free,
    seal_review_capacity_truth,
)

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
    policy_value = policy() | {"lane_runtime_max_seconds": {"glm": 5400}}
    ns = helpers("_worker_launch_command", policy_value=policy_value)
    ns["os"] = __import__("os")
    launch = ns["_worker_launch_command"]
    argv = ["python", "wrapper.py", "--", "bash", "-lc", "printf '%s' '$HOME'"]
    command = launch("skfleet-worker-glm-24b00001.service", "/workspace", argv)
    assert command[-len(argv) :] == argv
    assert "--setenv=SKFLEET_PRODUCTION_POLICY=/private/production.json" in command
    assert "--setenv=SKFLEET_AUTHORITY_HOST=controller" in command
    for expected in (
        "CPUQuota=200%",
        "MemoryMax=3221225472",
        "TasksMax=256",
        "RuntimeMaxSec=5400",
    ):
        assert "--property=" + expected in command


def test_lane_runtime_override_does_not_extend_other_lanes():
    policy_value = policy() | {"lane_runtime_max_seconds": {"glm": 5400}}
    glm = worker_resource_properties(policy_value, "controller", "glm")
    codex = worker_resource_properties(policy_value, "controller", "codex")
    assert "--property=RuntimeMaxSec=5400" in glm
    assert "--property=RuntimeMaxSec=3600" in codex


def test_production_review_rails_preserve_candidate_and_truthful_checks():
    ns = helpers("_production_worker_rails")
    ns.update(
        os=SimpleNamespace(environ={}),
        _worker_search_instructions=lambda: "BOUNDED SEARCH\n",
        _worker_mail_instructions=lambda routing: "MAIL ROUTING\n",
        _worker_mail_routing=lambda env, originator: originator,
    )
    tree = ast.parse(ROTATE.read_text())
    assignment = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "PRODUCTION_POLICY"
        and any(
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Name)
            and item.func.id == "_production_worker_rails"
            for item in ast.walk(node)
        )
    )
    ns.update(PRODUCTION_POLICY=policy(), core={}, _RAILS="LEGACY PUSH REQUIREMENT")
    exec(compile(ast.Module(body=[assignment], type_ignores=[]), str(ROTATE), "exec"), ns)
    rails = ns["_RAILS"]
    assert "LEGACY PUSH REQUIREMENT" not in rails
    assert "Review requires no new source commit" in rails
    assert "alter the candidate being reviewed" in rails
    assert "never fabricate CI SUCCESS" in rails
    assert "second auto-compaction" in rails
    assert "BOUNDED SEARCH" in rails
    assert "MAIL ROUTING" in rails
    ns.update(PRODUCTION_POLICY=None, _RAILS="LEGACY PUSH REQUIREMENT")
    exec(compile(ast.Module(body=[assignment], type_ignores=[]), str(ROTATE), "exec"), ns)
    assert ns["_RAILS"] == "LEGACY PUSH REQUIREMENT"


def test_production_skips_legacy_catalog_writer_and_readiness_gate():
    tree = ast.parse(ROTATE.read_text())

    def branch_calling(name):
        return next(
            node
            for node in tree.body
            if isinstance(node, ast.If)
            and any(
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name)
                and item.func.id == name
                for item in ast.walk(node)
            )
        )

    calls = []
    ns = {
        "PRODUCTION_POLICY": policy(),
        "DRY": False,
        "GLM_TARGET": 12,
        "glm_held": False,
        "glm_catalog_ready": False,
        "_prepare_pi_glm_catalog": lambda: (calls.append("legacy") or False, "missing"),
        "production_lanes": production_lanes,
        "_SCAN_BUDGET": 12,
        "log": lambda *args: None,
        "d": None,
        "HOST": "controller",
    }
    legacy = branch_calling("_prepare_pi_glm_catalog")
    production = branch_calling("production_lanes")
    exec(compile(ast.Module(body=[legacy, production], type_ignores=[]), str(ROTATE), "exec"), ns)
    assert calls == []
    assert next(row for row in ns["LANES"] if row["name"] == "glm")["target"] == 12
    ns["PRODUCTION_POLICY"] = None
    exec(compile(ast.Module(body=[legacy], type_ignores=[]), str(ROTATE), "exec"), ns)
    assert calls == ["legacy"]
    assert ns["glm_catalog_ready"] is False


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


@pytest.mark.parametrize("invalid", [None, "stale", "tampered", "ambiguous"])
def test_actual_producer_selection_requires_fresh_sealed_gateway_facts(invalid):
    ns = helpers("_production_card_routes", policy_value=policy())
    row = gateway_route()
    snapshot = seal_review_capacity_truth(
        {"schema_version": 1, "observed_at": time.time(), "routes": [row]},
        occupancy={},
        occupancy_ambiguous=False,
        physical_maximum=None,
    )
    if invalid == "stale":
        snapshot = seal_review_capacity_truth(
            {**snapshot, "observed_at": time.time() - 121},
            occupancy={},
            occupancy_ambiguous=False,
            physical_maximum=None,
        )
    if invalid == "tampered":
        snapshot["routes"][0]["model_or_bucket"] = "unqualified-replacement"
    ns.update(
        {
            "time": time,
            "ROUTE_MAX_AGE_SECONDS": 120,
            "_review_capacity_truth_is_current": _review_capacity_truth_is_current,
            "_review_route_snapshot": snapshot,
            "_review_route_ambiguous": invalid == "ambiguous",
            "_producer_routes_for": lambda *args: snapshot["routes"],
            "_size_class_for": lambda *args: "M",
            "resolve_production_routes": resolve_production_routes,
        }
    )
    result = ns["_production_card_routes"]({}, ["deepseek-only"], "deepseek")
    assert bool(result) is (invalid is None)


def test_actual_review_capacity_gate_admits_any_family_for_a_distinct_reviewer():
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
    assert result["available"] == 50
    assert result["routes"] == rows
    same = ns["evaluate_review_capacity"](
        {}, "M", [], "pi-codex-node-deadbeef", "pi-codex-node-deadbeef"
    )
    assert same["available"] == 0
    assert same["reason"] == "independent-provider-unavailable"
    unknown = ns["evaluate_review_capacity"]({}, "M", [], "unknown-source", "reviewer")
    assert unknown["available"] == 0
    assert unknown["reason"] == "source-provider-evidence-required"


def test_actual_review_capacity_gate_passes_explicit_glm_distinct_agent_policy():
    ns = helpers("evaluate_review_capacity", policy_value=policy())
    glm = {
        "logical_route": "sk-zai-m",
        "model_or_bucket": "sk-zai-m",
        "capacity_domain": "zai",
        "provider": "zai",
        "free": 3,
    }
    ns.update(
        {
            "_evaluate_review_capacity": lambda *args, **kwargs: {
                "reason": "eligible",
                "routes": [glm],
                "available": 3,
            },
            "aggregate_review_capacity": aggregate_review_capacity,
            "independent_review_routes": independent_review_routes,
        }
    )
    result = ns["evaluate_review_capacity"](
        {},
        "M",
        ["review", "glm-only", "review-distinct-agent"],
        "jarvis",
        "pi-seraph-chiap08-1234abcd",
    )
    assert result["reason"] == "eligible"
    assert result["routes"] == [glm]


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
        "_record_offer_state": lambda *args: None,
        "_BUILDER_OFFER_SECONDS": 45.0,
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


def test_builder_offer_phase_stops_at_its_own_cap():
    """The builder phase must not consume the whole cycle (exit 70 at the 270s limit)."""
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
        "MAX_CANDIDATE_SCAN": 5,
        "owned": [],
        "PRODUCTION_POLICY": policy(),
        "_cycle_started": 0,
        "_production_cycle_budget": 250,
        "time": type("Clock", (), {"monotonic": staticmethod(lambda: 1)}),
        "builder_dispatch": Builder(),
        "fleet_store": SimpleNamespace(Writer=lambda **kwargs: kwargs),
        "default_fleet_paths": lambda: None,
        "log": lambda *args: None,
        "d": None,
        "_record_offer_state": lambda *args: None,
        "_BUILDER_OFFER_SECONDS": 0.0,
    }
    exec(compile(ast.Module(body=[block], type_ignores=[]), str(ROTATE), "exec"), ns)
    assert offers == []
