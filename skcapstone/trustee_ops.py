"""
Trustee Operations: autonomous agent team management for AI trustees.

Provides restart, scale, rotate, health-report, and log retrieval
operations on deployed teams. All mutations are written to an audit
trail so every trustee action is transparent and accountable.

Designed for AI trustees (Lumina, Opus) and human trustees (Chef)
operating under the Trustee Oath:
  "I escalate when uncertain. Never guess with sovereignty."

Private helpers (audit, snapshot, log utilities) live in
_trustee_helpers.py to keep this module under 500 lines.

restart_agent, scale_agent, and rotate_agent are gated by
trustee_actuation.guard (card e51a3e7e, AUTONOMY_ARCHITECTURE.md section
3.5(d)): actuation-readiness/freeze first, then a capauth PDP allow (fail
closed if capauth is unreachable), and for rotate_agent an additional
approved-ITIL-change requirement, because rotation is never routine.
_audit below is correctly demoted from safeguard to record: it is NOT the
gate, it only records what the gate already allowed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import trustee_actuation
from ._trustee_helpers import (
    audit_lines_for_agent,
    refresh_deployment_status,
    snapshot_agent_context,
    stub_spec,
    write_audit,
)
from .fleet.paths import FleetPaths
from .team_engine import AgentStatus, DeployedAgent, TeamEngine

logger = logging.getLogger(__name__)

# Re-export for external callers (e.g. tests that import _write_audit)
_write_audit = write_audit


class TrusteeOps:
    """High-level trustee operations over a deployed team.

    Args:
        engine: A configured TeamEngine instance.
        home: Agent home directory (used for audit log path).
        paths: Fleet tree paths for the actuation-readiness/freeze gate
            (card e51a3e7e). Defaults to `FleetPaths(root=home / "fleet")`,
            matching `fleet.paths.default_paths`'s own `<home>/fleet`
            convention; override in tests to point at a throwaway tree.
        shared_root: Root the ITIL change lookup (for `rotate_agent`'s
            approved-change requirement) reads from. Defaults to `home`.
        subject: Override for the caller's capauth-authenticated fingerprint.
            When None (the default), it is resolved per call via
            `trustee_actuation.resolve_subject()`; tests inject a fixed
            value instead of standing up a real capauth identity.
        decide_fn: Override for `capauth.decide`, forwarded to
            `trustee_actuation.authorize`. Tests use this to simulate an
            allow, a deny, or an unreachable PDP without a real capauth
            store.
        capauth_base_dir: Storage root capauth reads devices/tokens from.
            Defaults to `home`.
    """

    def __init__(
        self,
        engine: TeamEngine,
        home: Optional[Path] = None,
        *,
        paths: Optional[FleetPaths] = None,
        shared_root: Optional[Path] = None,
        subject: Optional[str] = None,
        decide_fn: Optional[Callable[..., Any]] = None,
        capauth_base_dir: Optional[Path] = None,
    ) -> None:
        self._engine = engine
        self._home = (home or Path("~/.skcapstone")).expanduser()
        self._paths = paths or FleetPaths(root=self._home / "fleet")
        self._shared_root = shared_root or self._home
        self._subject_override = subject
        self._decide_fn = decide_fn
        self._capauth_base_dir = capauth_base_dir if capauth_base_dir is not None else self._home

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _enforce_gate(
        self,
        capability: str,
        *,
        require_approved_change: bool = False,
        change_id: Optional[str] = None,
    ) -> None:
        """Refuse the calling verb unless `trustee_actuation.guard` allows it.

        Raises `trustee_actuation.ActuationRefusedError` (never a bare exception)
        so every caller -- MCP handler, CLI, test -- can branch on
        `exc.reason` without parsing text. See the module docstring and
        card e51a3e7e / AUTONOMY_ARCHITECTURE.md section 3.5(d).
        """
        subject = (
            self._subject_override
            if self._subject_override is not None
            else trustee_actuation.resolve_subject()
        )
        result = trustee_actuation.guard(
            capability,
            paths=self._paths,
            subject=subject,
            shared_root=self._shared_root,
            require_approved_change=require_approved_change,
            change_id=change_id,
            decide_fn=self._decide_fn,
            base_dir=self._capauth_base_dir,
        )
        if not result.allowed:
            raise trustee_actuation.ActuationRefusedError(
                result.reason or "refused", result.detail
            )

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
            trustee_actuation.ActuationRefusedError: If the actuation gate refuses
                (not actuation-ready, frozen, or no capauth capability grant).
        """
        deployment = self._engine.get_deployment(deployment_id)
        if not deployment:
            raise ValueError(f"Deployment '{deployment_id}' not found.")

        if agent_name:
            if agent_name not in deployment.agents:
                raise ValueError(f"Agent '{agent_name}' not in deployment '{deployment_id}'.")
            targets = {agent_name: deployment.agents[agent_name]}
        else:
            targets = dict(deployment.agents)

        self._enforce_gate(trustee_actuation.CAP_RESTART)

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
        self._audit(
            "restart_agent", deployment_id, agent_name=agent_name or "ALL", results=results
        )
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
            trustee_actuation.ActuationRefusedError: If the actuation gate refuses
                (not actuation-ready, frozen, or no capauth capability grant).
        """
        if count < 1:
            raise ValueError("count must be >= 1.")

        deployment = self._engine.get_deployment(deployment_id)
        if not deployment:
            raise ValueError(f"Deployment '{deployment_id}' not found.")

        self._enforce_gate(trustee_actuation.CAP_SCALE)

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
            "scale_agent",
            deployment_id,
            agent_spec_key=agent_spec_key,
            desired_count=count,
            added=added,
            removed=removed,
        )
        return {"added": added, "removed": removed, "current_count": count}

    # ------------------------------------------------------------------
    # Rotate
    # ------------------------------------------------------------------

    def rotate_agent(
        self,
        deployment_id: str,
        agent_name: str,
        *,
        change_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Snapshot context, destroy, and redeploy an agent fresh.

        Used when an agent shows context degradation. Snapshots the
        agent's memory directory before destruction so nothing is lost.

        Rotation is a credential-adjacent operation (a fresh agent means a
        fresh identity) and is never routine: unlike restart/scale, it
        additionally requires `change_id` to name an ITIL change that is
        currently APPROVED (card e51a3e7e / AUTONOMY_ARCHITECTURE.md section
        3.5(d)). Omitting `change_id`, or naming a change that is not
        approved, refuses exactly like an unapproved one -- there is no
        "skip the check" default.

        Args:
            deployment_id: Target deployment.
            agent_name: Name of the specific agent instance to rotate.
            change_id: The ITIL change id authorizing this rotation. Must
                fold to APPROVED status or the call is refused.

        Returns:
            Dict with "snapshot_path", "destroyed", "redeployed" keys.

        Raises:
            ValueError: If deployment or agent is not found.
            trustee_actuation.ActuationRefusedError: If the actuation gate refuses
                (not actuation-ready, frozen, no capauth capability grant, or
                no approved ITIL change).
        """
        deployment = self._engine.get_deployment(deployment_id)
        if not deployment:
            raise ValueError(f"Deployment '{deployment_id}' not found.")

        if agent_name not in deployment.agents:
            raise ValueError(f"Agent '{agent_name}' not in deployment '{deployment_id}'.")

        self._enforce_gate(
            trustee_actuation.CAP_ROTATE,
            require_approved_change=True,
            change_id=change_id,
        )

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

    def _live_agent_health(
        self,
        name: str,
        agent: DeployedAgent,
    ) -> Dict[str, Any]:
        """Run a live health check on a single agent and return its row.

        Calls ``provider.health_check`` when a provider is configured,
        updating the agent's cached status and heartbeat in place; without a
        provider it falls back to the last-known status from disk. Shared by
        :meth:`health_report` and :meth:`agent_health` so both surfaces
        evaluate health identically.

        Args:
            name: The agent instance name.
            agent: The deployed agent record to evaluate (mutated in place).

        Returns:
            Dict with name, status, host, last_heartbeat, error, healthy.
        """
        provider = self._engine._provider
        live_status = agent.status

        if provider:
            try:
                live_status = provider.health_check(name, self._provision_result(agent))
                agent.status = live_status
                if live_status == AgentStatus.RUNNING:
                    agent.last_heartbeat = datetime.now(timezone.utc).isoformat()
            except Exception as exc:
                live_status = AgentStatus.DEGRADED
                agent.status = live_status
                agent.error = str(exc)

        return {
            "name": name,
            "status": live_status.value,
            "host": agent.host or "\u2014",
            "last_heartbeat": agent.last_heartbeat or "\u2014",
            "error": agent.error or "",
            "healthy": live_status == AgentStatus.RUNNING,
        }

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

        report: List[Dict[str, Any]] = [
            self._live_agent_health(name, agent) for name, agent in deployment.agents.items()
        ]

        refresh_deployment_status(deployment)
        self._engine._save_deployment(deployment)
        self._audit(
            "health_report",
            deployment_id,
            agent_count=len(report),
            healthy=sum(1 for r in report if r["healthy"]),
        )
        return report

    # ------------------------------------------------------------------
    # Focused single-agent (role) status surface
    # ------------------------------------------------------------------

    def agent_health(
        self,
        query: str,
        deployment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Report the live health of one agent, resolved by name or role.

        Locates a deployed agent whose instance ``name`` or
        ``agent_spec_key`` matches ``query`` (case-insensitive) and runs a
        live health check on it. Unlike :meth:`health_report`, this surface
        is role-focused: it answers "how is the Sentinel doing?" across every
        deployment (or within a single ``deployment_id``) and, crucially,
        reports ``present=False`` with status ``"absent"`` when no matching
        agent is deployed anywhere. An absent Sentinel is the signal that the
        security role guarding a team is not running and its posture is
        unmonitored.

        Args:
            query: Agent instance name or spec key (e.g. ``"sentinel"``).
            deployment_id: Restrict the search to a single deployment; when
                omitted, all deployments are searched and the first match
                wins.

        Returns:
            A status dict with keys: query, present, healthy, status,
            deployment_id, name, spec_key, host, last_heartbeat, error.
            When absent, ``present`` and ``healthy`` are ``False`` and
            ``status`` is ``"absent"``.

        Raises:
            ValueError: If ``deployment_id`` is given but does not exist.
        """
        q = query.strip().lower()

        if deployment_id:
            dep = self._engine.get_deployment(deployment_id)
            if not dep:
                raise ValueError(f"Deployment '{deployment_id}' not found.")
            deployments = [dep]
        else:
            deployments = self._engine.list_deployments()

        for dep in deployments:
            for name, agent in dep.agents.items():
                if q in (name.lower(), agent.agent_spec_key.lower()):
                    row = self._live_agent_health(name, agent)
                    refresh_deployment_status(dep)
                    self._engine._save_deployment(dep)
                    self._audit(
                        "agent_health",
                        dep.deployment_id,
                        agent_name=name,
                        query=query,
                        healthy=row["healthy"],
                    )
                    return {
                        "query": query,
                        "present": True,
                        "deployment_id": dep.deployment_id,
                        "spec_key": agent.agent_spec_key,
                        **row,
                    }

        return {
            "query": query,
            "present": False,
            "healthy": False,
            "status": "absent",
            "deployment_id": None,
            "name": None,
            "spec_key": None,
            "host": None,
            "last_heartbeat": None,
            "error": "",
        }

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
                raise ValueError(f"Agent '{agent_name}' not in deployment '{deployment_id}'.")
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
                logs[name] = audit_lines_for_agent(self._home, deployment_id, name, tail=tail)

        self._audit("get_logs", deployment_id, agent_name=agent_name or "ALL", tail=tail)
        return logs
