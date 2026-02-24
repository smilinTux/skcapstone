"""Tests for the skcapstone doctor diagnostics module.

Covers:
- DiagnosticReport structure and properties
- Package checks (installed vs missing)
- Agent home directory checks
- Identity checks (present vs missing)
- Memory store checks
- CLI integration (doctor command via CliRunner)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from skcapstone.doctor import (
    Check,
    DiagnosticReport,
    _check_agent_home,
    _check_identity,
    _check_memory,
    _check_packages,
    run_diagnostics,
)


@pytest.fixture
def agent_home(tmp_path):
    """Create a fully populated agent home for testing."""
    home = tmp_path / ".skcapstone"
    for d in ["identity", "memory", "trust", "security", "sync", "config",
              "memory/short-term", "memory/mid-term", "memory/long-term",
              "sync/outbox", "sync/inbox"]:
        (home / d).mkdir(parents=True, exist_ok=True)

    (home / "manifest.json").write_text(json.dumps({
        "name": "TestAgent", "version": "0.1.0",
    }))
    (home / "identity" / "identity.json").write_text(json.dumps({
        "name": "TestAgent",
        "fingerprint": "AABBCCDD11223344",
        "capauth_managed": True,
    }))
    (home / "config" / "config.yaml").write_text(yaml.dump({"agent_name": "TestAgent"}))
    (home / "memory" / "index.json").write_text("{}")

    # Add a memory file
    (home / "memory" / "short-term" / "mem1.json").write_text(json.dumps({
        "memory_id": "mem1", "content": "test",
    }))

    return home


@pytest.fixture
def empty_home(tmp_path):
    """A non-existent agent home path."""
    return tmp_path / ".skcapstone-nonexistent"


class TestCheck:
    """Test Check dataclass."""

    def test_passing_check(self):
        """Passing check has correct attributes."""
        c = Check(name="test", description="A test", passed=True, detail="ok")
        assert c.passed
        assert c.detail == "ok"

    def test_failing_check_with_fix(self):
        """Failing check carries a fix suggestion."""
        c = Check(name="test", description="Broken", passed=False, fix="run this")
        assert not c.passed
        assert c.fix == "run this"


class TestDiagnosticReport:
    """Test DiagnosticReport properties."""

    def test_counts(self):
        """Report counts passed and failed correctly."""
        report = DiagnosticReport(checks=[
            Check(name="a", description="A", passed=True),
            Check(name="b", description="B", passed=True),
            Check(name="c", description="C", passed=False),
        ])

        assert report.passed_count == 2
        assert report.failed_count == 1
        assert report.total_count == 3
        assert not report.all_passed

    def test_all_passed(self):
        """all_passed is True when everything passes."""
        report = DiagnosticReport(checks=[
            Check(name="a", description="A", passed=True),
        ])
        assert report.all_passed

    def test_to_dict(self):
        """to_dict produces JSON-serializable output."""
        report = DiagnosticReport(
            agent_home="/test",
            checks=[Check(name="x", description="X", passed=True, detail="ok")],
        )
        d = report.to_dict()

        assert d["agent_home"] == "/test"
        assert d["passed"] == 1
        assert d["total"] == 1
        assert len(d["checks"]) == 1
        json.dumps(d)


class TestCheckAgentHome:
    """Test agent home directory checks."""

    def test_existing_home(self, agent_home):
        """Fully populated home passes all checks."""
        checks = _check_agent_home(agent_home)

        names = {c.name for c in checks}
        assert "home:exists" in names
        assert "home:manifest" in names
        assert all(c.passed for c in checks), [c.name for c in checks if not c.passed]

    def test_missing_home(self, empty_home):
        """Missing home directory fails immediately."""
        checks = _check_agent_home(empty_home)

        assert len(checks) == 1
        assert checks[0].name == "home:exists"
        assert not checks[0].passed


class TestCheckIdentity:
    """Test identity checks."""

    def test_identity_present(self, agent_home):
        """Valid identity file passes checks."""
        checks = _check_identity(agent_home)

        profile_check = next(c for c in checks if c.name == "identity:profile")
        assert profile_check.passed
        assert "AABBCCDD" in profile_check.detail

    def test_identity_missing(self, empty_home):
        """Missing identity directory fails."""
        empty_home.mkdir(parents=True, exist_ok=True)
        (empty_home / "identity").mkdir()

        checks = _check_identity(empty_home)
        profile_check = next(c for c in checks if c.name == "identity:profile")
        assert not profile_check.passed


class TestCheckMemory:
    """Test memory store checks."""

    def test_memory_healthy(self, agent_home):
        """Populated memory store passes."""
        checks = _check_memory(agent_home)

        store_check = next(c for c in checks if c.name == "memory:store")
        assert store_check.passed
        assert "1 memories" in store_check.detail

        index_check = next(c for c in checks if c.name == "memory:index")
        assert index_check.passed

    def test_memory_missing(self, empty_home):
        """Missing memory directory fails."""
        empty_home.mkdir(parents=True, exist_ok=True)

        checks = _check_memory(empty_home)
        assert len(checks) == 1
        assert not checks[0].passed


class TestCheckPackages:
    """Test Python package checks."""

    def test_skcapstone_is_installed(self):
        """skcapstone itself should always be importable in tests."""
        checks = _check_packages()
        skcap = next(c for c in checks if c.name == "pkg:skcapstone")
        assert skcap.passed

    def test_missing_package_detected(self):
        """A fake package should fail the check."""
        with patch("skcapstone.doctor.importlib.import_module", side_effect=ImportError):
            checks = _check_packages()
            assert all(not c.passed for c in checks)
            assert all(c.fix for c in checks)


class TestRunDiagnostics:
    """Test the full diagnostic run."""

    def test_full_run_on_populated_home(self, agent_home):
        """Full diagnostics on a populated home produces a report."""
        report = run_diagnostics(agent_home)

        assert report.total_count > 0
        assert report.passed_count > 0
        assert report.agent_home == str(agent_home)

    def test_full_run_on_empty_home(self, empty_home):
        """Full diagnostics on missing home still produces a report."""
        report = run_diagnostics(empty_home)

        assert report.total_count > 0
        assert report.failed_count > 0


class TestCLIDoctorCommand:
    """Test the CLI doctor command via CliRunner."""

    @patch("skcapstone.cli.Path.expanduser")
    def test_doctor_help(self, mock_expand):
        """doctor --help works."""
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--help"])
        assert result.exit_code == 0
        assert "Diagnose" in result.output
        assert "--json-out" in result.output

    def test_doctor_json_output(self, agent_home):
        """doctor --json-out produces valid JSON."""
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--home", str(agent_home), "--json-out"])
        assert result.exit_code == 0

        data = json.loads(result.output)
        assert "passed" in data
        assert "failed" in data
        assert "checks" in data
        assert isinstance(data["checks"], list)

    def test_doctor_human_output(self, agent_home):
        """doctor without --json produces human-readable output."""
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--home", str(agent_home)])
        assert result.exit_code == 0
        assert "Python Packages" in result.output
        assert "passed" in result.output or "checks" in result.output.lower()
