"""Signing uses the ACTING AGENT's capauth home, and refuses a broken keypair.

Found live on noroc2027 2026-08-14: every signed GTD event verified as
`invalid`. The signing code was fine. Two resolvers disagreed.

* ``capauth.resolve_agent_identity()`` is agent-aware and answered
  ``lumina / 02BC0EB3…``.
* ``capauth.resolve_capauth_home()`` is agent-BLIND and always answers
  ``~/.skcapstone/capauth``, the OPERATOR home.

So the envelope claimed lumina while the signature was made with whatever key
sat in the operator home. On this box that was a stray ``test-agent`` private
key next to the operator's real public key, so nothing verified. Meanwhile
lumina's own home held a perfectly healthy matching pair the signer never
looked at.

Two invariants come out of that:

1. Sign with the acting agent's own key when the agent has a capauth home, so
   the key that signs is the identity the envelope claims.
2. Never emit a signature that cannot be verified against the same home's
   public half. A signer that produces unverifiable signatures is worse than
   no signer: it manufactures false assurance, and the failure only surfaces
   at the verify boundary, usually much later and to someone else.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pgpy
import pytest
from pgpy.constants import (
    CompressionAlgorithm,
    EllipticCurveOID,
    HashAlgorithm,
    KeyFlags,
    PubKeyAlgorithm,
    SymmetricKeyAlgorithm,
)

import skcapstone.mcp_tools._helpers as _helpers
from skcapstone.fleet import signing


def _key(name: str, email: str) -> pgpy.PGPKey:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        k = pgpy.PGPKey.new(PubKeyAlgorithm.EdDSA, EllipticCurveOID.Ed25519)
        uid = pgpy.PGPUID.new(name, email=email)
        k.add_uid(
            uid,
            usage={KeyFlags.Sign, KeyFlags.Certify},
            hashes=[HashAlgorithm.SHA256],
            ciphers=[SymmetricKeyAlgorithm.AES256],
            compression=[CompressionAlgorithm.Uncompressed],
        )
    return k


def _write_identity(home: Path, private: pgpy.PGPKey, public: pgpy.PGPKey) -> Path:
    ident = home / "identity"
    ident.mkdir(parents=True, exist_ok=True)
    (ident / "private.asc").write_text(str(private), encoding="utf-8")
    (ident / "public.asc").write_text(str(public.pubkey), encoding="utf-8")
    return home


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """An operator home and an agent home, each with their own key."""
    monkeypatch.setenv("SKAGENT", "lumina")
    monkeypatch.delenv("CAPAUTH_PASSPHRASE", raising=False)
    root = tmp_path / "skcapstone"
    operator_key = _key("Operator", "operator@test.io")
    agent_key = _key("Lumina", "lumina@test.io")
    _write_identity(root / "capauth", operator_key, operator_key)
    _write_identity(root / "agents" / "lumina" / "capauth", agent_key, agent_key)
    # _shared_root() reads the module-level SHARED_ROOT constant, so the env
    # var alone does not move it. This is the patch pattern the rest of the
    # suite uses.
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(root))
    return root, operator_key, agent_key


def test_the_agent_home_is_preferred_over_the_operator_home(homes, monkeypatch):
    root, operator_key, agent_key = homes
    home = signing._capauth_home()
    assert home == root / "agents" / "lumina" / "capauth", (
        "signing must use the acting agent's home, or the envelope claims one "
        "identity while a different key signs it"
    )


def test_it_defers_to_capauths_resolver_when_the_agent_has_no_home(homes, monkeypatch):
    """An agent without its own capauth home must fall back, not invent a path.

    Asserted against capauth's resolver rather than a fixture path, because
    that resolver is deliberately agent-blind and does not honour the test
    root: pinning a tmp path here would assert a behaviour that does not exist.
    """
    from capauth import resolve_capauth_home

    root, _, _ = homes
    monkeypatch.setenv("SKAGENT", "nosuchagent")

    home = signing._capauth_home()
    assert home == resolve_capauth_home()
    assert home != root / "agents" / "nosuchagent" / "capauth"


def test_capauth_home_env_still_wins(homes, monkeypatch, tmp_path):
    """The explicit override must beat both, or tests and DR lose their escape."""
    override = tmp_path / "explicit"
    (override / "identity").mkdir(parents=True)
    monkeypatch.setenv("CAPAUTH_HOME", str(override))
    assert signing._capauth_home() == override


def test_a_mismatched_keypair_yields_no_signer(homes, monkeypatch, tmp_path):
    """The noroc2027 shape: a stray private key next to a real public key.

    Degrading to unsigned is correct. Emitting a signature nobody can verify
    is a lie that only surfaces at someone else's verify boundary.
    """
    root, _, _ = homes
    stranger = _key("Stray Test Agent", "test-agent@skcapstone.local")
    agent_home = root / "agents" / "lumina" / "capauth"
    (agent_home / "identity" / "private.asc").write_text(str(stranger), encoding="utf-8")

    assert signing.capauth_signer() is None


def test_a_matching_keypair_signs_and_verifies_round_trip(homes, monkeypatch):
    """The whole point: what this seat signs, this seat's roster verifies."""
    root, _, _ = homes

    sign = signing.capauth_signer()
    verify = signing.capauth_verifier()
    assert sign is not None and verify is not None

    payload = b'{"action":"capture","item_id":"abc"}'
    assert verify(payload, sign(payload)) is True
    assert verify(b'{"action":"tampered"}', sign(payload)) is False


def test_passphrase_reads_owner_only_systemd_credential(monkeypatch, tmp_path):
    credential = tmp_path / signing.SYSTEMD_CREDENTIAL_NAME
    credential.write_text("correct horse battery staple\n", encoding="utf-8")
    credential.chmod(0o600)
    monkeypatch.delenv("CAPAUTH_PASSPHRASE", raising=False)
    monkeypatch.delenv(signing.PASSPHRASE_FILE_ENV, raising=False)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))

    assert signing._passphrase() == "correct horse battery staple"


@pytest.mark.parametrize("mode", [0o640, 0o604])
def test_passphrase_rejects_group_or_world_access(monkeypatch, tmp_path, mode):
    credential = tmp_path / signing.SYSTEMD_CREDENTIAL_NAME
    credential.write_text("must-not-load", encoding="utf-8")
    credential.chmod(mode)
    monkeypatch.delenv("CAPAUTH_PASSPHRASE", raising=False)
    monkeypatch.setenv(signing.PASSPHRASE_FILE_ENV, str(credential))

    assert signing._passphrase() == ""


def test_passphrase_rejects_symlink(monkeypatch, tmp_path):
    target = tmp_path / "target"
    target.write_text("must-not-load", encoding="utf-8")
    target.chmod(0o600)
    credential = tmp_path / "credential"
    credential.symlink_to(target)
    monkeypatch.delenv("CAPAUTH_PASSPHRASE", raising=False)
    monkeypatch.setenv(signing.PASSPHRASE_FILE_ENV, str(credential))

    assert signing._passphrase() == ""
