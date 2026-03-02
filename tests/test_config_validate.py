"""
Tests for skcapstone.config_validator.

Covers:
- validate_consciousness_yaml: valid, type errors, syntax error, unknown key
- validate_model_profiles_yaml: valid, missing key, bad regex, enum error
- validate_identity_json: valid, missing field, JSON parse error, bad fingerprint
- validate_soul_blueprint_json: valid, missing required field, bad type
- validate_all: integration — correct files collected, missing files are warnings
- CLI smoke: skcapstone config validate --json-out
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from skcapstone.config_validator import (
    ConfigValidationReport,
    FileValidationResult,
    ValidationIssue,
    validate_all,
    validate_consciousness_yaml,
    validate_identity_json,
    validate_model_profiles_yaml,
    validate_soul_blueprint_json,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    """Create a minimal agent home directory structure."""
    home = tmp_path / ".skcapstone"
    for d in ("config", "identity", "soul", "soul/installed"):
        (home / d).mkdir(parents=True, exist_ok=True)
    return home


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# consciousness.yaml tests
# ---------------------------------------------------------------------------


class TestConsciousnessYaml:
    """Tests for validate_consciousness_yaml()."""

    def test_valid_config_passes(self, tmp_path: Path) -> None:
        """Happy path: well-formed consciousness.yaml produces no errors."""
        path = tmp_path / "consciousness.yaml"
        _write(path, """
enabled: true
use_inotify: true
inotify_debounce_ms: 200
response_timeout: 120
max_context_tokens: 8000
max_history_messages: 10
auto_memory: true
auto_ack: true
privacy_default: false
max_concurrent_requests: 3
fallback_chain:
  - ollama
  - anthropic
  - passthrough
desktop_notifications: true
""")
        result = validate_consciousness_yaml(path)
        assert result.is_valid, f"Unexpected errors: {result.errors}"
        assert result.errors == []

    def test_missing_file_is_warning_not_error(self, tmp_path: Path) -> None:
        """Missing file produces a warning, not an error."""
        path = tmp_path / "consciousness.yaml"
        result = validate_consciousness_yaml(path)
        assert not result.found
        assert result.is_valid  # missing → valid (uses defaults)
        assert len(result.warnings) == 1
        assert result.errors == []

    def test_bool_field_wrong_type_is_error(self, tmp_path: Path) -> None:
        """Passing a string for a bool field reports an error with the field name."""
        path = tmp_path / "consciousness.yaml"
        _write(path, "enabled: yes_please\n")
        result = validate_consciousness_yaml(path)
        err_fields = [e.field for e in result.errors]
        assert "enabled" in err_fields

    def test_int_field_wrong_type_is_error(self, tmp_path: Path) -> None:
        """Passing a string for an int field reports an error."""
        path = tmp_path / "consciousness.yaml"
        _write(path, "response_timeout: fast\n")
        result = validate_consciousness_yaml(path)
        err_fields = [e.field for e in result.errors]
        assert "response_timeout" in err_fields

    def test_int_field_zero_is_error(self, tmp_path: Path) -> None:
        """response_timeout: 0 must report an error (must be > 0)."""
        path = tmp_path / "consciousness.yaml"
        _write(path, "response_timeout: 0\n")
        result = validate_consciousness_yaml(path)
        assert any(e.field == "response_timeout" for e in result.errors)

    def test_yaml_syntax_error_reports_line(self, tmp_path: Path) -> None:
        """A YAML syntax error is an error and the line number is set."""
        path = tmp_path / "consciousness.yaml"
        _write(path, "enabled: true\nbroken: [\n")
        result = validate_consciousness_yaml(path)
        assert len(result.errors) == 1
        assert result.errors[0].line is not None
        assert result.errors[0].line >= 1

    def test_unknown_key_is_warning(self, tmp_path: Path) -> None:
        """An unknown top-level key generates a warning, not an error."""
        path = tmp_path / "consciousness.yaml"
        _write(path, "enabled: true\nmy_custom_setting: 42\n")
        result = validate_consciousness_yaml(path)
        assert result.is_valid  # no errors
        warn_fields = [w.field for w in result.warnings]
        assert "my_custom_setting" in warn_fields

    def test_fallback_chain_unknown_backend_is_warning(self, tmp_path: Path) -> None:
        """An unknown backend in fallback_chain is a warning, not an error."""
        path = tmp_path / "consciousness.yaml"
        _write(path, "fallback_chain:\n  - my_custom_backend\n")
        result = validate_consciousness_yaml(path)
        assert result.is_valid
        assert any("my_custom_backend" in w.message for w in result.warnings)

    def test_fallback_chain_not_list_is_error(self, tmp_path: Path) -> None:
        """A non-list fallback_chain reports an error."""
        path = tmp_path / "consciousness.yaml"
        _write(path, "fallback_chain: ollama\n")
        result = validate_consciousness_yaml(path)
        assert any(e.field == "fallback_chain" for e in result.errors)


# ---------------------------------------------------------------------------
# model_profiles.yaml tests
# ---------------------------------------------------------------------------


class TestModelProfilesYaml:
    """Tests for validate_model_profiles_yaml()."""

    def test_valid_profiles_pass(self, tmp_path: Path) -> None:
        """Happy path: valid profiles list produces no errors."""
        path = tmp_path / "model_profiles.yaml"
        _write(path, """
