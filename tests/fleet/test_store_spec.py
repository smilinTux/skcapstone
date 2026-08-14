"""Tests for spec writes: generation, ownership, writer block."""

from __future__ import annotations

import pytest

from skcapstone.fleet import signing, store


def test_write_spec_bumps_generation(paths, operator) -> None:
    first = store.write_spec(paths, "node", "node-41", {"cordoned": False}, writer=operator)
    assert first["generation"] == 1
    assert first["kind"] == "Node"
    second = store.write_spec(paths, "node", "node-41", {"cordoned": True}, writer=operator)
    assert second["generation"] == 2
    on_disk = store.read_spec(paths, "node", "node-41")
    assert on_disk["spec"]["cordoned"] is True
    assert on_disk["generation"] == 2


def test_spec_carries_writer_identity_block(paths, operator) -> None:
    payload = store.write_spec(paths, "node", "node-41", {}, writer=operator)
    # suite_id names the crypto suite INSIDE the signed block (SPE P3), so a
    # future algorithm change is detectable rather than silent.
    assert payload["writer"] == {
        "suite_id": signing.SUITE_ID,
        "role": "operator",
        "node": "node-158",
        "identity": "capauth:chef@skworld.io",
        "signature": None,
    }


def test_non_operator_cannot_write_spec(paths, noded41) -> None:
    with pytest.raises(store.OwnershipError):
        store.write_spec(paths, "node", "node-41", {}, writer=noded41)


def test_bad_names_rejected(paths, operator) -> None:
    with pytest.raises(store.OwnershipError):
        store.write_spec(paths, "node", "../evil", {}, writer=operator)
    with pytest.raises(store.OwnershipError):
        store.write_spec(paths, "no/kind", "x", {}, writer=operator)


def test_list_specs_sorted_and_empty(paths, operator) -> None:
    assert store.list_specs(paths, "service") == []
    store.write_spec(paths, "node", "node-b", {}, writer=operator)
    store.write_spec(paths, "node", "node-a", {}, writer=operator)
    assert [s["name"] for s in store.list_specs(paths, "node")] == ["node-a", "node-b"]
