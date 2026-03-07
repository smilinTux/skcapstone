"""Tests for skcapstone shell completion.

Covers:
  - Dynamic completion callbacks (complete_memory_tags, complete_agent_names,
    complete_task_ids) — unit tests with tmp_path fixtures.
  - CLI commands: skcapstone install-completion --help, completions --help,
    completions show, completions install (mocked), completions uninstall.
  - Graceful degradation: callbacks return [] when dirs are missing or files
    are malformed JSON.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from skcapstone.cli import main

try:
    from skcapstone.completions import (
        complete_memory_tags,
        complete_agent_names,
        complete_task_ids,
        generate_script,
        detect_shell,
        SUPPORTED_SHELLS,
    )
except ImportError:
    pytest.skip(
        "skcapstone.completions missing required names "
        "(complete_memory_tags, complete_agent_names, complete_task_ids)",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx_param():
    """Return stub (ctx, param) suitable for shell_complete callbacks."""
    return MagicMock(), MagicMock()


# ---------------------------------------------------------------------------
# complete_memory_tags
# ---------------------------------------------------------------------------


class TestCompleteMemoryTags:
    def test_returns_matching_tags(self, tmp_path: Path, monkeypatch):
        """Tags that start with the incomplete prefix are returned."""
        monkeypatch.setenv("SKCAPSTONE_ROOT", str(tmp_path))
        layer_dir = tmp_path / "memory" / "short-term"
        layer_dir.mkdir(parents=True)
        (layer_dir / "mem1.json").write_text(
            json.dumps({"tags": ["kubernetes", "python", "infra"]}), encoding="utf-8"
        )
        (layer_dir / "mem2.json").write_text(
            json.dumps({"tags": ["kubernetes", "docker"]}), encoding="utf-8"
        )

        ctx, param = _make_ctx_param()
        results = complete_memory_tags(ctx, param, "ku")

        values = {r.value for r in results}
        assert "kubernetes" in values
        assert "python" not in values

    def test_returns_all_tags_on_empty_prefix(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_ROOT", str(tmp_path))
        layer_dir = tmp_path / "memory" / "mid-term"
        layer_dir.mkdir(parents=True)
        (layer_dir / "m.json").write_text(
            json.dumps({"tags": ["alpha", "beta"]}), encoding="utf-8"
        )

        ctx, param = _make_ctx_param()
        values = {r.value for r in complete_memory_tags(ctx, param, "")}
        assert "alpha" in values
        assert "beta" in values

    def test_deduplicates_tags_across_files(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_ROOT", str(tmp_path))
        layer_dir = tmp_path / "memory" / "long-term"
        layer_dir.mkdir(parents=True)
        for i in range(3):
            (layer_dir / f"m{i}.json").write_text(
                json.dumps({"tags": ["shared-tag"]}), encoding="utf-8"
            )

        ctx, param = _make_ctx_param()
        results = complete_memory_tags(ctx, param, "shared")
        assert len(results) == 1
        assert results[0].value == "shared-tag"

    def test_empty_when_no_memory_dir(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_ROOT", str(tmp_path))
        ctx, param = _make_ctx_param()
        assert complete_memory_tags(ctx, param, "") == []

    def test_skips_malformed_json(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_ROOT", str(tmp_path))
        layer_dir = tmp_path / "memory" / "short-term"
        layer_dir.mkdir(parents=True)
        (layer_dir / "bad.json").write_text("not json{{", encoding="utf-8")
        (layer_dir / "good.json").write_text(
            json.dumps({"tags": ["valid"]}), encoding="utf-8"
        )

        ctx, param = _make_ctx_param()
        values = {r.value for r in complete_memory_tags(ctx, param, "")}
        assert "valid" in values

    def test_covers_all_three_layers(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_ROOT", str(tmp_path))
        for layer in ("short-term", "mid-term", "long-term"):
            d = tmp_path / "memory" / layer
            d.mkdir(parents=True)
            (d / "m.json").write_text(
                json.dumps({"tags": [f"tag-{layer}"]}), encoding="utf-8"
            )

        ctx, param = _make_ctx_param()
        values = {r.value for r in complete_memory_tags(ctx, param, "")}
        assert {"tag-short-term", "tag-mid-term", "tag-long-term"}.issubset(values)


# ---------------------------------------------------------------------------
# complete_agent_names
# ---------------------------------------------------------------------------


class TestCompleteAgentNames:
    def test_returns_matching_agents(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_ROOT", str(tmp_path))
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        for name in ("opus", "lumina", "grok"):
            (hb_dir / f"{name}.json").write_text("{}", encoding="utf-8")

        ctx, param = _make_ctx_param()
        results = complete_agent_names(ctx, param, "o")
        assert any(r.value == "opus" for r in results)
        assert not any(r.value == "lumina" for r in results)

    def test_returns_all_on_empty_prefix(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_ROOT", str(tmp_path))
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        for name in ("alpha", "beta"):
            (hb_dir / f"{name}.json").write_text("{}", encoding="utf-8")

        ctx, param = _make_ctx_param()
        values = {r.value for r in complete_agent_names(ctx, param, "")}
        assert {"alpha", "beta"}.issubset(values)

    def test_empty_when_no_heartbeat_dir(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_ROOT", str(tmp_path))
        ctx, param = _make_ctx_param()
        assert complete_agent_names(ctx, param, "") == []


# ---------------------------------------------------------------------------
# complete_task_ids
# ---------------------------------------------------------------------------


class TestCompleteTaskIds:
    def _write_task(self, tasks_dir: Path, task_id: str, title: str) -> None:
        (tasks_dir / f"{task_id}-{title.lower().replace(' ', '-')}.json").write_text(
            json.dumps({"id": task_id, "title": title}), encoding="utf-8"
        )

    def test_returns_matching_task_ids(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_ROOT", str(tmp_path))
        tasks_dir = tmp_path / "coordination" / "tasks"
        tasks_dir.mkdir(parents=True)
        self._write_task(tasks_dir, "abc12345", "Add shell completion")
        self._write_task(tasks_dir, "def67890", "Fix routing bug")

        ctx, param = _make_ctx_param()
        results = complete_task_ids(ctx, param, "abc")
        assert len(results) == 1
        assert results[0].value == "abc12345"

    def test_help_text_is_task_title(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_ROOT", str(tmp_path))
        tasks_dir = tmp_path / "coordination" / "tasks"
        tasks_dir.mkdir(parents=True)
        self._write_task(tasks_dir, "aaa11111", "My Important Task")

        ctx, param = _make_ctx_param()
        results = complete_task_ids(ctx, param, "aaa")
        assert len(results) == 1
        assert "My Important Task" in results[0].help

    def test_empty_when_no_tasks_dir(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_ROOT", str(tmp_path))
        ctx, param = _make_ctx_param()
        assert complete_task_ids(ctx, param, "") == []

    def test_skips_malformed_task_files(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SKCAPSTONE_ROOT", str(tmp_path))
        tasks_dir = tmp_path / "coordination" / "tasks"
        tasks_dir.mkdir(parents=True)
        (tasks_dir / "bad.json").write_text("{{bad", encoding="utf-8")
        self._write_task(tasks_dir, "fff99999", "Valid task")

        ctx, param = _make_ctx_param()
        results = complete_task_ids(ctx, param, "fff")
        assert len(results) == 1


# ---------------------------------------------------------------------------
# generate_script / detect_shell
# ---------------------------------------------------------------------------


class TestGenerateScript:
    def test_bash_script_uses_env_var(self):
        script = generate_script("bash")
        assert "_SKCAPSTONE_COMPLETE=bash_source" in script

    def test_zsh_script_uses_env_var(self):
        script = generate_script("zsh")
        assert "_SKCAPSTONE_COMPLETE=zsh_source" in script

    def test_fish_script_uses_env_var(self):
        script = generate_script("fish")
        assert "_SKCAPSTONE_COMPLETE=fish_source" in script

    def test_invalid_shell_raises(self):
        with pytest.raises(ValueError, match="Unsupported shell"):
            generate_script("powershell")

    def test_all_supported_shells_generate(self):
        for shell in SUPPORTED_SHELLS:
            assert generate_script(shell)


class TestDetectShell:
    def test_detects_bash(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/bash")
        assert detect_shell() == "bash"

    def test_detects_zsh(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/zsh")
        assert detect_shell() == "zsh"

    def test_detects_fish(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/fish")
        assert detect_shell() == "fish"

    def test_returns_none_for_unknown(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/dash")
        assert detect_shell() is None


# ---------------------------------------------------------------------------
# CLI — install-completion command
# ---------------------------------------------------------------------------


class TestInstallCompletionCommand:
    def setup_method(self):
        self.runner = CliRunner()

    def test_help_shows_description(self):
        result = self.runner.invoke(main, ["install-completion", "--help"])
        assert result.exit_code == 0
        assert "completion" in result.output.lower()

    def test_install_with_explicit_shell(self):
        with patch("skcapstone.completions.install_completions") as mock_install:
            mock_install.return_value = {
                "success": True,
                "shell": "bash",
                "script_path": "/home/user/.bash_completion.d/skcapstone.bash-completion",
                "rc_updated": False,
            }
            result = self.runner.invoke(main, ["install-completion", "bash"])
        assert result.exit_code == 0
        mock_install.assert_called_once_with(shell="bash")

    def test_install_auto_detects_shell(self):
        with patch("skcapstone.completions.install_completions") as mock_install:
            mock_install.return_value = {
                "success": True,
                "shell": "zsh",
                "script_path": "/home/user/.zfunc/_skcapstone",
                "rc_updated": True,
                "rc_path": "/home/user/.zshrc",
            }
            result = self.runner.invoke(main, ["install-completion"])
        assert result.exit_code == 0

    def test_install_prints_rc_path_when_updated(self):
        with patch("skcapstone.completions.install_completions") as mock_install:
            mock_install.return_value = {
                "success": True,
                "shell": "zsh",
                "script_path": "/home/user/.zfunc/_skcapstone",
                "rc_updated": True,
                "rc_path": "/home/user/.zshrc",
            }
            result = self.runner.invoke(main, ["install-completion", "zsh"])
        assert result.exit_code == 0
        assert ".zshrc" in result.output

    def test_invalid_shell_rejected(self):
        result = self.runner.invoke(main, ["install-completion", "powershell"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# CLI — completions group
# ---------------------------------------------------------------------------


class TestCompletionsGroup:
    def setup_method(self):
        self.runner = CliRunner()

    def test_completions_help(self):
        result = self.runner.invoke(main, ["completions", "--help"])
        assert result.exit_code == 0
        assert "install" in result.output
        assert "show" in result.output
        assert "uninstall" in result.output

    def test_completions_show_bash(self):
        result = self.runner.invoke(main, ["completions", "show", "--shell", "bash"])
        assert result.exit_code == 0
        assert "_SKCAPSTONE_COMPLETE=bash_source" in result.output

    def test_completions_show_zsh(self):
        result = self.runner.invoke(main, ["completions", "show", "--shell", "zsh"])
        assert result.exit_code == 0
        assert "_SKCAPSTONE_COMPLETE=zsh_source" in result.output

    def test_completions_show_fish(self):
        result = self.runner.invoke(main, ["completions", "show", "--shell", "fish"])
        assert result.exit_code == 0
        assert "_SKCAPSTONE_COMPLETE=fish_source" in result.output

    def test_completions_install(self):
        with patch("skcapstone.completions.install_completions") as mock_install:
            mock_install.return_value = {
                "success": True,
                "shell": "bash",
                "script_path": "/tmp/skcapstone.bash-completion",
                "rc_updated": False,
            }
            result = self.runner.invoke(
                main, ["completions", "install", "--shell", "bash"]
            )
        assert result.exit_code == 0
        mock_install.assert_called_once_with(shell="bash")

    def test_completions_install_failure(self):
        with patch("skcapstone.completions.install_completions") as mock_install:
            mock_install.return_value = {
                "success": False,
                "error": "Could not detect shell. Use --shell bash/zsh/fish.",
            }
            result = self.runner.invoke(main, ["completions", "install"])
        assert result.exit_code != 0
        assert "detect" in result.output.lower() or "shell" in result.output.lower()

    def test_completions_uninstall_no_scripts(self):
        with patch("skcapstone.completions.uninstall_completions") as mock_uninstall:
            mock_uninstall.return_value = {
                "success": True,
                "removed": [],
                "note": "Source lines in RC files were not removed.",
            }
            result = self.runner.invoke(main, ["completions", "uninstall"])
        assert result.exit_code == 0
        assert "No completion scripts found" in result.output

    def test_completions_uninstall_removes_scripts(self):
        with patch("skcapstone.completions.uninstall_completions") as mock_uninstall:
            mock_uninstall.return_value = {
                "success": True,
                "removed": ["/home/user/.zfunc/_skcapstone"],
                "note": "Source lines in RC files were not removed.",
            }
            result = self.runner.invoke(
                main, ["completions", "uninstall", "--shell", "zsh"]
            )
        assert result.exit_code == 0
        assert "_skcapstone" in result.output
