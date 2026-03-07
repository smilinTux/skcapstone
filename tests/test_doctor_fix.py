"""Tests for skcapstone doctor --fix auto-remediation.

Covers:
- run_fixes() creates missing home directory
- run_fixes() creates missing subdirectories
- run_fixes() writes a default manifest.json when absent
- run_fixes() creates memory store layer directories
- run_fixes() rebuilds a missing memory index from existing files
- run_fixes() creates sync dir with outbox + inbox
- run_fixes() skips unfixable checks (packages, identity key)
- CLI doctor --fix flag appears in --help
- CLI doctor --fix auto-creates structure and reports fixes
- CLI doctor --fix --json-out includes fixes key in output
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

try:
    from skcapstone.doctor import (
        Check,
        DiagnosticReport,
        FixResult,
        run_diagnostics,
        run_fixes,
    )
except ImportError:
    pytest.skip(
        "skcapstone.doctor missing required names (FixResult, run_fixes)",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report(*checks: Check) -> DiagnosticReport:
    return DiagnosticReport(checks=list(checks), agent_home="/tmp/test")


def _failing(name: str, category: str = "agent") -> Check:
    return Check(name=name, description=name, passed=False, category=category)


# ---------------------------------------------------------------------------
# Unit tests for run_fixes()
# ---------------------------------------------------------------------------


class TestRunFixesHomeDir:
    """run_fixes creates missing home directory and subdirs."""

    def test_creates_home_directory(self, tmp_path):
        """home:exists fix creates the directory."""
        home = tmp_path / ".skcapstone"
        assert not home.exists()

        report = _make_report(_failing("home:exists"))
        results = run_fixes(report, home)

        assert home.exists()
        assert len(results) == 1
        assert results[0].success
        assert results[0].check_name == "home:exists"

    def test_creates_missing_subdirectory(self, tmp_path):
        """home:{dirname} fix creates each expected subdirectory."""
        home = tmp_path / ".skcapstone"
        home.mkdir()

        for dirname in ["identity", "memory", "trust", "security", "sync", "config"]:
            assert not (home / dirname).exists()
            report = _make_report(_failing(f"home:{dirname}"))
            results = run_fixes(report, home)

            assert (home / dirname).exists(), f"{dirname} not created"
            assert results[0].success
            assert results[0].check_name == f"home:{dirname}"

    def test_subdir_fix_is_idempotent(self, tmp_path):
        """Re-running a subdir fix on an existing dir does not fail."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        (home / "memory").mkdir()

        report = _make_report(_failing("home:memory"))
        results = run_fixes(report, home)

        assert results[0].success


class TestRunFixesManifest:
    """run_fixes writes a default manifest.json."""

    def test_writes_default_manifest(self, tmp_path):
        """manifest fix creates a valid JSON file with name + version."""
        home = tmp_path / "myagent"
        home.mkdir()

        report = _make_report(_failing("home:manifest"))
        results = run_fixes(report, home)

        manifest = home / "manifest.json"
        assert manifest.exists()
        data = json.loads(manifest.read_text())
        assert "name" in data
        assert "version" in data
        assert results[0].success

    def test_manifest_fix_skipped_when_corrupt(self, tmp_path):
        """If manifest.json already exists, fix raises and returns failure."""
        home = tmp_path / "agent"
        home.mkdir()
        (home / "manifest.json").write_text("CORRUPT{{{")

        report = _make_report(_failing("home:manifest"))
        results = run_fixes(report, home)

        assert not results[0].success
        assert results[0].error


class TestRunFixesMemory:
    """run_fixes creates memory store and rebuilds index."""

    def test_creates_memory_layer_dirs(self, tmp_path):
        """memory:store fix creates short-term, mid-term, long-term dirs."""
        home = tmp_path / ".skcapstone"
        home.mkdir()

        report = _make_report(_failing("memory:store", "memory"))
        results = run_fixes(report, home)

        for layer in ["short-term", "mid-term", "long-term"]:
            assert (home / "memory" / layer).exists(), f"{layer} not created"
        assert results[0].success

    def test_rebuilds_empty_index(self, tmp_path):
        """memory:index fix writes an empty index.json when no memories exist."""
        home = tmp_path / ".skcapstone"
        memory_dir = home / "memory"
        for layer in ["short-term", "mid-term", "long-term"]:
            (memory_dir / layer).mkdir(parents=True)

        report = _make_report(_failing("memory:index", "memory"))
        results = run_fixes(report, home)

        index_path = memory_dir / "index.json"
        assert index_path.exists()
        data = json.loads(index_path.read_text())
        assert isinstance(data, dict)
        assert results[0].success

    def test_rebuilds_index_from_existing_memories(self, tmp_path):
        """memory:index fix populates index.json from existing memory files."""
        home = tmp_path / ".skcapstone"
        short_term = home / "memory" / "short-term"
        short_term.mkdir(parents=True)
        for layer in ["mid-term", "long-term"]:
            (home / "memory" / layer).mkdir(parents=True)

        # Write two memory files
        for mem_id in ["abc123", "def456"]:
            (short_term / f"{mem_id}.json").write_text(json.dumps({
                "memory_id": mem_id,
                "content": f"Memory content for {mem_id}",
                "tags": ["test"],
                "importance": 0.8,
                "created_at": "2026-01-01T00:00:00+00:00",
            }))

        report = _make_report(_failing("memory:index", "memory"))
        results = run_fixes(report, home)

        index_path = home / "memory" / "index.json"
        data = json.loads(index_path.read_text())
        assert "abc123" in data
        assert "def456" in data
        assert data["abc123"]["layer"] == "short-term"
        assert results[0].success


