"""SK* suite registration orchestrator.

Single source of truth for all SK* packages and their MCP servers.
Called by `skcapstone register` and wired into `skcapstone update`.

Usage:
    from skcapstone.register import register_all

    results = register_all()            # auto-detect everything
    results = register_all(dry_run=True)  # show what would happen
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from typing import Optional

from skmemory.register import detect_environments, register_package


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_skgit_env() -> Optional[dict]:
    """Read skgit token from config file if available."""
    token_path = Path.home() / ".config" / "skgit" / "token"
    try:
        token = token_path.read_text().strip()
    except (FileNotFoundError, PermissionError):
        token = ""

    if not token:
        return None

    return {
        "GITEA_HOST": "https://skgit.skstack01.douno.it",
        "GITEA_ACCESS_TOKEN": token,
    }


# ── SK* package registry ─────────────────────────────────────────────────────


def _build_package_registry(workspace: Optional[Path] = None) -> list[dict]:
    """Build the SK* package registry with resolved env vars and plugin paths."""
    if workspace is None:
        workspace = Path.home() / "clawd"

    return [
        {
            "name": "skmemory",
            "mcp_cmd": "skmemory-mcp",
            "mcp_args": [],
            "mcp_env": None,
            "openclaw_plugin_path": workspace / "pillar-repos" / "skmemory" / "openclaw-plugin" / "src" / "index.ts",
        },
        {
            "name": "skcapstone",
            "mcp_cmd": "skcapstone-mcp",
            "mcp_args": [],
            "mcp_env": None,
            "openclaw_plugin_path": workspace / "skcapstone" / "openclaw-plugin" / "src" / "index.ts",
        },
        {
            "name": "skcomm",
            "mcp_cmd": "skcomm-mcp",
            "mcp_args": [],
            "mcp_env": None,
            "openclaw_plugin_path": workspace / "pillar-repos" / "skcomm" / "openclaw-plugin" / "src" / "index.ts",
        },
        {
            "name": "skchat",
            "mcp_cmd": "skchat-mcp",
            "mcp_args": [],
            "mcp_env": None,
            "openclaw_plugin_path": workspace / "pillar-repos" / "skchat" / "openclaw-plugin" / "src" / "index.ts",
        },
        {
            "name": "capauth",
            "mcp_cmd": None,
            "mcp_args": None,
            "mcp_env": None,
            "openclaw_plugin_path": workspace / "pillar-repos" / "capauth" / "openclaw-plugin" / "src" / "index.ts",
        },
        {
            "name": "cloud9",
            "mcp_cmd": None,
            "mcp_args": None,
            "mcp_env": None,
            "openclaw_plugin_path": workspace / "pillar-repos" / "cloud9-python" / "openclaw-plugin" / "src" / "index.ts",
        },
        {
            "name": "sksecurity",
            "mcp_cmd": None,
            "mcp_args": None,
            "mcp_env": None,
            "openclaw_plugin_path": workspace / "pillar-repos" / "sksecurity" / "openclaw-plugin" / "src" / "index.ts",
        },
        {
            "name": "skseed",
            "mcp_cmd": None,
            "mcp_args": None,
            "mcp_env": None,
            "openclaw_plugin_path": workspace / "pillar-repos" / "skseed" / "openclaw-plugin" / "src" / "index.ts",
        },
        {
            "name": "skgit",
            "mcp_cmd": "node",
            "mcp_args": [str(Path.home() / ".npm-global" / "lib" / "node_modules"
                             / "forgejo-mcp" / "build" / "index.js")],
            "mcp_env": _get_skgit_env(),
            "openclaw_plugin_path": workspace / "skills" / "skgit" / "openclaw-plugin" / "src" / "index.ts",
        },
    ]


# ── SKILL.md locator ─────────────────────────────────────────────────────────

# Mapping from package name to pillar-repo directory name
_PILLAR_DIR_MAP: dict[str, Optional[str]] = {
    "skmemory": "skmemory",
    "skcapstone": None,  # lives in workspace root, not pillar-repos
    "skcomm": "skcomm",
    "skchat": "skchat",
    "capauth": "capauth",
    "cloud9": "cloud9-python",
    "sksecurity": "sksecurity",
    "skseed": "skseed",
    "skgit": None,  # skill dir only, no pillar repo
}


def find_skill_md(pkg_name: str, workspace: Optional[Path] = None) -> Optional[Path]:
    """Locate SKILL.md for a package.

    Search order:
      1. Workspace skills directory (~/clawd/skills/<name>/SKILL.md)
      2. Pillar repos directory (~/clawd/pillar-repos/<dir>/SKILL.md)
      3. Installed package data (importlib.resources)

    Args:
        pkg_name: Package name.
        workspace: Workspace root (defaults to ~/clawd/).

    Returns:
        Path to SKILL.md, or None if not found.
    """
    if workspace is None:
        workspace = Path.home() / "clawd"

    # 1. Check skills directory (may be a symlink — that's fine)
    skill_path = workspace / "skills" / pkg_name / "SKILL.md"
    if skill_path.exists():
        return skill_path.resolve()

    # 2. Check pillar repos
    pillar_dir = _PILLAR_DIR_MAP.get(pkg_name)
    if pillar_dir:
        pillar_path = workspace / "pillar-repos" / pillar_dir / "SKILL.md"
        if pillar_path.exists():
            return pillar_path

    # 2b. Special case: skcapstone lives in workspace root
    if pkg_name == "skcapstone":
        capstone_path = workspace / "skcapstone" / "SKILL.md"
        if capstone_path.exists():
            return capstone_path

    # 3. Check installed package data
    try:
        pkg_module = pkg_name.replace("-", "_")
        ref = importlib.resources.files(pkg_module) / "SKILL.md"
        if ref is not None:
            with importlib.resources.as_file(ref) as p:
                if p.exists():
                    return p
    except (ModuleNotFoundError, TypeError, FileNotFoundError):
        pass

    return None


# ── Orchestrator ──────────────────────────────────────────────────────────────


def register_all(
    workspace: Optional[Path] = None,
    environments: Optional[list[str]] = None,
    dry_run: bool = False,
) -> dict:
    """Register all SK* packages in all detected environments.

    Args:
        workspace: Workspace root (defaults to ~/clawd/).
        environments: Target environments (auto-detect if None).
        dry_run: If True, only report what would be done.

    Returns:
        Dict with 'environments' and 'packages' keys.
    """
    if environments is None:
        environments = detect_environments()

    if workspace is None:
        workspace = Path.home() / "clawd"

    packages = _build_package_registry(workspace)

    results: dict = {
        "environments": environments,
        "packages": {},
    }

    for pkg in packages:
        name = pkg["name"]
        skill_md = find_skill_md(name, workspace)

        if skill_md is None and not dry_run:
            results["packages"][name] = {
                "skill": {"action": "error", "error": "SKILL.md not found"},
            }
            continue

        mcp_env = pkg.get("mcp_env")
        mcp_cmd = pkg.get("mcp_cmd")

        # Skip MCP registration for skgit if no token available
        if name == "skgit" and mcp_env is None:
            mcp_cmd = None

        # Resolve OpenClaw plugin path — skip if not on disk
        plugin_path = pkg.get("openclaw_plugin_path")
        if plugin_path and not Path(plugin_path).exists():
            plugin_path = None

        results["packages"][name] = register_package(
            name=name,
            skill_md_path=skill_md or Path("/dev/null"),
            mcp_command=mcp_cmd,
            mcp_args=pkg.get("mcp_args") or [],
            mcp_env=mcp_env,
            openclaw_plugin_path=plugin_path,
            workspace=workspace,
            environments=environments,
            dry_run=dry_run,
        )

    return results
