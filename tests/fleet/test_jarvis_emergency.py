"""Integration tests for every Jarvis emergency mutation entrypoint."""

from datetime import datetime, timedelta, timezone

import pytest

from skcapstone.jarvis_emergency import JarvisEmergencyGateway
from skcapstone.operator_authorization import AuthorizationEnvelope, authorization_id
from skcapstone.seat_boundaries import Action, BoundaryError

SCOPE = "skcapstone,skdashboard,skworld"
TARGET = "smilinTux/skcapstone#572"


def _envelope(action: Action, *, issuer: str = "casey", target: str = TARGET):
    now = datetime.now(timezone.utc)
    envelope = AuthorizationEnvelope(
        authorization_id="pending",
        issuer=issuer,
        issuer_role="owner",
        issuer_fingerprint="CASEY-FINGERPRINT",
        action=f"jarvis.{action.value}",
        target=target,
        change_id="casey-direction-20a637fe",
        scope=SCOPE,
        issued_at=(now - timedelta(minutes=1)).isoformat(),
        expires_at=(now + timedelta(minutes=5)).isoformat(),
        nonce="casey_direction_nonce_1234567890",
        signature="signed-by-casey",
    )
    envelope.authorization_id = authorization_id(envelope)
    return envelope


ENTRYPOINTS = [
    ("create_card", Action.CREATE_CARD),
    ("claim_card", Action.CLAIM),
    ("move_card", Action.MOVE_CARD),
    ("complete_card", Action.COMPLETE_CARD),
    ("fleet", Action.LAUNCH),
    ("merge", Action.MERGE),
    ("deploy", Action.DEPLOY),
    ("release", Action.RELEASE_ARTIFACT),
    ("verify", Action.VERIFY),
    ("actuate", Action.ACTUATE_APPLICATION),
]


@pytest.mark.parametrize(("method", "action"), ENTRYPOINTS)
def test_each_entrypoint_verifies_before_mutation(tmp_path, method: str, action: Action) -> None:
    calls = []
    gateway = JarvisEmergencyGateway(
        actor="jarvis",
        envelope=_envelope(action),
        public_key_armor="CASEY PUBLIC KEY",
        expected_fingerprint="CASEY-FINGERPRINT",
        change_id="casey-direction-20a637fe",
        verifier=lambda data, signature, key: (
            bool(data) and signature == "signed-by-casey" and key == "CASEY PUBLIC KEY"
        ),
        operations={action: lambda target: calls.append(target) or "ok"},
        replay_store=tmp_path / "used",
    )

    assert getattr(gateway, method)(TARGET) == "ok"
    assert calls == [TARGET]


@pytest.mark.parametrize(("method", "action"), ENTRYPOINTS)
def test_each_entrypoint_fails_closed_without_direction(
    tmp_path, method: str, action: Action
) -> None:
    calls = []
    gateway = JarvisEmergencyGateway(
        actor="jarvis",
        envelope=None,
        public_key_armor="",
        expected_fingerprint="CASEY-FINGERPRINT",
        change_id="casey-direction-20a637fe",
        verifier=lambda *_: True,
        operations={action: lambda target: calls.append(target)},
        replay_store=tmp_path / "used",
    )

    with pytest.raises(BoundaryError, match="signed Casey direction"):
        getattr(gateway, method)(TARGET)
    assert calls == []


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda envelope: envelope.model_copy(update={"issuer": "mallory"}), "issuer"),
        (lambda envelope: envelope.model_copy(update={"target": "other"}), "does not match"),
        (lambda envelope: envelope.model_copy(update={"scope": "sklegal"}), "does not match"),
        (lambda envelope: envelope.model_copy(update={"signature": "forged"}), "signature"),
        (
            lambda envelope: envelope.model_copy(
                update={
                    "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
                }
            ),
            "not currently valid",
        ),
    ],
)
def test_bypass_attempts_never_reach_operation(tmp_path, mutation, match: str) -> None:
    calls = []
    envelope = mutation(_envelope(Action.MERGE))
    gateway = JarvisEmergencyGateway(
        actor="jarvis",
        envelope=envelope,
        public_key_armor="CASEY PUBLIC KEY",
        expected_fingerprint="CASEY-FINGERPRINT",
        change_id="casey-direction-20a637fe",
        verifier=lambda _data, signature, _key: signature == "signed-by-casey",
        operations={Action.MERGE: lambda target: calls.append(target)},
        replay_store=tmp_path / "used",
    )

    with pytest.raises(BoundaryError, match=match):
        gateway.merge(TARGET)
    assert calls == []


