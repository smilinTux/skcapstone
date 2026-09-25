"""Exact owner mandates use isolated CardStore fixtures, never the live board."""

from __future__ import annotations

import copy

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet import crew_contract as contract

SOURCE = {
    "repository": "https://example.test/project.git",
    "base_ref": "main",
    "base_revision": "a" * 40,
}


def helper_packet():
    """Return one bounded existing helper packet."""
    return {
        "request_id": "verify-1",
        "title": "[S] Verify seam",
        "objective": "Verify seam",
        "criteria": ["Return exact evidence"],
        "allowed_paths": ["tests/test_seam.py"],
        "verification_commands": ["pytest -q tests/test_seam.py"],
    }


def crew_packet():
    """Return a crew with disjoint owner and helper scopes."""
    return {
        "schema": "skfleet.crew/v1",
        "crew_id": "crew-1",
        "parent_id": "aaaa1111",
        "parent_claim_revision": "parent-1",
        "coordinator_node": "fixture-host",
        "owner_paths": ["src"],
        "max_active_helpers": 2,
        "max_helpers": 4,
        "slots": [
            {
                "slot_id": "verify",
                "role": "verification",
                "trigger": "initial",
                "packet": helper_packet(),
            }
        ],
    }


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Create one active claimed parent and a deterministic local hostname."""
    CardStore(tmp_path).create(
        CardCore(
            id="aaaa1111",
            title="[M] Parent",
            description="Preserve all source and do not deploy.",
            initial_owner="test-owner",
            initial_claim_revision="parent-1",
            initial_labels=["source-only", "sk-m"],
            acceptance_criteria=["Integrate exact helper results"],
            meta=SOURCE,
        )
    )
    monkeypatch.setattr(contract.socket, "gethostname", lambda: "fixture-host")
    return tmp_path


def test_registration_is_read_only_replayable_and_records_original_authority(home):
    before = CardStore(home).fold("aaaa1111").model_dump()
    packet = crew_packet()
    record = contract.register_payload(home, "test-owner", packet)
    assert contract.validate_mandate(home, record) == record
    assert contract.register_payload(home, "test-owner", packet) == record
    assert record["authorizer"] == "test-owner"
    assert record["source"] == SOURCE
    assert record["parent_restrictions"] == {"labels": ["sk-m", "source-only"], "dependencies": []}
    packet["slots"][0]["packet"]["objective"] = "changed caller memory"
    assert record["packet"]["slots"][0]["packet"]["objective"] == "Verify seam"
    assert CardStore(home).fold("aaaa1111").model_dump() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_active_helpers", True),
        ("max_helpers", 33),
        ("crew_id", "../escape"),
        ("parent_id", "wrong"),
        ("parent_claim_revision", ""),
        ("slots", []),
        ("owner_paths", ["src/../tests"]),
    ],
)
def test_invalid_schema_and_typed_bounds(home, field, value):
    packet = crew_packet()
    packet[field] = value
    with pytest.raises(ValueError):
        contract.register_payload(home, "test-owner", packet)


@pytest.mark.parametrize("overlap", ["owner", "sibling", "ancestor"])
def test_disjoint_scope_reservations(home, overlap):
    packet = crew_packet()
    if overlap == "owner":
        packet["slots"][0]["packet"]["allowed_paths"] = ["src/worker.py"]
    else:
        other = copy.deepcopy(packet["slots"][0])
        other["slot_id"], other["packet"]["request_id"] = "second", "second-1"
        if overlap == "ancestor":
            other["packet"]["allowed_paths"] = ["tests"]
        packet["slots"].append(other)
    with pytest.raises(ValueError, match="overlap"):
        contract.register_payload(home, "test-owner", packet)


def test_stale_diagnostic_requires_read_only_template(home):
    packet = crew_packet()
    packet["slots"][0]["trigger"] = "stale-progress"
    with pytest.raises(ValueError, match="read-only"):
        contract.register_payload(home, "test-owner", packet)
    packet["slots"][0]["packet"]["allowed_paths"] = []
    assert contract.register_payload(home, "test-owner", packet)


def test_exact_resource_context_and_wrong_host(home):
    packet = crew_packet()
    packet["slots"][0]["resource"] = {
        "resource_id": "fixture",
        "version": "v1",
        "policy_sha256": "b" * 64,
        "context_sha256": "c" * 64,
    }
    assert contract.register_payload(home, "test-owner", packet)
    packet["slots"][0]["resource"]["context_sha256"] = "unknown"
    with pytest.raises(ValueError, match="digest"):
        contract.register_payload(home, "test-owner", packet)
    packet = crew_packet()
    packet["coordinator_node"] = "other-host"
    with pytest.raises(ValueError, match="actual coordinator"):
        contract.register_payload(home, "test-owner", packet)


@pytest.mark.parametrize(
    "mutation", ["owner", "claim", "title", "criteria", "labels", "deps", "source"]
)
def test_changed_parent_under_same_manifest_is_rejected(home, monkeypatch, mutation):
    record = contract.register_payload(home, "test-owner", crew_packet())
    original = CardStore.fold

    def changed(store, card_id):
        card = original(store, card_id).model_copy(deep=True)
        if mutation == "owner":
            card.owner = "other-owner"
        elif mutation == "claim":
            card.meta["_claim_revision"] = "new-claim"
        elif mutation == "title":
            card.title = "[M] New contract"
        elif mutation == "criteria":
            card.acceptance_criteria = ["New criteria"]
        elif mutation == "labels":
            card.labels = ["source-only"]
        elif mutation == "deps":
            card.dependencies = ["cccc3333"]
        else:
            card.meta["base_revision"] = "b" * 40
        return card

    monkeypatch.setattr(CardStore, "fold", changed)
    with pytest.raises(ValueError):
        contract.validate_mandate(home, record)


def test_capability_is_checked_on_registration_and_consumption(home, monkeypatch):
    calls = []
    original = contract.authorize_coord_mutation

    def watched(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(contract, "authorize_coord_mutation", watched)
    record = contract.register_payload(home, "test-owner", crew_packet())
    contract.validate_mandate(home, record)
    assert calls == [("test-owner", contract.Action.CREATE_CARD, "aaaa1111", None, None)] * 2
    record["manifest_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="digest"):
        contract.validate_mandate(home, record)


@pytest.mark.parametrize("change", ["source", "contract", "labels", "dependencies"])
def test_creation_rejects_same_claim_change_after_outer_validation(home, monkeypatch, change):
    from skcapstone.coord_helpers import request_help

    record = contract.register_payload(home, "test-owner", crew_packet())
    contract.validate_mandate(home, record)
    original = CardStore.fold

    def changed(store, card_id):
        card = original(store, card_id)
        if card_id == "aaaa1111":
            card = card.model_copy(deep=True)
            if change == "source":
                card.meta["base_revision"] = "b" * 40
            elif change == "contract":
                card.description = "A later wider contract"
            elif change == "labels":
                card.labels = ["source-only"]
            else:
                card.dependencies = ["cccc3333"]
        return card

    monkeypatch.setattr(CardStore, "fold", changed)
    with pytest.raises(ValueError, match="original authorized"):
        request_help(
            home,
            "aaaa1111",
            "test-owner",
            "parent-1",
            helper_packet(),
            expected_parent={
                "contract_sha256": record["parent_contract_sha256"],
                "source": record["source"],
                **record["parent_restrictions"],
            },
        )
    assert CardStore(home).list_card_ids() == ["aaaa1111"]
