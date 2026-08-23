"""SPE writer identity resolves to a canonical subject, never the literal
"operator" (coord card N9, `c974fa98`).

The autonomous operator seat hardcoded `identity="operator"` at two sites
(`operator_seat/cli.py::_seat_writer`, `operator_seat/fleet_adapter.py::
fleet_act`'s default writer) instead of resolving through capauth, the same
gap the spec calls out for P3 fleet adoption
(`docs/specs/2026-08-14-signed-provenance-envelope-arch.md:224-227`). Both
now call `store.resolved_writer_identity()`, which follows the settled
degrade-never-raise policy already proven at
`operator_seat/fleet_adapter.py::_operator_action_entry`: an unresolved
identity becomes the literal "unattributed", never a synthesized value.
"""

from __future__ import annotations

from skcapstone.fleet import store


class _FakeIdentity:
    def __init__(self, capauth_uri: str, agent: str = "") -> None:
        self.capauth_uri = capauth_uri
        self.agent = agent


def test_resolves_and_canonicalizes_a_real_identity(monkeypatch):
    """A resolvable capauth identity comes back canonical, not the raw wire form."""
    monkeypatch.setattr(
        "capauth.resolve_agent_identity",
        lambda: _FakeIdentity("capauth:lumina@skworld.io"),
    )
    assert store.resolved_writer_identity() == "lumina@chef.skworld.io"


def test_degrades_to_unattributed_when_resolver_raises(monkeypatch):
    """A resolver failure must never block a spec write; it degrades instead."""

    def _boom():
        raise RuntimeError("capauth unavailable")

    monkeypatch.setattr("capauth.resolve_agent_identity", _boom)
    assert store.resolved_writer_identity() == "unattributed"


def test_degrades_to_unattributed_when_canonicalization_fails(monkeypatch):
    """A resolved identity that fails the fqid grammar still must not raise."""
    monkeypatch.setattr(
        "capauth.resolve_agent_identity",
        lambda: _FakeIdentity("capauth:not a valid subject!"),
    )
    assert store.resolved_writer_identity() == "unattributed"


def test_degrades_to_unattributed_when_identity_is_empty(monkeypatch):
    """An identity that resolves to nothing at all is honest about knowing nothing."""
    monkeypatch.setattr(
        "capauth.resolve_agent_identity",
        lambda: _FakeIdentity("", agent=""),
    )
    assert store.resolved_writer_identity() == "unattributed"


def test_never_returns_the_retired_operator_literal(monkeypatch):
    """The whole point of the card: "operator" must never come back as identity,
    resolved or degraded."""

    def _boom():
        raise RuntimeError("no identity")

    monkeypatch.setattr("capauth.resolve_agent_identity", _boom)
    assert store.resolved_writer_identity() != "operator"

    monkeypatch.setattr(
        "capauth.resolve_agent_identity",
        lambda: _FakeIdentity("capauth:lumina@skworld.io"),
    )
    assert store.resolved_writer_identity() != "operator"
