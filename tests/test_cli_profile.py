"""Tests for skcapstone profile CLI commands."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from skcapstone.cli import main
from skcapstone.prompt_adapter import ModelProfile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile(**kwargs) -> ModelProfile:
    """Return a minimal ModelProfile with optional overrides."""
    defaults = {
        "model_pattern": "test-.*",
        "family": "test",
        "last_updated": "2026-03-02",
    }
    defaults.update(kwargs)
    return ModelProfile(**defaults)


def _make_adapter(profiles: list[ModelProfile]) -> MagicMock:
    """Return a mock PromptAdapter pre-loaded with *profiles*."""
    adapter = MagicMock()
    adapter.profiles = profiles
    adapter.resolve_profile.side_effect = lambda model: next(
        (p for p in profiles if p.family in model or p.model_pattern in model),
        _make_profile(model_pattern=".*", family="generic"),
    )
    return adapter


# ---------------------------------------------------------------------------
# skcapstone profile list
# ---------------------------------------------------------------------------


class TestProfileList:
    """Tests for 'skcapstone profile list'."""

    def test_list_shows_families(self):
        """Happy path: table includes all profile families."""
        runner = CliRunner()
        profiles = [
            _make_profile(model_pattern="claude-.*", family="claude"),
            _make_profile(model_pattern="grok-.*", family="grok"),
        ]
        adapter = _make_adapter(profiles)

        with patch("skcapstone.cli.profile_cmd._get_adapter", return_value=adapter):
            result = runner.invoke(main, ["profile", "list"])

        assert result.exit_code == 0
        assert "claude" in result.output
        assert "grok" in result.output

    def test_list_json_output(self):
        """--json flag emits a valid JSON array."""
        runner = CliRunner()
        profiles = [
            _make_profile(model_pattern="claude-.*", family="claude"),
        ]
        adapter = _make_adapter(profiles)

        with patch("skcapstone.cli.profile_cmd._get_adapter", return_value=adapter):
            result = runner.invoke(main, ["profile", "list", "--json"])

        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert isinstance(parsed, list)
        assert parsed[0]["family"] == "claude"

    def test_list_empty_exits_1(self):
        """When no profiles are loaded the command exits 1."""
        runner = CliRunner()
        adapter = _make_adapter([])

        with patch("skcapstone.cli.profile_cmd._get_adapter", return_value=adapter):
            result = runner.invoke(main, ["profile", "list"])

        assert result.exit_code == 1
        assert "No profiles" in result.output

    def test_list_uses_bundled_profiles_by_default(self):
        """Without mocking, the command loads real bundled profiles."""
        runner = CliRunner()
        result = runner.invoke(main, ["profile", "list"])

        assert result.exit_code == 0
        # Bundled profiles include at least claude and grok
        assert "claude" in result.output
        assert "grok" in result.output

    def test_list_json_includes_all_fields(self):
        """JSON output contains expected ModelProfile fields."""
        runner = CliRunner()
        profiles = [_make_profile(family="deepseek-r1", model_pattern="deepseek-r1.*")]
        adapter = _make_adapter(profiles)

        with patch("skcapstone.cli.profile_cmd._get_adapter", return_value=adapter):
            result = runner.invoke(main, ["profile", "list", "--json"])

        parsed = json.loads(result.output)
        record = parsed[0]
        assert "family" in record
        assert "model_pattern" in record
        assert "system_prompt_mode" in record
        assert "last_updated" in record


# ---------------------------------------------------------------------------
# skcapstone profile show
# ---------------------------------------------------------------------------


class TestProfileShow:
    """Tests for 'skcapstone profile show MODEL'."""

    def test_show_known_model(self):
        """show resolves a known model and prints its family."""
        runner = CliRunner()
        result = runner.invoke(main, ["profile", "show", "claude-opus-4-5"])

        assert result.exit_code == 0
        assert "claude" in result.output

    def test_show_prints_fields(self):
        """show prints key profile fields (system_prompt_mode, structure_format, etc.)."""
        runner = CliRunner()
        result = runner.invoke(main, ["profile", "show", "claude-opus-4-5"])

        assert result.exit_code == 0
        assert "system_prompt_mode" in result.output
        assert "structure_format" in result.output
        assert "last_updated" in result.output

    def test_show_json_output(self):
        """--json flag emits a JSON object with all profile fields."""
        runner = CliRunner()
        result = runner.invoke(main, ["profile", "show", "grok-3", "--json"])

        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert isinstance(parsed, dict)
        assert parsed["family"] == "grok"
        assert "model_pattern" in parsed

    def test_show_unknown_model_falls_back_to_generic(self):
        """Unknown model falls back to generic profile gracefully."""
        runner = CliRunner()
        result = runner.invoke(main, ["profile", "show", "totally-unknown-xyz"])

        assert result.exit_code == 0
        assert "generic" in result.output

    def test_show_deepseek_r1_omits_system(self):
        """DeepSeek R1 profile shows system_prompt_mode = omit."""
        runner = CliRunner()
        result = runner.invoke(main, ["profile", "show", "deepseek-r1-70b"])

        assert result.exit_code == 0
        assert "omit" in result.output


# ---------------------------------------------------------------------------
# skcapstone profile stale
# ---------------------------------------------------------------------------


class TestProfileStale:
    """Tests for 'skcapstone profile stale'."""

    def test_stale_shows_old_profiles(self):
        """Profiles with old last_updated dates appear in stale output."""
        runner = CliRunner()
        old_date = (date.today() - timedelta(days=200)).isoformat()
        profiles = [
            _make_profile(family="old-model", last_updated=old_date),
            _make_profile(family="new-model", last_updated=date.today().isoformat()),
        ]
        adapter = _make_adapter(profiles)

        with patch("skcapstone.cli.profile_cmd._get_adapter", return_value=adapter):
            result = runner.invoke(main, ["profile", "stale"])

        assert result.exit_code == 0
        assert "old-model" in result.output
        assert "new-model" not in result.output

    def test_stale_all_fresh_prints_ok(self):
        """When all profiles are recent, a green OK message is shown."""
        runner = CliRunner()
        fresh = date.today().isoformat()
        profiles = [
            _make_profile(family="claude", last_updated=fresh),
            _make_profile(family="grok", last_updated=fresh),
        ]
        adapter = _make_adapter(profiles)

        with patch("skcapstone.cli.profile_cmd._get_adapter", return_value=adapter):
            result = runner.invoke(main, ["profile", "stale"])

        assert result.exit_code == 0
        assert "All profiles updated" in result.output

    def test_stale_missing_last_updated_flagged(self):
        """Profile with missing last_updated is always considered stale."""
        runner = CliRunner()
        profiles = [
            _make_profile(family="no-date-model", last_updated=""),
        ]
        adapter = _make_adapter(profiles)

        with patch("skcapstone.cli.profile_cmd._get_adapter", return_value=adapter):
            result = runner.invoke(main, ["profile", "stale"])

        assert result.exit_code == 0
        assert "no-date-model" in result.output

    def test_stale_custom_days_flag(self):
        """--days flag changes the staleness threshold."""
        runner = CliRunner()
        # 45 days old — stale at 30 days, fresh at 90 days
        mid_date = (date.today() - timedelta(days=45)).isoformat()
        profiles = [
            _make_profile(family="mid-model", last_updated=mid_date),
        ]
        adapter = _make_adapter(profiles)

        with patch("skcapstone.cli.profile_cmd._get_adapter", return_value=adapter):
            # Should be stale at 30-day threshold
            r30 = runner.invoke(main, ["profile", "stale", "--days", "30"])
            # Should be fresh at 90-day threshold
            r90 = runner.invoke(main, ["profile", "stale", "--days", "90"])

        assert r30.exit_code == 0
        assert "mid-model" in r30.output

        assert r90.exit_code == 0
        assert "All profiles updated" in r90.output

    def test_stale_json_output(self):
        """--json flag emits a JSON array of stale profiles."""
        runner = CliRunner()
        old_date = (date.today() - timedelta(days=200)).isoformat()
        profiles = [
            _make_profile(family="stale-json-model", last_updated=old_date),
        ]
        adapter = _make_adapter(profiles)

        with patch("skcapstone.cli.profile_cmd._get_adapter", return_value=adapter):
            result = runner.invoke(main, ["profile", "stale", "--json"])

        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["family"] == "stale-json-model"
        assert "_days_old" in parsed[0]
        assert parsed[0]["_days_old"] >= 200


# ---------------------------------------------------------------------------
# _parse_last_updated helper
# ---------------------------------------------------------------------------


class TestParseLastUpdated:
    """Unit tests for _parse_last_updated."""

    def test_valid_date(self):
        from skcapstone.cli.profile_cmd import _parse_last_updated
        result = _parse_last_updated("2026-03-02")
        assert result == date(2026, 3, 2)

    def test_empty_string_returns_none(self):
        from skcapstone.cli.profile_cmd import _parse_last_updated
        assert _parse_last_updated("") is None

    def test_invalid_format_returns_none(self):
        from skcapstone.cli.profile_cmd import _parse_last_updated
        assert _parse_last_updated("not-a-date") is None
