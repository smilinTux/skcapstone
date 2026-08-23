"""Tests for `enabled: false` handling in check_all_services().

A backend the operator has formally decommissioned (skvector/Qdrant and
skgraph/FalkorDB both moved to the local Postgres stack on 2026-06-08) must not
be probed at all. Before the fix, `enabled: false` only skipped reading host/port
out of the yaml - execution then fell through to the localhost default and probed
anyway, filing a fresh false "down" incident on every health sweep.
"""

from __future__ import annotations

import pytest

from skcapstone import service_health


@pytest.fixture
def no_env(monkeypatch):
    """Clear the env overrides so the yaml fallback path is exercised."""
    for var in (
        "SKMEMORY_SKVECTOR_URL",
        "SKMEMORY_SKVECTOR_API_KEY",
        "SKMEMORY_SKGRAPH_HOST",
        "SKMEMORY_SKGRAPH_PORT",
        "SKCAPSTONE_DAEMON_URL",
        "SKCHAT_DAEMON_URL",
        "SYNCTHING_API_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def _stub_probes(monkeypatch):
    """Make every probe a no-op 'up' so only the skip logic is under test."""
    monkeypatch.setattr(
        service_health,
        "_http_check",
        lambda name, url, **kw: {"name": name, "status": "up", "error": None},
    )
    monkeypatch.setattr(
        service_health,
        "_tcp_check",
        lambda name, host, port: {"name": name, "status": "up", "error": None},
    )
    monkeypatch.setattr(
        service_health,
        "_pid_check",
        lambda name, path: {"name": name, "status": "up", "error": None},
    )
    monkeypatch.setattr(service_health, "_load_registry_entries", list)


def test_disabled_backends_are_not_probed(monkeypatch, no_env):
    """enabled: false means the service is absent from the results entirely."""
    _stub_probes(monkeypatch)
    monkeypatch.setattr(
        service_health,
        "_load_agent_yaml",
        lambda name, agent=None: {"enabled": False, "host": "dead.example", "port": 443},
    )

    names = {r["name"] for r in service_health.check_all_services()}

    assert "skvector (Qdrant)" not in names
    assert "skgraph (FalkorDB)" not in names


def test_enabled_backends_are_still_probed(monkeypatch, no_env):
    """Happy path: an enabled backend is probed as before."""
    _stub_probes(monkeypatch)
    monkeypatch.setattr(
        service_health,
        "_load_agent_yaml",
        lambda name, agent=None: {"enabled": True, "host": "localhost", "port": 6333},
    )

    names = {r["name"] for r in service_health.check_all_services()}

    assert "skvector (Qdrant)" in names
    assert "skgraph (FalkorDB)" in names


def test_env_override_wins_over_disabled_config(monkeypatch, no_env):
    """An explicit env URL still probes even when the yaml says disabled.

    This is the rollback path: the operator can re-point at a live endpoint
    without editing the (synced) yaml.
    """
    _stub_probes(monkeypatch)
    monkeypatch.setenv("SKMEMORY_SKVECTOR_URL", "http://localhost:6333")
    monkeypatch.setenv("SKMEMORY_SKGRAPH_HOST", "localhost")
    monkeypatch.setenv("SKMEMORY_SKGRAPH_PORT", "6379")
    monkeypatch.setattr(
        service_health,
        "_load_agent_yaml",
        lambda name, agent=None: {"enabled": False},
    )

    names = {r["name"] for r in service_health.check_all_services()}

    assert "skvector (Qdrant)" in names
    assert "skgraph (FalkorDB)" in names


class TestIncidentAuthorityNode:
    """Only the designated node may file health incidents into the shared store.

    The ITIL tree is Syncthing-synced but the probes are host-local, so a
    secondary node used to file false global outages for services it never
    hosted. See _incident_authority_node().
    """

    def test_unconfigured_allows_any_node(self, monkeypatch):
        """Default (no config, no env) preserves the old allow-all behaviour."""
        monkeypatch.delenv("SKCAPSTONE_HEALTH_INCIDENT_NODE", raising=False)
        monkeypatch.setattr(service_health, "_incident_authority_node", lambda: None)
        assert service_health._may_file_incidents() is True

    def test_designated_node_may_file(self, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_HEALTH_INCIDENT_NODE", "noroc2027")
        monkeypatch.setattr(service_health.socket, "gethostname", lambda: "noroc2027")
        assert service_health._may_file_incidents() is True

    def test_other_node_may_not_file(self, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_HEALTH_INCIDENT_NODE", "noroc2027")
        monkeypatch.setattr(
            service_health.socket, "gethostname", lambda: "cbrd21-laptop12thgenintelcore"
        )
        assert service_health._may_file_incidents() is False

    def test_non_authority_node_creates_no_incident(self, monkeypatch):
        """The gate is enforced at the creation call, not just the predicate."""
        monkeypatch.setenv("SKCAPSTONE_HEALTH_INCIDENT_NODE", "noroc2027")
        monkeypatch.setattr(service_health.socket, "gethostname", lambda: "some-other-box")

        called = []
        monkeypatch.setattr(
            service_health, "_failure_class", lambda e: called.append(e) or "unknown"
        )
        service_health._create_incident_for_down_service({"name": "skvoice", "error": "refused"})
        assert called == [], "incident creation must short-circuit before doing work"
