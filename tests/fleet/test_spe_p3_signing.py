"""SPE P3: populate the fleet writer signature, and stop claiming what is not true.

Card ``3c7134ab`` (epic ``373a33ca``, spec section 7). ``fleet/signing.py``
already ships the entire stack, so this is a mode flip and an attribution fix,
not a build.

Three things the card asks for, and one thing it warns about:

* ``writer.signature`` is populated and verifies, with ``sig.suite_id`` naming
  the suite so a future algorithm change is detectable rather than silent.
* ``fleet_act``'s hardcoded ``by: "atlas"`` goes through the identity resolver,
  because an audit entry that names a constant attributes nothing.
* The ``SIGNED`` claim in ``fleet_act``'s docstring must either become true or
  stop being made. An entry described as signed, that is not, is worse than an
  entry described as unsigned: it invites trust it has not earned.

Permissive is the whole posture here: signing is attempted, failure degrades
to unsigned, and nothing refuses to actuate. Enforcement stays a Chef-hand
flip (spec section 7), so this cannot brick the fleet.
"""

from __future__ import annotations

import json
import warnings

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
from skcapstone.fleet import signing, store


def _key(name: str, email: str) -> pgpy.PGPKey:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        k = pgpy.PGPKey.new(PubKeyAlgorithm.EdDSA, EllipticCurveOID.Ed25519)
        k.add_uid(
            pgpy.PGPUID.new(name, email=email),
            usage={KeyFlags.Sign, KeyFlags.Certify},
            hashes=[HashAlgorithm.SHA256],
            ciphers=[SymmetricKeyAlgorithm.AES256],
            compression=[CompressionAlgorithm.Uncompressed],
        )
    return k


@pytest.fixture
def seat(tmp_path, monkeypatch):
    """An agent seat with a healthy capauth home and signing permissive."""
    monkeypatch.setenv("SKAGENT", "lumina")
    monkeypatch.setenv("SKFLEET_SIGNING", "permissive")
    monkeypatch.delenv("CAPAUTH_PASSPHRASE", raising=False)
    root = tmp_path / "skcapstone"
    ident = root / "agents" / "lumina" / "capauth" / "identity"
    ident.mkdir(parents=True)
    key = _key("Lumina", "lumina@skworld.io")
    (ident / "private.asc").write_text(str(key), encoding="utf-8")
    (ident / "public.asc").write_text(str(key.pubkey), encoding="utf-8")
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(root))
    monkeypatch.delenv("CAPAUTH_HOME", raising=False)
    return root, key


# ── writer.signature is real ──────────────────────────────────────────────


def test_the_writer_signature_is_populated_in_permissive_mode(seat):
    payload = {"kind": "service", "name": "x", "spec": {"a": 1}}
    w = store.Writer(role="operator", node="n1", identity="capauth:lumina@skworld.io")
    signed = store._maybe_sign({**payload, "writer": store._writer_block(w)}, None)
    assert signed["writer"]["signature"], "permissive mode must actually sign"


def test_the_signature_verifies_against_this_seats_roster(seat):
    payload = {"kind": "service", "name": "x", "spec": {"a": 1}}
    w = store.Writer(role="operator", node="n1", identity="capauth:lumina@skworld.io")
    signed = store._maybe_sign({**payload, "writer": store._writer_block(w)}, None)

    verify = signing.capauth_verifier()
    assert verify is not None
    assert verify(signing.canonical_bytes(signed), signed["writer"]["signature"]) is True


def test_a_tampered_payload_no_longer_verifies(seat):
    payload = {"kind": "service", "name": "x", "spec": {"a": 1}}
    w = store.Writer(role="operator", node="n1", identity="capauth:lumina@skworld.io")
    signed = store._maybe_sign({**payload, "writer": store._writer_block(w)}, None)

    tampered = json.loads(json.dumps(signed))
    tampered["spec"]["a"] = 999
    verify = signing.capauth_verifier()
    assert verify(signing.canonical_bytes(tampered), signed["writer"]["signature"]) is False


def test_the_writer_block_names_its_suite(seat):
    """Without suite_id, an algorithm change is silent and unverifiable later."""
    w = store.Writer(role="operator", node="n1", identity="capauth:lumina@skworld.io")
    block = store._writer_block(w)
    assert block["suite_id"] == signing.SUITE_ID
    assert block["suite_id"]


