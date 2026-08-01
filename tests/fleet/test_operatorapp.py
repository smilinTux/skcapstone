"""Tests for the Operatorapp kind: normalization, ProposalsRatified, human-only field."""

from __future__ import annotations

import pytest

from skcapstone.fleet import operatorapp, store
from skcapstone.fleet.explain import explain
from skcapstone.fleet.operatorapp_controller import operatorapp_rows
from skcapstone.fleet.store import Writer

NOW = "2026-07-30T12:00:00Z"


# --- normalization -----------------------------------------------------------


def test_normalize_defaults() -> None:
    spec = operatorapp.normalize_operatorapp_spec({"name": "skchat"})
    assert spec == {
        "name": "skchat",
        "cli": None,
        "repos": [],
        "contractVersion": 1,
        "proposedStandardActions": [],
        "ratifiedStandardActions": [],
        "conditions": [],
        "deleted": False,
    }


def test_normalize_echoes_fields() -> None:
    spec = operatorapp.normalize_operatorapp_spec(
        {
            "name": "skchat",
            "cli": "skchat operator",
            "repos": ["skchat"],
            "proposedStandardActions": ["restart-daemon", "restart-telegram-bridge"],
            "ratifiedStandardActions": ["restart-daemon"],
            "conditions": ["DaemonReady", "BridgeAlive"],
        }
    )
    assert spec["cli"] == "skchat operator"
    assert spec["proposedStandardActions"] == ["restart-daemon", "restart-telegram-bridge"]
    assert spec["ratifiedStandardActions"] == ["restart-daemon"]


def test_normalize_rejects_missing_name() -> None:
    with pytest.raises(operatorapp.OperatorappSpecError):
        operatorapp.normalize_operatorapp_spec({})


def test_normalize_rejects_bad_action_list() -> None:
    with pytest.raises(operatorapp.OperatorappSpecError):
        operatorapp.normalize_operatorapp_spec({"name": "skchat", "proposedStandardActions": [""]})


# --- conditions --------------------------------------------------------------


def test_proposals_ratified_healthy_when_all_ratified() -> None:
    spec = operatorapp.normalize_operatorapp_spec(
        {
            "name": "skchat",
            "proposedStandardActions": ["restart-daemon"],
            "ratifiedStandardActions": ["restart-daemon"],
        }
    )
    conds = {c["type"]: c["status"] for c in operatorapp.operatorapp_conditions(spec, {}, NOW)}
    assert conds["ProposalsRatified"] == "True"


def test_proposals_ratified_fires_when_pending() -> None:
    spec = operatorapp.normalize_operatorapp_spec(
        {
            "name": "skchat",
            "proposedStandardActions": ["restart-daemon", "restart-telegram-bridge"],
            "ratifiedStandardActions": ["restart-daemon"],
        }
    )
    conds = {c["type"]: c["status"] for c in operatorapp.operatorapp_conditions(spec, {}, NOW)}
    assert conds["ProposalsRatified"] == "False"  # bridge restart not yet ratified


def test_no_proposals_is_trivially_ratified() -> None:
    spec = operatorapp.normalize_operatorapp_spec({"name": "skcode"})
    conds = {c["type"]: c["status"] for c in operatorapp.operatorapp_conditions(spec, {}, NOW)}
    assert conds["ProposalsRatified"] == "True"


# --- explain -----------------------------------------------------------------


def test_operatorapp_in_explain_registry() -> None:
    assert "operatorapp" in explain()["kinds"]
    entry = explain("operatorapp")
    assert entry["kind"] == "Operatorapp"
    assert "ProposalsRatified" in entry["conditions"]


# --- human-only ratifiedStandardActions guard --------------------------------


def _agent_seat() -> Writer:
    return Writer(role="operator", node="node-41", identity="atlas", agent_seat=True)


def test_ai_seat_may_register_with_nothing_ratified(paths, operator) -> None:
    seat = _agent_seat()
    payload = store.write_spec(
        paths,
        "operatorapp",
        "skchat",
        operatorapp.normalize_operatorapp_spec(
            {"name": "skchat", "proposedStandardActions": ["restart-daemon"]}
        ),
        writer=seat,
    )
    assert payload["spec"]["ratifiedStandardActions"] == []


def test_ai_seat_cannot_ratify(paths) -> None:
    seat = _agent_seat()
    with pytest.raises(store.OwnershipError):
        store.write_spec(
            paths,
            "operatorapp",
            "skchat",
            operatorapp.normalize_operatorapp_spec(
                {"name": "skchat", "ratifiedStandardActions": ["restart-daemon"]}
            ),
            writer=seat,
        )


def test_human_ratifies_then_ai_may_refresh_other_fields(paths, operator) -> None:
    # Human ratifies.
    store.write_spec(
        paths,
        "operatorapp",
        "skchat",
        operatorapp.normalize_operatorapp_spec(
            {
                "name": "skchat",
                "proposedStandardActions": ["restart-daemon"],
                "ratifiedStandardActions": ["restart-daemon"],
            }
        ),
        writer=operator,
    )
    # AI refreshes repos/conditions but keeps the same ratified list: allowed.
    seat = _agent_seat()
    payload = store.write_spec(
        paths,
        "operatorapp",
        "skchat",
        operatorapp.normalize_operatorapp_spec(
            {
                "name": "skchat",
                "repos": ["skchat"],
                "proposedStandardActions": ["restart-daemon"],
                "ratifiedStandardActions": ["restart-daemon"],
                "conditions": ["DaemonReady"],
            }
        ),
        writer=seat,
    )
    assert payload["spec"]["repos"] == ["skchat"]
    # But the AI cannot then drop the human's ratification.
    with pytest.raises(store.OwnershipError):
        store.write_spec(
            paths,
            "operatorapp",
            "skchat",
            operatorapp.normalize_operatorapp_spec(
                {"name": "skchat", "ratifiedStandardActions": []}
            ),
            writer=seat,
        )


# --- controller --------------------------------------------------------------


def test_operatorapp_rows_derive_counts(paths, operator) -> None:
    store.write_spec(
        paths,
        "operatorapp",
        "skchat",
        operatorapp.normalize_operatorapp_spec(
            {
                "name": "skchat",
                "cli": "skchat operator",
                "repos": ["skchat"],
                "proposedStandardActions": ["restart-daemon", "restart-telegram-bridge"],
                "ratifiedStandardActions": ["restart-daemon"],
            }
        ),
        writer=operator,
    )
    rows = operatorapp_rows(paths, NOW)
    assert len(rows) == 1
    row = rows[0]
    assert row.name == "skchat"
    assert row.cli == "skchat operator"
    assert row.proposed_count == 2
    assert row.ratified_count == 1
    assert row.proposals_ratified is False  # one proposal still pending


def test_operatorapp_rows_skip_deleted(paths, operator) -> None:
    store.write_spec(
        paths,
        "operatorapp",
        "gone",
        operatorapp.normalize_operatorapp_spec({"name": "gone", "deleted": True}),
        writer=operator,
    )
    assert operatorapp_rows(paths, NOW) == []
