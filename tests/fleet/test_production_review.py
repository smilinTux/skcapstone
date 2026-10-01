"""Exact source-family and qualified route bindings for independent review."""

import copy

import pytest

from skcapstone.fleet.production_review import independent_review_routes, producer_family


def policy():
    """Return the four configured transport routes with disabled Kimi."""
    return {
        "lanes": {
            **{
                lane: {"enabled": True, "provider": "skgateway", "model": model}
                for lane, model in {
                    "codex": "gpt-5.6-sol",
                    "glm": "sk-zai-m",
                    "deepseek": "deepseek-flash",
                    "qwen": "qwen-model",
                }.items()
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


def test_two_healthy_families_never_admit_producer_family():
    routes = [route("gpt-5.6-sol", "codex"), route("sk-zai-m", "zai")]
    assert independent_review_routes(
        routes, policy=policy(), producer_identity="pi-codex-chiap03-deadbeef"
    ) == [routes[1]]
    assert (
        independent_review_routes(
            routes[:1], policy=policy(), producer_identity="pi-codex-chiap03-deadbeef"
        )
        == []
    )


def test_qwen_replica_change_is_not_provider_independence():
    routes = [
        route("qwen-model", "chiap01-qwen38"),
        route("qwen-model", "chiap08-qwen38"),
        route("deepseek-flash", "deepseek"),
    ]
    result = independent_review_routes(
        routes, policy=policy(), producer_identity="pi-qwen-builder-chiap08-1234abcd"
    )
    assert result == [routes[2]]


@pytest.mark.parametrize(
    "change",
    [
        {"model_or_bucket": "substituted"},
        {"logical_route": "pool-alias"},
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


def test_ambiguous_configured_model_is_not_accepted():
    p = policy()
    p["lanes"]["glm"]["model"] = "deepseek-flash"
    assert (
        independent_review_routes(
            [route("deepseek-flash", "deepseek")],
            policy=p,
            producer_identity="pi-codex-chiap08-id",
        )
        == []
    )


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