def test_suite_id_is_covered_by_the_signature(seat):
    """A suite id an attacker can rewrite is decoration."""
    payload = {"kind": "service", "name": "x", "spec": {"a": 1}}
    w = store.Writer(role="operator", node="n1", identity="capauth:lumina@skworld.io")
    signed = store._maybe_sign({**payload, "writer": store._writer_block(w)}, None)

    swapped = json.loads(json.dumps(signed))
    swapped["writer"]["suite_id"] = "something-else-v9"
    verify = signing.capauth_verifier()
    assert verify(signing.canonical_bytes(swapped), signed["writer"]["signature"]) is False


# ── permissive never blocks ───────────────────────────────────────────────


def test_signing_failure_degrades_to_unsigned_and_still_writes(seat, monkeypatch):
    """Permissive means the write lands; enforcement is a separate, human flip."""

    def _boom():
        def _sign(_data):
            raise RuntimeError("smartcard removed")

        return _sign

    monkeypatch.setattr(signing, "capauth_signer", _boom)
    payload = {"kind": "service", "name": "x", "spec": {"a": 1}}
    w = store.Writer(role="operator", node="n1", identity="capauth:lumina@skworld.io")
    out = store._maybe_sign({**payload, "writer": store._writer_block(w)}, None)
    assert out["writer"]["signature"] is None
    assert out["spec"] == {"a": 1}


def test_off_mode_still_writes_nothing_into_the_signature_slot(seat, monkeypatch):
    monkeypatch.setenv("SKFLEET_SIGNING", "off")
    payload = {"kind": "service", "name": "x", "spec": {"a": 1}}
    w = store.Writer(role="operator", node="n1", identity="capauth:lumina@skworld.io")
    out = store._maybe_sign({**payload, "writer": store._writer_block(w)}, None)
    assert out["writer"]["signature"] is None


# ── attribution: no more hardcoded "atlas" ────────────────────────────────


def test_the_operator_action_entry_names_the_resolved_identity(seat, tmp_path, monkeypatch):
    """`by: "atlas"` was a constant: an audit line that attributes nothing."""
    from skcapstone.operator_seat import fleet_adapter

    monkeypatch.setattr(fleet_adapter, "_acting_identity", lambda: "capauth:lumina@skworld.io")
    entry = fleet_adapter._operator_action_entry(
        action="restart_service",
        now_iso="2026-08-14T00:00:00Z",
        classification={"change_class": "standard"},
        proposal={"rationale": "because"},
    )
    assert entry["by"] == "capauth:lumina@skworld.io"
    assert entry["by"] != "atlas"


def test_attribution_falls_back_rather_than_raising(seat, monkeypatch):
    """A resolver failure must not stop the fleet acting; it degrades."""
    from skcapstone.operator_seat import fleet_adapter

    def _boom():
        raise RuntimeError("capauth unavailable")

    monkeypatch.setattr(fleet_adapter, "_acting_identity", _boom)
    entry = fleet_adapter._operator_action_entry(
        action="restart_service",
        now_iso="2026-08-14T00:00:00Z",
        classification={"change_class": "standard"},
        proposal={},
    )
    assert entry["by"], "an unattributed entry is still better than no entry"


# ── the docstring must not overclaim ──────────────────────────────────────


def test_fleet_act_does_not_claim_the_entry_itself_is_signed():
    """The entry is not signed; the SPEC WRITE carries the writer signature.

    An entry described as SIGNED that is not invites trust it has not earned,
    which is worse than describing it as unsigned.
    """
    from skcapstone.operator_seat.fleet_adapter import fleet_act

    doc = fleet_act.__doc__ or ""
    assert "SIGNED entry" not in doc, "the operatorActions entry itself is not signed"
    assert (
        "signature" in doc.lower() or "signed" in doc.lower()
    ), "it should still say where the signature actually lives"


def test_the_suite_id_is_a_registered_id_not_an_invented_one():
    """PROVENANCE_AND_MUTATION_STANDARD s1: suite_id MUST come from the
    skcomms.crypto_suites registry. An invented id, however descriptive, is
    the "hardcoded primitive" anti-pattern (CRYPTO_AGILITY s4).

    This caught a real one: the first cut of this PR used
    "capauth-pgp-ed25519-v1", which is not registered anywhere.
    """
    registry = pytest.importorskip("skcomms.crypto_suites")
    registered = {s.suite_id for s in registry.all_suites()}
    assert (
        signing.SUITE_ID in registered
    ), f"{signing.SUITE_ID!r} is not in the crypto_suites registry: {sorted(registered)}"


def test_the_suite_id_names_the_algorithm_the_keys_actually_use():
    """ed25519-v1 is only correct while the identity keys are EdDSA. If the
    keys move to a PQ suite, this must move with them, not drift."""
    registry = pytest.importorskip("skcomms.crypto_suites")
    assert signing.SUITE_ID == registry.DEFAULT_SIG_SUITE
