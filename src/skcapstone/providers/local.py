"""
Local Provider — runs agent teams as local processes.

Zero infrastructure required. Each agent runs as a subprocess on the
local machine. Best for development, testing, and single-machine
sovereign setups. The "run it all on your laptop" provider.

Per hosted-agents best practice: session-isolated state, filesystem
memory, health checks via PID monitoring.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from ..blueprints.schema import AgentSpec, ProviderType
from ..team_engine import AgentStatus, ProviderBackend

logger = logging.getLogger(__name__)


class LocalProvider(ProviderBackend):
    """Deploy agents as local processes.

    Args:
        home: Agent home directory.
        work_dir: Where to store agent working directories.
    """

    provider_type = ProviderType.LOCAL

    def __init__(
        self,
        home: Optional[Path] = None,
        work_dir: Optional[Path] = None,
    ) -> None:
        self._home = (home or Path("~/.skcapstone")).expanduser()
        self._work_dir = work_dir or (self._home / "agents" / "local")
        self._work_dir.mkdir(parents=True, exist_ok=True)

    def provision(
        self,
        agent_name: str,
        spec: AgentSpec,
        team_name: str,
    ) -> Dict[str, Any]:
        """Create a working directory for the agent.

        Args:
            agent_name: Unique agent instance name.
            spec: Agent specification.
            team_name: Parent team name.

        Returns:
            Dict with 'work_dir' and 'host'.
        """
        agent_dir = self._work_dir / agent_name
        agent_dir.mkdir(parents=True, exist_ok=True)

        (agent_dir / "memory").mkdir(exist_ok=True)
        (agent_dir / "scratch").mkdir(exist_ok=True)

        config = {
            "agent_name": agent_name,
            "team_name": team_name,
            "role": spec.role.value,
            "model": spec.model_name or spec.model.value,
            "skills": spec.skills,
            "soul_blueprint": spec.soul_blueprint,
        }
        (agent_dir / "config.json").write_text(
            json.dumps(config, indent=2), encoding="utf-8"
        )

        return {
            "host": "localhost",
            "work_dir": str(agent_dir),
        }

    def configure(
        self,
        agent_name: str,
        spec: AgentSpec,
        provision_result: Dict[str, Any],
    ) -> bool:
        """Write agent configuration files.

        Args:
            agent_name: Agent instance name.
            spec: Agent specification.
            provision_result: Output from provision().

        Returns:
            True (local config is written during provision).
        """
        return True

    def start(
        self,
        agent_name: str,
        provision_result: Dict[str, Any],
    ) -> bool:
        """Start the agent as a background process.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            True if the process started.

        Note:
            In local mode, agents are lightweight process stubs.
            Full agent runtime integration (OpenClaw sessions, etc.)
            will be wired in when the agent runtime is ready.
        """
        work_dir = provision_result.get("work_dir", "")
        pid_file = Path(work_dir) / "agent.pid"

        # Reason: For now, we write a PID placeholder. The actual agent
        # process launch will integrate with OpenClaw sessions or the
        # skcapstone daemon when those are ready.
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        provision_result["pid"] = os.getpid()

        logger.info("Local agent %s ready at %s", agent_name, work_dir)
        return True

    def stop(
        self,
        agent_name: str,
        provision_result: Dict[str, Any],
    ) -> bool:
        """Stop a local agent process.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            True if stopped or already not running.
        """
        pid = provision_result.get("pid")
        if pid and pid != os.getpid():
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as exc:
                logger.warning("Could not stop %s (pid %d): %s", agent_name, pid, exc)
                return False
        return True

    def destroy(
        self,
        agent_name: str,
        provision_result: Dict[str, Any],
    ) -> bool:
        """Remove all agent files.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            True if cleanup succeeded.
        """
        self.stop(agent_name, provision_result)

        work_dir = provision_result.get("work_dir", "")
        if work_dir:
            import shutil
            work_dir_path = Path(work_dir)
            if work_dir_path.exists():
                shutil.rmtree(work_dir_path)

        return True

    def health_check(
        self,
        agent_name: str,
        provision_result: Dict[str, Any],
    ) -> AgentStatus:
        """Check if the agent process is alive.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            AgentStatus based on process state.
        """
        pid = provision_result.get("pid")
        if not pid:
            return AgentStatus.STOPPED

        try:
            os.kill(pid, 0)
            return AgentStatus.RUNNING
        except ProcessLookupError:
            return AgentStatus.STOPPED
        except OSError:
            return AgentStatus.DEGRADED
