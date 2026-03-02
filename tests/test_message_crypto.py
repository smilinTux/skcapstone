"""Tests for skcapstone.message_crypto — AES-256-GCM message encryption.

Covers:
- encrypt_message / decrypt_message happy-path roundtrip
- Wrong key raises InvalidTag (authentication failure)
- Tampered ciphertext raises InvalidTag
- Short key raises ValueError
- pack_encrypted / unpack_encrypted envelope helpers
- is_encrypted_content detection
- decrypt_content pass-through for plaintext
- KMS-backed encrypt_content / decrypt_content roundtrip
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from skcapstone.message_crypto import (
    decrypt_content,
    decrypt_message,
    encrypt_content,
    encrypt_message,
    is_encrypted_content,
    pack_encrypted,
    unpack_encrypted,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def aes_key() -> bytes:
    """Return a random 32-byte AES key for testing."""
    return os.urandom(32)


@pytest.fixture
def kms_home(tmp_path: Path) -> Path:
    """Create a minimal agent home with identity for KMS key derivation."""
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir(parents=True)
    identity = {
        "name": "test-agent",
        "email": "test@skcapstone.local",
        "fingerprint": "DEADBEEF1234567890ABCDEF1234567890ABCDEF",
        "capauth_managed": False,
    }
    (identity_dir / "identity.json").write_text(json.dumps(identity), encoding="utf-8")
    (tmp_path / "security").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# Low-level encrypt/decrypt
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_roundtrip(aes_key):
    """Happy path: encrypt then decrypt returns the original plaintext."""
    plaintext = "Hello, sovereign world! 🔒"
    token = encrypt_message(plaintext, aes_key)
    assert isinstance(token, str)
    assert len(token) > 0
    result = decrypt_message(token, aes_key)
    assert result == plaintext


def test_encrypt_produces_different_ciphertext_each_call(aes_key):
    """Each encryption call uses a fresh nonce → different output."""
    msg = "repeat me"
    t1 = encrypt_message(msg, aes_key)
    t2 = encrypt_message(msg, aes_key)
    assert t1 != t2, "Nonces must differ — ciphertexts should not be identical"


def test_wrong_key_raises_on_decrypt(aes_key):
    """Decrypting with a different key raises InvalidTag (AES-GCM auth failure)."""
    from cryptography.exceptions import InvalidTag

    token = encrypt_message("secret", aes_key)
    wrong_key = os.urandom(32)
    with pytest.raises(InvalidTag):
        decrypt_message(token, wrong_key)


def test_tampered_ciphertext_raises(aes_key):
    """Bit-flipping the ciphertext causes authentication to fail."""
    import base64
    from cryptography.exceptions import InvalidTag

    token = encrypt_message("tamper me", aes_key)
    raw = bytearray(base64.b64decode(token))
    raw[-1] ^= 0xFF  # flip the last byte (inside the GCM tag)
    bad_token = base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(InvalidTag):
        decrypt_message(bad_token, aes_key)


def test_short_key_raises_value_error():
    """A key shorter than 32 bytes should raise ValueError immediately."""
    with pytest.raises(ValueError, match="32-byte"):
        encrypt_message("oops", b"too-short")


def test_short_key_decrypt_raises_value_error():
    """decrypt_message with a short key raises ValueError."""
    with pytest.raises(ValueError, match="32-byte"):
        decrypt_message("sometoken", b"too-short")


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------


def test_pack_unpack_roundtrip(aes_key):
    """pack_encrypted wraps token; unpack_encrypted recovers it."""
    token = encrypt_message("envelope test", aes_key)
    envelope = pack_encrypted(token)
    data = json.loads(envelope)
    assert data["skchat_encrypted"] is True
    assert data["v"] == 1
    assert data["ciphertext"] == token
    recovered = unpack_encrypted(envelope)
    assert recovered == token


def test_unpack_returns_none_for_plaintext():
    """unpack_encrypted on plain text returns None."""
    assert unpack_encrypted("just a normal message") is None


def test_unpack_returns_none_for_invalid_json():
    """unpack_encrypted on malformed JSON returns None."""
    assert unpack_encrypted("{not valid json") is None


def test_is_encrypted_content_true(aes_key):
    """is_encrypted_content returns True for a proper envelope."""
    token = encrypt_message("check me", aes_key)
    envelope = pack_encrypted(token)
    assert is_encrypted_content(envelope) is True


def test_is_encrypted_content_false_for_plaintext():
    """is_encrypted_content returns False for plain strings."""
    assert is_encrypted_content("hello world") is False
    assert is_encrypted_content("") is False
    assert is_encrypted_content('{"some": "json"}') is False


# ---------------------------------------------------------------------------
# KMS-backed API
# ---------------------------------------------------------------------------


def test_kms_encrypt_decrypt_roundtrip(kms_home):
    """encrypt_content → decrypt_content using KMS-derived key roundtrips cleanly."""
    plaintext = "sovereign message via KMS"
    envelope = encrypt_content(plaintext, kms_home)
    assert is_encrypted_content(envelope), "Output must be an encrypted envelope"
    result = decrypt_content(envelope, kms_home)
    assert result == plaintext


def test_decrypt_content_passthrough_for_plaintext(kms_home):
    """decrypt_content leaves non-encrypted content unchanged."""
    plain = "nothing to see here"
    result = decrypt_content(plain, kms_home)
    assert result == plain


def test_kms_same_key_produced_across_calls(kms_home):
    """Two derive_chat_key calls return identical material (deterministic HKDF)."""
    from skcapstone.message_crypto import derive_chat_key

    key1 = derive_chat_key(kms_home)
    key2 = derive_chat_key(kms_home)
    assert key1 == key2, "HKDF derivation must be deterministic for same identity"
    assert len(key1) == 32
