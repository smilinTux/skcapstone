"""Tests for non-interactive lightweight agent provisioning.

Covers:
- slugify_name() normalization
- default_agent_home() layout
- provision_lightweight_agent() happy path (identity.json, profile.yaml, MANDATE.md)
- role templates (reviewer / worker / custom), custom mandate, --no-mandate
- failure cases (existing profile without force, empty slug)
- the `skcapstone init --non-interactive` CLI surface
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from skcapstone.cli import main
from skcapstone.lightweight import (
    default_agent_home,
    mandate_template,
    provision_lightweight_agent,
    slugify_name,
)

# ---------------------------------------------------------------------------
# slugify_name
# ---------------------------------------------------------------------------


class TestSlugifyName:
    """Slug normalization for agent directory names."""

    def test_simple_name(self) -> None:
        """Lowercases a plain name."""
        assert slugify_name("Veritas") == "veritas"

    def test_spaces_become_hyphens(self) -> None:
        """Spaces and unsafe characters collapse to single hyphens."""
        assert slugify_name("Review Bot 2") == "review-bot-2"

    def test_empty_slug_raises(self) -> None:
        """A name with no usable characters is rejected."""
        with pytest.raises(ValueError):
            slugify_name("!!!")


# ---------------------------------------------------------------------------
# default_agent_home
# ---------------------------------------------------------------------------


class TestDefaultAgentHome:
    """Conventional home layout under the shared root."""

    def test_under_shared_root_agents(self, tmp_path: Path) -> None:
        """Home resolves to <shared_root>/agents/<slug>."""
        home = default_agent_home("Veritas", shared_root=tmp_path)
        assert home == tmp_path / "agents" / "veritas"


# ---------------------------------------------------------------------------
# provision_lightweight_agent - happy path
# ---------------------------------------------------------------------------


class TestProvisionHappyPath:
    """The scaffold matches the hand-built veritas reference layout."""

    def test_creates_identity_profile_mandate(self, tmp_path: Path) -> None:
        """All three artifacts are written under the agent home."""
        home = tmp_path / "agents" / "veritas"
        result = provision_lightweight_agent("Veritas", role="reviewer", home=home)

        assert result.agent == "veritas"
        assert result.role == "reviewer"
        assert (home / "identity" / "identity.json").is_file()
        assert (home / "profile.yaml").is_file()
        assert (home / "MANDATE.md").is_file()
        assert len(result.files) == 3

    def test_identity_document_fields(self, tmp_path: Path) -> None:
        """identity.json records name, role, mandate, and non-capauth status."""
        home = tmp_path / "agents" / "veritas"
        provision_lightweight_agent("Veritas", role="reviewer", home=home)

        doc = json.loads((home / "identity" / "identity.json").read_text(encoding="utf-8"))
        assert doc["name"] == "Veritas"
        assert doc["role"] == "reviewer"
        assert doc["profile"] == "lightweight"
        assert doc["capauth_managed"] is False
        assert doc["mandate"]
        assert doc["created_at"]
        assert doc["created_by"]

    def test_profile_yaml_matches_agent_profile_init_shape(self, tmp_path: Path) -> None:
        """profile.yaml carries the bridge-curation block the bridge reads."""
        home = tmp_path / "agents" / "worker1"
        provision_lightweight_agent("worker1", home=home)

        doc = yaml.safe_load((home / "profile.yaml").read_text(encoding="utf-8"))
        assert doc["agent"] == "worker1"
        assert doc["bridge"]["tools"] == "default"
        assert doc["bridge"]["voice_reply"] == "voice"
        assert "skcapstone agent profile --agent worker1" in doc["_note"]

    def test_gather_profile_reads_provisioned_home(self, tmp_path: Path) -> None:
        """The capability manifest aggregator accepts a lightweight home."""
        from skcapstone.cli.agent_profile_cmd import gather_profile

        home = tmp_path / "agents" / "veritas"
        provision_lightweight_agent("Veritas", role="reviewer", home=home)

        manifest = gather_profile(home, "veritas")
        assert manifest["agent"] == "veritas"
        assert manifest["bridge"]["tools"] == "default"


# ---------------------------------------------------------------------------
# Role templates and mandate handling
# ---------------------------------------------------------------------------


class TestMandates:
    """Role templates and custom mandate text."""

    def test_reviewer_template_content(self, tmp_path: Path) -> None:
        """Reviewer template enforces separation of duties."""
        home = tmp_path / "agents" / "veritas"
        provision_lightweight_agent("Veritas", role="reviewer", home=home)
        text = (home / "MANDATE.md").read_text(encoding="utf-8")
        assert "Separation of duties" in text
        assert "NEVER review a card this agent implemented" in text

    def test_worker_template_content(self, tmp_path: Path) -> None:
        """Worker template mentions worktree isolation and evidence reporting."""
        home = tmp_path / "agents" / "builder"
        provision_lightweight_agent("builder", role="worker", home=home)
        text = (home / "MANDATE.md").read_text(encoding="utf-8")
        assert "isolated worktree" in text

    def test_custom_role_gets_generic_template(self) -> None:
        """Unknown roles still get a usable template."""
        text = mandate_template("Scout", "scout")
        assert "`scout`" in text

    def test_custom_mandate_text(self, tmp_path: Path) -> None:
        """A custom mandate lands in both identity.json and MANDATE.md."""
        home = tmp_path / "agents" / "veritas"
        provision_lightweight_agent(
            "Veritas", role="reviewer", home=home, mandate="Verify all the things."
        )
        doc = json.loads((home / "identity" / "identity.json").read_text(encoding="utf-8"))
        assert doc["mandate"] == "Verify all the things."
        assert "Verify all the things." in (home / "MANDATE.md").read_text(encoding="utf-8")

    def test_no_mandate_skips_file(self, tmp_path: Path) -> None:
        """write_mandate=False writes only identity.json and profile.yaml."""
        home = tmp_path / "agents" / "veritas"
        result = provision_lightweight_agent("Veritas", home=home, write_mandate=False)
        assert not (home / "MANDATE.md").exists()
        assert len(result.files) == 2


# ---------------------------------------------------------------------------
# Failure cases
# ---------------------------------------------------------------------------


class TestProvisionFailures:
    """Edge and failure behavior."""

    def test_existing_profile_refused_without_force(self, tmp_path: Path) -> None:
        """Re-provisioning an existing agent raises FileExistsError."""
        home = tmp_path / "agents" / "veritas"
        provision_lightweight_agent("Veritas", home=home)
        with pytest.raises(FileExistsError):
            provision_lightweight_agent("Veritas", home=home)

    def test_force_overwrites(self, tmp_path: Path) -> None:
        """force=True rewrites the identity document."""
        home = tmp_path / "agents" / "veritas"
        provision_lightweight_agent("Veritas", role="worker", home=home)
        provision_lightweight_agent("Veritas", role="reviewer", home=home, force=True)
        doc = json.loads((home / "identity" / "identity.json").read_text(encoding="utf-8"))
        assert doc["role"] == "reviewer"

    def test_unusable_name_raises(self, tmp_path: Path) -> None:
        """A name that slugifies to nothing raises ValueError."""
        with pytest.raises(ValueError):
            provision_lightweight_agent("!!!", home=tmp_path / "agents" / "x")


# ---------------------------------------------------------------------------
# CLI: skcapstone init --non-interactive
# ---------------------------------------------------------------------------


class TestInitNonInteractiveCli:
    """The non-interactive surface on the init command."""

    def test_provisions_agent_under_home_agents(self, tmp_path: Path) -> None:
        """init --non-interactive scaffolds <home>/agents/<name>/ with no prompts."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "init",
                "--non-interactive",
                "--name",
                "Veritas",
                "--role",
                "reviewer",
                "--home",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        home = tmp_path / "agents" / "veritas"
        assert (home / "identity" / "identity.json").is_file()
        assert (home / "profile.yaml").is_file()
        assert (home / "MANDATE.md").is_file()

    def test_requires_name(self, tmp_path: Path) -> None:
        """--non-interactive without --name fails instead of prompting."""
        runner = CliRunner()
        result = runner.invoke(
            main, ["init", "--non-interactive", "--home", str(tmp_path)], input="\n"
        )
        assert result.exit_code != 0
        assert "--name is required" in result.output

    def test_existing_agent_fails_without_force(self, tmp_path: Path) -> None:
        """Re-running against an existing agent exits non-zero."""
        runner = CliRunner()
        args = ["init", "--non-interactive", "--name", "veritas", "--home", str(tmp_path)]
        assert runner.invoke(main, args).exit_code == 0
        second = runner.invoke(main, args)
        assert second.exit_code != 0
        assert "--force" in second.output

    def test_no_mandate_flag(self, tmp_path: Path) -> None:
        """--no-mandate skips MANDATE.md."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "init",
                "--non-interactive",
                "--name",
                "veritas",
                "--no-mandate",
                "--home",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert not (tmp_path / "agents" / "veritas" / "MANDATE.md").exists()

    def test_interactive_path_still_delegates_to_wizard(self, tmp_path: Path) -> None:
        """Without --non-interactive, init still calls run_onboard unchanged."""
        from unittest.mock import patch

        runner = CliRunner()
        with patch("skcapstone.onboard.run_onboard") as mock_onboard:
            result = runner.invoke(main, ["init", "--home", str(tmp_path)])
        mock_onboard.assert_called_once_with(str(tmp_path))
        assert result.exit_code == 0
