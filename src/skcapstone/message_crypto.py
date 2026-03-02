"""
SKChat message encryption — AES-256-GCM content encryption.

Provides encrypt/decrypt for chat message content with a key derived
from the agent's KMS service key (label: 'skchat').

Wire format (JSON envelope):
    {
        "skchat_encrypted": true,
        "v": 1,
        "ciphertext": "<base64(nonce_12 || ciphertext+tag_16)>"
    }

Key derivation:
    KeyStore.derive_service_key("skchat") → 32-byte AES key

Usage:
    from skcapstone.message_crypto import encrypt_content, decrypt_content

    token = encrypt_content("hello", home)   # → JSON envelope str
    plain = decrypt_content(token, home)      # → "hello"
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.message_crypto")

# Marker key in the envelope JSON
_MARKER = "skchat_encrypted"
_VERSION = 1


# ---------------------------------------------------------------------------
# Low-level AES-256-GCM primitives (no KMS dependency)
# ---------------------------------------------------------------------------

def encrypt_message(plaintext: str, key: bytes) -> str:
    """Encrypt a plaintext string with AES-256-GCM.

    Args:
        plaintext: UTF-8 message content.
        key: 32-byte AES key.

    Returns:
        Base64-encoded bytes: nonce (12) || ciphertext+tag.

    Raises:
        ValueError: If key is not exactly 32 bytes.
    """
    if len(key) != 32:
        raise ValueError(f"AES-256 requires a 32-byte key, got {len(key)}")

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt_message(token: str, key: bytes) -> str:
    """Decrypt a base64 AES-256-GCM token.

    Args:
        token: Base64-encoded nonce||ciphertext+tag (from encrypt_message).
        key: 32-byte AES key.

    Returns:
        Decrypted plaintext string.

    Raises:
        ValueError: If key is not exactly 32 bytes.
        cryptography.exceptions.InvalidTag: If authentication fails (wrong key
            or tampered ciphertext).
    """
    if len(key) != 32:
        raise ValueError(f"AES-256 requires a 32-byte key, got {len(key)}")

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = base64.b64decode(token)
    if len(raw) < 12:
        raise ValueError("Ciphertext too short — must be at least 12 bytes (nonce)")

    nonce, ct = raw[:12], raw[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------

def pack_encrypted(ciphertext_b64: str) -> str:
    """Wrap a base64 ciphertext in the skchat_encrypted JSON envelope.

    Args:
        ciphertext_b64: Base64 token from encrypt_message.

    Returns:
        JSON string with the skchat_encrypted envelope.
    """
    return json.dumps({
        _MARKER: True,
        "v": _VERSION,
        "ciphertext": ciphertext_b64,
    })


def unpack_encrypted(content: str) -> Optional[str]:
    """Extract the base64 ciphertext from an encrypted envelope, or None.

    Args:
        content: Message content string (may or may not be an envelope).

    Returns:
        Base64 ciphertext if content is an encrypted envelope, else None.
    """
    try:
        data = json.loads(content)
        if data.get(_MARKER) is True and "ciphertext" in data:
            return data["ciphertext"]
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return None


def is_encrypted_content(content: str) -> bool:
    """Return True if content is an skchat_encrypted envelope.

    Args:
        content: Message content string.

    Returns:
        bool: True when the content carries an encrypted payload.
    """
    return unpack_encrypted(content) is not None


# ---------------------------------------------------------------------------
# KMS-backed key derivation
# ---------------------------------------------------------------------------

def derive_chat_key(home: Path) -> bytes:
    """Derive the skchat service key from the agent's KMS.

    Initializes the KeyStore (creating it if absent) and derives a
    deterministic 32-byte AES key for the 'skchat' service using
    HKDF-SHA256.

    Args:
        home: Agent home directory (~/.skcapstone).

    Returns:
        32-byte key material.

    Raises:
        RuntimeError: If key derivation fails.
    """
    try:
        from .kms import KeyStore

        store = KeyStore(home)
        store.initialize()
        record = store.derive_service_key("skchat")
        return store.get_key_material(record.key_id)
    except Exception as exc:
        raise RuntimeError(f"Failed to derive skchat key from KMS: {exc}") from exc


# ---------------------------------------------------------------------------
# Convenience API (KMS-aware)
# ---------------------------------------------------------------------------

def encrypt_content(plaintext: str, home: Path) -> str:
    """Encrypt message content using the agent's KMS-derived skchat key.

    Args:
        plaintext: Message text to encrypt.
        home: Agent home directory.

    Returns:
        JSON envelope string ready to use as message content.
    """
    key = derive_chat_key(home)
    token = encrypt_message(plaintext, key)
    return pack_encrypted(token)


def decrypt_content(content: str, home: Path) -> str:
    """Decrypt an encrypted message envelope using the KMS-derived skchat key.

    If the content is not an encrypted envelope, returns it unchanged
    (pass-through for plaintext messages).

    Args:
        content: Message content (envelope or plain).
        home: Agent home directory.

    Returns:
        Decrypted plaintext (or original content if not encrypted).
    """
    token = unpack_encrypted(content)
    if token is None:
        return content

    key = derive_chat_key(home)
    return decrypt_message(token, key)
