"""Tests for the skcapstone whoami identity card module.

Covers:
- IdentityCard model creation and serialization
- generate_card from a populated agent home
- generate_card from a minimal/empty home
- export_card writes valid JSON
- import_card reads it back
- CLI command integration (whoami, --json-out, --export)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from skcapstone.whoami import (
    IdentityCard,
    export_card,
    generate_card,
    import_card,
)


@pytest.fixture
def agent_home(tmp_path):
    """Create a populated agent home for testing."""
    home = tmp_path / ".skcapstone"
    for d in ["identity", "memory", "memory/short-term", "memory/mid-term",
              "memory/long-term", "trust", "security", "sync", "config", "skills"]:
        (home / d).mkdir(parents=True, exist_ok=True)

    (home / "manifest.json").write_text(json.dumps({
        "name": "TestAgent", "version": "0.1.0",
    }))
    (home / "identity" / "identity.json").write_text(json.dumps({
        "name": "TestAgent",
        "email": "test@skcapstone.local",
        "fingerprint": "AABBCCDD11223344AABBCCDD11223344AABBCCDD",
    }))
    (home / "config" / "config.yaml").write_text(yaml.dump({"agent_name": "TestAgent"}))
    (home / "memory" / "index.json").write_text("{}")
    (home / "memory" / "short-term" / "m1.json").write_text(
        json.dumps({"memory_id": "m1", "content": "test", "tags": [],
                     "source": "test", "importance": 0.5, "layer": "short-term",
                     "created_at": "2026-02-24T00:00:00Z", "access_count": 0,
                     "accessed_at": None, "metadata": {}})
    )
    (home / "skills" / "test-skill.json").write_text(
        json.dumps({"name": "test-skill", "version": "1.0"})
    )

    return home


@pytest.fixture
def empty_home(tmp_path):
    """An empty agent home (exists but no files)."""
    home = tmp_path / ".skcapstone"
    home.mkdir()
    return home


class TestIdentityCard:
    """Test the IdentityCard model."""

    def test_defaults(self):
        """Card has sensible defaults."""
        card = IdentityCard()
        assert card.skcapstone_card == "1.0.0"
        assert card.name == "unknown"
        assert card.created_at != ""

    def test_serialization_roundtrip(self):
        """Card survives JSON round-trip."""
        card = IdentityCard(
            name="Opus",
            fingerprint="AABB1122",
            entity_type="ai",
            capabilities=["capauth:identity", "skcomm:messaging"],
            contact_uris=["capauth:AABB1122"],
        )
        json_str = card.model_dump_json()
        restored = IdentityCard.model_validate_json(json_str)

        assert restored.name == "Opus"
        assert restored.fingerprint == "AABB1122"
        assert len(restored.capabilities) == 2

    def test_all_fields_serializable(self):
        """Every field can be JSON-serialized."""
        card = IdentityCard(
            name="Test",
            fingerprint="FP123",
            public_key="-----BEGIN PGP PUBLIC KEY BLOCK-----\ndata\n-----END PGP PUBLIC KEY BLOCK-----",
            entity_type="human",
            email="test@test.com",
            handle="test@capauth.local",
            capabilities=["a", "b"],
            trust_status="active",
            consciousness="CONSCIOUS",
            memory_count=42,
            contact_uris=["capauth:test"],
            hostname="testhost",
        )
        data = json.loads(card.model_dump_json())
        assert data["name"] == "Test"
        assert data["memory_count"] == 42


class TestGenerateCard:
    """Test card generation from agent home."""

    def test_generates_from_populated_home(self, agent_home):
        """Card loads identity from a populated home."""
        card = generate_card(agent_home)

        assert card.name == "TestAgent"
        assert card.fingerprint == "AABBCCDD11223344AABBCCDD11223344AABBCCDD"
        assert card.email == "test@skcapstone.local"

    def test_includes_memory_count(self, agent_home):
        """Card includes memory count from store."""
        card = generate_card(agent_home)
        assert card.memory_count >= 1

    def test_includes_capabilities(self, agent_home):
        """Card includes detected package capabilities."""
        card = generate_card(agent_home)
        assert len(card.capabilities) > 0
        assert any("skill:test-skill" in c for c in card.capabilities)

    def test_includes_contact_uris(self, agent_home):
        """Card builds contact URIs from identity data."""
        card = generate_card(agent_home)
        assert len(card.contact_uris) > 0
        assert any("capauth:" in u for u in card.contact_uris)

    def test_generates_from_empty_home(self, empty_home):
        """Card generates with defaults from an empty home."""
        card = generate_card(empty_home)
        assert card.skcapstone_card == "1.0.0"
        assert card.name != ""

    def test_generates_from_nonexistent_home(self, tmp_path):
        """Card generates without crashing from a nonexistent home."""
        card = generate_card(tmp_path / "nope")
        assert card.skcapstone_card == "1.0.0"


class TestExportImport:
    """Test card file export and import."""

    def test_export_creates_file(self, agent_home, tmp_path):
        """Export writes a JSON file."""
        card = generate_card(agent_home)
        output = tmp_path / "card.json"

        result = export_card(card, output)

        assert result.exists()
        data = json.loads(result.read_text())
        assert data["name"] == "TestAgent"

    def test_import_reads_card(self, agent_home, tmp_path):
        """Import reads back an exported card."""
        card = generate_card(agent_home)
        output = tmp_path / "card.json"
        export_card(card, output)

        imported = import_card(output)

        assert imported.name == card.name
        assert imported.fingerprint == card.fingerprint
        assert imported.capabilities == card.capabilities

    def test_import_missing_file(self, tmp_path):
        """Import of nonexistent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            import_card(tmp_path / "nope.json")

    def test_import_invalid_json(self, tmp_path):
        """Import of invalid JSON raises ValueError."""
        bad = tmp_path / "bad.json"
        bad.write_text("not json at all {{{")
        with pytest.raises(ValueError):
            import_card(bad)


class TestCLI:
    """Test whoami CLI command."""

    def test_whoami_help(self):
        """whoami --help works."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["whoami", "--help"])
        assert result.exit_code == 0
        assert "--export" in result.output
        assert "--json-out" in result.output
        assert "--compact" in result.output

    def test_whoami_json(self, agent_home):
        """whoami --json-out produces valid JSON."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["whoami", "--home", str(agent_home), "--json-out"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "name" in data
        assert "fingerprint" in data

    def test_whoami_compact_json(self, agent_home):
        """whoami --json-out --compact omits public key."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, [
            "whoami", "--home", str(agent_home), "--json-out", "--compact"
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "public_key" not in data

    def test_whoami_export(self, agent_home, tmp_path):
        """whoami --export saves a card file."""
        from skcapstone.cli import main
        runner = CliRunner()
        output = tmp_path / "exported.json"
        result = runner.invoke(main, [
            "whoami", "--home", str(agent_home), "--export", str(output)
        ])
        assert result.exit_code == 0
        assert output.exists()

    def test_whoami_human_output(self, agent_home):
        """whoami without flags shows the Rich panel."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["whoami", "--home", str(agent_home)])
        assert result.exit_code == 0
        assert "TestAgent" in result.output or "Identity" in result.output
