"""Qualified review route bindings: any enabled family, a distinct reviewer."""

import copy

import pytest

from skcapstone.fleet.production_review import (
    independent_review_routes,
    producer_family,
    review_family_allowed,
)


def policy():
    """Return the four configured transport routes with disabled Kimi."""
    return {
        "lanes": {
            **{
                lane: {"enabled": True, "provider": "skgateway"}
                for lane in ("codex", "glm", "deepseek", "qwen")
            },
            "kimi": {"enabled": False, "provider": "skgateway"},
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
        ("jarvis", None, "codex"),
        ("codex-production-review-746626c9", "openai", "codex"),
        ("opaque-author", "zai", "zai"),
        ("opaque-author", "skgateway", "gateway"),
        ("pi-gateway-builder-chiap02-1234abcd", None, "gateway"),
    ],
)
def test_source_family_from_actual_identity_or_typed_provider(identity, provider, family):
    assert producer_family(identity, provider) == family


@pytest.mark.parametrize(
    "identity,provider",
    [
        ("pi-seraph-chiap08-1234abcd", None),
        ("pretend-pi-codex-chiap08-id", None),
        ("pi-codex", None),
        ("pi-codex-chiap08-id", "zai"),
        ("opaque", "local"),
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


@pytest.mark.parametrize(
    "label,family,lane",
    [
        ("glm-only", "zai", "glm"),
        ("deepseek-only", "deepseek", "deepseek"),
        ("qwen-only", "qwen", "qwen"),
        ("kimi-only", "kimi", "kimi"),
    ],
)
def test_disabled_review_family_pin_falls_back_and_rebinds(label, family, lane):
    p = policy()
    p["lanes"][lane]["enabled"] = False
    routes = [route(name, name) for name in ("codex", "zai", "deepseek", "qwen", "kimi")]
    routes = [row | {"capacity_domain": row["provider"]} for row in routes]
    kwargs = {
        "policy": p,
        "producer_identity": "pi-codex-builder-chiap08-1234abcd",
        "reviewer_identity": "pi-seraph-chiap04-5678abcd",
        "labels": ["review", label],
    }

    fallback = independent_review_routes(routes, **kwargs)
    assert all(row["provider"] != family for row in fallback)
    assert fallback

    p["lanes"][lane]["enabled"] = True
    rebound = independent_review_routes(routes, **kwargs)
    assert rebound == [next(row for row in routes if row["provider"] == family)]


def test_disabled_glm_pin_falls_back_in_family_check_and_rebinds():
    p = policy()
    p["lanes"]["glm"]["enabled"] = False
    assert review_family_allowed("codex", "codex", ["glm-only"], policy=p)
    p["lanes"]["glm"]["enabled"] = True
    assert not review_family_allowed("codex", "codex", ["glm-only"], policy=p)
    assert review_family_allowed("codex", "zai", ["glm-only"], policy=p)


def test_explicit_glm_distinct_agent_policy_admits_glm_for_untyped_source():
    routes = [route("sk-zai-m", "zai"), route("gpt-5.6-sol", "codex")]
    assert independent_review_routes(
        routes,
        policy=policy(),
        producer_identity="jarvis",
        reviewer_identity="pi-seraph-chiap08-1234abcd",
        labels=["review", "glm-only", "review-distinct-agent"],
    ) == [routes[0]]


def test_unknown_producer_glm_fallback_relaxes_when_glm_is_disabled():
    routes = [route("sk-zai-m", "zai"), route("gpt-5.6-sol", "codex")]
    p = policy()
    p["lanes"]["glm"]["enabled"] = False
    result = independent_review_routes(
        routes,
        policy=p,
        producer_identity="unmapped-producer",
        reviewer_identity="pi-seraph-chiap08-1234abcd",
        labels=["review", "glm-only", "review-distinct-agent"],
    )
    assert result == [routes[1]]


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
            [route("deepseek-flash", "deepseek")],
            policy=policy(),
            producer_identity="unmapped-producer",
        )


def test_jarvis_maps_to_codex_and_preserves_distinct_principal_rule():
    routes = [route("deepseek-flash", "deepseek"), route("sk-zai-m", "zai")]
    kwargs = dict(
        policy=policy(),
        producer_identity="jarvis",
        reviewer_identity="pi-seraph-chiap08-1234abcd",
        labels=["review"],
    )
    assert independent_review_routes(routes, **kwargs) == routes
    assert independent_review_routes(routes, **(kwargs | {"reviewer_identity": "jarvis"})) == []
    assert independent_review_routes(
        routes, **(kwargs | {"labels": kwargs["labels"] + ["glm-only"]})
    ) == [routes[1]]


def test_verified_legacy_hosted_review_keeps_provider_unknown_and_distinct():
    routes = [route("deepseek-flash", "deepseek"), route("sk-zai-m", "zai")]
    kwargs = dict(
        policy=policy(),
        producer_identity="legacy-producer",
        reviewer_identity="pi-seraph-chiap08-1234abcd",
        labels=["review", "review-distinct-agent"],
        legacy_producer_verified=True,
    )
    assert independent_review_routes(routes, **kwargs) == routes
    with pytest.raises(ValueError):
        independent_review_routes(routes, **(kwargs | {"reviewer_identity": "legacy-producer"}))
    assert independent_review_routes(
        routes, **(kwargs | {"labels": kwargs["labels"] + ["glm-only"]})
    ) == [routes[1]]
    with pytest.raises(ValueError):
        producer_family("unmapped-producer")
    with pytest.raises(ValueError):
        independent_review_routes(routes, **(kwargs | {"producer_provider": "unknown"}))
    with pytest.raises(ValueError):
        independent_review_routes(
            routes, **(kwargs | {"labels": kwargs["labels"] + ["source-only"]})
        )
