"""Ansible playbook runner tool — run_ansible_playbook.

Streams stdout/stderr lines to the activity feed SSE queue as
``ansible.playbook.line`` / ``ansible.playbook.stderr`` events.
Stores the exit code and play-recap summary in agent memory with
``tag=ansible-run``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response

TOOLS: list[Tool] = [
    Tool(
        name="run_ansible_playbook",
        description=(
            "Run an Ansible playbook via ansible-playbook subprocess. "
            "Streams stdout lines to the activity feed SSE queue as "
            "ansible.playbook.line events (stderr lines as "
            "ansible.playbook.stderr). Stores exit code and play-recap "
            "summary in agent memory with tag=ansible-run. "
            "dry_run=true adds --check (no changes applied). "
            "Requires ansible-playbook binary in PATH."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "playbook_path": {
                    "type": "string",
                    "description": (
                        "Absolute or relative path to the Ansible playbook YAML file"
                    ),
                },
                "inventory": {
                    "type": "string",
                    "description": (
                        "Inventory file path, directory, or comma-separated host pattern"
                    ),
                },
                "extra_vars": {
                    "type": "object",
                    "description": (
                        "Extra variables passed to ansible-playbook via --extra-vars "
                        "(serialised as a JSON string)"
                    ),
                    "additionalProperties": True,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "If true, pass --check so ansible-playbook simulates changes "
                        "without applying them (default: false)"
                    ),
                },
            },
            "required": ["playbook_path", "inventory"],
        },
    ),
]


# ── handler ───────────────────────────────────────────────────────────────────


async def _handle_run_ansible_playbook(args: dict) -> list[TextContent]:
    """Run an Ansible playbook and stream output to the activity bus."""
    playbook_path = args.get("playbook_path", "").strip()
    inventory = args.get("inventory", "").strip()
    extra_vars: dict = args.get("extra_vars") or {}
    dry_run: bool = bool(args.get("dry_run", False))

    # --- input validation ---
    if not playbook_path:
        return _error_response("playbook_path is required")
    if not inventory:
        return _error_response("inventory is required")

    # Require ansible-playbook binary in PATH
    if not shutil.which("ansible-playbook"):
        return _error_response(
            "ansible-playbook binary not found in PATH; "
            "install Ansible first (e.g. pip install ansible or dnf install ansible)"
        )

    playbook = Path(playbook_path).expanduser().resolve()
    if not playbook.exists():
        return _error_response(f"playbook not found: {playbook_path!r}")

    # --- build command ---
    cmd: list[str] = [
        "ansible-playbook",
        str(playbook),
        "-i", inventory,
    ]
    if dry_run:
        cmd.append("--check")
    if extra_vars:
        cmd.extend(["--extra-vars", json.dumps(extra_vars)])

    # --- emit start event ---
    run_id = uuid.uuid4().hex[:8]

    try:
        from .. import activity as _activity

        _activity.push("ansible.playbook.start", {
            "run_id": run_id,
            "playbook": str(playbook),
            "inventory": inventory,
            "dry_run": dry_run,
            "cmd": cmd,
        })
    except Exception:
        _activity = None  # type: ignore[assignment]

    # --- stream subprocess output ---
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def _drain(
            stream: asyncio.StreamReader,
            store: list[str],
            event_type: str,
        ) -> None:
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = raw.decode(errors="replace").rstrip("\n")
                store.append(line)
                try:
                    if _activity is not None:
                        _activity.push(event_type, {"run_id": run_id, "line": line})
                except Exception:
                    pass

        await asyncio.gather(
            _drain(proc.stdout, stdout_lines, "ansible.playbook.line"),
            _drain(proc.stderr, stderr_lines, "ansible.playbook.stderr"),
        )
        await proc.wait()
        exit_code: int = proc.returncode  # type: ignore[assignment]

    except Exception as exc:
        return _error_response(f"Failed to launch ansible-playbook: {exc}")

    # --- build summary ---
    success = exit_code == 0

    # Extract PLAY RECAP block lines (host rows contain "ok=" or "failed=")
    recap_lines = [
        line for line in stdout_lines
        if "PLAY RECAP" in line or ("ok=" in line and "changed=" in line)
    ]

    summary = {
        "run_id": run_id,
        "playbook": str(playbook),
        "inventory": inventory,
        "dry_run": dry_run,
        "exit_code": exit_code,
        "success": success,
        "stdout_lines": len(stdout_lines),
        "stderr_lines": len(stderr_lines),
        "recap": recap_lines,
    }

    # --- emit completion event ---
    try:
        if _activity is not None:
            _activity.push("ansible.playbook.done", summary)
    except Exception:
        pass

    # --- store in memory with tag=ansible-run ---
    try:
        from ..memory_engine import store as _mem_store

        recap_str = " | ".join(recap_lines) if recap_lines else "none"
        _mem_store(
            home=_home(),
            content=(
                f"Ansible run {'(dry-run) ' if dry_run else ''}— "
                f"playbook: {str(playbook)!r}, inventory: {inventory!r}, "
                f"exit_code: {exit_code}, success: {success}. "
                f"Recap: {recap_str}"
            ),
            tags=["ansible-run"],
            source="mcp:run_ansible_playbook",
            importance=0.6 if success else 0.8,
            metadata={
                "run_id": run_id,
                "playbook": str(playbook),
                "inventory": inventory,
                "dry_run": dry_run,
                "exit_code": exit_code,
                "recap": recap_lines,
            },
        )
    except Exception:
        pass  # memory failure must not block the tool response

    if not success:
        # Surface last 20 stderr lines for quick diagnosis
        stderr_tail = stderr_lines[-20:] if len(stderr_lines) > 20 else stderr_lines
        return _json_response({
            **summary,
            "stderr_tail": stderr_tail,
            "error": f"ansible-playbook exited with code {exit_code}",
        })

    return _json_response(summary)


HANDLERS: dict = {
    "run_ansible_playbook": _handle_run_ansible_playbook,
}
