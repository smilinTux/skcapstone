"""
Local Provider — runs agent teams as local processes backed by crush/OpenClaw sessions.

Each agent is spawned as a real ``crush`` (OpenClaw fork) subprocess that receives
its full identity via a generated session config: soul blueprint, skills list, model
tier, and coordination context.  If the crush binary is not present the provider
falls back to a lightweight Python stub so development can proceed without the full
runtime installed.

Session lifecycle
-----------------
1. ``provision()``  — create work dir, write ``config.json`` + ``session.json``
2. ``configure()``  — resolve soul blueprint & skill paths; write ``crush.json``
3. ``start()``      — spawn ``crush run --session session.json`` as a daemon process
4. ``health_check()``— read session state file; fall back to PID liveness check
5. ``stop()``       — SIGTERM → wait → SIGKILL; write tombstone
6. ``destroy()``    — stop + remove work dir

Per hosted-agents best practice: session-isolated state, filesystem memory,
health checks via session state file then PID monitoring.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..blueprints.schema import AgentSpec, ProviderType
from ..team_engine import AgentStatus, ProviderBackend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CRUSH_BINARY_NAMES: List[str] = ["crush", "openclaw"]
_SESSION_STATE_FILE = "session_state.json"
_PID_FILE = "agent.pid"
_SESSION_CONFIG_FILE = "session.json"
_CRUSH_CONFIG_FILE = "crush.json"
_STOP_TIMEOUT_SECONDS = 15
_STOP_KILL_TIMEOUT_SECONDS = 5

# Session state values written by the crush daemon
_STATE_RUNNING = "running"
_STATE_IDLE = "idle"
_STATE_ERROR = "error"
_STATE_STOPPED = "stopped"


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _find_crush_binary() -> Optional[str]:
    """Locate the crush or openclaw binary on PATH.

    Returns:
        Absolute path to the binary, or None if neither is found.
    """
    import shutil

    for name in _CRUSH_BINARY_NAMES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _resolve_soul_blueprint_path(
    soul_blueprint: Optional[str],
    work_dir: Path,
    repo_root: Optional[Path] = None,
) -> Optional[str]:
    """Resolve a soul blueprint reference to an absolute path.

    Checks (in order):
    1. Absolute path as-is.
    2. Relative to repo soul-blueprints/blueprints/ directory.
    3. Relative to the agent's work_dir.

    Args:
        soul_blueprint: Blueprint slug or path from AgentSpec.
        work_dir: Agent working directory.
        repo_root: Optional repo root for resolving workspace-relative paths.

    Returns:
        Resolved absolute path string, or the original value if unresolvable.
    """
    if not soul_blueprint:
        return None

    candidate = Path(soul_blueprint)
    if candidate.is_absolute() and candidate.exists():
        return str(candidate)

    if repo_root:
        # Try soul-blueprints/blueprints/<slug>/<LUMINA.md> style
        blueprint_dir = repo_root / "soul-blueprints" / "blueprints" / soul_blueprint
        if blueprint_dir.exists():
            return str(blueprint_dir)
        # Try soul-blueprints/<value> directly
        direct = repo_root / "soul-blueprints" / soul_blueprint
        if direct.exists():
            return str(direct)
        # Try relative to repo root
        relative = repo_root / soul_blueprint
        if relative.exists():
            return str(relative)

    # Relative to work dir
    relative_to_wd = work_dir / soul_blueprint
    if relative_to_wd.exists():
        return str(relative_to_wd)

    # Return original value; crush may resolve it itself
    return soul_blueprint


def _resolve_skill_paths(
    skills: List[str],
    repo_root: Optional[Path] = None,
    agent: str = "global",
) -> List[str]:
    """Resolve skill names to absolute paths where possible.

    Uses the session_skills bridge to check the SKSkills registry first,
    then falls back to legacy OpenClaw skill paths.

    Args:
        skills: List of skill names or paths from AgentSpec.
        repo_root: Optional repo root for resolving workspace-relative paths.
        agent: Agent namespace for SKSkills per-agent lookup.

    Returns:
        List of resolved paths (unresolvable names kept as-is).
    """
    try:
        from ..session_skills import resolve_skill_paths_with_skskills
        return resolve_skill_paths_with_skskills(skills, agent=agent, repo_root=repo_root)
    except ImportError:
        pass

    # Fallback: legacy resolution without SKSkills
    resolved: List[str] = []
    for skill in skills:
        path = Path(skill)
        if path.is_absolute() and path.exists():
            resolved.append(str(path))
            continue

        if repo_root:
            skill_file = repo_root / "openclaw-skills" / f"{skill}.skill"
            if skill_file.exists():
                resolved.append(str(skill_file))
                continue
            skill_dir = repo_root / "openclaw-skills" / skill
            if skill_dir.exists():
                resolved.append(str(skill_dir))
                continue

        resolved.append(skill)

    return resolved


# ---------------------------------------------------------------------------
# Session config builders
# ---------------------------------------------------------------------------


def _resolve_model_via_router(
    spec: AgentSpec,
    description: str = "",
) -> str:
    """Resolve the concrete model name using the Model Router.

    If the spec has an explicit model_name, that takes priority.
    Otherwise, the router selects based on the model tier and task context.

    Args:
        spec: Agent specification containing model tier and optional model name.
        description: Task/role description for tag-based routing.

    Returns:
        Concrete model name string.
    """
    if spec.model_name:
        return spec.model_name

    try:
        from ..model_router import ModelRouter, TaskSignal

        router = ModelRouter()
        signal = TaskSignal(
            description=description or spec.description or f"{spec.role.value} agent",
            tags=[spec.role.value, spec.model.value],
        )
        decision = router.route(signal)
        logger.debug(
            "Model router: tier=%s model=%s reason=%s",
            decision.tier.value, decision.model_name, decision.reasoning,
        )
        return decision.model_name
    except ImportError:
        return spec.model.value


def _build_session_config(
    agent_name: str,
    team_name: str,
    spec: AgentSpec,
    work_dir: Path,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build the session.json payload for a crush agent session.

    Uses the Model Router to resolve the model tier to a concrete model name
    when no explicit model_name is set in the agent spec.

    Args:
        agent_name: Unique agent instance name.
        team_name: Parent team name.
        spec: Agent specification containing soul, skills, model, role.
        work_dir: Agent working directory (used for memory/scratch paths).
        repo_root: Optional repo root for path resolution.

    Returns:
        Dictionary ready to be serialised as session.json.
    """
    soul_path = _resolve_soul_blueprint_path(
        spec.soul_blueprint, work_dir, repo_root
    )
    skill_paths = _resolve_skill_paths(spec.skills, repo_root, agent=agent_name)
    model = _resolve_model_via_router(spec, f"{agent_name} in team {team_name}")

    config: Dict[str, Any] = {
        "agent_name": agent_name,
        "team_name": team_name,
        "role": spec.role.value,
        "model": model,
        "model_tier": spec.model.value,
        "soul_blueprint": soul_path,
        "skills": skill_paths,
        "memory_dir": str(work_dir / "memory"),
        "scratch_dir": str(work_dir / "scratch"),
        "state_file": str(work_dir / _SESSION_STATE_FILE),
        "env": spec.env,
    }
    return config