def test_action_substitution_never_reaches_operation(tmp_path) -> None:
    calls = []
    gateway = JarvisEmergencyGateway(
        actor="jarvis",
        envelope=_envelope(Action.DEPLOY),
        public_key_armor="CASEY PUBLIC KEY",
        expected_fingerprint="CASEY-FINGERPRINT",
        change_id="casey-direction-20a637fe",
        verifier=lambda *_: True,
        operations={Action.MERGE: lambda target: calls.append(target)},
        replay_store=tmp_path / "used",
    )

    with pytest.raises(BoundaryError, match="does not match"):
        gateway.merge(TARGET)
    assert calls == []


def test_wrong_signer_fingerprint_never_reaches_operation(tmp_path) -> None:
    calls = []
    gateway = JarvisEmergencyGateway(
        actor="jarvis",
        envelope=_envelope(Action.MERGE),
        public_key_armor="CASEY PUBLIC KEY",
        expected_fingerprint="A-DIFFERENT-FINGERPRINT",
        change_id="casey-direction-20a637fe",
        verifier=lambda *_: True,
        operations={Action.MERGE: lambda target: calls.append(target)},
        replay_store=tmp_path / "used",
    )

    with pytest.raises(BoundaryError, match="does not match Casey"):
        gateway.merge(TARGET)
    assert calls == []


def test_wrong_change_never_reaches_operation(tmp_path) -> None:
    calls = []
    gateway = JarvisEmergencyGateway(
        actor="jarvis",
        envelope=_envelope(Action.MERGE),
        public_key_armor="CASEY PUBLIC KEY",
        expected_fingerprint="CASEY-FINGERPRINT",
        change_id="a-different-change",
        verifier=lambda *_: True,
        operations={Action.MERGE: lambda target: calls.append(target)},
        replay_store=tmp_path / "used",
    )

    with pytest.raises(BoundaryError, match="does not match"):
        gateway.merge(TARGET)
    assert calls == []


def test_authorization_is_single_use(tmp_path) -> None:
    calls = []
    gateway = JarvisEmergencyGateway(
        actor="jarvis",
        envelope=_envelope(Action.MERGE),
        public_key_armor="CASEY PUBLIC KEY",
        expected_fingerprint="CASEY-FINGERPRINT",
        change_id="casey-direction-20a637fe",
        verifier=lambda *_: True,
        operations={Action.MERGE: lambda target: calls.append(target) or "ok"},
        replay_store=tmp_path / "used",
    )

    assert gateway.merge(TARGET) == "ok"
    with pytest.raises(ValueError, match="already been consumed"):
        gateway.merge(TARGET)
    assert calls == [TARGET]


def test_unavailable_verification_fails_closed(tmp_path) -> None:
    calls = []

    def unavailable(*_args):
        raise RuntimeError("verification unavailable")

    gateway = JarvisEmergencyGateway(
        actor="jarvis",
        envelope=_envelope(Action.MERGE),
        public_key_armor="CASEY PUBLIC KEY",
        expected_fingerprint="CASEY-FINGERPRINT",
        change_id="casey-direction-20a637fe",
        verifier=unavailable,
        operations={Action.MERGE: lambda target: calls.append(target)},
        replay_store=tmp_path / "used",
    )

    with pytest.raises(RuntimeError, match="verification unavailable"):
        gateway.merge(TARGET)
    assert calls == []
