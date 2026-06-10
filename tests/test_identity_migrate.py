"""Tests for ``skcapstone identity migrate`` (skcomms T2 migration walker).

The walker backfills realm/operator/fqid/pgp_fingerprint into every
provisioned agent's identity.json. These tests use a tmp ``~/.skcapstone`` home
with fixture agents + cluster.json — they NEVER touch the real home. The
canonical resolver (``capauth.resolve_agent_identity``) is patched so fqid and
fingerprint are deterministic and no real profile/cluster is read.

Covers:
- bare identity.json gets realm/operator/fqid/pgp_fingerprint added
- already-complete identity is unchanged (idempotent)
- dry-run (the default) writes nothing
- template / non-capauth agents are skipped
- missing cluster.json is handled gracefully
- CLI integration (default is dry-run; --apply writes)
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from skcapstone.cli.identity_cmd import migrate_identities, register_identity_commands

# Patch target: the name as imported inside identity_cmd's _plan_agent.
_RESOLVER = "capauth.resolve_agent_identity"


def _fake_ident(agent: str, *, fqid="{a}@chef.skworld", fingerprint="A" * 40):
    """Build a fake AgentIdentity-like object for a given agent."""
    return SimpleNamespace(
        agent=agent,
        capauth_uri=f"capauth:{agent}@skworld.io",
        fqid=fqid.format(a=agent) if fqid else None,
        fingerprint=fingerprint,
    )


def _mk_agent(home, name, *, capauth=True, identity_payload=None):
    """Create an agent dir under home/agents with optional capauth + identity."""
    adir = home / "agents" / name
    (adir / "identity").mkdir(parents=True, exist_ok=True)
    if capauth:
        (adir / "capauth").mkdir(parents=True, exist_ok=True)
    if identity_payload is not None:
        (adir / "identity" / "identity.json").write_text(json.dumps(identity_payload))
    return adir


@pytest.fixture
def home_with_cluster(tmp_path):
    """A tmp shared root with cluster.json + one bare-identity provisioned agent."""
    home = tmp_path / ".skcapstone"
    home.mkdir(parents=True)
    (home / "cluster.json").write_text(json.dumps({
        "realm": "skworld", "operator": "chef",
    }))
    _mk_agent(home, "lumina", identity_payload={
        "name": "Lumina", "capauth_managed": True,
        "capauth_uri": "capauth:lumina@skworld.io",
    })
    return home


# ---------------------------------------------------------------------------
# core walker: migrate_identities
# ---------------------------------------------------------------------------


class TestMigrateWalker:
    """migrate_identities core behaviour."""

    def test_bare_identity_gets_all_fields(self, home_with_cluster):
        """A bare identity.json gains realm/operator/fqid/pgp_fingerprint."""
        with patch(_RESOLVER, side_effect=lambda a: _fake_ident(a)):
            plan = migrate_identities(home_with_cluster, apply=True)

        ap = plan.agents[0]
        assert ap.agent == "lumina"
        assert ap.applied is True
        data = json.loads(ap.path.read_text())
        assert data["realm"] == "skworld"
        assert data["operator"] == "chef"
        assert data["fqid"] == "lumina@chef.skworld"
        assert data["pgp_fingerprint"] == "A" * 40
        # Unrelated fields preserved (merge, not clobber).
        assert data["name"] == "Lumina"
        assert data["capauth_uri"] == "capauth:lumina@skworld.io"

    def test_idempotent_already_complete(self, tmp_path):
        """A complete identity is reported unchanged and not rewritten."""
        home = tmp_path / ".skcapstone"
        home.mkdir(parents=True)
        (home / "cluster.json").write_text(json.dumps({
            "realm": "skworld", "operator": "chef",
        }))
        payload = {
            "name": "Opus", "capauth_managed": True,
            "realm": "skworld", "operator": "chef",
            "fqid": "opus@chef.skworld", "pgp_fingerprint": "B" * 40,
        }
        adir = _mk_agent(home, "opus", identity_payload=payload)
        ident_path = adir / "identity" / "identity.json"
        mtime_before = ident_path.stat().st_mtime

        with patch(_RESOLVER, side_effect=lambda a: _fake_ident(
            a, fqid="opus@chef.skworld", fingerprint="B" * 40
        )):
            plan = migrate_identities(home, apply=True)

        ap = plan.agents[0]
        assert ap.changed is False
        assert plan.changed_count == 0
        assert plan.unchanged_count == 1
        assert ident_path.stat().st_mtime == mtime_before  # not rewritten

    def test_dry_run_writes_nothing(self, home_with_cluster):
        """Default (apply=False) computes a plan but writes nothing."""
        ident_path = home_with_cluster / "agents" / "lumina" / "identity" / "identity.json"
        before = ident_path.read_text()

        with patch(_RESOLVER, side_effect=lambda a: _fake_ident(a)):
            plan = migrate_identities(home_with_cluster, apply=False)

        assert plan.dry_run is True
        ap = plan.agents[0]
        assert ap.changed is True            # plan SHOWS the additions
        assert ap.applied is False           # but did not write
        assert ident_path.read_text() == before
        # The would-be additions are still surfaced for the diff.
        assert "fqid" in ap.additions

    def test_templates_and_noncapauth_skipped(self, home_with_cluster):
        """*-template and non-capauth agents are excluded from the walk."""
        _mk_agent(home_with_cluster, "lumina-template", identity_payload={"name": "T"})
        _mk_agent(home_with_cluster, "scaffold", capauth=False, identity_payload={"name": "S"})

        with patch(_RESOLVER, side_effect=lambda a: _fake_ident(a)):
            plan = migrate_identities(home_with_cluster, apply=True)

        names = {a.agent for a in plan.agents}
        assert names == {"lumina"}

    def test_missing_cluster_graceful(self, tmp_path):
        """No cluster.json: realm/operator are skipped, fqid/fingerprint still tried."""
        home = tmp_path / ".skcapstone"
        home.mkdir(parents=True)
        _mk_agent(home, "lumina", identity_payload={"name": "Lumina"})

        # Resolver returns no fqid (cluster-derived) but does have a fingerprint.
        with patch(_RESOLVER, side_effect=lambda a: _fake_ident(
            a, fqid=None, fingerprint="C" * 40
        )):
            plan = migrate_identities(home, apply=True)

        assert plan.cluster_found is False
        ap = plan.agents[0]
        # realm/operator/fqid absent (no cluster), but pgp_fingerprint added.
        assert "realm" not in ap.additions
        assert "operator" not in ap.additions
        assert "fqid" not in ap.additions
        assert ap.additions.get("pgp_fingerprint") == "C" * 40

    def test_no_provisioned_agents(self, tmp_path):
        """Empty home yields an empty plan, not a crash."""
        home = tmp_path / ".skcapstone"
        home.mkdir(parents=True)
        plan = migrate_identities(home, apply=True)
        assert plan.agents == []
        assert plan.changed_count == 0

    def test_unreadable_identity_is_error_not_crash(self, home_with_cluster):
        """A corrupt identity.json is reported as an error, not raised."""
        ident_path = home_with_cluster / "agents" / "lumina" / "identity" / "identity.json"
        ident_path.write_text("{ not json")

        with patch(_RESOLVER, side_effect=lambda a: _fake_ident(a)):
            plan = migrate_identities(home_with_cluster, apply=True)

        ap = plan.agents[0]
        assert ap.error
        assert ap.applied is False


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


@pytest.fixture
def cli():
    """A minimal Click group with only the identity commands registered."""
    import click

    @click.group()
    def root():
        pass

    register_identity_commands(root)
    return root


class TestMigrateCLI:
    """skcapstone identity migrate CLI."""

    def test_default_is_dry_run(self, cli, home_with_cluster):
        """Invoking without --apply writes nothing (dry-run is the default)."""
        ident_path = home_with_cluster / "agents" / "lumina" / "identity" / "identity.json"
        before = ident_path.read_text()

        with patch(_RESOLVER, side_effect=lambda a: _fake_ident(a)):
            result = CliRunner().invoke(
                cli, ["identity", "migrate", "--home", str(home_with_cluster)]
            )

        assert result.exit_code == 0, result.output
        assert "DRY-RUN" in result.output
        assert ident_path.read_text() == before

    def test_apply_writes(self, cli, home_with_cluster):
        """--apply actually writes the backfilled fields."""
        ident_path = home_with_cluster / "agents" / "lumina" / "identity" / "identity.json"

        with patch(_RESOLVER, side_effect=lambda a: _fake_ident(a)):
            result = CliRunner().invoke(
                cli,
                ["identity", "migrate", "--home", str(home_with_cluster), "--apply"],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(ident_path.read_text())
        assert data["fqid"] == "lumina@chef.skworld"
        assert data["pgp_fingerprint"] == "A" * 40

    def test_json_out(self, cli, home_with_cluster):
        """--json-out emits a machine-readable plan."""
        with patch(_RESOLVER, side_effect=lambda a: _fake_ident(a)):
            result = CliRunner().invoke(
                cli,
                ["identity", "migrate", "--home", str(home_with_cluster), "--json-out"],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["dry_run"] is True
        assert payload["agents"][0]["agent"] == "lumina"
