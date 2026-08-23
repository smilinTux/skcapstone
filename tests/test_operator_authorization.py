"""Tests for scoped ATLAS authorization envelopes."""

from datetime import datetime, timedelta, timezone

import pytest

from skcapstone.operator_authorization import (
    AuthorizationEnvelope,
    AuthorizationError,
    authorization_id,
    consume_authorization,
    verify_authorization,
)


def _envelope() -> AuthorizationEnvelope:
    now = datetime.now(timezone.utc)
    env = AuthorizationEnvelope(
        authorization_id="pending",
        issuer="chef",
        issuer_role="owner",
        issuer_fingerprint="A" * 40,
        action="itil.cab.vote.approved",
        target="cmdb-network-reconcile",
        change_id="chg-a543c87b",
        scope="scope-123",
        issued_at=(now - timedelta(seconds=1)).isoformat(),
        expires_at=(now + timedelta(minutes=5)).isoformat(),
        nonce="a_secure_random_nonce_1234",
        signature="signed",
    )
    env.authorization_id = authorization_id(env)
    return env


def _verify(env: AuthorizationEnvelope) -> None:
    verify_authorization(
        env,
        public_key_armor="pub",
        verifier=lambda payload, signature, public: signature == "signed",
        expected_action="itil.cab.vote.approved",
        expected_target="cmdb-network-reconcile",
        expected_change_id="chg-a543c87b",
        expected_scope="scope-123",
    )


def test_exact_bound_authorization_verifies() -> None:
    _verify(_envelope())


@pytest.mark.parametrize("field,value", [("change_id", "chg-other"), ("scope", "other")])
def test_rebinding_is_rejected(field: str, value: str) -> None:
    env = _envelope()
    setattr(env, field, value)
    with pytest.raises(AuthorizationError):
        _verify(env)


def test_authorization_is_single_use(tmp_path) -> None:
    env = _envelope()
    consume_authorization(env, tmp_path)
    with pytest.raises(AuthorizationError, match="already"):
        consume_authorization(env, tmp_path)
