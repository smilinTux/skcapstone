"""Tests for the read-only shadow portfolio CLI."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from click.testing import CliRunner

from skcapstone.cli import main

AS_OF = datetime(2026, 8, 23, 16, 0, tzinfo=timezone.utc)
SHA = "a" * 64


def _input(*, parity_state: str = "healthy") -> dict[str, object]:
    observed = AS_OF - timedelta(seconds=30)
    return {
        "schema_version": "portfolio-plan-input.v1",
        "candidates": [
            {
                "card_id": "card-a",
                "title": "Implement one bounded leaf",
                "kind": "task",
                "state": "backlog",
                "card_revision": "card-r1",
                "priority": "medium",
                "class_of_service": "standard",
                "human_order": None,
                "enrollment_state": "enrolled",
                "enrollment_policy_version": "enrollment-v1",
                "tags": ["leaf-task", "repo:skcoord", "size:M"],
                "dependency_ids": [],
                "dependency_states": {},
                "dependency_revisions": {},
                "acceptance_criteria": ["exact test passes"],
                "repo_ids": ["skcoord"],
                "size_values": ["M"],
                "execution_ready_attestation": "ready-r1",
                "owner_principal_id": None,
                "lease_state": "clear",
                "lease_generation": 0,
                "lease_expires_at": None,
                "human_gate_state": "not-required",
                "approval_ref": None,
                "approved_card_revision": None,
                "approved_card_hash": None,
                "target_executor_principal_id": "autocoder",
                "downstream_unlock_count": 0,
                "ready_at": observed.isoformat(),
                "fixed_date_at": None,
                "expedite_approval_ref": None,
                "expedite_approval_expires_at": None,
            }
        ],
        "capacities": {
            "autocoder": {
                "principal_id": "autocoder",
                "profile": {
                    "profile_id": "autocoder-profile",
                    "profile_kind": "service",
                    "profile_state": "healthy",
                    "selectable": False,
                    "fallback_eligible": False,
                    "memory_principal_id": "autocoder-memory",
                    "default_tools": [],
                    "capability_policy_ref": "capauth:portfolio-v1",
                    "profile_revision": "profile-r1",
                    "profile_hash": SHA,
                },
                "allowed_task_classes": ["task"],
                "allowed_repo_ids": ["skcoord"],
                "wip_limit": 2,
                "active_wip": 0,
                "active_card_ids": [],
                "lease_state_fresh": True,
                "capability_state": "healthy",
                "capacity_revision": "capacity-r1",
                "observed_at": observed.isoformat(),
                "expires_at": (AS_OF + timedelta(minutes=2)).isoformat(),
            }
        },
        "quality": {
            "source_owner": "cardstore",
            "snapshot_id": "snapshot-1",
            "snapshot_hash": SHA,
            "board_revision": "board-r1",
            "projection_revision": "projection-r1",
            "parity_state": parity_state,
            "read_state": "healthy",
            "observed_at": observed.isoformat(),
            "expires_at": (AS_OF + timedelta(minutes=4)).isoformat(),
        },
        "policy": {
            "policy_id": "portfolio-shadow",
            "policy_version": "1",
            "policy_hash": SHA,
            "enrollment_policy_version": "enrollment-v1",
            "snapshot_max_age_seconds": 300,
        },
        "objective_hash": SHA,
        "as_of": AS_OF.isoformat(),
        "source_refs": [],
    }


def _run(payload: dict[str, object], *options: str):
    return CliRunner().invoke(
        main,
        ["coord", "portfolio-plan", "--shadow", "--format", "json", *options],
        input=json.dumps(payload),
    )


def test_proposed_json_is_exact_and_repeatable() -> None:
    payload = _input()
    first = _run(payload)
    second = _run(payload)

    assert first.exit_code == 0, first.output
    assert first.output == second.output
    proposal = json.loads(first.output)
    assert proposal["schema_version"] == "portfolio-plan-content.v1"
    assert proposal["status"] == "proposed"
    assert proposal["recommendations"][0]["card_id"] == "card-a"
    assert proposal["claims"] == []
    assert proposal["mutations"] == []


def test_abstention_is_successful_unless_strict() -> None:
    payload = _input(parity_state="unsafe")
    normal = _run(payload)
    strict = _run(payload, "--strict")

    assert normal.exit_code == 0, normal.output
    assert strict.exit_code == 2
    assert json.loads(normal.output)["abstention"]["reason_codes"] == ["parity_unsafe"]
    assert json.loads(strict.output)["status"] == "abstained"


def test_command_never_calls_board_mutation_apis(monkeypatch) -> None:
    from skcoord.card import CardEventLog
    from skcoord.card_store import CardStore
    from skcoord.coordination import Board

    def forbidden(*_args, **_kwargs):
        raise AssertionError("portfolio-plan called a mutation API")

    for target, name in (
        (Board, "create_task"),
        (Board, "claim_task"),
        (Board, "complete_task"),
        (CardStore, "create"),
        (CardEventLog, "append"),
    ):
        monkeypatch.setattr(target, name, forbidden)

    result = _run(_input())
    assert result.exit_code == 0, result.output


def test_malformed_input_fails_closed() -> None:
    payload = _input()
    payload["quality"] = {"parity_state": "healthy"}

    result = _run(payload)

    assert result.exit_code == 2
    assert "invalid portfolio plan input" in result.output
