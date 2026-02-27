"""Tests for TrusteeMonitor — autonomous agent monitoring."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.team_engine import AgentStatus, DeployedAgent, TeamDeployment, TeamEngine
from skcapstone.trustee_ops import TrusteeOps
from skcapstone.trustee_monitor import (
    MonitorConfig,
    MonitorReport,
    TrusteeMonitor,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_home(tmp_path: Path) -> Path:
    """Create a temporary skcapstone home directory."""
    home = tmp_path / ".skcapstone"
    (home / "deployments").mkdir(parents=True)
    (home / "coordination").mkdir(parents=True)
    return home


@pytest.fixture
def engine(tmp_home: Path) -> TeamEngine:
    """TeamEngine with no provider (dry-run mode)."""
    return TeamEngine(home=tmp_home, provider=None, comms_root=None)


@pytest.fixture
def ops(engine: TeamEngine, tmp_home: Path) -> TrusteeOps:
    """TrusteeOps wired to the tmp engine."""
    return TrusteeOps(engine=engine, home=tmp_home)


def _make_deployment(
    engine: TeamEngine,
    agent_statuses: dict[str, AgentStatus] | None = None,
    heartbeats: dict[str, str | None] | None = None,
) -> TeamDeployment:
    """Helper to create and persist a test deployment."""
    if agent_statuses is None:
        agent_statuses = {"worker-1": AgentStatus.RUNNING, "worker-2": AgentStatus.RUNNING}
    if heartbeats is None:
        heartbeats = {}

    deployment = TeamDeployment(
        deployment_id="test-deploy",
        blueprint_slug="test",
        team_name="Test Team",
        provider="local",
        status="running",
    )

    now = datetime.now(timezone.utc)
    for name, status in agent_statuses.items():
        hb = heartbeats.get(name, now.isoformat())
        deployment.agents[name] = DeployedAgent(
            name=name,
            instance_id=f"test-deploy/{name}",
            blueprint_slug="test",
            agent_spec_key="worker",
            status=status,
            host="localhost",
            last_heartbeat=hb,
            started_at=now.isoformat(),
        )

    engine._save_deployment(deployment)
    return deployment


# ---------------------------------------------------------------------------
# MonitorConfig
# ---------------------------------------------------------------------------

class TestMonitorConfig:
    def test_defaults(self):
        cfg = MonitorConfig()
        assert cfg.heartbeat_timeout == 120.0
        assert cfg.max_restart_attempts == 3
        assert cfg.critical_threshold == 0.5
        assert cfg.auto_restart is True
        assert cfg.auto_rotate is True

    def test_custom_values(self):
        cfg = MonitorConfig(heartbeat_timeout=60, max_restart_attempts=5)
        assert cfg.heartbeat_timeout == 60
        assert cfg.max_restart_attempts == 5


# ---------------------------------------------------------------------------
# Heartbeat detection
# ---------------------------------------------------------------------------

class TestHeartbeatDetection:
    def test_fresh_heartbeat_not_stale(self, ops, engine, tmp_home):
        _make_deployment(engine)
        monitor = TrusteeMonitor(ops, engine, MonitorConfig(heartbeat_timeout=120))
        deployment = engine.get_deployment("test-deploy")
        agent = deployment.agents["worker-1"]
        assert not monitor._is_heartbeat_stale(agent)

    def test_old_heartbeat_is_stale(self, ops, engine, tmp_home):
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        _make_deployment(engine, heartbeats={"worker-1": old_time, "worker-2": old_time})
        monitor = TrusteeMonitor(ops, engine, MonitorConfig(heartbeat_timeout=120))
        deployment = engine.get_deployment("test-deploy")
        agent = deployment.agents["worker-1"]
        assert monitor._is_heartbeat_stale(agent)

    def test_missing_heartbeat_for_running_agent(self, ops, engine, tmp_home):
        _make_deployment(engine, heartbeats={"worker-1": None})
        monitor = TrusteeMonitor(ops, engine, MonitorConfig(heartbeat_timeout=120))
        deployment = engine.get_deployment("test-deploy")
        agent = deployment.agents["worker-1"]
        agent.last_heartbeat = None
        assert monitor._is_heartbeat_stale(agent)

    def test_stopped_agent_without_heartbeat_not_stale(self, ops, engine, tmp_home):
        _make_deployment(
            engine,
            agent_statuses={"worker-1": AgentStatus.STOPPED},
            heartbeats={"worker-1": None},
        )
        monitor = TrusteeMonitor(ops, engine, MonitorConfig())
        deployment = engine.get_deployment("test-deploy")
        agent = deployment.agents["worker-1"]
        agent.last_heartbeat = None
        assert not monitor._is_heartbeat_stale(agent)


# ---------------------------------------------------------------------------
# check_deployment
# ---------------------------------------------------------------------------

class TestCheckDeployment:
    def test_all_healthy(self, ops, engine, tmp_home):
        deployment = _make_deployment(engine)
        monitor = TrusteeMonitor(ops, engine, MonitorConfig())
        report = monitor.check_deployment(deployment)
        assert report.agents_healthy == 2
        assert report.agents_degraded == 0
        assert report.restarts_triggered == []
        assert report.rotations_triggered == []

    def test_failed_agent_triggers_restart(self, ops, engine, tmp_home):
        deployment = _make_deployment(
            engine,
            agent_statuses={"worker-1": AgentStatus.RUNNING, "worker-2": AgentStatus.FAILED},
        )
        monitor = TrusteeMonitor(ops, engine, MonitorConfig())
        report = monitor.check_deployment(deployment)
        assert report.agents_healthy == 1
        assert report.agents_degraded == 1
        # Restart attempted (no provider so it "succeeds" in dry-run)
        assert "worker-2" in report.restarts_triggered

    def test_stale_heartbeat_triggers_restart(self, ops, engine, tmp_home):
        old = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        deployment = _make_deployment(
            engine,
            heartbeats={"worker-1": old, "worker-2": datetime.now(timezone.utc).isoformat()},
        )
        monitor = TrusteeMonitor(ops, engine, MonitorConfig(heartbeat_timeout=120))
        report = monitor.check_deployment(deployment)
        assert "worker-1" in report.restarts_triggered

    def test_max_restarts_triggers_rotation(self, ops, engine, tmp_home):
        deployment = _make_deployment(
            engine,
            agent_statuses={"worker-1": AgentStatus.FAILED},
        )
        cfg = MonitorConfig(max_restart_attempts=2)
        monitor = TrusteeMonitor(ops, engine, cfg)
        # Simulate prior restart failures
        incident = monitor._get_incident("test-deploy/worker-1")
        incident.restart_attempts = 3
        report = monitor.check_deployment(deployment)
        assert "worker-1" in report.rotations_triggered

    def test_rotation_only_once(self, ops, engine, tmp_home):
        deployment = _make_deployment(
            engine,
            agent_statuses={"worker-1": AgentStatus.FAILED},
        )
        cfg = MonitorConfig(max_restart_attempts=1)
        monitor = TrusteeMonitor(ops, engine, cfg)
        incident = monitor._get_incident("test-deploy/worker-1")
        incident.restart_attempts = 5
        incident.rotated = True
        report = monitor.check_deployment(deployment)
        # Already rotated, won't rotate again
        assert report.rotations_triggered == []

    def test_auto_restart_disabled(self, ops, engine, tmp_home):
        deployment = _make_deployment(
            engine,
            agent_statuses={"worker-1": AgentStatus.FAILED},
        )
        cfg = MonitorConfig(auto_restart=False)
        monitor = TrusteeMonitor(ops, engine, cfg)
        report = monitor.check_deployment(deployment)
        assert report.restarts_triggered == []

    def test_auto_rotate_disabled(self, ops, engine, tmp_home):
        deployment = _make_deployment(
            engine,
            agent_statuses={"worker-1": AgentStatus.FAILED},
        )
        cfg = MonitorConfig(auto_rotate=False, max_restart_attempts=1)
        monitor = TrusteeMonitor(ops, engine, cfg)
        incident = monitor._get_incident("test-deploy/worker-1")
        incident.restart_attempts = 5
        report = monitor.check_deployment(deployment)
        assert report.rotations_triggered == []

    def test_empty_deployment(self, ops, engine, tmp_home):
        deployment = TeamDeployment(
            deployment_id="empty",
            blueprint_slug="test",
            team_name="Empty",
            provider="local",
        )
        engine._save_deployment(deployment)
        monitor = TrusteeMonitor(ops, engine, MonitorConfig())
        report = monitor.check_deployment(deployment)
        assert report.agents_healthy == 0
        assert report.agents_degraded == 0


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

class TestEscalation:
    def test_critical_degradation_escalates(self, ops, engine, tmp_home):
        deployment = _make_deployment(
            engine,
            agent_statuses={
                "worker-1": AgentStatus.FAILED,
                "worker-2": AgentStatus.FAILED,
            },
        )
        cfg = MonitorConfig(critical_threshold=0.5, auto_escalate=True)
        monitor = TrusteeMonitor(ops, engine, cfg)
        report = monitor.check_deployment(deployment)
        assert report.escalations_sent == ["test-deploy"]

    def test_escalation_cooldown(self, ops, engine, tmp_home):
        deployment = _make_deployment(
            engine,
            agent_statuses={"worker-1": AgentStatus.FAILED, "worker-2": AgentStatus.FAILED},
        )
        cfg = MonitorConfig(critical_threshold=0.5, escalation_cooldown=300)
        monitor = TrusteeMonitor(ops, engine, cfg)
        # First pass triggers escalation
        r1 = monitor.check_deployment(deployment)
        assert r1.escalations_sent == ["test-deploy"]
        # Second pass within cooldown should not
        r2 = monitor.check_deployment(deployment)
        assert r2.escalations_sent == []

    def test_escalation_disabled(self, ops, engine, tmp_home):
        deployment = _make_deployment(
            engine,
            agent_statuses={"worker-1": AgentStatus.FAILED, "worker-2": AgentStatus.FAILED},
        )
        cfg = MonitorConfig(critical_threshold=0.5, auto_escalate=False)
        monitor = TrusteeMonitor(ops, engine, cfg)
        report = monitor.check_deployment(deployment)
        assert report.escalations_sent == []

    def test_below_threshold_no_escalation(self, ops, engine, tmp_home):
        deployment = _make_deployment(
            engine,
            agent_statuses={
                "worker-1": AgentStatus.RUNNING,
                "worker-2": AgentStatus.RUNNING,
                "worker-3": AgentStatus.FAILED,
            },
        )
        cfg = MonitorConfig(critical_threshold=0.5)
        monitor = TrusteeMonitor(ops, engine, cfg)
        report = monitor.check_deployment(deployment)
        # Only 1/3 failed = 33%, below 50% threshold
        assert report.escalations_sent == []


# ---------------------------------------------------------------------------
# check_all
# ---------------------------------------------------------------------------

class TestCheckAll:
    def test_aggregates_multiple_deployments(self, ops, engine, tmp_home):
        _make_deployment(engine)
        # Second deployment
        d2 = TeamDeployment(
            deployment_id="test-deploy-2",
            blueprint_slug="test2",
            team_name="Team 2",
            provider="local",
            status="running",
        )
        now = datetime.now(timezone.utc).isoformat()
        d2.agents["alpha"] = DeployedAgent(
            name="alpha",
            instance_id="test-deploy-2/alpha",
            blueprint_slug="test2",
            agent_spec_key="worker",
            status=AgentStatus.RUNNING,
            host="localhost",
            last_heartbeat=now,
            started_at=now,
        )
        engine._save_deployment(d2)

        monitor = TrusteeMonitor(ops, engine, MonitorConfig())
        report = monitor.check_all()
        assert report.deployments_checked == 2
        assert report.agents_healthy == 3

    def test_no_deployments(self, ops, engine, tmp_home):
        monitor = TrusteeMonitor(ops, engine, MonitorConfig())
        report = monitor.check_all()
        assert report.deployments_checked == 0
        assert report.agents_healthy == 0


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

class TestRecovery:
    def test_incident_clears_on_recovery(self, ops, engine, tmp_home):
        deployment = _make_deployment(engine)
        monitor = TrusteeMonitor(ops, engine, MonitorConfig())

        # Simulate prior incident
        incident = monitor._get_incident("test-deploy/worker-1")
        incident.restart_attempts = 2
        incident.rotated = True

        # Agent is healthy now
        report = monitor.check_deployment(deployment)
        assert report.agents_healthy == 2

        # Incident should be cleared
        assert incident.restart_attempts == 0
        assert incident.rotated is False


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------

class TestRunLoop:
    def test_run_with_max_iterations(self, ops, engine, tmp_home):
        _make_deployment(engine)
        monitor = TrusteeMonitor(ops, engine, MonitorConfig())
        monitor.run(interval=0.01, max_iterations=3)
        # Should complete without error

    def test_stop(self, ops, engine, tmp_home):
        _make_deployment(engine)
        monitor = TrusteeMonitor(ops, engine, MonitorConfig())
        monitor.stop()
        assert not monitor._running


# ---------------------------------------------------------------------------
# MonitorReport
# ---------------------------------------------------------------------------

class TestMonitorReport:
    def test_defaults(self):
        report = MonitorReport()
        assert report.deployments_checked == 0
        assert report.agents_healthy == 0
        assert report.agents_degraded == 0
        assert report.restarts_triggered == []
        assert report.rotations_triggered == []
        assert report.escalations_sent == []
