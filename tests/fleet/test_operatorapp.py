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
        "endpoint": None,
        "node": None,
        "transport": None,
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


# --- contractVersion 2 schema (card 90b5b277 Phase 2) ------------------------


def test_v1_spec_rejects_endpoint() -> None:
    # "v1 specs stay valid and mean cli-local": a v1 spec declaring a v2 field
    # is a spec error, not a silently-ignored extra key.
    with pytest.raises(operatorapp.OperatorappSpecError, match="endpoint"):
        operatorapp.normalize_operatorapp_spec(
            {"name": "skgateway", "endpoint": "https://100.64.0.5:9392/operator/v1"}
        )


def test_v1_spec_rejects_node() -> None:
    with pytest.raises(operatorapp.OperatorappSpecError, match="node"):
        operatorapp.normalize_operatorapp_spec({"name": "skgateway", "node": "node-noroc2027"})


def test_v1_spec_rejects_transport() -> None:
    with pytest.raises(operatorapp.OperatorappSpecError, match="transport"):
        operatorapp.normalize_operatorapp_spec({"name": "skgateway", "transport": "http"})


def test_v2_spec_accepts_endpoint_node_transport() -> None:
    spec = operatorapp.normalize_operatorapp_spec(
        {
            "name": "skgateway",
            "contractVersion": 2,
            "endpoint": "https://100.64.0.5:9392/operator/v1",
            "node": "node-noroc2027",
            "transport": "http",
        }
    )
    assert spec["contractVersion"] == 2
    assert spec["endpoint"] == "https://100.64.0.5:9392/operator/v1"
    assert spec["node"] == "node-noroc2027"
    assert spec["transport"] == "http"


def test_v2_spec_without_any_v2_field_still_normalizes() -> None:
    # v2 does not REQUIRE endpoint/node/transport; a v2 spec that sets none of
    # them just carries three Nones, same shape as a v1 spec plus the bumped
    # version.
    spec = operatorapp.normalize_operatorapp_spec({"name": "skgateway", "contractVersion": 2})
    assert spec["endpoint"] is None
    assert spec["node"] is None
    assert spec["transport"] is None


def test_v2_spec_rejects_empty_endpoint() -> None:
    with pytest.raises(operatorapp.OperatorappSpecError):
        operatorapp.normalize_operatorapp_spec(
            {"name": "skgateway", "contractVersion": 2, "endpoint": ""}
        )


def test_v2_spec_rejects_bad_transport() -> None:
    with pytest.raises(operatorapp.OperatorappSpecError, match="transport"):
        operatorapp.normalize_operatorapp_spec(
            {"name": "skgateway", "contractVersion": 2, "transport": "carrier-pigeon"}
        )


def test_contract_version_rejects_bool() -> None:
    # bool is an int subclass in Python; guard against `contractVersion: true`
    # silently passing the isinstance(int) check.
    with pytest.raises(operatorapp.OperatorappSpecError):
        operatorapp.normalize_operatorapp_spec({"name": "skgateway", "contractVersion": True})


# --- cli_exec_eligible: the home-node precedence rule -------------------------


def test_v1_spec_always_cli_eligible_regardless_of_node() -> None:
    # The whole point of "v1 means cli-local": no node check ever applies.
    spec = operatorapp.normalize_operatorapp_spec({"name": "cmdb", "cli": "skcapstone cmdb"})
    eligible, reason = operatorapp.cli_exec_eligible(spec, "node-anywhere")
    assert eligible is True
    assert reason == ""


def test_v2_spec_eligible_only_on_matching_home_node() -> None:
    spec = operatorapp.normalize_operatorapp_spec(
        {
            "name": "skgateway",
            "contractVersion": 2,
            "node": "node-noroc2027",
            "cli": "skgateway operator",
        }
    )
    eligible, _ = operatorapp.cli_exec_eligible(spec, "node-noroc2027")
    assert eligible is True


def test_v2_spec_ineligible_on_a_different_node() -> None:
    spec = operatorapp.normalize_operatorapp_spec(
        {
            "name": "skgateway",
            "contractVersion": 2,
            "node": "node-noroc2027",
            "cli": "skgateway operator",
        }
    )
    eligible, reason = operatorapp.cli_exec_eligible(spec, "node-100")
    assert eligible is False
    assert "node-noroc2027" in reason
    assert "node-100" in reason
    assert "remote seat never execs" in reason


def test_v2_spec_with_no_node_is_never_locally_eligible() -> None:
    # A v2 spec that declares no home node cannot be exec'd by ANY seat,
    # including one that happens to be running on the app's actual host --
    # there is no way to know it is the home node without the field.
    spec = operatorapp.normalize_operatorapp_spec(
        {"name": "skgateway", "contractVersion": 2, "cli": "skgateway operator"}
    )
    eligible, reason = operatorapp.cli_exec_eligible(spec, "node-noroc2027")
    assert eligible is False
    assert "declares no home node" in reason


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
