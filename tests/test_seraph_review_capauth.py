"""Request authentication, durable replay, and audit tests for Seraph publication."""

import json
from types import SimpleNamespace

import pytest

from skcapstone.fleet.operator_http import AuthResult
from skcapstone.seraph_review_capauth import (
    LEGACY_GRACE_ENV,
    SERAPH_FINGERPRINT,
    DurableNonceCache,
    JsonlAuditSink,
    SeraphCapAuthVerifier,
)
from skcapstone.seraph_review_contracts import ReviewPublicationError


def verifier(tmp_path, *, fingerprint=SERAPH_FINGERPRINT, allow=True, audit=True):
    def verify_request(method, path, body, headers, **kwargs):
        assert (method, path, body) == ("POST", "/skfleet/v1/seraph-review", b"a" * 64)
        return AuthResult(fingerprint, None, "ok")

    obligations = [SimpleNamespace(kind="audit")] if audit else []

    def decide(*args, **kwargs):
        return SimpleNamespace(
            allow=allow, reason="granted signed verified", obligations=obligations
        )

    return SeraphCapAuthVerifier(
        base_dir=tmp_path,
        nonce_store=DurableNonceCache(tmp_path / "runtime" / "nonces.sqlite3"),
        audit_sink=JsonlAuditSink(tmp_path / "runtime" / "audit.jsonl"),
        verify_request=verify_request,
        decide=decide,
    )


def call(value):
    return value.verify(
        {"headers": {"synthetic": "header"}},
        required_principal="capauth:seraph@skworld.io",
        required_capability="forge-review:publish",
        request_sha256="a" * 64,
    )


def test_exact_signed_verified_request_is_audited_before_allow(tmp_path):
    value = call(verifier(tmp_path))
    assert value.request_sha256 == "a" * 64
    record = json.loads((tmp_path / "runtime" / "audit.jsonl").read_text())
    assert record["allow"] is True and record["fingerprint"] == SERAPH_FINGERPRINT
    assert "signature" not in record and "token" not in record


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"fingerprint": "A" * 40}, "signed_request_invalid"),
        ({"allow": False}, "capability_denied"),
        ({"audit": False}, "capability_denied"),
    ],
)
def test_wrong_identity_denial_or_missing_obligation_fails_closed(tmp_path, changes, error):
    with pytest.raises(ReviewPublicationError, match=error):
        call(verifier(tmp_path, **changes))


def test_legacy_unsigned_grace_is_forbidden_even_when_pdp_allows(tmp_path, monkeypatch):
    monkeypatch.setenv(LEGACY_GRACE_ENV, "2099-01-01T00:00:00Z")
    with pytest.raises(ReviewPublicationError, match="legacy_grace_forbidden"):
        call(verifier(tmp_path))


def test_audit_failure_denies(tmp_path, monkeypatch):
    value = verifier(tmp_path)
    monkeypatch.setattr(value.audit_sink, "write", lambda record: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        call(value)


def test_nonce_replay_survives_new_cache_instance(tmp_path):
    path = tmp_path / "runtime" / "nonces.sqlite3"
    first = DurableNonceCache(path)
    assert first.check_and_record(SERAPH_FINGERPRINT, "one", 1000) is True
    second = DurableNonceCache(path)
    assert second.check_and_record(SERAPH_FINGERPRINT, "one", 1001) is False
    assert second.check_and_record(SERAPH_FINGERPRINT, "two", 1001) is True


def test_runtime_stores_require_private_directory(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o755)
    with pytest.raises(ReviewPublicationError, match="permissions"):
        DurableNonceCache(runtime / "nonces.sqlite3")