profiles:
  - model_pattern: "claude-.*"
    family: claude
    system_prompt_mode: separate_param
    structure_format: xml
    thinking_enabled: true
    thinking_mode: budget
    tool_format: anthropic
""")
        result = validate_model_profiles_yaml(path)
        assert result.is_valid, f"Unexpected errors: {result.errors}"

    def test_missing_profiles_key_is_error(self, tmp_path: Path) -> None:
        """YAML without a top-level 'profiles' key is an error."""
        path = tmp_path / "model_profiles.yaml"
        _write(path, "family: openai\n")
        result = validate_model_profiles_yaml(path)
        err_fields = [e.field for e in result.errors]
        assert "profiles" in err_fields

    def test_missing_required_field_in_profile(self, tmp_path: Path) -> None:
        """A profile missing 'model_pattern' or 'family' reports an error."""
        path = tmp_path / "model_profiles.yaml"
        _write(path, """
profiles:
  - family: openai
""")
        result = validate_model_profiles_yaml(path)
        assert any("model_pattern" in (e.field or "") for e in result.errors)

    def test_invalid_regex_in_model_pattern(self, tmp_path: Path) -> None:
        """An invalid regex in model_pattern reports an error."""
        path = tmp_path / "model_profiles.yaml"
        _write(path, """
profiles:
  - model_pattern: "[invalid("
    family: broken
""")
        result = validate_model_profiles_yaml(path)
        assert any("model_pattern" in (e.field or "") for e in result.errors)
        assert any("regex" in e.message.lower() for e in result.errors)

    def test_invalid_enum_value_is_error(self, tmp_path: Path) -> None:
        """An out-of-range enum value (e.g. structure_format) is an error."""
        path = tmp_path / "model_profiles.yaml"
        _write(path, """
profiles:
  - model_pattern: ".*"
    family: generic
    structure_format: html
""")
        result = validate_model_profiles_yaml(path)
        assert any("structure_format" in (e.field or "") for e in result.errors)

    def test_line_numbers_reported_for_profile_error(self, tmp_path: Path) -> None:
        """Error on a known profile field includes a non-None line number."""
        path = tmp_path / "model_profiles.yaml"
        content = (
            "profiles:\n"
            "  - model_pattern: \"[bad(\"\n"
            "    family: x\n"
        )
        _write(path, content)
        result = validate_model_profiles_yaml(path)
        regex_errors = [e for e in result.errors if "model_pattern" in (e.field or "")]
        assert regex_errors
        assert regex_errors[0].line is not None

    def test_missing_file_is_warning(self, tmp_path: Path) -> None:
        """Missing model_profiles.yaml is a warning (bundled defaults used)."""
        path = tmp_path / "model_profiles.yaml"
        result = validate_model_profiles_yaml(path)
        assert not result.found
        assert result.is_valid
        assert len(result.warnings) == 1

    def test_yaml_syntax_error_is_error(self, tmp_path: Path) -> None:
        """A YAML syntax error in model_profiles.yaml is reported as an error."""
        path = tmp_path / "model_profiles.yaml"
        _write(path, "profiles:\n  - broken: [\n")
        result = validate_model_profiles_yaml(path)
        assert len(result.errors) == 1


# ---------------------------------------------------------------------------
# identity.json tests
# ---------------------------------------------------------------------------


class TestIdentityJson:
    """Tests for validate_identity_json()."""

    def test_valid_identity_passes(self, tmp_path: Path) -> None:
        """Happy path: identity.json with all required fields passes."""
        path = tmp_path / "identity.json"
        _write(path, json.dumps({
            "name": "Opus",
            "fingerprint": "A" * 40,
            "email": "opus@skworld.io",
            "capauth_managed": True,
        }))
        result = validate_identity_json(path)
        assert result.is_valid, f"Unexpected errors: {result.errors}"

    def test_missing_name_is_error(self, tmp_path: Path) -> None:
        """identity.json without 'name' is an error."""
        path = tmp_path / "identity.json"
        _write(path, json.dumps({"fingerprint": "A" * 40}))
        result = validate_identity_json(path)
        assert any(e.field == "name" for e in result.errors)

    def test_missing_fingerprint_is_error(self, tmp_path: Path) -> None:
        """identity.json without 'fingerprint' is an error."""
        path = tmp_path / "identity.json"
        _write(path, json.dumps({"name": "Opus"}))
        result = validate_identity_json(path)
        assert any(e.field == "fingerprint" for e in result.errors)

    def test_short_fingerprint_is_warning(self, tmp_path: Path) -> None:
        """A fingerprint that doesn't match 40-hex-char format is a warning."""
        path = tmp_path / "identity.json"
        _write(path, json.dumps({"name": "Opus", "fingerprint": "DEADBEEF"}))
        result = validate_identity_json(path)
        assert result.is_valid  # warning only
        assert any(e.field == "fingerprint" for e in result.warnings)

    def test_json_parse_error_reports_line(self, tmp_path: Path) -> None:
        """A JSON syntax error is an error and the line number is set."""
        path = tmp_path / "identity.json"
        _write(path, '{"name": "Opus"\n  broken\n}')
        result = validate_identity_json(path)
        assert len(result.errors) == 1
        assert result.errors[0].line is not None
        assert result.errors[0].line >= 1

    def test_missing_file_is_warning(self, tmp_path: Path) -> None:
        """Missing identity.json is a warning (not yet initialised)."""
        path = tmp_path / "identity.json"
        result = validate_identity_json(path)
        assert not result.found
        assert result.is_valid
        assert len(result.warnings) == 1

    def test_empty_name_is_error(self, tmp_path: Path) -> None:
        """An empty string for 'name' is an error."""
        path = tmp_path / "identity.json"
        _write(path, json.dumps({"name": "   ", "fingerprint": "A" * 40}))
        result = validate_identity_json(path)
        assert any(e.field == "name" for e in result.errors)


