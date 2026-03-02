"""Tests for skcapstone.claude_md — CLAUDE.md auto-regeneration.

Covers:
  - generate_claude_md() produces correct markdown structure
  - generate_claude_md() is graceful with an uninitialized home
  - generate_claude_md() embeds recent memories when present
  - write_claude_md() persists content to disk
  - write_claude_md() --backup renames the existing file
  - refresh-context CLI command writes CLAUDE.md
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from skcapstone.claude_md import generate_claude_md, write_claude_md
from skcapstone.cli import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_agent(home: Path, name: str = "md-test") -> None:
    """Minimal agent init needed for context gathering."""
    from skcapstone.pillars.identity import generate_identity
    from skcapstone.pillars.memory import initialize_memory
    from skcapstone.pillars.security import initialize_security
    from skcapstone.pillars.sync import initialize_sync
    from skcapstone.pillars.trust import initialize_trust

    generate_identity(home, name)
    initialize_memory(home)
    initialize_trust(home)
    initialize_security(home)
    initialize_sync(home)

    manifest = {
        "name": name,
        "version": "0.1.0",
        "created_at": "2026-01-01T00:00:00Z",
        "connectors": [],
    }
    (home / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (home / "config").mkdir(exist_ok=True)
    (home / "config" / "config.yaml").write_text(yaml.dump({"agent_name": name}))


# ---------------------------------------------------------------------------
# generate_claude_md()
# ---------------------------------------------------------------------------

class TestGenerateClaudeMd:
    """Tests for generate_claude_md()."""

    def test_returns_string(self, tmp_agent_home: Path):
        """generate_claude_md() returns a non-empty string."""
        result = generate_claude_md(tmp_agent_home)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_markdown_h1_header(self, tmp_agent_home: Path):
        """Output begins with the expected H1 heading."""
        result = generate_claude_md(tmp_agent_home)
        assert "# SKCapstone Agent Context" in result

    def test_agent_identity_section(self, tmp_agent_home: Path):
        """Output contains the Agent Identity section."""
        _init_agent(tmp_agent_home, "regen-test")
        result = generate_claude_md(tmp_agent_home)

        assert "## Agent Identity" in result
        assert "regen-test" in result

    def test_pillar_status_table(self, tmp_agent_home: Path):
        """Output contains a pillar status table."""
        _init_agent(tmp_agent_home)
        result = generate_claude_md(tmp_agent_home)

        assert "## Pillar Status" in result
        assert "| Pillar | Status |" in result
        assert "identity" in result

    def test_coordination_board_section(self, tmp_agent_home: Path):
        """Output contains the Coordination Board section."""
        result = generate_claude_md(tmp_agent_home)
        assert "## Coordination Board" in result

    def test_cli_reference_section(self, tmp_agent_home: Path):
        """Output contains the CLI Reference section with key commands."""
        result = generate_claude_md(tmp_agent_home)

        assert "## CLI Reference" in result
        assert "skcapstone status" in result
        assert "skcapstone memory" in result
        assert "skcapstone coord" in result

    def test_graceful_with_uninitialized_home(self, tmp_agent_home: Path):
        """generate_claude_md() does not raise on an empty home directory."""
        result = generate_claude_md(tmp_agent_home)
        # Must still return valid markdown even without initialized pillars
        assert "# SKCapstone Agent Context" in result

    def test_embeds_recent_memories(self, tmp_agent_home: Path):
        """Memories stored before generation appear in the output."""
        _init_agent(tmp_agent_home)
        from skcapstone.memory_engine import store
        store(tmp_agent_home, "Penguin test memory for claude-md", tags=["pengu"])

        result = generate_claude_md(tmp_agent_home, memory_limit=5)

        assert "Penguin test memory for claude-md" in result

    def test_memory_limit_applied(self, tmp_agent_home: Path):
        """memory_limit=1 produces output shorter than memory_limit=10."""
        _init_agent(tmp_agent_home)
        from skcapstone.memory_engine import store
        for i in range(8):
            store(tmp_agent_home, f"Memory item {i}", tags=["bulk"])

        short = generate_claude_md(tmp_agent_home, memory_limit=1)
        long = generate_claude_md(tmp_agent_home, memory_limit=8)

        assert len(short) <= len(long)

    def test_consciousness_section_absent_when_disabled(self, tmp_agent_home: Path):
        """When consciousness is disabled, status shows INACTIVE."""
        import urllib.error
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            result = generate_claude_md(tmp_agent_home)
        # Section may be omitted or show INACTIVE — either is fine
        if "## Consciousness" in result:
            assert "INACTIVE" in result

    def test_soul_overlay_included(self, tmp_agent_home: Path):
        """Active soul name appears in the output when an overlay is set."""
        _init_agent(tmp_agent_home)
        soul_dir = tmp_agent_home / "soul"
        soul_dir.mkdir(exist_ok=True)
        import json as _json
        (soul_dir / "active.json").write_text(
            _json.dumps({"active_soul": "lumina", "base_soul": "default"}),
            encoding="utf-8",
        )

        result = generate_claude_md(tmp_agent_home)
        assert "lumina" in result


# ---------------------------------------------------------------------------
# write_claude_md()
# ---------------------------------------------------------------------------

class TestWriteClaudeMd:
    """Tests for write_claude_md()."""

    def test_creates_file(self, tmp_agent_home: Path, tmp_path: Path):
        """write_claude_md() creates the destination file."""
        dest = tmp_path / "CLAUDE.md"
        write_claude_md(tmp_agent_home, dest)

        assert dest.exists()

    def test_file_content_is_markdown(self, tmp_agent_home: Path, tmp_path: Path):
        """Written file has CLAUDE.md markdown structure."""
        dest = tmp_path / "CLAUDE.md"
        write_claude_md(tmp_agent_home, dest)

        content = dest.read_text(encoding="utf-8")
        assert "# SKCapstone Agent Context" in content

    def test_backup_renames_existing(self, tmp_agent_home: Path, tmp_path: Path):
        """backup=True renames an existing CLAUDE.md to .bak before writing."""
        dest = tmp_path / "CLAUDE.md"
        dest.write_text("old content", encoding="utf-8")

        write_claude_md(tmp_agent_home, dest, backup=True)

        bak = tmp_path / "CLAUDE.md.bak"
        assert bak.exists(), "Expected .bak file to be created"
        assert bak.read_text(encoding="utf-8") == "old content"
        assert dest.exists()
        assert "# SKCapstone Agent Context" in dest.read_text(encoding="utf-8")

    def test_no_backup_overwrites_existing(self, tmp_agent_home: Path, tmp_path: Path):
        """Without backup=True the existing file is silently overwritten."""
        dest = tmp_path / "CLAUDE.md"
        dest.write_text("old content", encoding="utf-8")

        write_claude_md(tmp_agent_home, dest, backup=False)

        bak = tmp_path / "CLAUDE.md.bak"
        assert not bak.exists(), "No .bak file expected when backup=False"
        assert "# SKCapstone Agent Context" in dest.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# refresh-context CLI
# ---------------------------------------------------------------------------

class TestRefreshContextCli:
    """Tests for `skcapstone refresh-context` CLI command."""

    def _run(self, *args, home: str | None = None) -> "click.testing.Result":
        runner = CliRunner(mix_stderr=False)
        cmd: list[str] = ["refresh-context"]
        if home:
            cmd += ["--home", home]
        cmd += list(args)
        return runner.invoke(main, cmd, catch_exceptions=False)

    def test_writes_claude_md_to_dest(self, tmp_agent_home: Path, tmp_path: Path):
        """--dest writes CLAUDE.md to the specified path."""
        dest = tmp_path / "CLAUDE.md"
        result = self._run("--dest", str(dest), home=str(tmp_agent_home))

        assert result.exit_code == 0, result.output
        assert dest.exists()
        assert "# SKCapstone Agent Context" in dest.read_text(encoding="utf-8")

    def test_output_confirms_written_path(self, tmp_agent_home: Path, tmp_path: Path):
        """CLI prints the path of the written file."""
        dest = tmp_path / "CLAUDE.md"
        result = self._run("--dest", str(dest), home=str(tmp_agent_home))

        assert "Written" in result.output or str(dest) in result.output

    def test_backup_flag_creates_bak(self, tmp_agent_home: Path, tmp_path: Path):
        """--backup renames the existing file before writing."""
        dest = tmp_path / "CLAUDE.md"
        dest.write_text("old content", encoding="utf-8")

        result = self._run("--backup", "--dest", str(dest), home=str(tmp_agent_home))

        assert result.exit_code == 0, result.output
        bak = tmp_path / "CLAUDE.md.bak"
        assert bak.exists()
        assert bak.read_text(encoding="utf-8") == "old content"

    def test_dest_dir_writes_claude_md_inside(self, tmp_agent_home: Path, tmp_path: Path):
        """Passing a directory as --dest writes CLAUDE.md inside it."""
        result = self._run("--dest", str(tmp_path), home=str(tmp_agent_home))

        assert result.exit_code == 0, result.output
        assert (tmp_path / "CLAUDE.md").exists()

    def test_falls_back_to_cwd_without_git(self, tmp_agent_home: Path, tmp_path: Path):
        """Without --dest and outside a git repo, writes to cwd/CLAUDE.md."""
        runner = CliRunner(mix_stderr=False)
        written: list[Path] = []

        with runner.isolated_filesystem(temp_dir=tmp_path) as iso_dir:
            with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
                result = runner.invoke(
                    main,
                    ["refresh-context", "--home", str(tmp_agent_home)],
                    catch_exceptions=False,
                )
            written.append(Path(iso_dir) / "CLAUDE.md")

        assert result.exit_code == 0, result.output
        assert written[0].exists(), f"Expected CLAUDE.md at {written[0]}"
