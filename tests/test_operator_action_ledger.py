"""Contract tests for the durable ATLAS ActionIntent ledger."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from skcapstone.operator_seat.action_ledger import (
    ActionIntent,
    ActionLedger,
    ActionState,
)

NOW = datetime(2026, 8, 20, 21, 0, tzinfo=timezone.utc)


def _intent(**overrides) -> ActionIntent:
    fields = {
        "condition_fingerprint": "condition:disk-full:node-41",
        "application": "skos",
        "target_kind": "Node",
        "target_id": "node-41",
        "action": "cleanup_disk",
        "catalog_generation": "sha256:catalog-v3",
        "created_at": NOW,
        "itil_change_id": "chg-1234",
        "cmdb_ci_id": "ci-node-41",
        "verification": {"condition": "DiskPressure", "want": "False"},
        "rollback": {"action": "restore_snapshot"},
    }
    fields.update(overrides)
    return ActionIntent(**fields)


def test_stable_id_links_and_happy_path(tmp_path) -> None:
    ledger = ActionLedger(tmp_path / "ledger")
    intent = _intent()

    observed = ledger.create(intent, actor="atlas", evidence_ref="obs://sha256/abc")
    for state in (
        ActionState.DIAGNOSED,
        ActionState.PROPOSED,
        ActionState.AUTHORIZED,
        ActionState.EXECUTING,
        ActionState.VERIFIED,
    ):
        ledger.append(intent.intent_id, state, occurred_at=NOW, actor="atlas")

    assert intent.intent_id.startswith("ai-")
    assert observed.state is ActionState.OBSERVED
    assert ledger.read_intent(intent.intent_id).itil_change_id == "chg-1234"
    assert ledger.read_intent(intent.intent_id).cmdb_ci_id == "ci-node-41"
    assert ledger.current_state(intent.intent_id) is ActionState.VERIFIED
    assert [event.sequence for event in ledger.events(intent.intent_id)] == list(range(6))


def test_same_identity_has_same_id_and_create_is_idempotent(tmp_path) -> None:
    first = _intent()
    second = _intent()
    ledger = ActionLedger(tmp_path)

    assert first.intent_id == second.intent_id
    assert ledger.create(first, actor="atlas") == ledger.create(second, actor="atlas")
    assert len(ledger.events(first.intent_id)) == 1


def test_governance_binding_changes_stable_id() -> None:
    assert _intent().intent_id != _intent(itil_change_id="chg-other").intent_id
    assert _intent().intent_id != _intent(catalog_generation="sha256:new").intent_id


def test_rejects_skipped_and_terminal_transitions(tmp_path) -> None:
    ledger = ActionLedger(tmp_path)
    intent = _intent()
    ledger.create(intent, actor="atlas")

    with pytest.raises(ValueError, match="observed -> executing"):
        ledger.append(intent.intent_id, ActionState.EXECUTING, occurred_at=NOW, actor="atlas")

    for state in (
        ActionState.DIAGNOSED,
        ActionState.PROPOSED,
        ActionState.AUTHORIZED,
        ActionState.EXECUTING,
        ActionState.VERIFIED,
    ):
        ledger.append(intent.intent_id, state, occurred_at=NOW, actor="atlas")
    with pytest.raises(ValueError, match="verified -> failed"):
        ledger.append(intent.intent_id, ActionState.FAILED, occurred_at=NOW, actor="atlas")


@pytest.mark.parametrize("terminal", [ActionState.ROLLED_BACK, ActionState.ESCALATED])
def test_failure_can_only_end_in_recovery_or_escalation(tmp_path, terminal) -> None:
    ledger = ActionLedger(tmp_path)
    intent = _intent(target_id=terminal.value)
    ledger.create(intent, actor="atlas")
    for state in (
        ActionState.DIAGNOSED,
        ActionState.PROPOSED,
        ActionState.AUTHORIZED,
        ActionState.EXECUTING,
        ActionState.FAILED,
        terminal,
    ):
        ledger.append(intent.intent_id, state, occurred_at=NOW, actor="atlas")
    assert ledger.current_state(intent.intent_id) is terminal


def test_detects_event_tampering(tmp_path) -> None:
    ledger = ActionLedger(tmp_path)
    intent = _intent()
    ledger.create(intent, actor="atlas")
    path = tmp_path / "events" / f"{intent.intent_id}.jsonl"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["actor"] = "attacker"
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="hash chain"):
        ledger.events(intent.intent_id)


def test_rejects_path_like_ids_and_unknown_intents(tmp_path) -> None:
    ledger = ActionLedger(tmp_path)
    with pytest.raises(ValueError, match="invalid action intent id"):
        ledger.events("../../escape")
    with pytest.raises(ValueError, match="unknown action intent"):
        ledger.append("ai-" + "a" * 24, ActionState.OBSERVED, occurred_at=NOW, actor="atlas")


def test_rejects_forged_stable_id() -> None:
    with pytest.raises(ValueError, match="does not match"):
        _intent(intent_id="ai-" + "0" * 24)


def test_signed_events_are_bound_to_authorization_and_verified(tmp_path) -> None:
    secret = b"test-ledger-key"

    def signer(data: bytes) -> str:
        import hashlib

        return hashlib.sha256(secret + data).hexdigest()

    ledger = ActionLedger(
        tmp_path, signer=signer, verifier=lambda data, sig: signer(data) == sig,
        require_signatures=True,
    )
    intent = _intent(authorization_ref="capauth://grant/grant-123")
    event = ledger.create(intent, actor="atlas")

    assert event.signature
    assert ledger.read_intent(intent.intent_id).authorization_ref.endswith("grant-123")
    assert ledger.events(intent.intent_id)[0].signature == event.signature


def test_signed_ledger_fails_closed_without_crypto(tmp_path) -> None:
    with pytest.raises(ValueError, match="requires signer and verifier"):
        ActionLedger(tmp_path, require_signatures=True)
