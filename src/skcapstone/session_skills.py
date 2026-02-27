"""Session Skills Bridge — wire SKSkills into agent runtime sessions.

Bridges the gap between the team engine's session lifecycle and the
SKSkills framework.  When an agent session starts, this module:

1. Resolves skill names → SKSkills registry entries (alongside legacy OpenClaw paths)
2. Loads the agent's skills into a SkillLoader/SkillAggregator
3. Writes an MCP config snippet so the crush session can reach skill tools
4. Provides hooks for session start/stop to manage skill server lifecycle

Architecture:
    TeamEngine → LocalProvider.start()
                     |
                 SessionSkillsBridge.prepare(agent_name, skills, work_dir)
                     |
                 SkillAggregator loaded per-agent namespace
                     |
                 crush/stub session gets SKSKILLS_SOCKET or MCP config
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def resolve_skill_paths_with_skskills(
    skills: List[str],
    agent: str = "global",
    repo_root: Optional[Path] = None,
) -> List[str]:
    """Resolve skill names to paths, checking SKSkills registry first.

    Resolution order per skill name:
    1. Absolute path — use as-is if it exists
    2. SKSkills registry — check ~/.skskills/installed/<name> or agents/<agent>/<name>
    3. Legacy OpenClaw paths — openclaw-skills/<name>.skill or directory
    4. Pass-through — let crush resolve from its own skill paths

    Args:
        skills: List of skill names or paths from AgentSpec.
        agent: Agent namespace for SKSkills per-agent lookup.
        repo_root: Workspace root for resolving legacy OpenClaw paths.

    Returns:
        List of resolved paths (unresolvable names kept as-is).
    """
    import os

    skskills_home = Path(os.environ.get("SKSKILLS_HOME", "~/.skskills")).expanduser()
    resolved: List[str] = []

    for skill in skills:
        path = Path(skill)

        # 1. Absolute path
        if path.is_absolute() and path.exists():
            resolved.append(str(path))
            continue

        # 2. SKSkills registry — per-agent first, then global
        found_in_skskills = False
        search_dirs = []
        if agent != "global":
            search_dirs.append(skskills_home / "agents" / agent / skill)
        search_dirs.append(skskills_home / "installed" / skill)

        for candidate in search_dirs:
            if candidate.exists() and (candidate / "skill.yaml").exists():
                resolved.append(str(candidate))
                found_in_skskills = True
                logger.debug("Resolved skill '%s' from SKSkills: %s", skill, candidate)
                break

        if found_in_skskills:
            continue

        # 3. Legacy OpenClaw paths
        if repo_root:
            skill_file = repo_root / "openclaw-skills" / f"{skill}.skill"
            if skill_file.exists():
                resolved.append(str(skill_file))
                continue
            skill_dir = repo_root / "openclaw-skills" / skill
            if skill_dir.exists():
                resolved.append(str(skill_dir))
                continue

        # 4. Pass-through
        resolved.append(skill)

    return resolved


def prepare_session_skills(
    agent_name: str,
    skills: List[str],
    work_dir: Path,
    agent: str = "global",
) -> Dict[str, Any]:
    """Prepare SKSkills for an agent session.

    Loads the agent's skills into a SkillLoader, writes MCP configuration
    into the work directory so the crush session can discover skill tools.

    Args:
        agent_name: The agent instance name.
        skills: List of skill names/paths from AgentSpec.
        work_dir: Agent working directory.
        agent: SKSkills agent namespace.

    Returns:
        Dict with loaded skill metadata and MCP config paths.
    """
    result: Dict[str, Any] = {
        "skills_loaded": 0,
        "skill_names": [],
        "tools_available": [],
        "mcp_config_path": None,
        "errors": [],
    }

    try:
        from skskills.loader import SkillLoader
        from skskills.models import parse_skill_yaml
    except ImportError:
        logger.debug("skskills not installed — skipping skill loading for %s", agent_name)
        return result

    loader = SkillLoader()

    for skill_path_str in skills:
        skill_path = Path(skill_path_str)
        skill_yaml = skill_path / "skill.yaml"

        if not skill_yaml.exists():
            # Not an SKSkills directory — skip (might be OpenClaw .skill file)
            continue

        try:
            server = loader.load(skill_path)
            result["skill_names"].append(server.manifest.name)
            result["skills_loaded"] += 1
        except Exception as exc:
            logger.warning("Failed to load skill at %s: %s", skill_path, exc)
            result["errors"].append(f"{skill_path}: {exc}")

    # Collect available tools
    for schema in loader.all_tools():
        result["tools_available"].append(schema["name"])

    # Write MCP config for the session
    if result["skills_loaded"] > 0:
        mcp_config = _build_skill_mcp_config(agent, work_dir)
        mcp_config_path = work_dir / "skskills_mcp.json"
        mcp_config_path.write_text(json.dumps(mcp_config, indent=2), encoding="utf-8")
        result["mcp_config_path"] = str(mcp_config_path)

    logger.info(
        "Prepared %d skills for %s: %s",
        result["skills_loaded"],
        agent_name,
        result["skill_names"],
    )
    return result


def _build_skill_mcp_config(agent: str, work_dir: Path) -> Dict[str, Any]:
    """Build an MCP server configuration for the SKSkills aggregator.

    This config can be merged into crush.json or used standalone to
    give the session access to SKSkills tools via MCP.

    Args:
        agent: Agent namespace for skill resolution.
        work_dir: Agent working directory.

    Returns:
        MCP server config dict.
    """
    return {
        "mcpServers": {
            "skskills": {
                "command": "skskills",
                "args": ["run", "--agent", agent],
                "description": "SKSkills aggregator — sovereign agent skills",
            },
        },
    }


def enrich_session_config(
    session_config: Dict[str, Any],
    skill_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Enrich a crush session config with SKSkills metadata.

    Adds the loaded skill tools and MCP config path to the session
    config so the crush daemon knows about available skills.

    Args:
        session_config: The existing session config dict.
        skill_result: Output from prepare_session_skills().

    Returns:
        The enriched session config (mutated in place).
    """
    if skill_result["skills_loaded"] > 0:
        session_config["skskills"] = {
            "loaded": skill_result["skills_loaded"],
            "skill_names": skill_result["skill_names"],
            "tools_available": skill_result["tools_available"],
            "mcp_config": skill_result.get("mcp_config_path"),
        }

    return session_config


