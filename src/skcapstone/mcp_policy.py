"""Host-local policy and lifecycle controls for SK MCP clients.

The repository declares safe defaults. A host may override non-secret values in
``~/.config/skcapstone/mcp-host.json``. Generated client files contain paths and
agent selectors only. Credentials remain in host-local environment or keyring
sources and are never copied into generated configuration.
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import tomllib


@dataclass(frozen=True)
class MCPPolicy:
    """Validated, non-secret MCP policy for one physical host."""

    approval_policy: str = "on-request"
    startup_timeout_sec: int = 20
    tool_timeout_sec: int = 120
    # 0 = no ceiling (default). Live load is 20+ children on a workstation, so a
    # small default would refuse real sessions; size per host via the overlay.
    max_stdio_processes: int = 0


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
    # Wrong JSON types (strings, bools, lists, null) must fail as a clear
    # ValueError, not a TypeError from a comparison or a bool passing as 1.
    if not isinstance(raw.get("approval_policy", ""), str):
        raise ValueError("approval_policy must be a string")
    for key in ("startup_timeout_sec", "tool_timeout_sec", "max_stdio_processes"):
        value = raw.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
    policy = MCPPolicy(**raw)
    if policy.approval_policy not in {"untrusted", "on-failure", "on-request", "never"}:
        raise ValueError("unsupported approval_policy")
    if not 1 <= policy.startup_timeout_sec <= 300:
        raise ValueError("startup_timeout_sec must be between 1 and 300")
    if not policy.startup_timeout_sec <= policy.tool_timeout_sec <= 3600:
        raise ValueError("tool_timeout_sec must be at least startup timeout and at most 3600")
    if not 0 <= policy.max_stdio_processes <= 256:
        raise ValueError("max_stdio_processes must be 0 (unlimited) or between 1 and 256")
    return policy


logger = logging.getLogger(__name__)


def load_host_policy_safe(path: Path | None = None) -> MCPPolicy:
    """Like :func:`load_host_policy` but a bad overlay degrades to defaults with a warning.

    Used on the MCP server launch path, where a malformed or group-readable
    ``mcp-host.json`` must not take the server down.
    """
    try:
        return load_host_policy(path)
    except Exception as exc:  # noqa: BLE001 - launch path must never crash on policy
        logger.warning("ignoring host MCP overlay, using defaults: %s", exc)
        return MCPPolicy()


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _insert_top_level(text: str, key: str, value: str) -> str:
    """Insert ``key = value`` before the first table header (never edits an existing key)."""
    first_table = re.search(r"^\[", text, re.MULTILINE)
    at = first_table.start() if first_table else len(text)
    prefix, suffix = text[:at], text[at:]
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + f"{key} = {value}\n" + suffix


def _fill_existing_table(text: str, name: str, missing: dict[str, str]) -> str:
    """Add missing scalar keys directly under an existing ``[mcp_servers.<name>]`` header.

    Inserting right after the header keeps every subtable (``.env`` etc.) and every
    existing key exactly as the host wrote it.
    """
    header = re.compile(rf"^\[mcp_servers\.{re.escape(name)}\][ \t]*(#.*)?$", re.MULTILINE)
    match = header.search(text)
    if not match or not missing:
        return text
    lines = "".join(f"{k} = {v}\n" for k, v in missing.items())
    return text[: match.end()] + "\n" + lines.rstrip("\n") + text[match.end() :]


def render_codex_config(existing: str, policy: MCPPolicy, *, home: Path, agent: str) -> str:
    """Fill in missing managed Codex entries, never rewriting what the host already set.

    The existing document is parsed with ``tomllib``. Present keys and tables
    (``approval_policy``, a host's own ``sk-mcp-env`` command, ``.env`` subtables)
    are left byte-for-byte alone; only absent pieces are added. If the existing
    file does not parse, or the result would not parse, it is returned unchanged.
    """
    try:
        parsed = tomllib.loads(existing)
    except tomllib.TOMLDecodeError:
        return existing
    text = existing
    if "approval_policy" not in parsed:
        text = _insert_top_level(text, "approval_policy", _toml_string(policy.approval_policy))
    servers = parsed.get("mcp_servers", {})
    env = {"SKAGENT": agent, "SKCAPSTONE_AGENT": agent, "SKMEMORY_AGENT": agent}
    env_text = ", ".join(f"{_toml_string(k)} = {_toml_string(v)}" for k, v in env.items())
    for name in _MANAGED_SERVERS:
        timeouts = {
            "startup_timeout_sec": str(policy.startup_timeout_sec),
            "tool_timeout_sec": str(policy.tool_timeout_sec),
        }
        entry = servers.get(name) if isinstance(servers, dict) else None
        if entry is None:
            command = home / ".skenv" / "bin" / f"{name}-mcp"
            block = "\n".join(
                (
                    f"[mcp_servers.{name}]",
                    f"command = {_toml_string(str(command))}",
                    f"env = {{ {env_text} }}",
                    *(f"{k} = {v}" for k, v in timeouts.items()),
                )
            )
            sep = "" if not text or text.endswith("\n") else "\n"
            text = text + sep + ("\n" if text else "") + block + "\n"
        elif isinstance(entry, dict):
            text = _fill_existing_table(
                text, name, {k: v for k, v in timeouts.items() if k not in entry}
            )
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return existing
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
        current, policy or load_host_policy_safe(), home=Path.home(), agent=agent
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

    effective = policy or load_host_policy_safe()
    if effective.max_stdio_processes == 0:
        yield -1  # no ceiling configured
        return
    root = runtime_dir or Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/skcapstone-{os.getuid()}"))
    slots = root / "skcapstone-mcp-slots"
    try:
        slots.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("MCP slot dir unavailable, not enforcing ceiling: %s", exc)
        yield -1
        return
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
