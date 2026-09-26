"""Stale parent claims block real local and remote owner-helper admission."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.coord_helpers import build_helper
from skcapstone.fleet import builder_dispatch
from skcapstone.fleet.crew_admission import validate_helper_generation


@pytest.fixture
def helper(tmp_path):
    """Create an exact queued helper without modifying any live card."""
    cards = CardStore(tmp_path)
    source = {
        "repository": "https://example.test/project.git",
        "base_ref": "main",
        "base_revision": "a" * 40,
    }
    cards.create(
        CardCore(
            id="aaaa1111",
            title="[M] Parent",
            initial_owner="test-owner",
            initial_claim_revision="parent-1",
            initial_labels=["source-only", "sk-m"],
            acceptance_criteria=["Integrate complete output"],
            meta=source,
        )
    )
    packet = {
        "request_id": "verify",
        "title": "[S] Check seam",
        "objective": "Verify seam",
        "criteria": ["Deliver exact output"],
        "allowed_paths": [],
        "verification_commands": ["pytest -q"],
        "logical_route": "sk-m",
    }
    task = build_helper(cards.fold("aaaa1111"), packet, "test-owner", "parent-1")
    cards.create(
        CardCore(
            id=task.id,
            title=task.title,
            description=task.description,
            acceptance_criteria=task.acceptance_criteria,
            initial_labels=task.tags,
            dependencies=task.dependencies,
            meta=task.meta,
        )
    )
    return tmp_path, cards.fold(task.id)


def request(card):
    """Return the exact source request consumed by the existing builder path."""
    return {
        "card_id": card.id,
        "repository": card.meta["repository"],
        "base_ref": card.meta["base_ref"],
        "base_revision": card.meta["base_revision"],
        "labels": sorted(card.labels),
        "logical_route": card.meta["logical_route"],
    }


def test_current_helper_passes_and_ordinary_cards_remain_unchanged(helper):
    home, card = helper
    validate_helper_generation(home, card)
    builder_dispatch._request_matches_current_card(home, request(card))
    validate_helper_generation(home, CardStore(home).fold("aaaa1111"))


def test_focused_route_replaces_parent_route_and_preserves_restrictions(helper):
    home, card = helper
    parent = CardStore(home).fold("aaaa1111")
    parent.labels.extend(["no-external-action", "parent-legacy", " SK-L "])
    card.labels = [
        "source-only",
        "no-external-action",
        "parent-legacy",
        "owner-helper",
        "parent-aaaa1111",
        "sk-deepseek-m",
    ]
    card.meta["logical_route"] = "sk-deepseek-m"
    original = CardStore.fold

    def changed(store, card_id):
        return parent if card_id == "aaaa1111" else original(store, card_id)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(CardStore, "fold", changed)
        validate_helper_generation(home, card)


@pytest.mark.parametrize("change", ["missing", "extra", "mismatch", "spelling", "type"])
def test_routed_helper_rejects_missing_extra_ambiguous_or_mismatched_route(helper, change):
    home, card = helper
    if change == "missing":
        card.labels.remove("sk-m")
    elif change == "extra":
        card.labels.append("sk-glm-m")
    elif change == "mismatch":
        card.meta["logical_route"] = "sk-glm-m"
    elif change == "spelling":
        card.labels[card.labels.index("sk-m")] = "SK-M"
    else:
        card.meta["logical_route"] = 17
    with pytest.raises(ValueError, match="route"):
        validate_helper_generation(home, card)


@pytest.mark.parametrize(
    "change", ["claim", "contract", "source", "restrictions", "deps", "ownerless", "terminal"]
)
def test_remote_preclaim_refuses_stale_parent(helper, monkeypatch, change):
    home, card = helper
    original = CardStore.fold

    def changed(store, card_id):
        result = original(store, card_id)
        if card_id == "aaaa1111":
            result = result.model_copy(deep=True)
            if change == "claim":
                result.meta["_claim_revision"] = "replacement"
            elif change == "contract":
                result.description = "Changed contract"
            elif change == "source":
                result.meta["base_revision"] = "b" * 40
            elif change == "restrictions":
                result.labels = [*result.labels, "no-new-scope"]
            elif change == "deps":
                result.dependencies = ["cccc3333"]
            elif change == "ownerless":
                result.owner = None
            else:
                result.archived = True
        return result

    monkeypatch.setattr(CardStore, "fold", changed)
    with pytest.raises(builder_dispatch.BuilderDispatchError, match="owner-helper"):
        builder_dispatch._request_matches_current_card(home, request(card))


@pytest.mark.parametrize("stale", [False, True])
def test_actual_rotation_authoritative_admission_uses_parent_guard(helper, monkeypatch, stale):
    home, card = helper
    script = Path(__file__).parents[2] / "scripts/fleet/skfleet-rotate.py"
    tree = ast.parse(script.read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "authoritative_claimability"
    )
    state = {
        "title": card.title,
        "description": card.description,
        "acceptance_criteria": card.acceptance_criteria,
        "links": {},
        "labels": card.labels,
        "legacy_owners": [],
        "owner": None,
    }
    namespace = {
        "Path": Path,
        "CARDS": str(home / "cards"),
        "CardStore": CardStore,
        "_authoritative_card_snapshot": lambda *args, **kwargs: (
            {"id": card.id, "meta": card.meta},
            dict(state),
            "fixture-generation",
        ),
        "_claimability_reason": lambda *args: "claimable",
        "host_pin": lambda *args: None,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(script), "exec"), namespace)
    if stale:
        original = CardStore.fold

        def changed(store, card_id):
            result = original(store, card_id)
            if card_id == "aaaa1111":
                result = result.model_copy(deep=True)
                result.meta["_claim_revision"] = "replacement"
            return result

        monkeypatch.setattr(CardStore, "fold", changed)
    result = namespace["authoritative_claimability"](card.id, fresh=True)
    assert result["claimable"] is not stale
    assert result["reason"] == ("helper-parent-stale" if stale else "claimable")


def test_partial_helper_contract_is_not_ordinary_work(helper):
    home, card = helper
    card.meta.pop("helper_parent_id")
    with pytest.raises(ValueError, match="parent"):
        validate_helper_generation(home, card)