def _build_crush_config(
    agent_name: str,
    session_config: Dict[str, Any],
    work_dir: Path,
) -> Dict[str, Any]:
    """Build the crush.json that the crush daemon reads on startup.

    Mirrors the structure of the project-level crush.json but scoped to a
    single agent session.

    Args:
        agent_name: Agent instance name.
        session_config: Output of _build_session_config().
        work_dir: Agent working directory.

    Returns:
        Dictionary ready to be serialised as crush.json.
    """
    crush_cfg: Dict[str, Any] = {
        "$schema": "https://charm.land/crush.json",
        "options": {
            "initialize_as": session_config.get("soul_blueprint", "AGENTS.md"),
            "context_paths": [
                session_config.get("soul_blueprint"),
            ],
            "skills_paths": [
                str(work_dir / "skills"),
                "~/.config/crush/skills",
                "~/.openclaw/skills",
            ],
            "debug": False,
            "disabled_tools": [],
        },
        "permissions": {
            "allowed_tools": [
                "view",
                "ls",
                "grep",
                "edit",
                "mcp_skcapstone_agent_status",
                "mcp_skcapstone_memory_store",
                "mcp_skcapstone_memory_recall",
                "mcp_skcapstone_coord_status",
                "mcp_skcapstone_coord_claim",
            ]
        },
        "session": {
            "agent_name": agent_name,
            "model": session_config.get("model", "fast"),
            "role": session_config.get("role", "worker"),
            "skills": session_config.get("skills", []),
            "memory_dir": session_config.get("memory_dir"),
            "state_file": session_config.get("state_file"),
        },
    }
    # Remove None values from context_paths
    crush_cfg["options"]["context_paths"] = [
        p for p in crush_cfg["options"]["context_paths"] if p is not None
    ]
    return crush_cfg


