"""Host-local policy and lifecycle controls for SK MCP clients.

The repository declares safe defaults. A host may override non-secret values in
``~/.config/skcapstone/mcp-host.json``. Generated client files contain paths and
agent selectors only. Credentials remain in host-local environment or keyring
sources and are never copied into generated configuration.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class MCPPolicy:
    """Validated, non-secret MCP policy for one physical host."""

    approval_policy: str = "on-request"
    startup_timeout_sec: int = 20
    tool_timeout_sec: int = 120
    max_stdio_processes: int = 4


_ALLOWED_OVERLAY_KEYS = {
    "approval_policy",
    "startup_timeout_sec",
    "tool_timeout_sec",
    "max_stdio_processes",
}
_SECRET_WORDS = ("secret", "token", "password", "credential", "auth", "cookie", "session")
_MANAGED_SERVERS = ("skcapstone", "skmemory")


def load_host_policy(path: Path | None = None) -> MCPPolicy:
    """Load a host overlay, rejecting secret-shaped and unknown fields."""
    overlay = path or Path.home() / ".config" / "skcapstone" / "mcp-host.json"
    if not overlay.exists():
        return MCPPolicy()
    if overlay.stat().st_mode & 0o077:
        raise ValueError(f"host overlay must not be group/world accessible: {overlay}")
    raw = json.loads(overlay.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("host overlay must be a JSON object")
    unknown = set(raw) - _ALLOWED_OVERLAY_KEYS
    secret_keys = {key for key in raw if any(word in key.lower() for word in _SECRET_WORDS)}
    if unknown or secret_keys:
        raise ValueError(f"unsupported host overlay fields: {sorted(unknown | secret_keys)}")
    policy = MCPPolicy(**raw)
    if policy.approval_policy not in {"untrusted", "on-failure", "on-request", "never"}:
        raise ValueError("unsupported approval_policy")
    if not 1 <= policy.startup_timeout_sec <= 300:
        raise ValueError("startup_timeout_sec must be between 1 and 300")
    if not policy.startup_timeout_sec <= policy.tool_timeout_sec <= 3600:
        raise ValueError("tool_timeout_sec must be at least startup timeout and at most 3600")
    if not 1 <= policy.max_stdio_processes <= 32:
        raise ValueError("max_stdio_processes must be between 1 and 32")
    return policy


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _replace_top_level(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    line = f"{key} = {value}"
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    first_table = re.search(r"^\[", text, re.MULTILINE)
    at = first_table.start() if first_table else len(text)
    prefix = text[:at]
    suffix = text[at:]
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + line + "\n" + suffix


def _replace_table(text: str, name: str, block: str) -> str:
    header = re.compile(rf"^\[mcp_servers\.{re.escape(name)}\]\s*$", re.MULTILINE)
    match = header.search(text)
    if not match:
        separator = "" if not text or text.endswith("\n") else "\n"
        return text + separator + block + "\n"
    next_table = re.search(r"^\[", text[match.end() :], re.MULTILINE)
    end = match.end() + next_table.start() if next_table else len(text)
    return text[: match.start()] + block + "\n" + text[end:]


def render_codex_config(existing: str, policy: MCPPolicy, *, home: Path, agent: str) -> str:
    """Render managed Codex entries while preserving unrelated configuration."""
    text = _replace_top_level(existing, "approval_policy", _toml_string(policy.approval_policy))
    env = {"SKAGENT": agent, "SKCAPSTONE_AGENT": agent, "SKMEMORY_AGENT": agent}
    env_text = ", ".join(f"{_toml_string(k)} = {_toml_string(v)}" for k, v in env.items())
    for name in _MANAGED_SERVERS:
        command = home / ".skenv" / "bin" / f"{name}-mcp"
        block = "\n".join(
            (
                f"[mcp_servers.{name}]",
                f"command = {_toml_string(str(command))}",
                f"env = {{ {env_text} }}",
                f"startup_timeout_sec = {policy.startup_timeout_sec}",
                f"tool_timeout_sec = {policy.tool_timeout_sec}",
            )
        )
        text = _replace_table(text, name, block)
    return text


def reconcile_codex_config(
    *,
    path: Path | None = None,
    policy: MCPPolicy | None = None,
    agent: str,
    dry_run: bool = False,
) -> dict[str, str]:
    """Reconcile host-local Codex MCP configuration and report exact drift."""
    target = path or Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "config.toml"
    current = target.read_text(encoding="utf-8") if target.exists() else ""
    desired = render_codex_config(
        current, policy or load_host_policy(), home=Path.home(), agent=agent
    )
    if desired == current:
        return {"action": "exists", "path": str(target)}
    action = "would-update" if target.exists() else "would-create"
    if dry_run:
        return {"action": action, "path": str(target)}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(desired, encoding="utf-8")
    return {"action": "updated" if current else "created", "path": str(target)}


@contextmanager
def stdio_process_slot(
    server_name: str,
    *,
    policy: MCPPolicy | None = None,
    runtime_dir: Path | None = None,
) -> Iterator[int]:
    """Hold one host-local advisory slot, refusing unbounded MCP growth."""
    import fcntl

    effective = policy or load_host_policy()
    root = runtime_dir or Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/skcapstone-{os.getuid()}"))
    slots = root / "skcapstone-mcp-slots"
    slots.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = None
    selected = -1
    for slot in range(effective.max_stdio_processes):
        candidate = (slots / f"{server_name}-{slot}.lock").open("a+")
        try:
            fcntl.flock(candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            candidate.close()
            continue
        handle, selected = candidate, slot
        break
    if handle is None:
        raise RuntimeError(
            f"{server_name} MCP process ceiling reached ({effective.max_stdio_processes})"
        )
    try:
        yield selected
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
