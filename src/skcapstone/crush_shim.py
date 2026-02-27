"""Crush shim — daemon entry point that bridges the crush CLI interface to claude.

Registered as the ``crush`` console_scripts entry point so that
``LocalProvider._find_crush_binary()`` discovers it on PATH.  When invoked
as ``crush run --session <path> --config <path> --headless --state-file <path>``
it:

1. Parses the crush-compatible CLI arguments.
2. Reads ``session.json`` for agent identity (name, model, soul, skills).
3. Reads ``crush.json`` for permissions, context paths, and MCP config.
4. Writes ``{"status": "running", ...}`` to the state file.
5. Enters a daemon loop that:
   - Polls the team comms inbox for incoming messages.
   - For each task: dispatches to ``claude -p`` with the correct model and
     system prompt derived from the soul blueprint.
   - Writes results to the agent outbox.
   - Updates the heartbeat in the state file every iteration.
6. On SIGTERM/SIGINT: writes stopped state and exits cleanly.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 5
_CLAUDE_BINARY = "claude"


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the crush-compatible argument parser.

    Returns:
        Configured ArgumentParser supporting ``crush run`` sub-command.
    """
    parser = argparse.ArgumentParser(
        prog="crush",
        description="Crush shim — bridges crush CLI interface to claude backend",
    )
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Run an agent session")
    run_parser.add_argument(
        "--session",
        required=True,
        help="Path to session.json",
    )
    run_parser.add_argument(
        "--config",
        required=True,
        help="Path to crush.json",
    )
    run_parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="Run in headless daemon mode",
    )
    run_parser.add_argument(
        "--state-file",
        required=True,
        help="Path to session state file",
    )
    return parser


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse crush CLI arguments.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Parsed namespace.
    """
    parser = build_arg_parser()
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_session_config(path: str) -> Dict[str, Any]:
    """Load and return the session.json configuration.

    Args:
        path: Path to session.json.

    Returns:
        Parsed session configuration dict.

    Raises:
        SystemExit: If the file cannot be read or parsed.
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load session config from %s: %s", path, exc)
        sys.exit(1)


