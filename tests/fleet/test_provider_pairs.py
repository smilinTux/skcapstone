"""Strict deterministic provider-pair selection tests."""

from __future__ import annotations

import copy

import pytest

from skcapstone.fleet.provider_pairs import select_provider_pair, validate_provider_pair


def snapshot(*families):
    """Return one exact healthy focused route per provider family."""
    return {
        "schema": "skgateway.route-health/v1",
        "revision": "a" * 64,
        "routes": [
            {
                "logical_route": f"sk-{family}-m",
                "provider_family": family,
                "work_class": "m",
                "focused": True,
                "qualified": True,
                "healthy": True,
            }
            for family in families
        ],
    }


def policy(counter=0, families=("deepseek", "glm"), explicit=None):
    """Return the exact bounded pairing policy."""
    return {
        "rotation_counter": counter,
        "automatic_families": list(families),
        "explicit_parent_route": explicit,
    }


def test_healthy_families_rotate_producer_and_cross_review():
    health = snapshot("glm", "deepseek")
    first = select_provider_pair("m", health, policy(0))
    second = select_provider_pair("m", health, policy(1))
    assert (first["producer"]["provider_family"], first["reviewer"]["provider_family"]) == (
        "deepseek",
        "glm",
    )
    assert (second["producer"]["provider_family"], second["reviewer"]["provider_family"]) == (
        "glm",
        "deepseek",
    )
    assert validate_provider_pair(first) == first


def test_explicit_codex_parent_is_retained_and_automatic_codex_is_excluded():
    health = snapshot("codex", "deepseek", "glm")
    automatic = select_provider_pair("m", health, policy(2, ("codex", "deepseek", "glm")))
    assert automatic["producer"]["provider_family"] == "deepseek"
    assert automatic["reviewer"]["provider_family"] == "glm"
    explicit = select_provider_pair(
        "m",
        health,
        policy(0, ("deepseek", "glm"), "sk-codex-m"),
    )
    assert explicit["producer"] == {
        "provider_family": "codex",
        "logical_route": "sk-codex-m",
    }
    assert explicit["reviewer"]["provider_family"] == "deepseek"


def test_one_family_reports_independent_review_unavailable():
    result = select_provider_pair("m", snapshot("deepseek"), policy(families=("deepseek",)))
    assert result["state"] == "independent_review_unavailable"
    assert result["producer"]["provider_family"] == "deepseek"
    assert result["reviewer"] is None


def test_same_family_cannot_validate_as_independent_review():
    result = select_provider_pair("m", snapshot("deepseek", "glm"), policy())
    result["reviewer"] = copy.deepcopy(result["producer"])
    with pytest.raises(ValueError, match="different provider families"):
        validate_provider_pair(result)


def test_pair_validation_rejects_mixed_work_classes():
    result = select_provider_pair("m", snapshot("deepseek", "glm"), policy())
    result["reviewer"]["logical_route"] = "sk-glm-s"
    with pytest.raises(ValueError, match="work classes"):
        validate_provider_pair(result)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra=True),
        lambda value: value.update(schema="wrong"),
        lambda value: value.update(revision="A" * 64),
        lambda value: value["routes"][0].update(extra=True),
        lambda value: value["routes"][0].update(logical_route="sk-deepseek-xl"),
        lambda value: value["routes"][0].update(logical_route="sk-DeepSeek-m"),
        lambda value: value["routes"][0].update(provider_family="DeepSeek"),
        lambda value: value["routes"][0].update(focused=1),
        lambda value: value["routes"].append(copy.deepcopy(value["routes"][0])),
    ],
)
def test_snapshot_fails_closed_on_noncanonical_or_duplicate_input(mutation):
    health = snapshot("deepseek", "glm")
    mutation(health)
    with pytest.raises(ValueError):
        select_provider_pair("m", health, policy())


def test_duplicate_family_with_a_different_route_is_rejected():
    health = snapshot("deepseek", "glm")
    duplicate = copy.deepcopy(health["routes"][0])
    duplicate.update(logical_route="sk-deepseek-s", work_class="s")
    health["routes"].append(duplicate)
    with pytest.raises(ValueError, match="duplicate provider family"):
        select_provider_pair("m", health, policy())


@pytest.mark.parametrize(
    "work_class,change",
    [
        ("xl", None),
        ("m", ("rotation_counter", True)),
        ("m", ("rotation_counter", 2**53)),
        ("m", ("automatic_families", ["deepseek", "deepseek"])),
        ("m", ("automatic_families", ["DeepSeek"])),
        ("m", ("explicit_parent_route", "sk-codex-xl")),
    ],
)
def test_policy_and_work_class_are_strict(work_class, change):
    pairing = policy()
    if change:
        pairing[change[0]] = change[1]
    with pytest.raises(ValueError):
        select_provider_pair(work_class, snapshot("deepseek", "glm"), pairing)


def test_nonfocused_and_unhealthy_routes_are_not_automatic_providers():
    health = snapshot("deepseek", "glm")
    health["routes"][0]["focused"] = False
    result = select_provider_pair("m", health, policy())
    assert result["state"] == "independent_review_unavailable"
    assert result["producer"]["provider_family"] == "glm"


def test_explicit_route_must_be_exact_healthy_qualified_focused_route():
    health = snapshot("codex", "deepseek")
    health["routes"][0]["healthy"] = False
    with pytest.raises(ValueError, match="explicit parent route"):
        select_provider_pair("m", health, policy(explicit="sk-codex-m", families=("deepseek",)))