# ---------------------------------------------------------------------------
# Soul blueprint JSON tests
# ---------------------------------------------------------------------------


class TestSoulBlueprintJson:
    """Tests for validate_soul_blueprint_json()."""

    def test_valid_blueprint_passes(self, tmp_path: Path) -> None:
        """Happy path: blueprint with all required fields passes."""
        path = tmp_path / "lumina.json"
        _write(path, json.dumps({
            "name": "lumina",
            "display_name": "Lumina",
            "category": "sovereign",
            "vibe": "warmth and clarity",
            "core_traits": ["empathy", "precision"],
            "emotional_topology": {"warmth": 0.9, "precision": 0.8},
        }))
        result = validate_soul_blueprint_json(path)
        assert result.is_valid, f"Unexpected errors: {result.errors}"

    def test_missing_name_is_error(self, tmp_path: Path) -> None:
        """Blueprint without 'name' is an error."""
        path = tmp_path / "missing_name.json"
        _write(path, json.dumps({"display_name": "Lumina"}))
        result = validate_soul_blueprint_json(path)
        assert any(e.field == "name" for e in result.errors)

    def test_missing_display_name_is_error(self, tmp_path: Path) -> None:
        """Blueprint without 'display_name' is an error."""
        path = tmp_path / "missing_dn.json"
        _write(path, json.dumps({"name": "lumina"}))
        result = validate_soul_blueprint_json(path)
        assert any(e.field == "display_name" for e in result.errors)

    def test_core_traits_wrong_type_is_error(self, tmp_path: Path) -> None:
        """core_traits must be a list."""
        path = tmp_path / "bad_traits.json"
        _write(path, json.dumps({
            "name": "lumina", "display_name": "Lumina",
            "core_traits": "empathy, precision",
        }))
        result = validate_soul_blueprint_json(path)
        assert any(e.field == "core_traits" for e in result.errors)

    def test_emotional_topology_non_numeric_is_error(self, tmp_path: Path) -> None:
        """emotional_topology values must be numeric."""
        path = tmp_path / "bad_topo.json"
        _write(path, json.dumps({
            "name": "lumina", "display_name": "Lumina",
            "emotional_topology": {"warmth": "high"},
        }))
        result = validate_soul_blueprint_json(path)
        assert any("emotional_topology" in (e.field or "") for e in result.errors)

    def test_json_parse_error_is_error(self, tmp_path: Path) -> None:
        """Invalid JSON in a soul file is reported as an error with a line number."""
        path = tmp_path / "broken.json"
        _write(path, '{"name": "x"\n  oops\n}')
        result = validate_soul_blueprint_json(path)
        assert len(result.errors) == 1
        assert result.errors[0].line is not None


# ---------------------------------------------------------------------------
# validate_all integration tests
# ---------------------------------------------------------------------------


