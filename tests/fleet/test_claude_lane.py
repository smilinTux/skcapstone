"""The optional claude lane: validation shape, exact model ids, review, ceiling."""

import json

import pytest

from skcapstone.fleet import builder_dispatch
from skcapstone.fleet.production_dispatch import (
    production_lanes,
    resolve_production_routes,
    routes_for_lane,
)
from skcapstone.fleet.production_policy import LANE_WORKER_CEILINGS, load_production_policy
from skcapstone.fleet.production_review import (
    independent_review_routes,
    producer_family,
    review_family_allowed,
)

SONNET, OPUS = "claude-sonnet-5-5", "claude-opus-5-5"


def policy(**lanes):
    base = {
        name: {"enabled": True, "provider": "skgateway"}
        for name in ("codex", "glm", "deepseek", "qwen")
    }
    return {
        "schema": "skfleet.production/v1",
        "authority_host": "chiap08",
        "capacity_authority": "skgateway",
        "gateway_url": "http://chiap01:18790",
        "lanes": {**base, "kimi": {"enabled": False}, **lanes},
        "node_quotas": {
            "chiap03": {
                "cpu_quota_percent": 200,
                "memory_max_bytes": 2147483648,
                "tasks_max": 256,
                "runtime_max_seconds": 1800,
            }
        },
    }


def write(tmp_path, value):
    path = tmp_path / "production.json"
    path.write_text(json.dumps(value))
    return path


def test_policy_without_a_claude_row_still_validates(tmp_path):
    """The rollout trap: every live policy file predates the claude lane."""
    value = load_production_policy(write(tmp_path, policy()), host="chiap08")
    assert "claude" not in value["lanes"]
    assert all(lane["name"] != "claude" for lane in production_lanes(value, 5))


@pytest.mark.parametrize("enabled", [True, False])
def test_policy_with_a_claude_row_validates(tmp_path, enabled):
    row = {"enabled": enabled, "provider": "skgateway"}
    value = load_production_policy(write(tmp_path, policy(claude=row)), host="chiap08")
    lane = next(x for x in production_lanes(value, 5) if x["name"] == "claude")
    assert lane["target"] == (5 if enabled else 0)
    assert lane["capacity_domains"] == ("anthropic",)


@pytest.mark.parametrize(
    "row",
    [
        {"enabled": True},
        {"enabled": "yes", "provider": "skgateway"},
        {"enabled": True, "provider": "anthropic"},
    ],
)
def test_malformed_claude_row_is_refused(tmp_path, row):
    with pytest.raises(ValueError):
        load_production_policy(write(tmp_path, policy(claude=row)), host="chiap08")


def test_missing_required_lane_is_still_refused(tmp_path):
    value = policy(claude={"enabled": True, "provider": "skgateway"})
    del value["lanes"]["deepseek"]
    with pytest.raises(ValueError):
        load_production_policy(write(tmp_path, value), host="chiap08")


def row(model, size, active=0, maximum=4):
    return {
        "logical_route": model,
        "model_or_bucket": model,
        "provider": "anthropic",
        "capacity_domain": "anthropic",
        "size_class": size,
        "policy_tier": "paid-cloud",
        "state": "healthy",
        "max": maximum,
        "gateway_active": active,
    }


ROUTES = [row(SONNET, "L"), row(OPUS, "XL")]
ENABLED = policy(claude={"enabled": True, "provider": "skgateway"})


@pytest.mark.parametrize("size,model", [("S", SONNET), ("M", SONNET), ("L", OPUS), ("XL", OPUS)])
def test_size_selects_the_exact_model_id(size, model):
    found = resolve_production_routes(
        ROUTES, policy=ENABLED, required_size=size, labels=[], lane="claude"
    )
    assert [r["model_or_bucket"] for r in found] == [model]
    assert found[0]["lane"] == "claude"


def test_claude_off_when_the_row_is_absent_or_disabled():
    for value in (policy(), policy(claude={"enabled": False, "provider": "skgateway"})):
        assert (
            resolve_production_routes(
                ROUTES, policy=value, required_size="M", labels=[], lane="claude"
            )
            == []
        )


def test_claude_only_label_pins_the_family_and_full_slots_withhold():
    both = ROUTES + [row("sk-zai-m", "M") | {"provider": "zai", "capacity_domain": "zai"}]
    found = resolve_production_routes(
        both, policy=ENABLED, required_size="M", labels=["claude-only"]
    )
    assert {r["family"] for r in found} == {"claude"}
    full = [row(SONNET, "L", active=4), row(OPUS, "XL", active=4)]
    assert (
        resolve_production_routes(full, policy=ENABLED, required_size="M", labels=["claude-only"])
        == []
    )


def test_routes_for_lane_binds_the_anthropic_domain():
    lane = next(x for x in production_lanes(ENABLED, 3) if x["name"] == "claude")
    assert [r["model_or_bucket"] for r in routes_for_lane(ROUTES, lane)] == [SONNET, OPUS]


def test_claude_producer_identity_resolves_and_reviewer_must_differ():
    assert producer_family("pi-claude-builder-node-chiap03-1234abcd") == "claude"
    glm = row("sk-zai-m", "M") | {"provider": "zai", "capacity_domain": "zai"}
    routes = ROUTES + [glm]
    kwargs = dict(policy=ENABLED, producer_identity="pi-claude-builder-node-chiap03-1234abcd")
    assert independent_review_routes(
        routes, reviewer_identity="pi-seraph-chiap08-feedbeef", **kwargs
    )
    assert (
        independent_review_routes(
            routes, reviewer_identity="pi-claude-builder-node-chiap03-1234abcd", **kwargs
        )
        == []
    )
    assert review_family_allowed("claude", "zai", [], policy=ENABLED)
    assert not review_family_allowed("zai", "codex", ["claude-only"], policy=ENABLED)
    assert review_family_allowed("zai", "claude", ["claude-only"], policy=ENABLED)


def test_ceiling_is_four_and_a_full_lane_is_excluded_from_offers():
    assert LANE_WORKER_CEILINGS == {"claude": 4}
    assert builder_dispatch.capped_families({"claude": 3}) == set()
    assert builder_dispatch.capped_families({"claude": 4}) == {"claude"}
    assert builder_dispatch.capped_families({"glm": 40}) == set()
