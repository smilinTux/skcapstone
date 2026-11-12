"""SPE P2: GTD mutations carry a resolved actor identity, signed permissively.

Card ``163493a0`` (epic ``373a33ca``, spec section 6). P1 gave the store a
journal, but its ``writer`` is a bare ``SKAGENT`` string: a self-asserted name
with nothing behind it. ``capauth.resolve_agent_identity()`` has been available
the whole time and was simply never imported into a GTD write path.

The posture is PERMISSIVE and that is the point: provenance exists to empower
self-correction, so a capture must never fail because a key is missing, a
keyring is locked, or capauth is not installed. A resolve failure degrades to
an unsigned envelope with the raw agent name noted, never to a refused write.

``gtd verify`` counts what actually landed, per state, so degradation is
visible instead of silent:

* ``verified``  signature checks out against the local trust roster
* ``unsigned``  attributed but not signed (no signer available)
* ``invalid``   signature present, signer trusted here, and it does not verify
* ``unverifiable`` no roster, or signed by a key not in this node's roster
* ``pre-spe``   an event from before attribution existed (P1-era)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import skcapstone.mcp_tools._helpers as _helpers


@pytest.fixture(autouse=True)
def _isolate_gtd_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))
    monkeypatch.setenv("SKOS_ALLOW_EMPTY_STORE", "1")
    monkeypatch.setenv("SKAGENT", "lumina")


def _capture(text: str = "a thing") -> str:
    from skcapstone.mcp_tools.gtd_tools import _handle_gtd_capture

    return json.loads(asyncio.run(_handle_gtd_capture({"text": text}))[0].text)["id"]


def _events() -> list[dict]:
    from skcapstone import gtd_journal

    return gtd_journal.read_all()


# ── attribution ───────────────────────────────────────────────────────────


def test_every_mutation_records_a_resolved_actor():
    _capture("buy milk")
    ev = _events()[0]
    actor = ev["actor"]
    assert actor["agent"]  # resolved, not blank
    assert actor["capauth_uri"].startswith("capauth:")
    assert actor["node"]
    assert "resolved" in actor


def test_the_actor_block_carries_the_resolvers_answer_not_the_env_var(monkeypatch):
    """The env name is what P1 already had; P2 must record the identity behind it.

    The resolver is injected rather than read off the host: fqid comes from
    cluster.json, which a dev box has and a CI runner does not, so asserting on
    the ambient identity would make this pass or fail by machine.
    """
    from skcapstone import gtd_journal

    class _Ident:
        agent = "resolved-not-env"
        capauth_uri = "capauth:resolved-not-env@skworld.io"
        fqid = "resolved-not-env@chef.skworld.io"
        fingerprint = "DEADBEEF"

    monkeypatch.setattr(gtd_journal, "_resolve_identity", _Ident)
    ev = gtd_journal.append("capture", "i1", {"id": "i1"}, to="inbox")

    actor = ev["actor"]
    assert actor["resolved"] is True
    assert actor["agent"] == "resolved-not-env"  # NOT the SKAGENT env value
    assert ev["writer"] == "lumina"  # which is still recorded separately
    assert actor["capauth_uri"] == "capauth:resolved-not-env@skworld.io"
    assert actor["fqid"] == "resolved-not-env@chef.skworld.io"
    assert actor["fingerprint"] == "DEADBEEF"


def test_a_resolver_that_yields_no_fqid_still_counts_as_resolved():
    """A node with no cluster.json has an identity, just not a sovereign FQID.

    That is the CI runner's shape, and it must not read as a degradation.
    """
    from skcapstone import gtd_journal

    ev = gtd_journal.append("capture", "i2", {"id": "i2"}, to="inbox")
    assert ev["actor"]["resolved"] is True
    assert ev["actor"]["agent"]
    assert "fqid" in ev["actor"]  # present, possibly None


def test_a_resolver_failure_degrades_to_an_unsigned_envelope(monkeypatch):
    """Permissive posture: a capture NEVER fails because identity failed."""
    from skcapstone import gtd_journal

    def _boom():
        raise RuntimeError("capauth keyring is locked")

    monkeypatch.setattr(gtd_journal, "_resolve_identity", _boom)

    item_id = _capture("still captured")
    assert item_id  # the write landed

    ev = _events()[0]
    assert ev["actor"]["resolved"] is False
    assert ev["actor"]["agent"] == "lumina"  # falls back to the raw SKAGENT name
    assert ev["actor"]["degraded"]  # and says why
    assert ev.get("sig") is None


def test_attribution_never_blocks_a_write_even_if_the_whole_block_explodes(monkeypatch):
    from skcapstone import gtd_journal
    from skcapstone.mcp_tools.gtd_tools import _load_list

    def _boom(*a, **kw):
        raise RuntimeError("envelope construction failed")

    monkeypatch.setattr(gtd_journal, "_envelope", _boom)
    item_id = _capture("must still land")
    assert [it["id"] for it in _load_list("inbox")] == [item_id]


# ── signing ───────────────────────────────────────────────────────────────


def test_a_signed_event_carries_the_signature_and_suite(monkeypatch):
    from skcapstone import gtd_journal

    monkeypatch.setattr(gtd_journal, "_signer", lambda: (lambda data: "SIG:" + str(len(data))))
    _capture("signed")
    ev = _events()[0]
    assert ev["sig"]["signature"].startswith("SIG:")
    assert ev["sig"]["suite_id"]


def test_a_signer_failure_degrades_to_unsigned(monkeypatch):
    from skcapstone import gtd_journal

    def _bad_signer():
        def _sign(_data):
            raise RuntimeError("smartcard removed")

        return _sign

    monkeypatch.setattr(gtd_journal, "_signer", _bad_signer)
    item_id = _capture("unsigned but captured")
    assert item_id
    ev = _events()[0]
    assert ev.get("sig") is None
    assert ev["actor"]["agent"]  # attribution survives even when signing does not


def test_the_signature_covers_the_event_body(monkeypatch):
    """Changing any field must change the signed bytes, or the signature is decoration."""
    from skcapstone import gtd_journal

    a = gtd_journal.canonical_event_bytes({"action": "done", "item_id": "x", "sig": None})
    b = gtd_journal.canonical_event_bytes({"action": "done", "item_id": "y", "sig": None})
    assert a != b


def test_the_signature_slot_is_excluded_from_its_own_input():
    """Otherwise nothing could ever verify."""
    from skcapstone import gtd_journal

    body = {"action": "done", "item_id": "x"}
    unsigned = gtd_journal.canonical_event_bytes(dict(body))
    signed = gtd_journal.canonical_event_bytes({**body, "sig": {"signature": "abc"}})
    assert unsigned == signed


# ── gtd verify ────────────────────────────────────────────────────────────


def test_verify_counts_unsigned_events(monkeypatch):
    from skcapstone import gtd_journal

    # Forced: a dev box with a usable capauth key really does sign, so leaving
    # this to the environment would make the assertion depend on which machine
    # ran it.
    monkeypatch.setattr(gtd_journal, "_signer", lambda: None)
    _capture("one")
    _capture("two")
    report = gtd_journal.verify()
    assert report["counts"]["unsigned"] == 2
    assert report["counts"]["verified"] == 0
    assert report["total"] == 2


def test_verify_counts_pre_spe_events():
    """A P1-era event has no actor block at all and must not read as a failure."""
    from skcapstone import gtd_journal

    gtd_journal.journal_dir()
    path = gtd_journal.journal_dir() / "old@host.jsonl"
    path.write_text(
        json.dumps(
            {
                "event_id": "e1",
                "ts": "2026-08-01T00:00:00+00:00",
                "writer": "lumina",
                "seq": 0,
                "action": "capture",
                "item_id": "old1",
                "to": "inbox",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = gtd_journal.verify()
    assert report["counts"]["pre-spe"] == 1
    assert report["counts"]["invalid"] == 0


def test_verify_counts_verified_and_invalid(monkeypatch):
    from skcapstone import gtd_journal

    monkeypatch.setattr(gtd_journal, "_signer", lambda: (lambda data: "SIG"))
    _capture("signed one")
    # A verifier that accepts exactly the signature the signer produced.
    monkeypatch.setattr(gtd_journal, "_verifier", lambda: (lambda data, sig: sig == "SIG"))
    assert gtd_journal.verify()["counts"]["verified"] == 1

    monkeypatch.setattr(gtd_journal, "_verifier", lambda: (lambda data, sig: False))
    monkeypatch.setattr(gtd_journal, "_trusted_fingerprints", lambda: None)
    assert gtd_journal.verify()["counts"]["invalid"] == 1


def _signed_event_by(fingerprint: str, writer: str = "otherhost") -> None:
    from skcapstone import gtd_journal

    path = gtd_journal.journal_dir() / f"{writer}@{writer}.jsonl"
    path.write_text(
        json.dumps(
            {
                "event_id": f"ev-{writer}",
                "ts": "2026-10-03T21:54:01+00:00",
                "writer": writer,
                "seq": 0,
                "action": "capture",
                "item_id": "x1",
                "to": "inbox",
                "actor": {"agent": writer, "fingerprint": fingerprint, "resolved": True},
                "sig": {"suite_id": "capauth-pgp-v1", "signature": "OTHER"},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_verify_signer_missing_from_roster_is_unverifiable_not_invalid(monkeypatch):
    """A valid signature by a host key this node never installed is not tampering."""
    from skcapstone import gtd_journal

    _signed_event_by("946d76c00bc214f0191983228f6208ec736ca735", writer="chiwk13")
    monkeypatch.setattr(gtd_journal, "_verifier", lambda: (lambda data, sig: False))
    monkeypatch.setattr(gtd_journal, "_trusted_fingerprints", lambda: {"C8D406A4"})
    report = gtd_journal.verify()
    assert report["counts"]["invalid"] == 0
    assert report["counts"]["unverifiable"] == 1
    assert report["problems"] == []
    assert report["unknown_signers"] == [
        {
            "fingerprint": "946D76C00BC214F0191983228F6208EC736CA735",
            "writers": ["chiwk13"],
            "events": 1,
        }
    ]


def test_verify_trusted_signer_that_fails_is_still_invalid(monkeypatch):
    """The claimed key IS trusted here and the signature fails: that is the alarm."""
    from skcapstone import gtd_journal

    _signed_event_by("C8D406A46F2DF4894E4FB41580A638570C9D41C4", writer="jarvis")
    monkeypatch.setattr(gtd_journal, "_verifier", lambda: (lambda data, sig: False))
    monkeypatch.setattr(
        gtd_journal,
        "_trusted_fingerprints",
        lambda: {"C8D406A46F2DF4894E4FB41580A638570C9D41C4"},
    )
    report = gtd_journal.verify()
    assert report["counts"]["invalid"] == 1
    assert report["unknown_signers"] == []


def test_verify_without_a_verifier_reports_unknown_not_invalid(monkeypatch):
    """No trust roster is not the same as a bad signature. Never cry wolf."""
    from skcapstone import gtd_journal

    monkeypatch.setattr(gtd_journal, "_signer", lambda: (lambda data: "SIG"))
    _capture("signed")
    monkeypatch.setattr(gtd_journal, "_verifier", lambda: None)
    report = gtd_journal.verify()
    assert report["counts"]["invalid"] == 0
    assert report["counts"]["unverifiable"] == 1
    assert report["verifier_available"] is False


def test_verify_is_exposed_as_a_cli_verb(monkeypatch):
    import click
    from click.testing import CliRunner

    from skcapstone import gtd_journal
    from skcapstone.cli.gtd import register_gtd_commands

    monkeypatch.setattr(gtd_journal, "_signer", lambda: None)
    _capture("counted")

    @click.group()
    def main():
        pass

    register_gtd_commands(main)
    result = CliRunner().invoke(main, ["gtd", "verify"])
    assert result.exit_code == 0, result.output
    assert "unsigned" in result.output.lower()
