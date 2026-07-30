"""Register app adapters as Operatorapp objects: derive, register_all, ratify."""

from __future__ import annotations

import pytest

from skcapstone.fleet import store
from skcapstone.fleet.operatorapp_controller import operatorapp_rows
from skcapstone.fleet.paths import FleetPaths
from skcapstone.operator_seat import registration, skchat_adapter


def _paths(tmp_path):
    return FleetPaths(root=tmp_path / "fleet")


def _seat():
    return store.Writer(role="operator", node="node-41", identity="operator", agent_seat=True)


def _human():
    return store.Writer(role="operator", node="cli", identity="chef", agent_seat=False)


# --- derive ------------------------------------------------------------------


def test_derive_proposes_only_standard_reversible_actions():
    spec = registration.derive_operatorapp_spec(
        "skchat", skchat_adapter.skchat_explain(), cli="skchat operator", repos=["skchat"]
    )
    # restart-daemon + restart-telegram-bridge are standard+reversible; purge-outbox
    # is neither, so it is NOT proposed.
    assert spec["proposedStandardActions"] == ["restart-daemon", "restart-telegram-bridge"]
    assert "purge-outbox" not in spec["proposedStandardActions"]
    assert spec["cli"] == "skchat operator"
    assert spec["conditions"] == list(skchat_adapter.CONDITIONS)
    # never auto-ratifies
    assert spec["ratifiedStandardActions"] == []


# --- register_all ------------------------------------------------------------


def test_register_all_writes_every_app(tmp_path):
    paths = _paths(tmp_path)
    written = registration.register_all(paths, writer=_seat())
    assert written == ["skchat", "skcode", "skcomms", "skgateway", "skmemory", "skos"]
    rows = {r.name: r for r in operatorapp_rows(paths, "2026-07-30T00:00:00Z")}
    assert set(rows) == set(written)
    assert rows["skcode"].cli == "skcode-hostd operator"


def test_seat_registration_never_ratifies(tmp_path):
    paths = _paths(tmp_path)
    registration.register_all(paths, writer=_seat())
    rows = {r.name: r for r in operatorapp_rows(paths, "2026-07-30T00:00:00Z")}
    # Everything proposed, nothing ratified: the human still holds that lever.
    assert rows["skchat"].proposed_count == 2
    assert rows["skchat"].ratified_count == 0
    assert rows["skchat"].proposals_ratified is False


def test_refresh_preserves_human_ratifications(tmp_path):
    paths = _paths(tmp_path)
    registration.register_all(paths, writer=_seat())
    # Human ratifies one action.
    registration.ratify(paths, "skchat", "restart-daemon", writer=_human())
    # The seat re-registers (a refresh) and must NOT blank the ratification.
    registration.register_all(paths, writer=_seat())
    rows = {r.name: r for r in operatorapp_rows(paths, "2026-07-30T00:00:00Z")}
    assert rows["skchat"].ratified_count == 1


# --- ratify ------------------------------------------------------------------


def test_ratify_adds_action(tmp_path):
    paths = _paths(tmp_path)
    registration.register_all(paths, writer=_seat())
    registration.ratify(paths, "skchat", "restart-daemon", writer=_human())
    on_disk = store.read_spec(paths, "operatorapp", "skchat")
    assert on_disk["spec"]["ratifiedStandardActions"] == ["restart-daemon"]


def test_ratify_is_idempotent(tmp_path):
    paths = _paths(tmp_path)
    registration.register_all(paths, writer=_seat())
    registration.ratify(paths, "skchat", "restart-daemon", writer=_human())
    registration.ratify(paths, "skchat", "restart-daemon", writer=_human())
    on_disk = store.read_spec(paths, "operatorapp", "skchat")
    assert on_disk["spec"]["ratifiedStandardActions"] == ["restart-daemon"]


def test_ratify_rejects_unproposed_action(tmp_path):
    paths = _paths(tmp_path)
    registration.register_all(paths, writer=_seat())
    with pytest.raises(ValueError):
        registration.ratify(paths, "skchat", "purge-outbox", writer=_human())


def test_ratify_rejects_unknown_app(tmp_path):
    paths = _paths(tmp_path)
    with pytest.raises(ValueError):
        registration.ratify(paths, "nope", "restart-daemon", writer=_human())


def test_seat_cannot_ratify_via_store_guard(tmp_path):
    # Belt-and-suspenders: even if the seat tried the ratify path, the store guard
    # blocks an agent_seat writer from changing ratifiedStandardActions.
    paths = _paths(tmp_path)
    registration.register_all(paths, writer=_seat())
    with pytest.raises(store.OwnershipError):
        registration.ratify(paths, "skchat", "restart-daemon", writer=_seat())
