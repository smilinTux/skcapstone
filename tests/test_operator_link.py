"""Tests for human-operator manifest linking."""

from __future__ import annotations

import json
from pathlib import Path

from skcapstone.operator_link import build_agent_manifest, discover_human_operator


def test_discover_human_operator_reads_capauth_profile(tmp_path: Path) -> None:
    """A CapAuth human profile is converted into operator metadata."""
    capauth_home = tmp_path / ".capauth"
    profile = capauth_home / "identity" / "profile.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        json.dumps(
            {
                "entity": {
                    "name": "Casey",
                    "entity_type": "human",
                    "email": "casey@example.com",
                    "handle": "casey@example.com",
                },
                "key_info": {
                    "fingerprint": "ABCDEF1234567890",
                },
            }
        ),
        encoding="utf-8",
    )

    operator = discover_human_operator(capauth_home)

    assert operator == {
        "name": "Casey",
        "relationship": "human-operator",
        "entity_type": "human",
        "source": "capauth",
        "email": "casey@example.com",
        "handle": "casey@example.com",
        "fingerprint": "ABCDEF1234567890",
    }


def test_discover_human_operator_ignores_non_human_profile(tmp_path: Path) -> None:
    """AI CapAuth profiles are not treated as human operators."""
    capauth_home = tmp_path / ".capauth"
    profile = capauth_home / "identity" / "profile.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        json.dumps(
            {
                "entity": {
                    "name": "Jarvis",
                    "entity_type": "ai",
                }
            }
        ),
        encoding="utf-8",
    )

    assert discover_human_operator(capauth_home) is None


def test_build_agent_manifest_includes_operator_when_available() -> None:
    """Operator metadata is persisted directly in the manifest."""
    manifest = build_agent_manifest(
        "jarvis",
        "0.6.0",
        created_at="2026-01-01T00:00:00+00:00",
        operator={"name": "Casey", "fingerprint": "FP123", "relationship": "human-operator"},
    )

    assert manifest["name"] == "jarvis"
    assert manifest["entity_type"] == "ai-agent"
    assert manifest["operator"]["name"] == "Casey"
    assert manifest["operator"]["fingerprint"] == "FP123"


# ── keypair guard on the attestation signer (card fa9a…, noroc2027) ───────────
#
# create_operator_attestation signs with the OPERATOR home's private.asc. Unlike
# GTD/fleet signing (fixed in #115 by resolving the acting agent's home), this
# one CANNOT be repointed: an operator attestation is the human-authorizes-agent
# link, so the operator's key is the correct signer by definition.
#
# On noroc2027 that home held a stray `test-agent` private key next to the
# operator's real public key, so any attestation minted there would have been
# signed by a test key, unverifiable against the published operator key, and
# perfectly well-formed. The stakes are higher here than for an audit record:
# this artifact is an authorization grant.


def _keypair(name: str, email: str):
    import warnings

    import pgpy
    from pgpy.constants import (
        CompressionAlgorithm,
        EllipticCurveOID,
        HashAlgorithm,
        KeyFlags,
        PubKeyAlgorithm,
        SymmetricKeyAlgorithm,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        key = pgpy.PGPKey.new(PubKeyAlgorithm.EdDSA, EllipticCurveOID.Ed25519)
        key.add_uid(
            pgpy.PGPUID.new(name, email=email),
            usage={KeyFlags.Sign, KeyFlags.Certify},
            hashes=[HashAlgorithm.SHA256],
            ciphers=[SymmetricKeyAlgorithm.AES256],
            compression=[CompressionAlgorithm.Uncompressed],
        )
    return key


def _operator_home(tmp_path: Path, private_key, public_key) -> Path:
    home = tmp_path / "capauth"
    ident = home / "identity"
    ident.mkdir(parents=True, exist_ok=True)
    (ident / "private.asc").write_text(str(private_key), encoding="utf-8")
    (ident / "public.asc").write_text(str(public_key.pubkey), encoding="utf-8")
    (ident / "profile.json").write_text(
        json.dumps(
            {
                "entity": {
                    "name": "Chef",
                    "entity_type": "human",
                    "email": "chef@skworld.io",
                    "handle": "chef@skworld.io",
                },
                "key_info": {
                    "fingerprint": str(public_key.fingerprint).replace(" ", ""),
                    "public_key_path": "identity/public.asc",
                    "private_key_path": "identity/private.asc",
                },
                "storage": {"primary": str(home)},
            }
        ),
        encoding="utf-8",
    )
    return home


def test_attestation_is_refused_when_the_operator_keypair_does_not_match(tmp_path: Path) -> None:
    """The noroc2027 shape: a stray private key beside the real public key."""
    from skcapstone.operator_link import create_operator_attestation

    operator = _keypair("Chef", "chef@skworld.io")
    stray = _keypair("test-agent", "test-agent@skcapstone.local")
    home = _operator_home(tmp_path, private_key=stray, public_key=operator)

    agent_pub = tmp_path / "agent-public.asc"
    agent_pub.write_text(str(_keypair("Lumina", "lumina@skworld.io").pubkey), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()

    result = create_operator_attestation(
        agent_name="lumina",
        agent_fingerprint="0123456789ABCDEF0123456789ABCDEF01234567",
        agent_public_key_path=agent_pub,
        output_dir=out,
        capauth_home=home,
    )

    assert result is None, "a mismatched operator keypair must not mint an attestation"
    assert not (out / "operator-attestation.json").exists(), "no artifact may be written"


def test_attestation_is_produced_when_the_operator_keypair_matches(tmp_path: Path) -> None:
    """The guard must not block the legitimate path."""
    from skcapstone.operator_link import create_operator_attestation

    operator = _keypair("Chef", "chef@skworld.io")
    home = _operator_home(tmp_path, private_key=operator, public_key=operator)

    agent_pub = tmp_path / "agent-public.asc"
    agent_pub.write_text(str(_keypair("Lumina", "lumina@skworld.io").pubkey), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()

    result = create_operator_attestation(
        agent_name="lumina",
        agent_fingerprint="0123456789ABCDEF0123456789ABCDEF01234567",
        agent_public_key_path=agent_pub,
        output_dir=out,
        capauth_home=home,
    )

    assert result is not None
    assert (out / "operator-attestation.json").exists()