def load_crush_config(path: str) -> Dict[str, Any]:
    """Load and return the crush.json configuration.

    Args:
        path: Path to crush.json.

    Returns:
        Parsed crush configuration dict.

    Raises:
        SystemExit: If the file cannot be read or parsed.
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load crush config from %s: %s", path, exc)
        sys.exit(1)


# ---------------------------------------------------------------------------
# State file management
# ---------------------------------------------------------------------------


def write_state(state_file: str, state: Dict[str, Any]) -> None:
    """Write the session state to disk.

    Args:
        state_file: Absolute path to the state file.
        state: State dictionary to persist.
    """
    try:
        Path(state_file).write_text(
            json.dumps(state, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("Failed to write state file %s: %s", state_file, exc)


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Soul blueprint reader
# ---------------------------------------------------------------------------


def build_system_prompt(session_config: Dict[str, Any]) -> str:
    """Construct a system prompt from the soul blueprint and agent context.

    Args:
        session_config: Parsed session.json.

    Returns:
        System prompt string for ``claude -p --system-prompt``.
    """
    parts: List[str] = []
    soul_path = session_config.get("soul_blueprint")

    if soul_path:
        sp = Path(soul_path)
        if sp.is_file():
            try:
                parts.append(sp.read_text(encoding="utf-8"))
            except OSError:
                parts.append(f"Soul blueprint: {soul_path}")
        elif sp.is_dir():
            for ext in ("*.md", "*.txt", "*.yaml"):
                for f in sorted(sp.glob(ext)):
                    try:
                        parts.append(f.read_text(encoding="utf-8"))
                    except OSError:
                        pass
            if not parts:
                parts.append(f"Soul blueprint: {soul_path}")
        else:
            parts.append(f"Soul blueprint: {soul_path}")

    agent_name = session_config.get("agent_name", "agent")
    parts.append(
        f"\nAgent: {agent_name}\n"
        f"Role: {session_config.get('role', 'worker')}\n"
        f"Team: {session_config.get('team_name', '')}\n"
        f"Skills: {json.dumps(session_config.get('skills', []))}\n"
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Claude dispatch
# ---------------------------------------------------------------------------


def dispatch_to_claude(
    prompt: str,
    model: str,
    system_prompt: str,
    claude_binary: str = _CLAUDE_BINARY,
) -> Optional[str]:
    """Call ``claude -p`` with the given prompt and return the output.

    Args:
        prompt: The user prompt to send.
        model: Model name (e.g. ``claude-opus-4-6``).
        system_prompt: System prompt for context.
        claude_binary: Path to the claude CLI binary.

    Returns:
        Claude's response text, or None on failure.
    """
    cmd = [
        claude_binary,
        "-p",
        "--model", model,
        "--system-prompt", system_prompt,
        "--dangerously-skip-permissions",
        prompt,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        logger.warning(
            "Claude returned exit code %d: %s", result.returncode, result.stderr
        )
        return None
    except subprocess.TimeoutExpired:
        logger.warning("Claude call timed out for prompt: %s...", prompt[:80])
        return None
    except OSError as exc:
        logger.error("Failed to invoke claude: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Inbox / outbox
# ---------------------------------------------------------------------------


def _comms_root() -> Path:
    """Return the skcapstone comms root directory."""
    return Path("~/.skcapstone/comms").expanduser()


def poll_inbox(team_name: str, agent_name: str) -> List[Path]:
    """Check the agent's inbox for pending message files.

    Args:
        team_name: Team name for comms routing.
        agent_name: Agent instance name.

    Returns:
        List of message file paths found in the inbox (sorted by name).
    """
    inbox = _comms_root() / team_name / agent_name / "inbox"
    if not inbox.is_dir():
        return []
    return sorted(inbox.iterdir())


def write_outbox(team_name: str, agent_name: str, message: Dict[str, Any]) -> None:
    """Write a response message to the agent's outbox.

    Args:
        team_name: Team name for comms routing.
        agent_name: Agent instance name.
        message: Message dict to write as JSON.
    """
    outbox = _comms_root() / team_name / agent_name / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    filename = f"{_now_iso().replace(':', '-')}.json"
    (outbox / filename).write_text(
        json.dumps(message, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------


_running = True


def _handle_signal(signum: int, frame: Any) -> None:
    """Signal handler for graceful shutdown."""
    global _running
    _running = False


def daemon_loop(
    session_config: Dict[str, Any],
    crush_config: Dict[str, Any],
    state_file: str,
) -> None:
    """Main daemon loop: poll inbox, dispatch to claude, write results.

    Args:
        session_config: Parsed session.json.
        crush_config: Parsed crush.json.
        state_file: Path to the session state file.
    """
    global _running

    agent_name = session_config.get("agent_name", "agent")
    team_name = session_config.get("team_name", "default")
    model = session_config.get("model", "fast")
    system_prompt = build_system_prompt(session_config)

    iteration = 0
    while _running:
        iteration += 1

        # Poll inbox
        messages = poll_inbox(team_name, agent_name)
        for msg_path in messages:
            if not _running:
                break
            try:
                msg_data = json.loads(msg_path.read_text(encoding="utf-8"))
                prompt = msg_data.get("prompt") or msg_data.get("task") or str(msg_data)
            except (json.JSONDecodeError, OSError):
                prompt = None

            if prompt:
                response = dispatch_to_claude(prompt, model, system_prompt)
                if response:
                    write_outbox(team_name, agent_name, {
                        "source": str(msg_path.name),
                        "response": response,
                        "timestamp": _now_iso(),
                    })

            # Remove processed message
            try:
                msg_path.unlink()
            except OSError:
                pass

        # Update heartbeat
        write_state(state_file, {
            "status": "running",
            "pid": os.getpid(),
            "agent_name": agent_name,
            "heartbeat": _now_iso(),
            "iteration": iteration,
            "binary": "crush-shim",
        })

        # Sleep between polls
        for _ in range(int(_POLL_INTERVAL_SECONDS * 10)):
            if not _running:
                break
            time.sleep(0.1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> None:
    """Crush shim entry point.

    Parses arguments, loads configs, registers signal handlers, and enters
    the daemon loop.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    args = parse_args(argv)

    if args.command != "run":
        logger.error("Unknown command: %s (expected 'run')", args.command)
        sys.exit(1)

    session_config = load_session_config(args.session)
    crush_config = load_crush_config(args.config)

    agent_name = session_config.get("agent_name", "unknown")
    logger.info("Starting crush shim for agent %s", agent_name)

    # Write initial running state
    write_state(args.state_file, {
        "status": "running",
        "pid": os.getpid(),
        "agent_name": agent_name,
        "started_at": _now_iso(),
        "binary": "crush-shim",
    })

    # Register signal handlers
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        daemon_loop(session_config, crush_config, args.state_file)
    except Exception:
        logger.exception("Daemon loop crashed")
        write_state(args.state_file, {
            "status": "error",
            "pid": os.getpid(),
            "agent_name": agent_name,
            "error_at": _now_iso(),
            "binary": "crush-shim",
        })
        sys.exit(1)

    # Clean shutdown
    write_state(args.state_file, {
        "status": "stopped",
        "agent_name": agent_name,
        "stopped_at": _now_iso(),
        "binary": "crush-shim",
    })
    logger.info("Crush shim for %s stopped cleanly", agent_name)


if __name__ == "__main__":
    main()
