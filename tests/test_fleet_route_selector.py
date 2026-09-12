"""Provider-neutral route plans for SKFleet worker briefs."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from skcapstone.fleet.route_selector import (
    RouteSelectionError,
    build_worker_route_plan,
    format_worker_route_brief,
    logical_route_candidates,
)


@pytest.mark.parametrize(
    ("size", "routes"),
    [
        ("S", ("sk-s", "sk-m", "sk-l")),
        ("M", ("sk-m", "sk-l")),
        ("L", ("sk-l",)),
        ("XL", ("sk-l",)),
    ],
)
def test_sizes_resolve_deterministically_without_provider_names(
    size: str, routes: tuple[str, ...]
) -> None:
    assert logical_route_candidates(size) == routes
    assert logical_route_candidates(size.lower()) == routes
    assert all(route in {"sk-s", "sk-m", "sk-l"} for route in routes)


def test_sovereign_corpus_requires_an_explicit_marker_and_uses_reviewed_route() -> None:
    assert logical_route_candidates("M", labels=["qwen-first"]) == ("sk-default",)
    assert logical_route_candidates("M", labels=["semantic-lane:sovereign-corpus"]) == (
        "sk-default",
    )
    with pytest.raises(RouteSelectionError, match="explicit semantic-lane marker"):
        logical_route_candidates("M", labels=["sovereign-corpus"])


@pytest.mark.parametrize("literal", ["glm-4.7", "gpt-5.6-sol", "qwen3.8-27b"])
def test_literal_model_names_are_refused_instead_of_guessed(literal: str) -> None:
    with pytest.raises(RouteSelectionError, match="literal model"):
        logical_route_candidates(literal)


@dataclass(frozen=True)
class _Preflight:
    served_identity: str
    provider: str = "gateway-provider"


def test_unavailable_primary_falls_forward_and_records_exact_reason() -> None:
    attempted: list[str] = []

    def preflight(route: str) -> _Preflight:
        attempted.append(route)
        if route == "sk-s":
            raise ValueError("no healthy member")
        return _Preflight(served_identity="served-by-gateway")

    plan = build_worker_route_plan("[CARD][S] Work", [], preflight=preflight)

    assert attempted == ["sk-s", "sk-m"]
    assert plan.route_candidates == ("sk-s", "sk-m", "sk-l")
    assert plan.selected_route == "sk-m"
    assert plan.fallback_reason == "sk-s unavailable: no healthy member"
    assert plan.served_model == "served-by-gateway"
    assert plan.served_provider == "gateway-provider"
    assert plan.attribution_phase == "preflight"


def test_all_unavailable_routes_fail_closed_with_the_attempt_order() -> None:
    attempted: list[str] = []

    def preflight(route: str) -> _Preflight:
        attempted.append(route)
        raise ValueError(f"{route} unavailable")

    with pytest.raises(RouteSelectionError, match="sk-s, sk-m, sk-l"):
        build_worker_route_plan("[CARD][S] Work", [], preflight=preflight)
    assert attempted == ["sk-s", "sk-m", "sk-l"]


def test_worker_brief_is_serialized_and_carries_every_attribution_field() -> None:
    plan = build_worker_route_plan(
        "[CARD][M] Work",
        [],
        preflight=lambda route: _Preflight(served_identity="exact-served-model"),
    )

    brief = format_worker_route_brief(plan)

    assert '"route_candidates":["sk-m","sk-l"]' in brief
    assert '"selected_route":"sk-m"' in brief
    assert '"fallback_reason":"primary route available"' in brief
    assert '"served_model":"exact-served-model"' in brief
    assert '"served_provider":"gateway-provider"' in brief
    assert '"attribution_phase":"preflight"' in brief


def test_unsized_or_ambiguous_cards_are_refused() -> None:
    with pytest.raises(RouteSelectionError, match="exactly one"):
        build_worker_route_plan("[CARD] Work", [])
    with pytest.raises(RouteSelectionError, match="exactly one"):
        build_worker_route_plan("[CARD][S][M] Work", [])