class TestValidateAll:
    """Integration tests for validate_all()."""

    def test_empty_home_produces_warnings_not_errors(self, agent_home: Path) -> None:
        """validate_all on a home with no config files produces warnings only."""
        report = validate_all(agent_home)
        assert report.total_errors == 0
        # At least consciousness.yaml + model_profiles.yaml + identity.json are checked
        assert len(report.results) >= 3
        # All missing files produce warnings
        assert report.total_warnings >= 3

    def test_valid_configs_produce_clean_report(self, agent_home: Path) -> None:
        """A home with all valid configs reports no errors or warnings (except soul)."""
        # consciousness.yaml
        _write(agent_home / "config" / "consciousness.yaml",
               "enabled: true\nfallback_chain:\n  - ollama\n")
        # identity.json
        _write(agent_home / "identity" / "identity.json", json.dumps({
            "name": "Opus",
            "fingerprint": "A" * 40,
        }))

        report = validate_all(agent_home)
        assert report.total_errors == 0

    def test_identity_error_is_counted(self, agent_home: Path) -> None:
        """An error in identity.json is reflected in the report totals."""
        _write(agent_home / "identity" / "identity.json",
               json.dumps({"fingerprint": "A" * 40}))  # missing 'name'
        report = validate_all(agent_home)
        assert report.total_errors >= 1

    def test_soul_installed_blueprints_are_validated(self, agent_home: Path) -> None:
        """Installed soul blueprints are included in validate_all."""
        soul_dir = agent_home / "soul" / "installed"
        _write(soul_dir / "lumina.json", json.dumps({
            "name": "lumina", "display_name": "Lumina",
        }))
        _write(soul_dir / "broken.json", '{"name": "x"}')  # missing display_name

        report = validate_all(agent_home)
        config_names = [r.config_name for r in report.results]
        assert any("lumina.json" in n for n in config_names)
        assert any("broken.json" in n for n in config_names)
        # broken.json is missing display_name → error
        broken = next(r for r in report.results if "broken.json" in r.config_name)
        assert not broken.is_valid

    def test_report_is_valid_iff_no_errors(self, agent_home: Path) -> None:
        """ConfigValidationReport.is_valid is False when any result has errors."""
        _write(agent_home / "identity" / "identity.json",
               json.dumps({"fingerprint": "tooshort"}))  # missing 'name'
        report = validate_all(agent_home)
        assert not report.is_valid

    def test_validate_all_soul_dir_absent(self, tmp_path: Path) -> None:
        """When soul/ directory doesn't exist, validate_all still runs."""
        home = tmp_path / ".skcapstone"
        (home / "config").mkdir(parents=True)
        (home / "identity").mkdir()
        # soul/ intentionally absent
        report = validate_all(home)
        config_names = [r.config_name for r in report.results]
        assert not any("soul/" in n for n in config_names)


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


class TestConfigValidateCli:
    """CLI smoke tests for `skcapstone config validate`."""

    def test_cli_json_output_structure(self, agent_home: Path) -> None:
        """--json-out emits a JSON object with 'valid', 'files', etc."""
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--agent", "", "config", "validate",
             "--home", str(agent_home), "--json-out"],
        )
        assert result.exit_code in (0, 1), result.output
        data = json.loads(result.output)
        assert "valid" in data
        assert "total_errors" in data
        assert "total_warnings" in data
        assert "files" in data
        assert isinstance(data["files"], list)

    def test_cli_exits_zero_when_valid(self, agent_home: Path) -> None:
        """CLI exits 0 when all configs are valid (missing files are warnings)."""
        from skcapstone.cli import main

        # Write a valid identity so the only issues are warnings (missing files)
        _write(agent_home / "identity" / "identity.json", json.dumps({
            "name": "TestAgent",
            "fingerprint": "A" * 40,
        }))
        # A fully valid consciousness config
        _write(agent_home / "config" / "consciousness.yaml",
               "enabled: true\n")

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["config", "validate", "--home", str(agent_home)],
        )
        # Warnings are present (missing model_profiles, soul) but exit 0
        assert result.exit_code == 0

    def test_cli_exits_one_on_error(self, agent_home: Path) -> None:
        """CLI exits 1 when identity.json has a schema error."""
        from skcapstone.cli import main

        _write(agent_home / "identity" / "identity.json",
               json.dumps({"fingerprint": "BADFINGERPRINT"}))  # missing 'name'

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["config", "validate", "--home", str(agent_home)],
        )
        assert result.exit_code == 1

    def test_cli_strict_exits_one_on_warnings(self, agent_home: Path) -> None:
        """--strict causes exit 1 when only warnings are present."""
        from skcapstone.cli import main

        # Valid identity but missing other configs → warnings
        _write(agent_home / "identity" / "identity.json", json.dumps({
            "name": "TestAgent",
            "fingerprint": "A" * 40,
        }))

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["config", "validate", "--home", str(agent_home), "--strict"],
        )
        # model_profiles.yaml and consciousness.yaml are missing → warnings
        # --strict turns those into a failure
        assert result.exit_code == 1