# ---------------------------------------------------------------------------
# Session state helpers
# ---------------------------------------------------------------------------


def _read_session_state(work_dir: Path) -> Optional[Dict[str, Any]]:
    """Read session state written by the crush daemon.

    Args:
        work_dir: Agent working directory.

    Returns:
        Parsed state dict, or None if missing/corrupt.
    """
    state_file = work_dir / _SESSION_STATE_FILE
    if not state_file.exists():
        return None
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_session_state(work_dir: Path, state: Dict[str, Any]) -> None:
    """Write session state to disk.

    Args:
        work_dir: Agent working directory.
        state: State dictionary to persist.
    """
    state_file = work_dir / _SESSION_STATE_FILE
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _read_pid(work_dir: Path) -> Optional[int]:
    """Read the PID from the agent PID file.

    Args:
        work_dir: Agent working directory.

    Returns:
        PID integer or None.
    """
    pid_file = work_dir / _PID_FILE
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _pid_is_alive(pid: int) -> bool:
    """Check whether a process is alive via signal 0.

    Args:
        pid: Process ID to check.

    Returns:
        True if the process exists and is accessible.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        # Permission denied means process exists but we can't signal it
        return True


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class LocalProvider(ProviderBackend):
    """Deploy agents as local processes backed by crush/OpenClaw sessions.

    Each agent is given its own working directory containing:
    - ``config.json``        — human-readable agent configuration
    - ``session.json``       — crush session payload (soul, skills, model)
    - ``crush.json``         — crush daemon config (written during configure())
    - ``agent.pid``          — PID of the crush daemon process
    - ``session_state.json`` — live state written by the crush daemon
    - ``memory/``            — persistent memory directory
    - ``scratch/``           — ephemeral scratch space

    Args:
        home: Agent home directory (default: ``~/.skcapstone``).
        work_dir: Root directory for agent working dirs.
        repo_root: Workspace root for resolving soul/skill paths.
        crush_binary: Explicit path to crush/openclaw binary (auto-detected
            from PATH if not provided).
    """

    provider_type = ProviderType.LOCAL

    def __init__(
        self,
        home: Optional[Path] = None,
        work_dir: Optional[Path] = None,
        repo_root: Optional[Path] = None,
        crush_binary: Optional[str] = None,
    ) -> None:
        self._home = (home or Path("~/.skcapstone")).expanduser()
        self._work_dir = work_dir or (self._home / "agents" / "local")
        self._work_dir.mkdir(parents=True, exist_ok=True)
        self._repo_root = repo_root
        self._crush_binary = crush_binary  # None = auto-detect at start time

    # ------------------------------------------------------------------
    # provision
    # ------------------------------------------------------------------

    def provision(
        self,
        agent_name: str,
        spec: AgentSpec,
        team_name: str,
    ) -> Dict[str, Any]:
        """Create the agent working directory and write session config.

        Args:
            agent_name: Unique agent instance name.
            spec: Agent specification.
            team_name: Parent team name.

        Returns:
            Dict with ``work_dir``, ``host``, and session configuration fields.
        """
        agent_dir = self._work_dir / agent_name
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "memory").mkdir(exist_ok=True)
        (agent_dir / "scratch").mkdir(exist_ok=True)
        (agent_dir / "skills").mkdir(exist_ok=True)

        session_config = _build_session_config(
            agent_name=agent_name,
            team_name=team_name,
            spec=spec,
            work_dir=agent_dir,
            repo_root=self._repo_root,
        )

        # Human-readable summary
        (agent_dir / "config.json").write_text(
            json.dumps(session_config, indent=2), encoding="utf-8"
        )

        # Crush session payload
        (agent_dir / _SESSION_CONFIG_FILE).write_text(
            json.dumps(session_config, indent=2), encoding="utf-8"
        )

        # Wire SKSkills into the session
        skill_result = self._prepare_skskills(
            agent_name, session_config.get("skills", []), agent_dir
        )
        if skill_result and skill_result.get("skills_loaded", 0) > 0:
            try:
                from ..session_skills import enrich_session_config
                enrich_session_config(session_config, skill_result)
            except ImportError:
                pass

        logger.info(
            "Provisioned agent %s (role=%s model=%s soul=%s skills=%s skskills=%d)",
            agent_name,
            spec.role.value,
            session_config["model"],
            session_config.get("soul_blueprint"),
            session_config.get("skills"),
            skill_result.get("skills_loaded", 0) if skill_result else 0,
        )

        return {
            "host": "localhost",
            "work_dir": str(agent_dir),
            "session_config": session_config,
            "skill_result": skill_result,
        }

    def _prepare_skskills(
        self,
        agent_name: str,
        skills: List[str],
        work_dir: Path,
    ) -> Optional[Dict[str, Any]]:
        """Prepare SKSkills for an agent session.

        Args:
            agent_name: Agent instance name.
            skills: Resolved skill paths.
            work_dir: Agent working directory.

        Returns:
            Skill preparation result dict, or None if skskills unavailable.
        """
        try:
            from ..session_skills import prepare_session_skills
            return prepare_session_skills(agent_name, skills, work_dir)
        except ImportError:
            return None

    # ------------------------------------------------------------------
    # configure
    # ------------------------------------------------------------------

    def configure(
        self,
        agent_name: str,
        spec: AgentSpec,
        provision_result: Dict[str, Any],
    ) -> bool:
        """Write the crush.json daemon config into the agent's work directory.

        Args:
            agent_name: Agent instance name.
            spec: Agent specification.
            provision_result: Output from provision().

        Returns:
            True if configuration succeeded, False on error.
        """
        work_dir_str = provision_result.get("work_dir", "")
        if not work_dir_str:
            logger.error("configure: missing work_dir for %s", agent_name)
            return False

        work_dir = Path(work_dir_str)
        session_config = provision_result.get("session_config", {})

        crush_cfg = _build_crush_config(agent_name, session_config, work_dir)

        # Enrich crush config with SKSkills MCP server entry
        skill_result = provision_result.get("skill_result")
        if skill_result:
            try:
                from ..session_skills import enrich_crush_config
                enrich_crush_config(crush_cfg, skill_result)
            except ImportError:
                pass

        try:
            (work_dir / _CRUSH_CONFIG_FILE).write_text(
                json.dumps(crush_cfg, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.error(
                "configure: failed to write crush.json for %s: %s", agent_name, exc
            )
            return False

        logger.debug("Configured crush session for %s at %s", agent_name, work_dir)
        return True

    # ------------------------------------------------------------------
    # start
    # ------------------------------------------------------------------

    def start(
        self,
        agent_name: str,
        provision_result: Dict[str, Any],
    ) -> bool:
        """Spawn a crush/OpenClaw session for the agent.

        Attempts to launch the crush binary found on PATH.  If crush is not
        installed, falls back to a lightweight Python stub process that writes
        the required session state so the rest of the engine can proceed.

        The session receives:
        - ``--session session.json``    — full agent identity config
        - ``--config crush.json``       — crush daemon config
        - ``--headless``                — non-interactive daemon mode

        Environment variables passed to the process:
        - ``AGENT_NAME``, ``TEAM_NAME``, ``SOUL_BLUEPRINT``
        - ``AGENT_MODEL``, ``AGENT_ROLE``, ``AGENT_SKILLS``
        - ``SKCAPSTONE_HOME``, ``AGENT_MEMORY_DIR``
        - Any extra env vars from ``AgentSpec.env``

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            True if the process started successfully.
        """
        work_dir_str = provision_result.get("work_dir", "")
        if not work_dir_str:
            logger.error("start: missing work_dir for %s", agent_name)
            return False

        work_dir = Path(work_dir_str)
        session_config = provision_result.get("session_config", {})

        env = self._build_process_env(session_config)
        binary = self._crush_binary or _find_crush_binary()

        if binary:
            return self._start_crush_session(
                agent_name, work_dir, binary, env, provision_result
            )
        else:
            logger.warning(
                "crush binary not found on PATH; using stub for %s", agent_name
            )
            return self._start_stub_session(
                agent_name, work_dir, env, provision_result
            )

    def _build_process_env(self, session_config: Dict[str, Any]) -> Dict[str, str]:
        """Build the environment dict for the crush/stub subprocess.

        Args:
            session_config: Agent session configuration dict.

        Returns:
            Environment variable dict (inherits current process env).
        """
        env = os.environ.copy()
        env.update({
            "AGENT_NAME": session_config.get("agent_name", ""),
            "TEAM_NAME": session_config.get("team_name", ""),
            "SOUL_BLUEPRINT": session_config.get("soul_blueprint") or "",
            "AGENT_MODEL": session_config.get("model", ""),
            "AGENT_MODEL_TIER": session_config.get("model_tier", ""),
            "AGENT_ROLE": session_config.get("role", ""),
            "AGENT_SKILLS": json.dumps(session_config.get("skills", [])),
            "SKCAPSTONE_HOME": str(self._home),
            "AGENT_MEMORY_DIR": session_config.get("memory_dir", ""),
            "AGENT_SCRATCH_DIR": session_config.get("scratch_dir", ""),
            "AGENT_STATE_FILE": session_config.get("state_file", ""),
        })
        # Merge spec-level env overrides
        extra_env = session_config.get("env", {}) or {}
        env.update({k: str(v) for k, v in extra_env.items()})
        return env

    def _start_crush_session(
        self,
        agent_name: str,
        work_dir: Path,
        binary: str,
        env: Dict[str, str],
        provision_result: Dict[str, Any],
    ) -> bool:
        """Launch a real crush daemon subprocess.

        Args:
            agent_name: Agent instance name.
            work_dir: Agent working directory.
            binary: Absolute path to crush/openclaw binary.
            env: Environment variables for the subprocess.
            provision_result: Mutated in-place with the spawned PID.

        Returns:
            True if the process started without error.
        """
        cmd = [
            binary,
            "run",
            "--session", str(work_dir / _SESSION_CONFIG_FILE),
            "--config", str(work_dir / _CRUSH_CONFIG_FILE),
            "--headless",
            "--state-file", str(work_dir / _SESSION_STATE_FILE),
        ]
        log_file = work_dir / "agent.log"

        try:
            with open(log_file, "ab") as log_fh:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(work_dir),
                    env=env,
                    stdout=log_fh,
                    stderr=log_fh,
                    start_new_session=True,  # detach from parent's process group
                )
        except OSError as exc:
            logger.error(
                "start: failed to launch crush for %s: %s", agent_name, exc
            )
            return False

        pid = proc.pid
        (work_dir / _PID_FILE).write_text(str(pid), encoding="utf-8")
        provision_result["pid"] = pid

        _write_session_state(work_dir, {
            "status": _STATE_RUNNING,
            "pid": pid,
            "agent_name": agent_name,
            "started_at": _now_iso(),
            "binary": binary,
        })

        logger.info(
            "Started crush session for %s (pid=%d binary=%s)",
            agent_name, pid, binary,
        )
        return True

    def _start_stub_session(
        self,
        agent_name: str,
        work_dir: Path,
        env: Dict[str, str],
        provision_result: Dict[str, Any],
    ) -> bool:
        """Launch a Python stub process when crush is not available.

        The stub writes a running state file and then sleeps until signalled,
        giving the engine a real process to monitor without a full AI runtime.

        Args:
            agent_name: Agent instance name.
            work_dir: Agent working directory.
            env: Environment variables for the subprocess.
            provision_result: Mutated in-place with the spawned PID.

        Returns:
            True if the stub started successfully.
        """
        state_file = str(work_dir / _SESSION_STATE_FILE)
        stub_script = _stub_script(agent_name, state_file)

        try:
            proc = subprocess.Popen(
                [os.sys.executable, "-c", stub_script],
                cwd=str(work_dir),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            logger.error(
                "start: failed to launch stub for %s: %s", agent_name, exc
            )
            return False

        pid = proc.pid
        (work_dir / _PID_FILE).write_text(str(pid), encoding="utf-8")
        provision_result["pid"] = pid

        _write_session_state(work_dir, {
            "status": _STATE_RUNNING,
            "pid": pid,
            "agent_name": agent_name,
            "started_at": _now_iso(),
            "binary": "python-stub",
        })

        logger.info(
            "Started stub session for %s (pid=%d)", agent_name, pid
        )
        return True

    # ------------------------------------------------------------------
    # stop
    # ------------------------------------------------------------------

    def stop(
        self,
        agent_name: str,
        provision_result: Dict[str, Any],
    ) -> bool:
        """Gracefully stop the agent session (SIGTERM → wait → SIGKILL).

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision() / start().

        Returns:
            True if the process is no longer running.
        """
        work_dir_str = provision_result.get("work_dir", "")
        work_dir = Path(work_dir_str) if work_dir_str else None

        pid = provision_result.get("pid")
        if pid is None and work_dir:
            pid = _read_pid(work_dir)

        if not pid:
            logger.debug("stop: no pid for %s — already stopped", agent_name)
            self._write_stopped_state(agent_name, work_dir)
            return True

        if not _pid_is_alive(pid):
            logger.debug(
                "stop: pid %d for %s is already dead", pid, agent_name
            )
            self._write_stopped_state(agent_name, work_dir)
            return True

        # SIGTERM — polite shutdown
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._write_stopped_state(agent_name, work_dir)
            return True
        except OSError as exc:
            logger.warning(
                "stop: SIGTERM failed for %s (pid %d): %s", agent_name, pid, exc
            )
            return False

        # Wait for graceful shutdown
        deadline = time.time() + _STOP_TIMEOUT_SECONDS
        while time.time() < deadline:
            if not _pid_is_alive(pid):
                break
            time.sleep(0.5)

        if _pid_is_alive(pid):
            # Escalate to SIGKILL
            logger.warning(
                "stop: %s (pid %d) did not exit after %ds, sending SIGKILL",
                agent_name, pid, _STOP_TIMEOUT_SECONDS,
            )
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

            kill_deadline = time.time() + _STOP_KILL_TIMEOUT_SECONDS
            while time.time() < kill_deadline:
                if not _pid_is_alive(pid):
                    break
                time.sleep(0.2)

        stopped = not _pid_is_alive(pid)
        self._write_stopped_state(agent_name, work_dir)

        # Clean up SKSkills session resources
        if work_dir:
            try:
                from ..session_skills import cleanup_session_skills
                cleanup_session_skills(agent_name, work_dir)
            except ImportError:
                pass

        logger.info("Stopped agent %s (pid=%d ok=%s)", agent_name, pid, stopped)
        return stopped

    def _write_stopped_state(
        self, agent_name: str, work_dir: Optional[Path]
    ) -> None:
        """Write a STOPPED tombstone to the session state file.

        Args:
            agent_name: Agent instance name.
            work_dir: Agent working directory (may be None).
        """
        if work_dir and work_dir.exists():
            _write_session_state(work_dir, {
                "status": _STATE_STOPPED,
                "agent_name": agent_name,
                "stopped_at": _now_iso(),
            })

    # ------------------------------------------------------------------
    # destroy
    # ------------------------------------------------------------------

    def destroy(
        self,
        agent_name: str,
        provision_result: Dict[str, Any],
    ) -> bool:
        """Stop the session and remove all agent files.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            True if cleanup succeeded.
        """
        self.stop(agent_name, provision_result)

        work_dir_str = provision_result.get("work_dir", "")
        if work_dir_str:
            import shutil

            work_dir_path = Path(work_dir_str)
            if work_dir_path.exists():
                shutil.rmtree(work_dir_path)
                logger.info("Destroyed agent directory: %s", work_dir_path)

        return True

    # ------------------------------------------------------------------
    # health_check
    # ------------------------------------------------------------------

    def health_check(
        self,
        agent_name: str,
        provision_result: Dict[str, Any],
    ) -> AgentStatus:
        """Check agent health via session state file, then PID liveness.

        Primary check: read ``session_state.json`` written by the crush daemon.
        Fallback: raw PID liveness check (signal 0).

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision() / start().

        Returns:
            AgentStatus based on session state and process liveness.
        """
        work_dir_str = provision_result.get("work_dir", "")
        work_dir = Path(work_dir_str) if work_dir_str else None

        pid = provision_result.get("pid")
        if pid is None and work_dir:
            pid = _read_pid(work_dir)

        # --- Primary: session state file ---
        if work_dir:
            state = _read_session_state(work_dir)
            if state:
                return _session_state_to_agent_status(state, pid)

        # --- Fallback: raw PID check ---
        if not pid:
            return AgentStatus.STOPPED

        if _pid_is_alive(pid):
            return AgentStatus.RUNNING

        return AgentStatus.STOPPED


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _session_state_to_agent_status(
    state: Dict[str, Any], pid: Optional[int]
) -> AgentStatus:
    """Map a session state dict to an AgentStatus value.

    Args:
        state: Dictionary read from session_state.json.
        pid: Current known PID for corroboration.

    Returns:
        Appropriate AgentStatus.
    """
    raw_status = state.get("status", "").lower()

    if raw_status in (_STATE_RUNNING, _STATE_IDLE):
        # Corroborate with PID liveness if we have a PID
        state_pid = state.get("pid") or pid
        if state_pid and not _pid_is_alive(int(state_pid)):
            return AgentStatus.DEGRADED
        return AgentStatus.RUNNING

    if raw_status == _STATE_ERROR:
        return AgentStatus.DEGRADED

    if raw_status == _STATE_STOPPED:
        return AgentStatus.STOPPED

    # Unknown state value
    return AgentStatus.DEGRADED


def _stub_script(agent_name: str, state_file: str) -> str:
    """Return Python source for the lightweight stub process.

    The stub writes a running state, then sleeps until SIGTERM/SIGINT.

    Args:
        agent_name: Agent instance name.
        state_file: Absolute path to the session state file.

    Returns:
        Python source string safe for ``python -c``.
    """
    # Reason: single-quoted string avoids shell escaping issues; json import
    # and signal handling give the stub a clean lifecycle for testing.
    return (
        "import json, os, signal, sys, time\n"
        f"state_file = {repr(state_file)}\n"
        f"agent_name = {repr(agent_name)}\n"
        "running = True\n"
        "def _stop(sig, frame):\n"
        "    global running\n"
        "    running = False\n"
        "signal.signal(signal.SIGTERM, _stop)\n"
        "signal.signal(signal.SIGINT, _stop)\n"
        "with open(state_file, 'w') as f:\n"
        "    json.dump({'status': 'running', 'pid': os.getpid(), "
        "               'agent_name': agent_name}, f)\n"
        "while running:\n"
        "    time.sleep(1)\n"
        "with open(state_file, 'w') as f:\n"
        "    json.dump({'status': 'stopped', 'agent_name': agent_name}, f)\n"
    )


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string.

    Returns:
        ISO 8601 timestamp string.
    """
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
