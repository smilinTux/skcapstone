"""Tests for trustee management CLI commands and TrusteeOps.

Covers restart, scale, rotate, health, and logs operations as well
as the audit trail and CLI integration via Click's test runner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from skcapstone._trustee_helpers import write_audit as _write_audit
from skcapstone.blueprints.schema import ProviderType
from skcapstone.team_engine import AgentStatus, DeployedAgent, TeamDeployment, TeamEngine
from skcapstone.trustee_ops import TrusteeOps


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_agent(
    name: str = "test-team-alpha",
    spec_key: str = "alpha",
    status: AgentStatus = AgentStatus.RUNNING,
) -> DeployedAgent:
    """Build a minimal DeployedAgent for tests."""
    return DeployedAgent(
        name=name,
        instance_id=f"test-deploy/{name}",
        blueprint_slug="test-team",
        agent_spec_key=spec_key,
        status=status,
        provider=ProviderType.LOCAL,
        host="localhost",
        pid=12345,
    )


def _make_deployment(
    deployment_id: str = "test-team-1000",
    agents: Dict[str, DeployedAgent] | None = None,
) -> TeamDeployment:
    """Build a minimal TeamDeployment for tests."""
    return TeamDeployment(
        deployment_id=deployment_id,
        blueprint_slug="test-team",
        team_name="Test Team",
        provider=ProviderType.LOCAL,
        agents=agents or {"test-team-alpha": _make_agent()},
        status="running",
    )


@pytest.fixture
def tmp_home(tmp_path: Path) -> Path:
    """Temporary skcapstone home directory."""
    home = tmp_path / ".skcapstone"
    home.mkdir()
    return home


@pytest.fixture
def engine_with_deployment(tmp_home: Path) -> tuple[TeamEngine, TeamDeployment]:
    """TeamEngine with one saved deployment."""
    engine = TeamEngine(home=tmp_home)
    deployment = _make_deployment()
    engine._save_deployment(deployment)
    return engine, deployment


@pytest.fixture
def ops(engine_with_deployment: tuple) -> TrusteeOps:
    """TrusteeOps wrapping the fixture engine."""
    engine, _ = engine_with_deployment
    return TrusteeOps(engine=engine, home=engine._home)


# ---------------------------------------------------------------------------
# Audit trail tests
# ---------------------------------------------------------------------------


class TestAuditTrail:
    """Verify audit entries are written correctly."""

    def test_write_audit_creates_file(self, tmp_home: Path) -> None:
        """Audit log is created on first write."""
        _write_audit("test_action", "dep-123", {"key": "value"}, home=tmp_home)
        audit_path = tmp_home / "coordination" / "audit.log"
        assert audit_path.exists()

    def test_write_audit_entry_structure(self, tmp_home: Path) -> None:
        """Each audit entry is valid JSON with required fields."""
        _write_audit("restart_agent", "dep-456", {"agent_name": "alpha"}, home=tmp_home)
        audit_path = tmp_home / "coordination" / "audit.log"
        entry = json.loads(audit_path.read_text().strip())
        assert entry["action"] == "restart_agent"
        assert entry["deployment_id"] == "dep-456"
        assert entry["agent_name"] == "alpha"
        assert "ts" in entry

    def test_write_audit_appends(self, tmp_home: Path) -> None:
        """Multiple audit writes append without overwriting."""
        for i in range(3):
            _write_audit("action", f"dep-{i}", {}, home=tmp_home)
        audit_path = tmp_home / "coordination" / "audit.log"
        lines = audit_path.read_text().strip().splitlines()
        assert len(lines) == 3

    def test_ops_writes_audit_on_restart(self, ops: TrusteeOps, tmp_home: Path) -> None:
        """TrusteeOps.restart_agent writes an audit entry."""
        ops.restart_agent("test-team-1000")
        audit_path = tmp_home / "coordination" / "audit.log"
        assert audit_path.exists()
        entry = json.loads(audit_path.read_text().strip().splitlines()[-1])
        assert entry["action"] == "restart_agent"


# ---------------------------------------------------------------------------
# TrusteeOps.restart_agent tests
# ---------------------------------------------------------------------------


class TestRestartAgent:
    """Tests for restart_agent method."""

    def test_restart_all_agents(self, ops: TrusteeOps) -> None:
        """Restarting without agent_name restarts all agents."""
        results = ops.restart_agent("test-team-1000")
        assert "test-team-alpha" in results
        assert results["test-team-alpha"] == "restarted"

    def test_restart_specific_agent(self, ops: TrusteeOps) -> None:
        """Restart a specific agent by name."""
        results = ops.restart_agent("test-team-1000", agent_name="test-team-alpha")
        assert results == {"test-team-alpha": "restarted"}

    def test_restart_unknown_deployment_raises(self, ops: TrusteeOps) -> None:
        """ValueError raised for unknown deployment ID."""
        with pytest.raises(ValueError, match="not found"):
            ops.restart_agent("does-not-exist")

    def test_restart_unknown_agent_raises(self, ops: TrusteeOps) -> None:
        """ValueError raised when agent name is not in deployment."""
        with pytest.raises(ValueError, match="not in deployment"):
            ops.restart_agent("test-team-1000", agent_name="ghost-agent")

    def test_restart_updates_status_to_running(
        self, engine_with_deployment: tuple, tmp_home: Path
    ) -> None:
        """After restart, agent status is RUNNING."""
        engine, _ = engine_with_deployment
        ops = TrusteeOps(engine=engine, home=tmp_home)
        ops.restart_agent("test-team-1000")
        deployment = engine.get_deployment("test-team-1000")
        assert deployment.agents["test-team-alpha"].status == AgentStatus.RUNNING

    def test_restart_with_provider_calls_stop_start(self, tmp_home: Path) -> None:
        """Provider stop + start are called during restart."""
        engine = TeamEngine(home=tmp_home)
        mock_provider = MagicMock()
        mock_provider.stop.return_value = True
        mock_provider.start.return_value = True
        engine._provider = mock_provider
        engine._save_deployment(_make_deployment())

        ops = TrusteeOps(engine=engine, home=tmp_home)
        ops.restart_agent("test-team-1000")

        mock_provider.stop.assert_called_once()
        mock_provider.start.assert_called_once()

    def test_restart_handles_provider_error(self, tmp_home: Path) -> None:
        """Provider errors are captured in results, not raised."""
        engine = TeamEngine(home=tmp_home)
        mock_provider = MagicMock()
        mock_provider.stop.side_effect = RuntimeError("process not found")
        engine._provider = mock_provider
        engine._save_deployment(_make_deployment())

        ops = TrusteeOps(engine=engine, home=tmp_home)
        results = ops.restart_agent("test-team-1000")
        assert "error" in results["test-team-alpha"]


# ---------------------------------------------------------------------------
# TrusteeOps.scale_agent tests
# ---------------------------------------------------------------------------


class TestScaleAgent:
    """Tests for scale_agent method."""

    def test_scale_up_adds_instances(self, ops: TrusteeOps) -> None:
        """Scaling up adds the correct number of new instances."""
        result = ops.scale_agent("test-team-1000", "alpha", count=3)
        assert len(result["added"]) == 2
        assert result["removed"] == []
        assert result["current_count"] == 3

    def test_scale_down_removes_instances(self, tmp_home: Path) -> None:
        """Scaling down removes excess instances."""
        agents = {
            "test-team-alpha-1": _make_agent("test-team-alpha-1", "alpha"),
            "test-team-alpha-2": _make_agent("test-team-alpha-2", "alpha"),
            "test-team-alpha-3": _make_agent("test-team-alpha-3", "alpha"),
        }
        engine = TeamEngine(home=tmp_home)
        engine._save_deployment(_make_deployment(agents=agents))
        ops = TrusteeOps(engine=engine, home=tmp_home)

        result = ops.scale_agent("test-team-1000", "alpha", count=1)
        assert len(result["removed"]) == 2
        assert result["current_count"] == 1

    def test_scale_count_zero_raises(self, ops: TrusteeOps) -> None:
        """count=0 raises ValueError."""
        with pytest.raises(ValueError, match="count must be >= 1"):
            ops.scale_agent("test-team-1000", "alpha", count=0)

    def test_scale_unknown_deployment_raises(self, ops: TrusteeOps) -> None:
        """ValueError for unknown deployment."""
        with pytest.raises(ValueError, match="not found"):
            ops.scale_agent("does-not-exist", "alpha", count=2)

    def test_scale_same_count_is_noop(self, ops: TrusteeOps) -> None:
        """Scaling to current count does nothing."""
        result = ops.scale_agent("test-team-1000", "alpha", count=1)
        assert result["added"] == []
        assert result["removed"] == []

    def test_scale_persists_to_disk(
        self, engine_with_deployment: tuple, tmp_home: Path
    ) -> None:
        """Scale operation persists new state."""
        engine, _ = engine_with_deployment
        ops = TrusteeOps(engine=engine, home=tmp_home)
        ops.scale_agent("test-team-1000", "alpha", count=2)
        reloaded = engine.get_deployment("test-team-1000")
        alpha_instances = [
            a for a in reloaded.agents.values() if a.agent_spec_key == "alpha"
        ]
        assert len(alpha_instances) == 2


# ---------------------------------------------------------------------------
# TrusteeOps.rotate_agent tests
# ---------------------------------------------------------------------------


class TestRotateAgent:
    """Tests for rotate_agent method."""

    def test_rotate_returns_snapshot_path(self, ops: TrusteeOps) -> None:
        """rotate_agent returns a snapshot_path key."""
        result = ops.rotate_agent("test-team-1000", "test-team-alpha")
        assert "snapshot_path" in result

    def test_rotate_resets_agent_status(
        self, engine_with_deployment: tuple, tmp_home: Path
    ) -> None:
        """After rotation, agent is not FAILED."""
        engine, _ = engine_with_deployment
        engine._deployments_dir
        deployment = engine.get_deployment("test-team-1000")
        deployment.agents["test-team-alpha"].status = AgentStatus.FAILED
        engine._save_deployment(deployment)

        ops = TrusteeOps(engine=engine, home=tmp_home)
        ops.rotate_agent("test-team-1000", "test-team-alpha")
        refreshed = engine.get_deployment("test-team-1000")
        assert refreshed.agents["test-team-alpha"].status != AgentStatus.FAILED

    def test_rotate_unknown_deployment_raises(self, ops: TrusteeOps) -> None:
        """ValueError for unknown deployment."""
        with pytest.raises(ValueError, match="not found"):
            ops.rotate_agent("no-such-deploy", "test-team-alpha")

    def test_rotate_unknown_agent_raises(self, ops: TrusteeOps) -> None:
        """ValueError for unknown agent."""
        with pytest.raises(ValueError, match="not in deployment"):
            ops.rotate_agent("test-team-1000", "ghost")

    def test_rotate_creates_snapshot_dir(self, ops: TrusteeOps, tmp_home: Path) -> None:
        """Snapshot directory parent is created even without source data."""
        result = ops.rotate_agent("test-team-1000", "test-team-alpha")
        snapshot_path = Path(result["snapshot_path"])
        assert snapshot_path.parent.exists()

    def test_rotate_copies_source_if_present(self, ops: TrusteeOps, tmp_home: Path) -> None:
        """If agent directory exists, its contents are snapshotted."""
        agent_dir = tmp_home / "agents" / "local" / "test-team-alpha"
        agent_dir.mkdir(parents=True)
        (agent_dir / "memory.json").write_text('{"key": "value"}')

        result = ops.rotate_agent("test-team-1000", "test-team-alpha")
        snapshot_path = Path(result["snapshot_path"])
        assert (snapshot_path / "memory.json").exists()


# ---------------------------------------------------------------------------
# TrusteeOps.health_report tests
# ---------------------------------------------------------------------------


class TestHealthReport:
    """Tests for health_report method."""

    def test_health_report_returns_list(self, ops: TrusteeOps) -> None:
        """health_report returns a list of dicts."""
        report = ops.health_report("test-team-1000")
        assert isinstance(report, list)
        assert len(report) == 1

    def test_health_report_entry_keys(self, ops: TrusteeOps) -> None:
        """Each entry has the expected keys."""
        report = ops.health_report("test-team-1000")
        entry = report[0]
        assert {"name", "status", "host", "last_heartbeat", "error", "healthy"} <= set(
            entry.keys()
        )

    def test_health_report_running_agent_is_healthy(self, ops: TrusteeOps) -> None:
        """A RUNNING agent should be healthy."""
        report = ops.health_report("test-team-1000")
        assert report[0]["healthy"] is True

    def test_health_report_failed_agent_is_unhealthy(
        self, engine_with_deployment: tuple, tmp_home: Path
    ) -> None:
        """A FAILED agent should not be healthy."""
        engine, _ = engine_with_deployment
        dep = engine.get_deployment("test-team-1000")
        dep.agents["test-team-alpha"].status = AgentStatus.FAILED
        engine._save_deployment(dep)

        ops = TrusteeOps(engine=engine, home=tmp_home)
        report = ops.health_report("test-team-1000")
        assert report[0]["healthy"] is False

    def test_health_report_unknown_deployment_raises(self, ops: TrusteeOps) -> None:
        """ValueError for unknown deployment."""
        with pytest.raises(ValueError, match="not found"):
            ops.health_report("ghost-deploy")

    def test_health_report_uses_provider_when_available(self, tmp_home: Path) -> None:
        """Provider.health_check is called when provider is set."""
        engine = TeamEngine(home=tmp_home)
        mock_provider = MagicMock()
        mock_provider.health_check.return_value = AgentStatus.RUNNING
        engine._provider = mock_provider
        engine._save_deployment(_make_deployment())

        ops = TrusteeOps(engine=engine, home=tmp_home)
        ops.health_report("test-team-1000")
        mock_provider.health_check.assert_called_once()


# ---------------------------------------------------------------------------
# TrusteeOps.get_logs tests
# ---------------------------------------------------------------------------


class TestGetLogs:
    """Tests for get_logs method."""

    def test_get_logs_returns_dict(self, ops: TrusteeOps) -> None:
        """get_logs returns a dict keyed by agent name."""
        logs = ops.get_logs("test-team-1000")
        assert isinstance(logs, dict)
        assert "test-team-alpha" in logs

    def test_get_logs_single_agent(self, ops: TrusteeOps) -> None:
        """Requesting one agent returns only that agent's logs."""
        logs = ops.get_logs("test-team-1000", agent_name="test-team-alpha")
        assert list(logs.keys()) == ["test-team-alpha"]

    def test_get_logs_reads_log_file(self, ops: TrusteeOps, tmp_home: Path) -> None:
        """Reads from agent.log when the file exists."""
        log_dir = tmp_home / "agents" / "local" / "test-team-alpha"
        log_dir.mkdir(parents=True)
        (log_dir / "agent.log").write_text("line1\nline2\nline3\n")

        logs = ops.get_logs("test-team-1000")
        assert "line1" in logs["test-team-alpha"]
        assert "line3" in logs["test-team-alpha"]

    def test_get_logs_respects_tail(self, ops: TrusteeOps, tmp_home: Path) -> None:
        """tail parameter limits number of lines returned."""
        log_dir = tmp_home / "agents" / "local" / "test-team-alpha"
        log_dir.mkdir(parents=True)
        lines = [f"line{i}" for i in range(100)]
        (log_dir / "agent.log").write_text("\n".join(lines))

        logs = ops.get_logs("test-team-1000", tail=5)
        assert len(logs["test-team-alpha"]) == 5

    def test_get_logs_fallback_to_audit(self, ops: TrusteeOps, tmp_home: Path) -> None:
        """Falls back to audit log when no agent.log exists."""
        # Write an audit entry that should surface
        _write_audit(
            "restart_agent",
            "test-team-1000",
            {"agent_name": "test-team-alpha"},
            home=tmp_home,
        )
        logs = ops.get_logs("test-team-1000", agent_name="test-team-alpha")
        assert len(logs["test-team-alpha"]) >= 1

    def test_get_logs_unknown_deployment_raises(self, ops: TrusteeOps) -> None:
        """ValueError for unknown deployment."""
        with pytest.raises(ValueError, match="not found"):
            ops.get_logs("no-deploy")

    def test_get_logs_unknown_agent_raises(self, ops: TrusteeOps) -> None:
        """ValueError for unknown agent name."""
        with pytest.raises(ValueError, match="not in deployment"):
            ops.get_logs("test-team-1000", agent_name="ghost")


# CLI integration tests live in test_trustee_cli_integration.py
