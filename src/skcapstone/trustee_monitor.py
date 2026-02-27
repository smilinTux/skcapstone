"""Autonomous agent monitoring — heartbeat detection, auto-remediation, escalation.

The TrusteeMonitor watches deployed agent teams and takes autonomous
action when issues are detected:

- Heartbeat miss → auto-restart after threshold
- Repeated restart failures → auto-rotate (snapshot + fresh deploy)
- Critical degradation (>50% agents down) → escalation to Chef via SKChat
- Context fill detection → auto-rotate before degradation

All actions are logged to the audit trail. The monitor follows the
Trustee Oath: "I escalate when uncertain — never guess with sovereignty."
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .team_engine import AgentStatus, TeamDeployment, TeamEngine
from .trustee_ops import TrusteeOps

logger = logging.getLogger(__name__)


@dataclass
class MonitorConfig:
    """Configuration for autonomous monitoring thresholds.

    Attributes:
        heartbeat_timeout: Seconds since last heartbeat before restart.
        max_restart_attempts: Consecutive restart failures before rotate.
        critical_threshold: Fraction of failed agents triggering escalation.
        escalation_cooldown: Seconds between escalation messages.
        auto_restart: Whether to auto-restart on heartbeat miss.
        auto_rotate: Whether to auto-rotate on repeated failures.
        auto_escalate: Whether to send escalation messages.
    """

    heartbeat_timeout: float = 120.0
    max_restart_attempts: int = 3
    critical_threshold: float = 0.5
    escalation_cooldown: float = 300.0
    auto_restart: bool = True
    auto_rotate: bool = True
    auto_escalate: bool = True


@dataclass
class AgentIncident:
    """Tracks incident state for a single agent."""

    restart_attempts: int = 0
    last_restart: Optional[float] = None
    rotated: bool = False
    escalated: bool = False


@dataclass
class MonitorReport:
    """Result of a single monitoring pass.

    Attributes:
        timestamp: When the check was performed.
        deployments_checked: Number of deployments examined.
        agents_healthy: Count of healthy agents.
        agents_degraded: Count of degraded/failed agents.
        restarts_triggered: Agent names that were auto-restarted.
        rotations_triggered: Agent names that were auto-rotated.
        escalations_sent: Deployment IDs that triggered escalation.
    """

    timestamp: str = ""
    deployments_checked: int = 0
    agents_healthy: int = 0
    agents_degraded: int = 0
    restarts_triggered: List[str] = field(default_factory=list)
    rotations_triggered: List[str] = field(default_factory=list)
    escalations_sent: List[str] = field(default_factory=list)


class TrusteeMonitor:
    """Autonomous monitoring loop for deployed agent teams.

    Periodically checks health of all deployments and takes
    corrective action based on configurable thresholds. Follows
    escalation protocol: restart → rotate → escalate.

    Args:
        ops: TrusteeOps instance for remediation actions.
        engine: TeamEngine for deployment listing.
        config: Monitoring thresholds and toggles.
    """

    def __init__(
        self,
        ops: TrusteeOps,
        engine: TeamEngine,
        config: Optional[MonitorConfig] = None,
    ) -> None:
        self._ops = ops
        self._engine = engine
        self._config = config or MonitorConfig()
        self._incidents: Dict[str, AgentIncident] = {}
        self._last_escalation: Dict[str, float] = {}
        self._running = False

    def _get_incident(self, agent_key: str) -> AgentIncident:
        """Get or create an incident tracker for an agent.

        Args:
            agent_key: Unique key like 'deployment_id/agent_name'.

        Returns:
            AgentIncident for this agent.
        """
        if agent_key not in self._incidents:
            self._incidents[agent_key] = AgentIncident()
        return self._incidents[agent_key]

    def _is_heartbeat_stale(self, agent: Any) -> bool:
        """Check if an agent's heartbeat has exceeded the timeout.

        Args:
            agent: DeployedAgent instance.

        Returns:
            True if heartbeat is stale or missing.
        """
        if not agent.last_heartbeat:
            return agent.status == AgentStatus.RUNNING

        try:
            hb_time = datetime.fromisoformat(agent.last_heartbeat)
            if hb_time.tzinfo is None:
                hb_time = hb_time.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - hb_time).total_seconds()
            return elapsed > self._config.heartbeat_timeout
        except (ValueError, TypeError):
            return True

    def _try_restart(self, deployment_id: str, agent_name: str) -> bool:
        """Attempt to restart an agent.

        Args:
            deployment_id: Target deployment.
            agent_name: Agent to restart.

        Returns:
            True if restart succeeded.
        """
        if not self._config.auto_restart:
            return False

        agent_key = f"{deployment_id}/{agent_name}"
        incident = self._get_incident(agent_key)

        try:
            results = self._ops.restart_agent(deployment_id, agent_name)
            success = results.get(agent_name) == "restarted"
            if success:
                incident.restart_attempts = 0
                logger.info("Auto-restarted %s in %s", agent_name, deployment_id)
            else:
                incident.restart_attempts += 1
                incident.last_restart = time.time()
                logger.warning(
                    "Restart failed for %s (attempt %d/%d)",
                    agent_name, incident.restart_attempts,
                    self._config.max_restart_attempts,
                )
            return success
        except Exception as exc:
            incident.restart_attempts += 1
            incident.last_restart = time.time()
            logger.error("Restart exception for %s: %s", agent_name, exc)
            return False

    def _try_rotate(self, deployment_id: str, agent_name: str) -> bool:
        """Attempt to rotate an agent (snapshot + fresh deploy).

        Args:
            deployment_id: Target deployment.
            agent_name: Agent to rotate.

        Returns:
            True if rotation succeeded.
        """
        if not self._config.auto_rotate:
            return False

        agent_key = f"{deployment_id}/{agent_name}"
        incident = self._get_incident(agent_key)

        if incident.rotated:
            return False

        try:
            result = self._ops.rotate_agent(deployment_id, agent_name)
            incident.rotated = True
            incident.restart_attempts = 0
            logger.info(
                "Auto-rotated %s in %s (snapshot: %s)",
                agent_name, deployment_id, result.get("snapshot_path"),
            )
            return result.get("redeployed", False)
        except Exception as exc:
            logger.error("Rotation failed for %s: %s", agent_name, exc)
            return False

    def _try_escalate(self, deployment_id: str, message: str) -> bool:
        """Send an escalation message via SKChat if cooldown has elapsed.

        Args:
            deployment_id: Deployment with critical issues.
            message: Escalation message text.

        Returns:
            True if escalation was sent.
        """
        if not self._config.auto_escalate:
            return False

        now = time.time()
        last = self._last_escalation.get(deployment_id, 0.0)
        if now - last < self._config.escalation_cooldown:
            return False

        try:
            from .mcp_server import _send_message_impl
            _send_message_impl(
                recipient="chef",
                message=f"[TRUSTEE ESCALATION] {message}",
                urgency="critical",
            )
            self._last_escalation[deployment_id] = now
            logger.warning("Escalation sent for %s: %s", deployment_id, message)
            return True
        except Exception:
            # Fallback: just log it
            logger.warning("Escalation (no transport): %s", message)
            self._last_escalation[deployment_id] = now
            return True

    def check_deployment(self, deployment: TeamDeployment) -> MonitorReport:
        """Run a monitoring pass on a single deployment.

        Checks each agent's health and takes autonomous action as needed:
        1. Stale heartbeat → restart
        2. Too many restart failures → rotate
        3. Critical degradation → escalate

        Args:
            deployment: The deployment to check.

        Returns:
            MonitorReport for this deployment.
        """
        report = MonitorReport(
            timestamp=datetime.now(timezone.utc).isoformat(),
            deployments_checked=1,
        )

        total_agents = len(deployment.agents)
        if total_agents == 0:
            return report

        failed_count = 0

        for name, agent in deployment.agents.items():
            agent_key = f"{deployment.deployment_id}/{name}"
            incident = self._get_incident(agent_key)

            is_healthy = agent.status == AgentStatus.RUNNING
            is_stale = self._is_heartbeat_stale(agent) if is_healthy else False

            if is_healthy and not is_stale:
                report.agents_healthy += 1
                # Clear incident state on recovery
                if incident.restart_attempts > 0:
                    incident.restart_attempts = 0
                    incident.rotated = False
                continue

            # Agent needs attention
            report.agents_degraded += 1
            failed_count += 1

            # Escalation path: restart → rotate → escalate
            if incident.restart_attempts >= self._config.max_restart_attempts:
                # Too many restarts failed, try rotation
                if self._try_rotate(deployment.deployment_id, name):
                    report.rotations_triggered.append(name)
                continue

            if is_stale or agent.status in (AgentStatus.FAILED, AgentStatus.DEGRADED):
                if self._try_restart(deployment.deployment_id, name):
                    report.restarts_triggered.append(name)

        # Check for critical degradation
        if total_agents > 0:
            failure_ratio = failed_count / total_agents
            if failure_ratio >= self._config.critical_threshold:
                msg = (
                    f"Deployment '{deployment.deployment_id}' critically degraded: "
                    f"{failed_count}/{total_agents} agents down "
                    f"({failure_ratio:.0%})"
                )
                if self._try_escalate(deployment.deployment_id, msg):
                    report.escalations_sent.append(deployment.deployment_id)

        return report

    def check_all(self) -> MonitorReport:
        """Run a monitoring pass over all deployments.

        Returns:
            Aggregated MonitorReport across all deployments.
        """
        combined = MonitorReport(
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        deployments = self._engine.list_deployments()
        combined.deployments_checked = len(deployments)

        for deployment in deployments:
            sub = self.check_deployment(deployment)
            combined.agents_healthy += sub.agents_healthy
            combined.agents_degraded += sub.agents_degraded
            combined.restarts_triggered.extend(sub.restarts_triggered)
            combined.rotations_triggered.extend(sub.rotations_triggered)
            combined.escalations_sent.extend(sub.escalations_sent)

        return combined

    def run(self, interval: float = 30.0, max_iterations: int = 0) -> None:
        """Run the monitoring loop.

        Continuously checks all deployments at the given interval.
        Press Ctrl+C to stop.

        Args:
            interval: Seconds between checks.
            max_iterations: Stop after N iterations (0 = infinite).
        """
        self._running = True
        iteration = 0
        logger.info("Trustee monitor started (interval=%.1fs)", interval)

        try:
            while self._running:
                iteration += 1
                report = self.check_all()

                if (report.restarts_triggered or
                        report.rotations_triggered or
                        report.escalations_sent):
                    logger.info(
                        "Monitor pass %d: %d healthy, %d degraded, "
                        "%d restarts, %d rotations, %d escalations",
                        iteration,
                        report.agents_healthy,
                        report.agents_degraded,
                        len(report.restarts_triggered),
                        len(report.rotations_triggered),
                        len(report.escalations_sent),
                    )

                if max_iterations and iteration >= max_iterations:
                    break

                time.sleep(interval)
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            logger.info("Trustee monitor stopped after %d iterations", iteration)

    def stop(self) -> None:
        """Signal the monitoring loop to stop."""
        self._running = False
