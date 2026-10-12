"""Outer filesystem boundary and immutable Cursor-plan configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .native_codex import GUARD, _path
from .production_cursor import require_model
from .worker_git import identity


def write_config(directory: Path, model: str) -> None:
    """Create fresh config without inherited models, agents, hooks, MCP or plugins."""
    require_model(model)
    config = {
        "version": 1,
        "model": {"modelId": model},
        "selectedModel": {"modelId": model, "parameters": []},
        "hasChangedDefaultModel": True,
        "exploreSubagentModel": "inherit",
        "subagentModels": {"explore": "disabled"},
        "permissions": {"allow": [], "deny": []},
        "approvalMode": "allowlist",
        "sandbox": {
            "mode": "enabled",
            "readBoundary": "workspace",
            "networkAccess": "user_config_only",
            "networkAllowlist": [],
        },
    }
    # The installed CLI supports a permission response before any subagent starts.
    # Disable every subagent type, not only the configurable explore default.
    deny = [{"command": "printf '%s\\n' '{\"permission\":\"deny\"}'"}]
    for name, value in {
        "cli-config.json": config,
        "hooks.json": {"version": 1, "hooks": {"subagentStart": deny}},
        "mcp.json": {"mcpServers": {}},
    }.items():
        (directory / name).write_text(json.dumps(value) + "\n")
    (directory / "empty").mkdir()


def sandbox_command(
    workspace: Path,
    executable: Path,
    auth: Path,
    config: Path,
    *,
    model: str,
    owner: str,
    card: str,
    claim: str,
    prompt: str,
) -> list[str]:
    """Mount one writable source tree, private tmpfs and read-only runtime/auth."""
    require_model(model)
    workspace = _path(workspace, directory=True, private=True)
    executable = _path(executable)
    auth = _path(auth, private=True)
    config = _path(config, directory=True, private=True)
    runtime = _path(executable.parent, directory=True)
    _path(runtime / "node")
    _path(runtime / "index.js")
    if executable.name != "cursor-agent":
        raise ValueError("Cursor executable must be the installed cursor-agent")
    if not (workspace / ".git").is_dir() or (workspace / ".git").is_symlink():
        raise ValueError("Cursor requires an isolated repository with a real .git directory")
    command = [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--new-session",
        "--uid",
        "0",
        "--gid",
        "0",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
    ]
    for directory in ("/lib", "/lib64"):
        if Path(directory).exists():
            command += ["--ro-bind", directory, directory]
    for path in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/ssl/certs"):
        if Path(path).exists():
            command += ["--ro-bind", str(Path(path).resolve()), path]
    command += [
        "--ro-bind",
        str(runtime),
        str(runtime),
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--ro-bind",
        str(config),
        "/tmp/.cursor",
        "--dir",
        "/tmp/.config/cursor",
        "--ro-bind",
        str(auth),
        "/tmp/.config/cursor/auth.json",
        "--bind",
        str(workspace),
        "/work",
    ]
    # Repository-local configuration cannot override the pinned operator model.
    for name in (".cursor", ".claude", ".agents"):
        path = workspace / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError("Cursor workspace configuration is redirected")
        # bwrap creates a missing, empty mount point inside this writable source.
        # Mask even absent config so the agent cannot introduce a model override.
        command += ["--ro-bind", str(config / "empty"), "/work/" + name]
    environment = {
        "HOME": "/tmp",
        "CURSOR_CONFIG_DIR": "/tmp/.cursor",
        "CURSOR_DATA_DIR": "/tmp/data",
        "XDG_CONFIG_HOME": "/tmp/.config",
        "PATH": str(runtime) + ":/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "SKAGENT": owner,
        "SKFLEET_CARD_ID": card,
        "SKFLEET_CLAIM_REVISION": claim,
        "SKFLEET_SESSION_ID": owner,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/dev/null",
        "GIT_CONFIG_KEY_1": "core.fsmonitor",
        "GIT_CONFIG_VALUE_1": "false",
        **identity(owner),
    }
    for key, value in environment.items():
        command += ["--setenv", key, value]
    return [
        *command,
        "--chdir",
        "/work",
        "--remount-ro",
        "/",
        "--",
        "/usr/bin/python3",
        "-I",
        "-c",
        GUARD.replace("Codex", "Cursor"),
        str(os.getuid()),
        str(os.getgid()),
        str(executable),
        "-p",
        "--output-format",
        "stream-json",
        "--model",
        model,
        "--sandbox",
        "enabled",
        "--workspace",
        "/work",
        prompt,
    ]
