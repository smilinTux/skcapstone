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

from skcapstone.codex_setup import ensure_codex_setup
from skcapstone.doctor import (
    Check,
    DiagnosticReport,
    _check_codex,
    _check_agent_home,
    _check_harness_env,
    _check_yolo,
    _check_identity,
    _check_identity_consistency,
    _provisioned_agents,
    _scan_capauth_local,
    _check_memory,
    _check_packages,
    run_fixes,
    run_diagnostics,
    run_fixes,
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


class TestCheckCodex:
    """Test Codex SK agent bootstrap checks and fixes."""

    def test_codex_missing_bootstrap_fails_when_codex_home_set(self, tmp_path, monkeypatch):
        """A detected Codex home without bootstrap is reported as fixable."""
        codex_home = tmp_path / ".codex"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        checks = _check_codex()
        check = next(c for c in checks if c.name == "codex:agent_context")

        assert not check.passed
        assert "missing" in check.detail
        assert check.fix == "skcapstone doctor --fix"

    def test_codex_bootstrap_fix_creates_loader_and_agents(self, tmp_path, monkeypatch):
        """doctor fixes create the loader script and global AGENTS.md guidance."""
        codex_home = tmp_path / ".codex"
        agent_home = tmp_path / ".skcapstone"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setenv("SKAGENT", "jarvis")

        report = DiagnosticReport(checks=[
            Check(
                name="codex:agent_context",
                description="Codex SK agent context bootstrap",
                passed=False,
                category="codex",
            )
        ])

        results = run_fixes(report, agent_home)

        assert results[0].success
        loader = codex_home / "bin" / "load-sk-agent-context.sh"
        agents = codex_home / "AGENTS.md"
        assert loader.exists()
        assert loader.stat().st_mode & 0o100
        agents_text = agents.read_text(encoding="utf-8")
        assert "SKCAPSTONE_CODEX_AGENT_CONTEXT_START" in agents_text
        assert "jarvis" in agents_text
        assert str(loader) in agents_text

        checks = _check_codex()
        assert next(c for c in checks if c.name == "codex:agent_context").passed

    def test_codex_fix_preserves_functional_custom_loader(self, tmp_path, monkeypatch):
        """Existing working loader scripts are not overwritten."""
        codex_home = tmp_path / ".codex"
        loader = codex_home / "bin" / "load-sk-agent-context.sh"
        loader.parent.mkdir(parents=True)
        custom_loader = "#!/usr/bin/env bash\nSKAGENT=x SKCAPSTONE_AGENT=x SKMEMORY_AGENT=x skcapstone status; skmemory ritual; skwhisper status\n"
        loader.write_text(custom_loader, encoding="utf-8")

        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        ensure_codex_setup()

        assert loader.read_text(encoding="utf-8") == custom_loader


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

    def test_doctor_help(self):
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


def _write_claude_config(home_root: Path, *, claude_json: dict, settings: dict | None = None,
                         mcp_json: dict | None = None) -> Path:
    """Lay down a fake Claude Code config tree under *home_root*. Returns the
    .claude config dir."""
    (home_root / ".claude.json").write_text(json.dumps(claude_json))
    cc = home_root / ".claude"
    cc.mkdir(exist_ok=True)
    if settings is not None:
        (cc / "settings.json").write_text(json.dumps(settings))
    if mcp_json is not None:
        (cc / "mcp.json").write_text(json.dumps(mcp_json))
    return cc


class TestCheckHarnessEnv:
    """Test the AI-harness (Claude Code) environment checks."""

    def _by_name(self, checks):
        return {c.name: c for c in checks}

    def test_gate_when_no_claude_code(self, tmp_path, monkeypatch):
        """No ~/.claude.json → one informational passing check, no failures."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        checks = _check_harness_env(tmp_path / ".skcapstone")
        assert len(checks) == 1
        assert checks[0].name == "harness:claude-code"
        assert checks[0].passed is True

    def test_registered_mcp_servers_pass(self, tmp_path, monkeypatch):
        """Servers present in ~/.claude.json mcpServers pass."""
        cc = _write_claude_config(tmp_path, claude_json={
            "mcpServers": {"skmemory": {}, "skcapstone": {}, "skchat": {}},
        })
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))
        by = self._by_name(_check_harness_env(tmp_path / ".skcapstone"))
        assert by["harness:mcp:skmemory"].passed is True
        assert by["harness:mcp:skcapstone"].passed is True
        assert by["harness:mcp:skchat"].passed is True

    def test_dead_config_is_detected(self, tmp_path, monkeypatch):
        """A server defined only in settings.json/mcp.json (ignored by CC) fails
        with a dead-config detail and a `claude mcp add` fix hint."""
        cc = _write_claude_config(
            tmp_path,
            claude_json={"mcpServers": {}},
            settings={"mcpServers": {"skmemory": {}}},
            mcp_json={"skchat": {}},
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))
        by = self._by_name(_check_harness_env(tmp_path / ".skcapstone"))
        assert by["harness:mcp:skmemory"].passed is False
        assert "ONLY" in by["harness:mcp:skmemory"].detail
        assert "claude mcp add skmemory" in by["harness:mcp:skmemory"].fix

    def test_unregistered_mcp_detected(self, tmp_path, monkeypatch):
        """A server registered nowhere fails with a 'not registered' detail."""
        cc = _write_claude_config(tmp_path, claude_json={"mcpServers": {}})
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))
        by = self._by_name(_check_harness_env(tmp_path / ".skcapstone"))
        assert by["harness:mcp:skcapstone"].passed is False
        assert by["harness:mcp:skcapstone"].detail == "not registered"

    def test_stale_hook_binary_detected(self, tmp_path, monkeypatch):
        """A hook pointing at an existing-but-different skcapstone than the one
        on PATH (the stale-install trap) is flagged."""
        live = tmp_path / "skenv" / "skcapstone"
        stale = tmp_path / "pyenv" / "skcapstone"
        live.parent.mkdir(); stale.parent.mkdir()
        live.write_text("#live"); stale.write_text("#stale")
        cc = _write_claude_config(
            tmp_path,
            claude_json={"mcpServers": {"skmemory": {}, "skcapstone": {}, "skchat": {}}},
            settings={"hooks": {"SessionStart": [{"hooks": [
                {"type": "command", "command": f"{stale} context show --format claude-md"}]}]}},
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))
        monkeypatch.setattr("skcapstone.doctor.shutil.which",
                            lambda name: str(live) if name == "skcapstone" else None)
        by = self._by_name(_check_harness_env(tmp_path / ".skcapstone"))
        c = by["harness:hook:sessionstart"]
        assert c.passed is False
        assert "stale" in c.detail.lower()

    def test_hook_on_live_binary_passes(self, tmp_path, monkeypatch):
        """A hook pointing at the live skcapstone passes."""
        live = tmp_path / "skenv" / "skcapstone"
        live.parent.mkdir(); live.write_text("#live")
        cc = _write_claude_config(
            tmp_path,
            claude_json={"mcpServers": {"skmemory": {}, "skcapstone": {}, "skchat": {}}},
            settings={"hooks": {"SessionStart": [{"hooks": [
                {"type": "command", "command": f"{live} context show --format claude-md"}]}]}},
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))
        monkeypatch.setattr("skcapstone.doctor.shutil.which",
                            lambda name: str(live) if name == "skcapstone" else None)
        by = self._by_name(_check_harness_env(tmp_path / ".skcapstone"))
        assert by["harness:hook:sessionstart"].passed is True

    def test_non_binary_hook_under_skcapstone_repos_ignored(self, tmp_path, monkeypatch):
        """A hook script whose PATH merely contains 'skcapstone' (e.g. one under
        skcapstone-repos/) must NOT be treated as a stale skcapstone binary."""
        live = tmp_path / "skenv" / "skcapstone"
        live.parent.mkdir(); live.write_text("#live")
        # A real-world false-positive: a skmemory hook living under a
        # skcapstone-repos/ checkout — its basename is NOT 'skcapstone'.
        script = tmp_path / "skcapstone-repos" / "skmemory" / "hooks" / "sk-activity-inject.sh"
        script.parent.mkdir(parents=True); script.write_text("#!/bin/sh\n")
        cc = _write_claude_config(
            tmp_path,
            claude_json={"mcpServers": {}},
            settings={"hooks": {"SessionStart": [{"hooks": [
                {"type": "command", "command": str(script)}]}]}},
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))
        monkeypatch.setattr("skcapstone.doctor.shutil.which",
                            lambda name: str(live) if name == "skcapstone" else None)
        by = self._by_name(_check_harness_env(tmp_path / ".skcapstone"))
        # No skcapstone-binary hook present → the check emits no sessionstart result.
        assert "harness:hook:sessionstart" not in by

    def test_fix_does_not_rewrite_non_binary_hook(self, tmp_path, monkeypatch):
        """run_fixes must not destructively rewrite a non-binary hook whose path
        merely contains 'skcapstone'."""
        live = tmp_path / "skenv" / "skcapstone"
        live.parent.mkdir(); live.write_text("#live")
        script = tmp_path / "skcapstone-repos" / "hooks" / "inject.sh"
        script.parent.mkdir(parents=True); script.write_text("#!/bin/sh\n")
        cc = _write_claude_config(
            tmp_path,
            claude_json={"mcpServers": {}},
            settings={"hooks": {"SessionStart": [{"hooks": [
                {"type": "command", "command": str(script)}]}]}},
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))
        monkeypatch.setattr("skcapstone.doctor.shutil.which",
                            lambda name: str(live) if name == "skcapstone" else None)
        report = DiagnosticReport()
        report.checks.append(Check(name="harness:hook:sessionstart",
                                   description="x", passed=False, category="harness"))
        run_fixes(report, tmp_path / ".skcapstone")
        # The script path must be left untouched (not rewritten to the binary).
        updated = json.loads((cc / "settings.json").read_text())
        assert updated["hooks"]["SessionStart"][0]["hooks"][0]["command"] == str(script)

    def test_fix_repoints_stale_hook(self, tmp_path, monkeypatch):
        """run_fixes rewrites a stale SessionStart hook to the live binary."""
        live = tmp_path / "skenv" / "skcapstone"
        live.parent.mkdir(); live.write_text("#live")
        cc = _write_claude_config(
            tmp_path,
            claude_json={"mcpServers": {}},
            settings={"hooks": {"SessionStart": [{"hooks": [
                {"type": "command", "command": "/old/path/skcapstone context show --format claude-md"}]}]}},
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))
        monkeypatch.setattr("skcapstone.doctor.shutil.which",
                            lambda name: str(live) if name == "skcapstone" else None)
        report = DiagnosticReport()
        report.checks.append(Check(name="harness:hook:sessionstart",
                                   description="x", passed=False, category="harness"))
        results = run_fixes(report, tmp_path / ".skcapstone")
        assert any(r.success and r.check_name == "harness:hook:sessionstart" for r in results)
        updated = json.loads((cc / "settings.json").read_text())
        new_cmd = updated["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert new_cmd.split()[0] == str(live)


class TestCheckYolo:
    """Permission-bypass (SK_*_YOLO) wiring checks."""

    @staticmethod
    def _by_name(checks):
        return {c.name: c for c in checks}

    def _clean_env(self, monkeypatch, tmp_path):
        """Point HOME at tmp_path and clear all YOLO vars."""
        monkeypatch.setenv("HOME", str(tmp_path))
        for var in ("SK_CLAUDE_YOLO", "SK_CODEX_YOLO", "SK_OPENCODE_YOLO"):
            monkeypatch.delenv(var, raising=False)

    def test_default_off_reports_safe(self, tmp_path, monkeypatch):
        """No env var and no rc persistence → single safe-default summary."""
        self._clean_env(monkeypatch, tmp_path)
        checks = _check_yolo()
        assert len(checks) == 1
        assert checks[0].name == "harness:yolo"
        assert checks[0].passed is True
        assert "disabled" in checks[0].detail

    def test_enabled_globally_passes(self, tmp_path, monkeypatch):
        """Env set AND persisted in ~/.bashrc → ENABLED, passing."""
        self._clean_env(monkeypatch, tmp_path)
        (tmp_path / ".bashrc").write_text("export SK_CLAUDE_YOLO=1\n")
        monkeypatch.setenv("SK_CLAUDE_YOLO", "1")
        by = self._by_name(_check_yolo())
        assert by["harness:yolo:claude"].passed is True
        assert "ENABLED" in by["harness:yolo:claude"].detail

    def test_active_but_not_persisted_warns(self, tmp_path, monkeypatch):
        """Env set but no rc persistence → warns with a fix hint."""
        self._clean_env(monkeypatch, tmp_path)
        (tmp_path / ".bashrc").write_text("# nothing here\n")
        monkeypatch.setenv("SK_CLAUDE_YOLO", "1")
        by = self._by_name(_check_yolo())
        assert by["harness:yolo:claude"].passed is False
        assert "NOT persisted" in by["harness:yolo:claude"].detail
        assert "export SK_CLAUDE_YOLO=1" in by["harness:yolo:claude"].fix

    def test_persisted_not_in_env_passes(self, tmp_path, monkeypatch):
        """Persisted in rc but not yet in env (fresh shell) → informational pass."""
        self._clean_env(monkeypatch, tmp_path)
        (tmp_path / ".bashrc").write_text("export SK_CODEX_YOLO=1\n")
        by = self._by_name(_check_yolo())
        assert by["harness:yolo:codex"].passed is True
        assert "re-source" in by["harness:yolo:codex"].detail


def _mk_agent(home, name, *, capauth=True, identity=True, identity_payload=None):
    """Create an agent dir under home/agents with optional capauth + identity."""
    adir = home / "agents" / name
    (adir / "identity").mkdir(parents=True, exist_ok=True)
    if capauth:
        (adir / "capauth").mkdir(parents=True, exist_ok=True)
    if identity:
        payload = identity_payload or {
            "name": name.capitalize(),
            "capauth_managed": True,
            "capauth_uri": f"capauth:{name}@skworld.io",
        }
        (adir / "identity" / "identity.json").write_text(json.dumps(payload))
    return adir


@pytest.fixture
def identity_home(tmp_path):
    """A home with a shared operator identity + two provisioned agents."""
    home = tmp_path / ".skcapstone"
    (home / "identity").mkdir(parents=True, exist_ok=True)
    (home / "identity" / "identity.json").write_text(json.dumps({
        "name": "Chef", "role": "operator", "capauth_managed": True,
        "capauth_uri": "capauth:chef@skworld.io",
    }))
    _mk_agent(home, "lumina")
    _mk_agent(home, "opus")
    return home


class TestProvisionedAgents:
    """_provisioned_agents: only capauth-backed, non-template dirs count."""

    def test_lists_capauth_agents(self, identity_home):
        assert _provisioned_agents(identity_home) == ["lumina", "opus"]

    def test_excludes_templates_and_scaffolds(self, identity_home):
        _mk_agent(identity_home, "lumina-template")          # template → excluded
        _mk_agent(identity_home, "scaffold", capauth=False)  # no capauth → excluded
        assert _provisioned_agents(identity_home) == ["lumina", "opus"]

    def test_no_agents_dir(self, tmp_path):
        assert _provisioned_agents(tmp_path / ".skcapstone") == []


class TestScanCapauthLocal:
    """_scan_capauth_local: surfaces the @capauth.local placeholder."""

    def test_clean_home(self, identity_home):
        assert _scan_capauth_local(identity_home) == []

    def test_detects_placeholder(self, identity_home):
        _mk_agent(identity_home, "stale", identity_payload={
            "name": "Stale", "email": "stale@capauth.local",
        })
        hits = _scan_capauth_local(identity_home)
        assert any("stale" in h for h in hits)


class TestIdentityConsistency:
    """_check_identity_consistency: the unified identity layer (skos T6)."""

    def _by_name(self, checks):
        return {c.name: c for c in checks}

    def test_operator_and_per_agent_pass(self, identity_home):
        by = self._by_name(_check_identity_consistency(identity_home))
        assert by["identity:operator"].passed is True
        assert by["identity:no-placeholder"].passed is True
        assert by["identity:per-agent"].passed is True
        assert "all present" in by["identity:per-agent"].detail

    def test_shared_not_operator_fails(self, tmp_path):
        home = tmp_path / ".skcapstone"
        (home / "identity").mkdir(parents=True)
        (home / "identity" / "identity.json").write_text(json.dumps({
            "name": "test-agent", "role": "agent",
        }))
        by = self._by_name(_check_identity_consistency(home))
        assert by["identity:operator"].passed is False
        assert "expected 'operator'" in by["identity:operator"].detail

    def test_placeholder_fails(self, identity_home):
        _mk_agent(identity_home, "stale", identity_payload={
            "name": "Stale", "email": "stale@capauth.local",
        })
        by = self._by_name(_check_identity_consistency(identity_home))
        assert by["identity:no-placeholder"].passed is False

    def test_missing_per_agent_identity_fails(self, identity_home):
        # provisioned (has capauth) but no identity.json
        _mk_agent(identity_home, "ghost", identity=False)
        by = self._by_name(_check_identity_consistency(identity_home))
        assert by["identity:per-agent"].passed is False
        assert "ghost" in by["identity:per-agent"].detail

    def test_resolver_importable(self, identity_home):
        """The canonical resolver check reports importability of capauth."""
        by = self._by_name(_check_identity_consistency(identity_home))
        assert "identity:resolver" in by
        # capauth is a hard dependency of the suite; resolver must import.
        assert by["identity:resolver"].passed is True
