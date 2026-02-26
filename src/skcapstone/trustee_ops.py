"""
Trustee Operations — autonomous agent team management for AI trustees.

Provides restart, scale, rotate, health-report, and log retrieval
operations on deployed teams. All mutations are written to an audit
trail so every trustee action is transparent and accountable.

Designed for AI trustees (Lumina, Opus) and human trustees (Chef)
operating under the Trustee Oath:
  "I escalate when uncertain — never guess with sovereignty."

Private helpers (audit, snapshot, log utilities) live in
_trustee_helpers.py to keep this module under 500 lines.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._trustee_helpers import (
    audit_lines_for_agent,
    refresh_deployment_status,
    snapshot_agent_context,
    stub_spec,
    write_audit,
)
from .team_engine import AgentStatus, DeployedAgent, TeamDeployment, TeamEngine

logger = logging.getLogger(__name__)

# Re-export for external callers (e.g. tests that import _write_audit)
_write_audit = write_audit


class TrusteeOps:
    """High-level trustee operations over a deployed team.

    Args:
        engine: A configured TeamEngine instance.
        home: Agent home directory (used for audit log path).
    """

    def __init__(
        self,
        engine: TeamEngine,
        home: Optional[Path] = None,
    ) -> None:
        self._engine = engine
        self._home = (home or Path("~/.skcapstone")).expanduser()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _audit(self, action: str, deployment_id: str, **details: Any) -> None:
        """Write an audit entry.

        Args:
            action: Action label.
            deployment_id: Deployment being acted on.
            **details: Additional context fields.
        """
        write_audit(action, deployment_id, details, home=self._home)

    def _provision_result(self, agent: DeployedAgent) -> Dict[str, Any]:
        """Build a minimal provision_result dict from a DeployedAgent.

        Args:
            agent: The deployed agent.

        Returns:
            Dict compatible with ProviderBackend methods.
        """
        return {
            "host": agent.host,
            "pid": agent.pid,
            "container_id": agent.container_id,
        }

    # ------------------------------------------------------------------
    # Restart
    # ------------------------------------------------------------------

    def restart_agent(
        self,
        deployment_id: str,
        agent_name: Optional[str] = None,
    ) -> Dict[str, str]:
        """Restart a failed agent or every agent in a team.

        Calls provider stop → start for each target agent, updates the
        deployment state, and writes an audit entry.

        Args:
            deployment_id: Target deployment.
            agent_name: If given, restart only this agent; otherwise
                restart all agents in the deployment.

        Returns:
            Dict mapping agent names to "restarted" or error strings.

        Raises:
            ValueError: If deployment or agent is not found.
        """
        deployment = self._engine.get_deployment(deployment_id)
        if not deployment:
            raise ValueError(f"Deployment '{deployment_id}' not found.")

        if agent_name:
            if agent_name not in deployment.agents:
                raise ValueError(
                    f"Agent '{agent_name}' not in deployment '{deployment_id}'."
                )
            targets = {agent_name: deployment.agents[agent_name]}
        else:
            targets = dict(deployment.agents)

        results: Dict[str, str] = {}
        provider = self._engine._provider

        for name, agent in targets.items():
            provision = self._provision_result(agent)
            try:
                if provider:
                    provider.stop(name, provision)
                    provider.start(name, provision)
                agent.status = AgentStatus.RUNNING
                agent.last_heartbeat = datetime.now(timezone.utc).isoformat()
                agent.error = None
                results[name] = "restarted"
                logger.info("Restarted agent %s in %s", name, deployment_id)
            except Exception as exc:
                agent.status = AgentStatus.FAILED
                agent.error = str(exc)
                results[name] = f"error: {exc}"
                logger.error("Failed to restart %s: %s", name, exc)

        refresh_deployment_status(deployment)
        self._engine._save_deployment(deployment)
        self._audit("restart_agent", deployment_id, agent_name=agent_name or "ALL", results=results)
        return results

    # ------------------------------------------------------------------
    # Scale
    # ------------------------------------------------------------------

    def scale_agent(
        self,
        deployment_id: str,
        agent_spec_key: str,
        count: int,
    ) -> Dict[str, Any]:
        """Scale the number of instances for an agent type up or down.

        Adds or removes instances while updating persisted deployment
        state. Scaling down stops excess instances; scaling up
        provisions new ones (dry-run if no provider).

        Args:
            deployment_id: Target deployment.
            agent_spec_key: The agent spec key (role identifier) to scale.
            count: Desired total instance count (must be >= 1).

        Returns:
            Dict with "added", "removed", and "current_count" keys.

        Raises:
            ValueError: If deployment not found or count < 1.
        """
        if count < 1:
            raise ValueError("count must be >= 1.")

        deployment = self._engine.get_deployment(deployment_id)
        if not deployment:
            raise ValueError(f"Deployment '{deployment_id}' not found.")

        current = {
            name: agent
            for name, agent in deployment.agents.items()
            if agent.agent_spec_key == agent_spec_key
        }
        current_count = len(current)
        added: List[str] = []
        removed: List[str] = []
        provider = self._engine._provider

        if count > current_count:
            for i in range(current_count + 1, count + 1):
                new_name = f"{deployment.blueprint_slug}-{agent_spec_key}-{i}"
                new_agent = DeployedAgent(
                    name=new_name,
                    instance_id=f"{deployment_id}/{new_name}",
                    blueprint_slug=deployment.blueprint_slug,
                    agent_spec_key=agent_spec_key,
                    provider=deployment.provider,
                    status=AgentStatus.PENDING,
                    host="localhost",
                )
                if provider:
                    try:
                        new_agent.status = AgentStatus.RUNNING
                        new_agent.started_at = datetime.now(timezone.utc).isoformat()
                        new_agent.last_heartbeat = new_agent.started_at
                    except Exception as exc:
                        new_agent.status = AgentStatus.FAILED
                        new_agent.error = str(exc)
                deployment.agents[new_name] = new_agent
                added.append(new_name)
                logger.info("Scaled up: added %s", new_name)

        elif count < current_count:
            to_remove = sorted(current.keys())[(count):]
            for name in to_remove:
                agent = deployment.agents[name]
                provision = self._provision_result(agent)
                if provider:
                    try:
                        provider.stop(name, provision)
                    except Exception as exc:
                        logger.warning("Error stopping %s during scale: %s", name, exc)
                del deployment.agents[name]
                removed.append(name)
                logger.info("Scaled down: removed %s", name)

        refresh_deployment_status(deployment)
        self._engine._save_deployment(deployment)
        self._audit(
            "scale_agent", deployment_id,
            agent_spec_key=agent_spec_key, desired_count=count,
            added=added, removed=removed,
        )
        return {"added": added, "removed": removed, "current_count": count}

    # ------------------------------------------------------------------
    # Rotate
    # ------------------------------------------------------------------

    def rotate_agent(
        self,
        deployment_id: str,
        agent_name: str,
    ) -> Dict[str, Any]:
        """Snapshot context, destroy, and redeploy an agent fresh.

        Used when an agent shows context degradation. Snapshots the
        agent's memory directory before destruction so nothing is lost.

        Args:
            deployment_id: Target deployment.
            agent_name: Name of the specific agent instance to rotate.

        Returns:
            Dict with "snapshot_path", "destroyed", "redeployed" keys.

        Raises:
            ValueError: If deployment or agent is not found.
        """
        deployment = self._engine.get_deployment(deployment_id)
        if not deployment:
            raise ValueError(f"Deployment '{deployment_id}' not found.")

        if agent_name not in deployment.agents:
            raise ValueError(f"Agent '{agent_name}' not in deployment '{deployment_id}'.")

        agent = deployment.agents[agent_name]
        provider = self._engine._provider
        snapshot_path = snapshot_agent_context(self._home, agent_name)

        destroyed = False
        if provider:
            try:
                provider.destroy(agent_name, self._provision_result(agent))
                destroyed = True
            except Exception as exc:
                logger.error("Rotation destroy failed for %s: %s", agent_name, exc)

        agent.status = AgentStatus.RUNNING if not provider else AgentStatus.PENDING
        agent.pid = None
        agent.container_id = None
        agent.error = None
        agent.started_at = datetime.now(timezone.utc).isoformat()
        agent.last_heartbeat = agent.started_at

        if provider:
            try:
                result = provider.provision(agent_name, stub_spec(), deployment.team_name)
                provider.configure(agent_name, stub_spec(), result)
                provider.start(agent_name, result)
                agent.status = AgentStatus.RUNNING
                agent.host = result.get("host", agent.host)
                agent.pid = result.get("pid", agent.pid)
            except Exception as exc:
                agent.status = AgentStatus.FAILED
                agent.error = str(exc)
                logger.error("Rotation redeploy failed for %s: %s", agent_name, exc)

        refresh_deployment_status(deployment)
        self._engine._save_deployment(deployment)

        result_data = {
            "snapshot_path": str(snapshot_path),
            "destroyed": destroyed,
            "redeployed": agent.status == AgentStatus.RUNNING,
        }
        self._audit("rotate_agent", deployment_id, agent_name=agent_name, **result_data)
        return result_data

    # ------------------------------------------------------------------
    # Health report
    # ------------------------------------------------------------------

    def health_report(self, deployment_id: str) -> List[Dict[str, Any]]:
        """Run health checks on all agents and return a status table.

        Calls provider.health_check for each agent when a provider is
        available; otherwise returns the cached status from disk.

        Args:
            deployment_id: Target deployment.

        Returns:
            List of dicts per agent: name, status, host, last_heartbeat,
            error, healthy.

        Raises:
            ValueError: If deployment not found.
        """
        deployment = self._engine.get_deployment(deployment_id)
        if not deployment:
            raise ValueError(f"Deployment '{deployment_id}' not found.")

        provider = self._engine._provider
        report: List[Dict[str, Any]] = []

        for name, agent in deployment.agents.items():
            provision = self._provision_result(agent)
            live_status = agent.status

            if provider:
                try:
                    live_status = provider.health_check(name, provision)
                    agent.status = live_status
                    if live_status == AgentStatus.RUNNING:
                        agent.last_heartbeat = datetime.now(timezone.utc).isoformat()
                except Exception as exc:
                    live_status = AgentStatus.DEGRADED
                    agent.status = live_status
                    agent.error = str(exc)

            report.append({
                "name": name,
                "status": live_status.value,
                "host": agent.host or "—",
                "last_heartbeat": agent.last_heartbeat or "—",
                "error": agent.error or "",
                "healthy": live_status == AgentStatus.RUNNING,
            })

        refresh_deployment_status(deployment)
        self._engine._save_deployment(deployment)
        self._audit(
            "health_report", deployment_id,
            agent_count=len(report),
            healthy=sum(1 for r in report if r["healthy"]),
        )
        return report

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------

    def get_logs(
        self,
        deployment_id: str,
        agent_name: Optional[str] = None,
        tail: int = 50,
    ) -> Dict[str, List[str]]:
        """Return recent log lines for one or all agents in a deployment.

        Reads from per-agent log files under the agents/local directory.
        Falls back to audit.log entries filtered by agent name when no
        dedicated log file exists.

        Args:
            deployment_id: Target deployment.
            agent_name: If given, return logs only for this agent.
            tail: Max lines per agent (default 50).

        Returns:
            Dict mapping agent name to list of log lines.

        Raises:
            ValueError: If deployment not found or agent not in deployment.
        """
        deployment = self._engine.get_deployment(deployment_id)
        if not deployment:
            raise ValueError(f"Deployment '{deployment_id}' not found.")

        if agent_name:
            if agent_name not in deployment.agents:
                raise ValueError(
                    f"Agent '{agent_name}' not in deployment '{deployment_id}'."
                )
            names = [agent_name]
        else:
            names = list(deployment.agents.keys())

        logs: Dict[str, List[str]] = {}
        agents_dir = self._home / "agents" / "local"

        for name in names:
            log_file = agents_dir / name / "agent.log"
            if log_file.exists():
                all_lines = log_file.read_text(encoding="utf-8").splitlines()
                logs[name] = all_lines[-tail:]
            else:
                logs[name] = audit_lines_for_agent(
                    self._home, deployment_id, name, tail=tail
                )

        self._audit("get_logs", deployment_id, agent_name=agent_name or "ALL", tail=tail)
        return logs