def enrich_crush_config(
    crush_config: Dict[str, Any],
    skill_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Enrich a crush.json config with SKSkills MCP server entry.

    Adds the SKSkills aggregator as an MCP server in the crush config
    so skill tools are available within the crush session.

    Args:
        crush_config: The existing crush.json config dict.
        skill_result: Output from prepare_session_skills().

    Returns:
        The enriched crush config (mutated in place).
    """
    if skill_result["skills_loaded"] > 0:
        if "mcpServers" not in crush_config:
            crush_config["mcpServers"] = {}

        agent = "global"
        crush_config["mcpServers"]["skskills"] = {
            "command": "skskills",
            "args": ["run", "--agent", agent],
        }

        # Add skill tool names to allowed_tools
        permissions = crush_config.get("permissions", {})
        allowed = permissions.get("allowed_tools", [])
        for tool_name in skill_result.get("tools_available", []):
            mcp_name = f"mcp_skskills_{tool_name.replace('.', '_').replace('-', '_')}"
            if mcp_name not in allowed:
                allowed.append(mcp_name)
        permissions["allowed_tools"] = allowed
        crush_config["permissions"] = permissions

    return crush_config


def cleanup_session_skills(agent_name: str, work_dir: Path) -> None:
    """Clean up skill resources when a session stops.

    Removes ephemeral skill server sockets and config files.

    Args:
        agent_name: The agent instance name.
        work_dir: Agent working directory.
    """
    mcp_config = work_dir / "skskills_mcp.json"
    if mcp_config.exists():
        mcp_config.unlink(missing_ok=True)

    logger.debug("Cleaned up skill resources for %s", agent_name)