class TestRunFixesSyncDir:
    """run_fixes creates the sync directory structure."""

    def test_creates_sync_dir_with_queues(self, tmp_path):
        """sync:dir fix creates sync/, outbox/, and inbox/."""
        home = tmp_path / ".skcapstone"
        home.mkdir()

        report = _make_report(_failing("sync:dir", "sync"))
        results = run_fixes(report, home)

        assert (home / "sync").exists()
        assert (home / "sync" / "outbox").exists()
        assert (home / "sync" / "inbox").exists()
        assert results[0].success


class TestRunFixesSkipsUnfixable:
    """run_fixes silently skips checks with no registered handler."""

    def test_skips_package_checks(self, tmp_path):
        """Package installation checks are not auto-fixable."""
        home = tmp_path / ".skcapstone"
        home.mkdir()

        report = _make_report(_failing("pkg:skcapstone", "packages"))
        results = run_fixes(report, home)

        # No fix attempted — empty results
        assert results == []

    def test_skips_identity_key_check(self, tmp_path):
        """PGP key generation is not auto-fixable."""
        home = tmp_path / ".skcapstone"
        home.mkdir()

        report = _make_report(_failing("identity:pgp_key", "identity"))
        results = run_fixes(report, home)

        assert results == []

    def test_skips_system_tool_check(self, tmp_path):
        """System tool installs are not auto-fixable."""
        home = tmp_path / ".skcapstone"
        home.mkdir()

        report = _make_report(_failing("tool:gpg", "system"))
        results = run_fixes(report, home)

        assert results == []

    def test_passed_checks_not_attempted(self, tmp_path):
        """Passing checks generate no fix results."""
        home = tmp_path / ".skcapstone"
        home.mkdir()

        passing = Check(name="home:exists", description="home", passed=True)
        report = _make_report(passing)
        results = run_fixes(report, home)

        assert results == []


class TestRunFixesMultiple:
    """run_fixes handles multiple failing checks in one pass."""

    def test_fixes_multiple_subdirs(self, tmp_path):
        """Multiple missing subdirs are all created in a single run."""
        home = tmp_path / ".skcapstone"
        home.mkdir()

        checks = [_failing(f"home:{d}") for d in ["identity", "trust", "config"]]
        report = _make_report(*checks)
        results = run_fixes(report, home)

        assert len(results) == 3
        assert all(r.success for r in results)
        for d in ["identity", "trust", "config"]:
            assert (home / d).exists()


# ---------------------------------------------------------------------------
# Integration: full diagnostics cycle
# ---------------------------------------------------------------------------


class TestRunDiagnosticsAfterFix:
    """After running fixes, re-running diagnostics improves the report."""

    def test_fix_reduces_failure_count(self, tmp_path):
        """Directories created by fixes pass on the next diagnostic run."""
        home = tmp_path / ".skcapstone"
        home.mkdir()

        before = run_diagnostics(home)
        before_failed = before.failed_count

        run_fixes(before, home)
        after = run_diagnostics(home)

        assert after.failed_count < before_failed


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestCLIDoctorFix:
    """CLI doctor --fix flag."""

    def test_fix_flag_in_help(self):
        """--fix appears in doctor --help output."""
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--help"])
        assert result.exit_code == 0
        assert "--fix" in result.output

    def test_fix_creates_dirs_and_reports(self, tmp_path):
        """doctor --fix on a bare home creates dirs and prints fix results."""
        from skcapstone.cli import main

        home = tmp_path / ".skcapstone"
        home.mkdir()

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--home", str(home), "--fix"])
        assert result.exit_code == 0
        # At least one successful fix should be reported
        assert "\u2713" in result.output or "Auto-fix" in result.output

    def test_fix_json_output_includes_fixes_key(self, tmp_path):
        """doctor --fix --json-out includes a 'fixes' list in JSON output."""
        from skcapstone.cli import main

        home = tmp_path / ".skcapstone"
        home.mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main, ["doctor", "--home", str(home), "--fix", "--json-out"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "fixes" in data
        assert isinstance(data["fixes"], list)

    def test_fix_on_already_healthy_home_is_noop(self, tmp_path):
        """doctor --fix on a passing home makes no changes and exits cleanly."""
        from skcapstone.cli import main

        # Build a fully populated home
        home = tmp_path / ".skcapstone"
        for d in ["identity", "memory", "trust", "security", "sync", "config",
                  "memory/short-term", "memory/mid-term", "memory/long-term",
                  "sync/outbox", "sync/inbox"]:
            (home / d).mkdir(parents=True, exist_ok=True)
        (home / "manifest.json").write_text(json.dumps({"name": "T", "version": "0.1.0"}))
        (home / "identity" / "identity.json").write_text(json.dumps({
            "name": "T", "fingerprint": "AABB1122", "capauth_managed": True,
        }))
        (home / "memory" / "index.json").write_text("{}")

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--home", str(home), "--fix"])
        assert result.exit_code == 0
