"""Qualified review route bindings: any enabled family, a distinct reviewer."""

import copy

import pytest

from skcapstone.fleet.production_review import independent_review_routes, producer_family


def policy():
    """Return the four configured transport routes with disabled Kimi."""
    return {
        "lanes": {
            **{
                lane: {"enabled": True, "provider": "skgateway"}
                for lane in ("codex", "glm", "deepseek", "qwen")
            },
            "kimi": {"enabled": False},
        }
    }


def route(model, provider, domain=None):
    """Build one already qualified route row as native capacity returns it."""
    return {
        "logical_route": model,
        "model_or_bucket": model,
        "provider": provider,
        "capacity_domain": domain or provider,
        "free": 3,
        "state": "healthy",
    }


@pytest.mark.parametrize(
    "identity,provider,family",
    [
        ("pi-codex-chiap08-1234abcd", None, "codex"),
        ("pi-glm-builder-chiap03-1234abcd", None, "zai"),
        ("pi-deepseek-builder-ziowk01-1234abcd", None, "deepseek"),
        ("pi-qwen-chiap08-1234abcd", "chiap01-qwen38", "qwen"),
        ("pi-escalate-chiap08-1234abcd", "codex", "codex"),
        ("codex-production-review-746626c9", "openai", "codex"),
        ("opaque-author", "zai", "zai"),
    ],
)
def test_source_family_from_actual_identity_or_typed_provider(identity, provider, family):
    assert producer_family(identity, provider) == family


@pytest.mark.parametrize(
    "identity,provider",
    [
        ("pi-seraph-chiap08-1234abcd", None),
        ("jarvis", None),
        ("pretend-pi-codex-chiap08-id", None),
        ("pi-codex", None),
        ("pi-codex-chiap08-id", "zai"),
        ("opaque", "local"),
        ("opaque", "skgateway"),
        ("pi-codex-chiap08-id", "unknown"),
    ],
)
def test_ambiguous_or_conflicting_producer_refuses(identity, provider):
    with pytest.raises(ValueError):
        producer_family(identity, provider)


def test_any_enabled_family_reviews_for_a_distinct_reviewer():
    routes = [route("gpt-5.6-sol", "codex"), route("sk-zai-m", "zai")]
    assert (
        independent_review_routes(
            routes,
            policy=policy(),
            producer_identity="pi-codex-chiap03-deadbeef",
            reviewer_identity="pi-seraph-chiap08-1234abcd",
        )
        == routes
    )


def test_same_reviewer_principal_gets_no_route():
    routes = [route("gpt-5.6-sol", "codex"), route("sk-zai-m", "zai")]
    assert (
        independent_review_routes(
            routes,
            policy=policy(),
            producer_identity="pi-codex-chiap03-deadbeef",
            reviewer_identity="pi-codex-chiap03-deadbeef",
        )
        == []
    )


def test_glm_only_pin_admits_only_glm_routes():
    routes = [route("deepseek-flash", "deepseek"), route("sk-zai-m", "zai")]
    assert independent_review_routes(
        routes,
        policy=policy(),
        producer_identity="pi-deepseek-builder-chiap02-1234abcd",
        reviewer_identity="pi-seraph-chiap08-1234abcd",
        labels=["review", "glm-only"],
    ) == [routes[1]]


def test_explicit_glm_distinct_agent_policy_admits_glm_for_untyped_source():
    routes = [route("sk-zai-m", "zai"), route("gpt-5.6-sol", "codex")]
    assert independent_review_routes(
        routes,
        policy=policy(),
        producer_identity="jarvis",
        reviewer_identity="pi-seraph-chiap08-1234abcd",
        labels=["review", "glm-only", "review-distinct-agent"],
    ) == [routes[0]]


def test_explicit_glm_distinct_agent_policy_rejects_same_principal():
    routes = [route("sk-zai-m", "zai")]
    assert (
        independent_review_routes(
            routes,
            policy=policy(),
            producer_identity="pi-glm-builder-chiap08-1234abcd",
            reviewer_identity="pi-glm-builder-chiap08-1234abcd",
            labels=["review", "glm-only", "review-distinct-agent"],
        )
        == []
    )


def test_explicit_glm_distinct_agent_policy_admits_known_glm_source():
    routes = [route("sk-zai-m", "zai")]
    assert (
        independent_review_routes(
            routes,
            policy=policy(),
            producer_identity="pi-glm-builder-chiap08-1234abcd",
            reviewer_identity="pi-seraph-chiap08-1234abcd",
            labels=["review", "glm-only", "review-distinct-agent"],
        )
        == routes
    )


def test_qwen_replicas_and_other_families_all_review():
    routes = [
        route("qwen-model", "chiap01-qwen38"),
        route("qwen-model", "chiap08-qwen38"),
        route("deepseek-flash", "deepseek"),
    ]
    result = independent_review_routes(
        routes, policy=policy(), producer_identity="pi-qwen-builder-chiap08-1234abcd"
    )
    assert result == routes


@pytest.mark.parametrize(
    "change",
    [
        {"model_or_bucket": ""},
        {"logical_route": None},
        {"capacity_domain": "codex"},
        {"provider": "codex"},
        {"provider": "skgateway"},
        {"capacity_domain": "unknown"},
    ],
)
def test_exact_model_provider_and_domain_must_agree(change):
    candidate = route("deepseek-flash", "deepseek") | change
    assert (
        independent_review_routes(
            [candidate], policy=policy(), producer_identity="pi-codex-chiap08-id"
        )
        == []
    )


def test_disabled_family_is_not_accepted():
    p = policy()
    p["lanes"]["deepseek"]["enabled"] = False
    assert (
        independent_review_routes(
            [route("deepseek-flash", "deepseek")],
            policy=p,
            producer_identity="pi-codex-chiap08-id",
        )
        == []
    )


def test_new_gateway_model_and_logical_bucket_preserve_independence():
    current = route("future-qualified-deepseek", "deepseek") | {"logical_route": "card-m-bucket"}
    assert independent_review_routes(
        [current], policy=policy(), producer_identity="pi-codex-author"
    ) == [current]


def test_result_does_not_mutate_or_alias_input_evidence():
    rows = [route("deepseek-flash", "deepseek") | {"proof": {"digest": "original"}}]
    original = copy.deepcopy(rows)
    result = independent_review_routes(
        rows, policy=policy(), producer_identity="opaque", producer_provider="zai"
    )
    result[0]["proof"]["digest"] = "caller mutation"
    assert rows == original


def test_no_unknown_source_fallback_even_with_healthy_routes():
    with pytest.raises(ValueError, match="authoritative source evidence"):
        independent_review_routes(
            [route("deepseek-flash", "deepseek")], policy=policy(), producer_identity="jarvis"
        )
