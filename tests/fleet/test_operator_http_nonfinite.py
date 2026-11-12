"""Regression tests for nonfinite signed-request timestamps."""

import hashlib

import pytest

from skcapstone.fleet import operator_http as oh


def _sign(data: bytes) -> str:
    return "sig:" + hashlib.sha256(data + b"key").hexdigest()


def _verify(data: bytes, signature: str, key: str) -> bool:
    return key == "key" and signature == _sign(data)


def _headers(timestamp: str) -> dict[str, str]:
    body = b""
    data = oh.canonical_request_bytes("GET", "/x", body, timestamp, "nonce")
    return {
        "X-SK-Fingerprint": "AAAA",
        "X-SK-Timestamp": timestamp,
        "X-SK-Nonce": "nonce",
        "X-SK-Signature": _sign(data),
    }


@pytest.mark.parametrize("timestamp", ["nan", "+nan", "-nan", "inf", "+inf", "-inf"])
def test_verify_signed_request_nonfinite_timestamp_refused(timestamp):
    result = oh.verify_signed_request(
        "GET",
        "/x",
        b"",
        _headers(timestamp),
        roster_by_fpr=lambda: {"AAAA": "key"},
        verify_one=_verify,
        now=123.0,
    )
    assert result.reason == oh.REASON_SIGNATURE_INVALID


def test_verify_signed_request_nonfinite_clock_refused():
    result = oh.verify_signed_request(
        "GET",
        "/x",
        b"",
        _headers("123"),
        roster_by_fpr=lambda: {"AAAA": "key"},
        verify_one=_verify,
        now=float("nan"),
    )
    assert result.reason == oh.REASON_SIGNATURE_INVALID
