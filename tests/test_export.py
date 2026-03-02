"""Tests for the sovereign agent export/import bundle system."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from skcapstone.export import (
    BUNDLE_VERSION,
    export_bundle,
    import_bundle,
)
from skcapstone.memory_engine import list_memories, store as memory_store
from skcapstone.models import MemoryLayer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    """Minimal agent home with required directory structure."""
    home = tmp_path / ".skcapstone"
    home.mkdir()
    return home


@pytest.fixture
def populated_home(agent_home: Path) -> Path:
    """Agent home pre-populated with identity, config, soul, memories, conversations."""
    # Identity
    identity_dir = agent_home / "identity"
    identity_dir.mkdir()
    (identity_dir / "identity.json").write_text(
        json.dumps({"name": "test-agent", "fingerprint": "DEADBEEF", "email": "test@example.com"}),
        encoding="utf-8",
    )

    # Config
    config_dir = agent_home / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"agent_name": "test-agent", "auto_rehydrate": False}),
        encoding="utf-8",
    )

    # Soul
    soul_dir = agent_home / "soul"
    soul_dir.mkdir()
    (soul_dir / "base.json").write_text(
        json.dumps({"name": "base", "display_name": "Base Soul"}), encoding="utf-8"
    )
    installed_dir = soul_dir / "installed"
    installed_dir.mkdir()
    (installed_dir / "lumina.json").write_text(
        json.dumps({"name": "lumina", "display_name": "Lumina", "vibe": "warm"}),
        encoding="utf-8",
    )

    # Memories
    memory_store(agent_home, "Sovereign memory one", tags=["test"], importance=0.6)
    memory_store(agent_home, "Sovereign memory two", tags=["core"], importance=0.9)

    # Conversations
    conv_dir = agent_home / "conversations"
    conv_dir.mkdir()
    (conv_dir / "peer-alice.json").write_text(
        json.dumps([
            {"role": "user", "content": "Hello", "timestamp": "2026-03-01T10:00:00+00:00"},
            {"role": "assistant", "content": "Hi there!", "timestamp": "2026-03-01T10:00:01+00:00"},
        ]),
        encoding="utf-8",
    )

    return agent_home


# ---------------------------------------------------------------------------
# Test: export_bundle structure
# ---------------------------------------------------------------------------


class TestExportBundle:
    """Tests for export_bundle()."""

    def test_export_returns_dict_with_required_keys(self, populated_home: Path):
        """Exported bundle must contain all required top-level keys."""
        bundle = export_bundle(populated_home)

        assert isinstance(bundle, dict)
        for key in ("bundle_version", "exported_at", "agent_name", "skcapstone_version",
                    "identity", "config", "soul", "memories", "conversations"):
            assert key in bundle, f"Missing key: {key}"

    def test_bundle_version_is_correct(self, agent_home: Path):
        """bundle_version must equal BUNDLE_VERSION constant."""
        bundle = export_bundle(agent_home)
        assert bundle["bundle_version"] == BUNDLE_VERSION

    def test_export_includes_identity(self, populated_home: Path):
        """Identity section must include the agent fingerprint."""
        bundle = export_bundle(populated_home)
        assert bundle["identity"]["fingerprint"] == "DEADBEEF"
        assert bundle["identity"]["name"] == "test-agent"

    def test_export_includes_config(self, populated_home: Path):
        """Config section must include the agent name."""
        bundle = export_bundle(populated_home)
        assert bundle["config"]["agent_name"] == "test-agent"

    def test_export_includes_soul(self, populated_home: Path):
        """Soul section must include base soul and installed overlays."""
        bundle = export_bundle(populated_home)
        soul = bundle["soul"]
        assert soul["base"]["name"] == "base"
        assert "lumina" in soul["installed"]
        assert soul["installed"]["lumina"]["vibe"] == "warm"

    def test_export_includes_memories(self, populated_home: Path):
        """Memories list must contain all stored memories."""
        bundle = export_bundle(populated_home)
        assert len(bundle["memories"]) == 2
        contents = {m["content"] for m in bundle["memories"]}
        assert "Sovereign memory one" in contents
        assert "Sovereign memory two" in contents

    def test_export_includes_conversations(self, populated_home: Path):
        """Conversations dict must include peer histories."""
        bundle = export_bundle(populated_home)
        assert "peer-alice" in bundle["conversations"]
        msgs = bundle["conversations"]["peer-alice"]
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "Hello"

    def test_export_is_json_serializable(self, populated_home: Path):
        """The bundle must be fully JSON serializable without errors."""
        bundle = export_bundle(populated_home)
        serialized = json.dumps(bundle)
        assert len(serialized) > 0
        # Round-trip: must be parseable back
        parsed = json.loads(serialized)
        assert parsed["bundle_version"] == BUNDLE_VERSION

    def test_export_empty_home(self, agent_home: Path):
        """Export from an empty agent home must succeed with empty sections."""
        bundle = export_bundle(agent_home)
        assert bundle["identity"] == {}
        assert bundle["config"] == {}
        assert bundle["memories"] == []
        assert bundle["conversations"] == {}

    def test_export_agent_name_from_identity(self, populated_home: Path):
        """Agent name is read from identity.json."""
        bundle = export_bundle(populated_home)
        assert bundle["agent_name"] == "test-agent"


# ---------------------------------------------------------------------------
# Test: import_bundle memories
# ---------------------------------------------------------------------------


class TestImportBundleMemories:
    """Tests for memory import in import_bundle()."""

    def test_import_restores_memories(self, agent_home: Path, populated_home: Path):
        """Importing a bundle should restore all memories into target home."""
        bundle = export_bundle(populated_home)

        target = agent_home.parent / "target"
        target.mkdir()

        result = import_bundle(target, bundle)
        assert result["memories_imported"] == 2

        memories = list_memories(target)
        contents = {m.content for m in memories}
        assert "Sovereign memory one" in contents
        assert "Sovereign memory two" in contents

    def test_import_is_idempotent_for_memories(self, agent_home: Path, populated_home: Path):
        """Re-importing the same bundle should not duplicate memories."""
        bundle = export_bundle(populated_home)

        target = agent_home.parent / "target2"
        target.mkdir()

        first = import_bundle(target, bundle)
        second = import_bundle(target, bundle)

        assert first["memories_imported"] == 2
        assert second["memories_imported"] == 0  # already present

        memories = list_memories(target)
        assert len(memories) == 2

    def test_import_preserves_existing_memories(self, populated_home: Path):
        """Import should not overwrite memories already in the target."""
        # Pre-store a memory in the target
        pre_entry = memory_store(populated_home, "Pre-existing memory")

        # Export from a second home
        source = populated_home.parent / "source"
        source.mkdir()
        memory_store(source, "New from source", tags=["new"])
        bundle = export_bundle(source)

        result = import_bundle(populated_home, bundle)
        assert result["memories_imported"] == 1

        memories = list_memories(populated_home)
        contents = {m.content for m in memories}
        assert "Pre-existing memory" in contents
        assert "New from source" in contents


# ---------------------------------------------------------------------------
# Test: import_bundle conversations
# ---------------------------------------------------------------------------


class TestImportBundleConversations:
    """Tests for conversation import in import_bundle()."""

    def test_import_restores_conversations(self, agent_home: Path, populated_home: Path):
        """Importing a bundle should restore conversation histories."""
        bundle = export_bundle(populated_home)

        target = agent_home.parent / "conv-target"
        target.mkdir()

        result = import_bundle(target, bundle)
        assert result["conversations_imported"] == 2

        conv_file = target / "conversations" / "peer-alice.json"
        assert conv_file.exists()
        messages = json.loads(conv_file.read_text())
        assert len(messages) == 2

    def test_import_merges_conversations(self, tmp_path: Path, populated_home: Path):
        """Import should merge new messages without duplicating existing ones."""
        # Create a fresh target with one existing message for peer-alice
        target = tmp_path / "merge-target"
        target.mkdir()
        conv_dir = target / "conversations"
        conv_dir.mkdir()
        existing = [{"role": "user", "content": "Hello", "timestamp": "2026-03-01T10:00:00+00:00"}]
        (conv_dir / "peer-alice.json").write_text(json.dumps(existing), encoding="utf-8")

        bundle = export_bundle(populated_home)
        result = import_bundle(target, bundle)

        # Should add only the "assistant" message (second msg), not re-add "Hello"
        assert result["conversations_imported"] == 1

        messages = json.loads((conv_dir / "peer-alice.json").read_text())
        assert len(messages) == 2


# ---------------------------------------------------------------------------
# Test: import_bundle identity / config / soul
# ---------------------------------------------------------------------------


class TestImportBundleFiles:
    """Tests for identity, config, and soul file import."""

    def test_import_writes_identity_when_absent(self, tmp_path: Path, populated_home: Path):
        """Identity should be written when the target file does not exist."""
        target = tmp_path / "fresh-agent"
        target.mkdir()
        bundle = export_bundle(populated_home)
        result = import_bundle(target, bundle)
        assert result["identity_written"] is True
        identity = json.loads((target / "identity" / "identity.json").read_text())
        assert identity["fingerprint"] == "DEADBEEF"

    def test_import_skips_identity_when_present(self, populated_home: Path):
        """Identity must not be overwritten by default when already present."""
        bundle = export_bundle(populated_home)
        # Modify bundle identity
        bundle["identity"]["fingerprint"] = "MODIFIED"
        result = import_bundle(populated_home, bundle)
        assert result["identity_written"] is False
        # Original fingerprint is preserved
        identity = json.loads((populated_home / "identity" / "identity.json").read_text())
        assert identity["fingerprint"] == "DEADBEEF"

    def test_import_overwrites_identity_with_flag(self, populated_home: Path):
        """--overwrite-identity should force-write identity."""
        bundle = export_bundle(populated_home)
        bundle["identity"]["fingerprint"] = "NEWPRINT"
        result = import_bundle(populated_home, bundle, overwrite_identity=True)
        assert result["identity_written"] is True
        identity = json.loads((populated_home / "identity" / "identity.json").read_text())
        assert identity["fingerprint"] == "NEWPRINT"

    def test_import_writes_soul_files(self, tmp_path: Path, populated_home: Path):
        """Soul base and installed overlays should be written to a new home."""
        target = tmp_path / "soul-target"
        target.mkdir()
        bundle = export_bundle(populated_home)
        result = import_bundle(target, bundle)
        assert result["soul_files_written"] >= 1
        assert (target / "soul" / "base.json").exists()
        assert (target / "soul" / "installed" / "lumina.json").exists()

    def test_import_writes_config(self, tmp_path: Path, populated_home: Path):
        """Config should be written to a new home."""
        target = tmp_path / "config-target"
        target.mkdir()
        bundle = export_bundle(populated_home)
        result = import_bundle(target, bundle)
        assert result["config_written"] is True
        config = yaml.safe_load((target / "config" / "config.yaml").read_text())
        assert config["agent_name"] == "test-agent"


# ---------------------------------------------------------------------------
# Test: import_bundle validation
# ---------------------------------------------------------------------------


class TestImportBundleValidation:
    """Tests for bundle validation in import_bundle()."""

    def test_import_rejects_non_dict(self, agent_home: Path):
        """A non-dict bundle should raise ValueError."""
        with pytest.raises(ValueError, match="JSON object"):
            import_bundle(agent_home, [])  # type: ignore[arg-type]

    def test_import_rejects_missing_version(self, agent_home: Path):
        """A bundle with no bundle_version should raise ValueError."""
        with pytest.raises(ValueError, match="bundle_version"):
            import_bundle(agent_home, {"memories": []})

    def test_import_rejects_wrong_version(self, agent_home: Path):
        """A bundle with a wrong version should raise ValueError."""
        with pytest.raises(ValueError, match="Unsupported bundle_version"):
            import_bundle(agent_home, {"bundle_version": 99, "memories": []})

    def test_import_empty_bundle_succeeds(self, agent_home: Path):
        """A minimal valid bundle with no data should import without error."""
        minimal = {"bundle_version": BUNDLE_VERSION}
        result = import_bundle(agent_home, minimal)
        assert result["memories_imported"] == 0
        assert result["conversations_imported"] == 0
        assert result["errors"] == []


# ---------------------------------------------------------------------------
# Test: CLI via Click test runner
# ---------------------------------------------------------------------------


class TestExportCLI:
    """Tests for skcapstone export/import via Click test runner."""

    def test_export_command_stdout(self, populated_home: Path):
        """skcapstone export should write valid JSON to stdout."""
        from click.testing import CliRunner
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["--agent", "", "export", "--home", str(populated_home)])
        assert result.exit_code == 0, result.output
        bundle = json.loads(result.output)
        assert bundle["bundle_version"] == BUNDLE_VERSION
        assert len(bundle["memories"]) == 2

    def test_export_command_to_file(self, populated_home: Path, tmp_path: Path):
        """skcapstone export --output should write a JSON file."""
        from click.testing import CliRunner
        from skcapstone.cli import main

        out_file = tmp_path / "bundle.json"
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--agent", "", "export", "--home", str(populated_home), "--output", str(out_file)],
        )
        assert result.exit_code == 0, result.output
        assert out_file.exists()
        bundle = json.loads(out_file.read_text())
        assert bundle["bundle_version"] == BUNDLE_VERSION

    def test_import_command(self, populated_home: Path, tmp_path: Path):
        """skcapstone import should restore memories from a bundle file."""
        from click.testing import CliRunner
        from skcapstone.cli import main

        # Export first
        out_file = tmp_path / "bundle.json"
        runner = CliRunner()
        runner.invoke(
            main,
            ["--agent", "", "export", "--home", str(populated_home), "--output", str(out_file)],
        )

        # Import into a fresh home
        new_home = tmp_path / "new_agent"
        new_home.mkdir()
        result = runner.invoke(
            main,
            ["--agent", "", "import", str(out_file), "--home", str(new_home)],
        )
        assert result.exit_code == 0, result.output
        assert "Import complete" in result.output

        memories = list_memories(new_home)
        assert len(memories) == 2

    def test_export_nonexistent_home_fails(self, tmp_path: Path):
        """export from a non-existent home should exit with error."""
        from click.testing import CliRunner
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--agent", "", "export", "--home", str(tmp_path / "does-not-exist")],
        )
        assert result.exit_code != 0

    def test_import_nonexistent_bundle_fails(self, agent_home: Path):
        """import from a missing file should exit with error."""
        from click.testing import CliRunner
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--agent", "", "import", "/tmp/nonexistent_bundle.json", "--home", str(agent_home)],
        )
        assert result.exit_code != 0
