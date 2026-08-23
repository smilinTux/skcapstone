"""Tests for skcapstone.key_io public key readers (card 4aa2b5c7)."""

from __future__ import annotations

import pytest

from skcapstone.key_io import armor_binary_key, read_armored_public_key


def _new_pgp_key():
    """Build a synthetic throwaway PGP key for fixture files."""
    pgpy = pytest.importorskip("pgpy")
    from pgpy.constants import (
        CompressionAlgorithm,
        HashAlgorithm,
        KeyFlags,
        PubKeyAlgorithm,
        SymmetricKeyAlgorithm,
    )

    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new("KeyIoTest", email="keyio@skcapstone.local")
    key.add_uid(
        uid,
        usage={KeyFlags.Sign},
        hashes=[HashAlgorithm.SHA256],
        ciphers=[SymmetricKeyAlgorithm.AES256],
        compression=[CompressionAlgorithm.Uncompressed],
    )
    return pgpy, key


def test_armored_text_passthrough(tmp_path):
    """An ASCII-armored file reads back as stripped text."""
    path = tmp_path / "public.asc"
    path.write_text("-----BEGIN PGP PUBLIC KEY BLOCK-----\nfake\n", encoding="utf-8")
    assert read_armored_public_key(path) == "-----BEGIN PGP PUBLIC KEY BLOCK-----\nfake"


def test_binary_key_is_armored(tmp_path):
    """A binary OpenPGP public.asc converts to parseable ASCII armor."""
    pgpy, key = _new_pgp_key()
    path = tmp_path / "public.asc"
    path.write_bytes(bytes(key.pubkey))

    armored = read_armored_public_key(path)

    assert armored.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    parsed, _ = pgpy.PGPKey.from_blob(armored)
    assert parsed.fingerprint == key.fingerprint


def test_garbage_binary_returns_empty(tmp_path):
    """A non-key binary file yields an empty result, never an exception."""
    pytest.importorskip("pgpy")
    path = tmp_path / "public.asc"
    path.write_bytes(b"\xff\x00\x01binary-garbage-not-a-key")
    assert read_armored_public_key(path) == ""
    assert armor_binary_key(path) == ""


def test_missing_file_returns_empty(tmp_path):
    """A missing file yields an empty result, never an exception."""
    assert read_armored_public_key(tmp_path / "nope.asc") == ""
