"""Tests for Card 3.5 signing primitives + signed store writes."""

from __future__ import annotations

import hashlib

import pytest

from skcapstone.fleet import signing, store


def fake_signer(data: bytes) -> str:
    return "sig:" + hashlib.sha256(data).hexdigest()


def fake_verifier(data: bytes, sig: str) -> bool:
    return sig == "sig:" + hashlib.sha256(data).hexdigest()


def test_mode_flag(monkeypatch) -> None:
    monkeypatch.delenv(signing.SIGNING_ENV, raising=False)
    assert signing.signing_mode() == "off"
    monkeypatch.setenv(signing.SIGNING_ENV, "permissive")
    assert signing.signing_mode() == "permissive"
    monkeypatch.setenv(signing.SIGNING_ENV, "enforce")
    assert signing.signing_mode() == "enforce"
    monkeypatch.setenv(signing.SIGNING_ENV, "garbage")
    assert signing.signing_mode() == "off"  # unknown value fails open


def test_canonical_bytes_ignore_only_the_signature() -> None:
    a = {
        "kind": "Service",
        "name": "s",
        "generation": 1,
        "writer": {"role": "operator", "node": "n", "identity": "i", "signature": None},
    }
    b = {**a, "writer": {**a["writer"], "signature": "sig:abc"}}
    assert signing.canonical_bytes(a) == signing.canonical_bytes(b)
    c = {**a, "generation": 2}
    assert signing.canonical_bytes(a) != signing.canonical_bytes(c)


def test_verify_payload_states() -> None:
    payload = {
        "kind": "Service",
        "name": "s",
        "writer": {"role": "operator", "node": "n", "identity": "i", "signature": None},
    }
    assert signing.verify_payload(payload, fake_verifier)[0] == "unsigned"
    signed = dict(payload)
    signed["writer"] = dict(
        payload["writer"], signature=fake_signer(signing.canonical_bytes(payload))
    )
    assert signing.verify_payload(signed, fake_verifier)[0] == "verified"
    tampered = dict(signed, name="evil")
    assert signing.verify_payload(tampered, fake_verifier)[0] == "invalid"

    def boom(data: bytes, sig: str) -> bool:
        raise RuntimeError("backend exploded")

    assert signing.verify_payload(signed, boom)[0] == "invalid"


def test_write_spec_signs_with_explicit_signer(paths, operator) -> None:
    payload = store.write_spec(
        paths, "service", "skgateway", {"unit": "u.service"}, writer=operator, signer=fake_signer
    )
    assert payload["writer"]["signature"].startswith("sig:")
    on_disk = store.read_spec(paths, "service", "skgateway")
    assert signing.verify_payload(on_disk, fake_verifier)[0] == "verified"


def test_write_placement_signs_with_explicit_signer(paths, scheduler_writer) -> None:
    payload, changed = store.write_placement(
        paths,
        "service",
        "skgateway",
        node="node-41",
        reason="r",
        writer=scheduler_writer,
        signer=fake_signer,
    )
    assert changed is True
    on_disk = store.read_placement(paths, "service", "skgateway")
    assert signing.verify_payload(on_disk, fake_verifier)[0] == "verified"


def test_writes_stay_unsigned_when_mode_off(paths, operator, monkeypatch) -> None:
    monkeypatch.delenv(signing.SIGNING_ENV, raising=False)
    payload = store.write_spec(paths, "node", "node-41", {}, writer=operator)
    assert payload["writer"]["signature"] is None  # exact Phase 1 behavior


def test_auto_sign_via_default_signer(paths, operator, monkeypatch) -> None:
    monkeypatch.setenv(signing.SIGNING_ENV, "permissive")
    monkeypatch.setattr(signing, "capauth_signer", lambda: fake_signer)
    payload = store.write_spec(paths, "node", "node-41", {}, writer=operator)
    assert payload["writer"]["signature"].startswith("sig:")
    assert (
        signing.verify_payload(store.read_spec(paths, "node", "node-41"), fake_verifier)[0]
        == "verified"
    )


def test_default_signer_none_when_key_missing(paths, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(signing.SIGNING_ENV, "enforce")
    monkeypatch.setenv("CAPAUTH_HOME", str(tmp_path / "empty-capauth"))
    assert signing.default_signer() is None  # no key material: no signer
    assert signing.capauth_verifier() is None  # no roster: no verifier


def test_load_roster_reads_local_key_files(monkeypatch, tmp_path) -> None:
    home = tmp_path / "capauth"
    (home / "identity").mkdir(parents=True)
    (home / "identity" / "public.asc").write_text("KEY-SELF")
    (home / "fleet-trust").mkdir()
    (home / "fleet-trust" / "chef.asc").write_text("KEY-CHEF")
    monkeypatch.setenv("CAPAUTH_HOME", str(home))
    assert sorted(signing.load_roster()) == ["KEY-CHEF", "KEY-SELF"]


def test_load_roster_handles_binary_and_unparseable_keys(monkeypatch, tmp_path) -> None:
    """Binary OpenPGP .asc files join the roster; unparseable ones are skipped."""
    pgpy = pytest.importorskip("pgpy")
    from pgpy.constants import (
        CompressionAlgorithm,
        HashAlgorithm,
        KeyFlags,
        PubKeyAlgorithm,
        SymmetricKeyAlgorithm,
    )

    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new("RosterTest", email="roster@skcapstone.local")
    key.add_uid(
        uid,
        usage={KeyFlags.Sign},
        hashes=[HashAlgorithm.SHA256],
        ciphers=[SymmetricKeyAlgorithm.AES256],
        compression=[CompressionAlgorithm.Uncompressed],
    )

    home = tmp_path / "capauth"
    (home / "identity").mkdir(parents=True)
    (home / "identity" / "public.asc").write_bytes(bytes(key.pubkey))
    (home / "fleet-trust").mkdir()
    (home / "fleet-trust" / "garbage.asc").write_bytes(b"\xff\x00not-a-key")
    (home / "fleet-trust" / "chef.asc").write_text("KEY-CHEF")
    monkeypatch.setenv("CAPAUTH_HOME", str(home))

    roster = signing.load_roster()

    assert "KEY-CHEF" in roster
    armored = [entry for entry in roster if entry.startswith("-----BEGIN PGP")]
    assert len(armored) == 1
    parsed, _ = pgpy.PGPKey.from_blob(armored[0])
    assert parsed.fingerprint == key.fingerprint
    assert not any("not-a-key" in entry for entry in roster)
