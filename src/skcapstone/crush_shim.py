"""Crush shim - daemon entry point that bridges the crush CLI interface to Codex.

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
   - For each task: dispatches to ``codex exec`` with the worker context and
     prompt derived from the soul blueprint.
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
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 5
_CODEX_BINARY = "codex"
_CODEX_DEFAULT_MODEL = "gpt-5.6-luna"
_CODEX_TIER_MODELS = {
    "s": "gpt-5.4-mini",
    "small": "gpt-5.4-mini",
    "fast": "gpt-5.4-mini",
    "codex-fast": "gpt-5.4-mini",
    "m": "gpt-5.6-luna",
    "medium": "gpt-5.6-luna",
    "mid": "gpt-5.6-luna",
    "balanced": "gpt-5.6-luna",
    "code": "gpt-5.6-luna",
    "coding": "gpt-5.6-luna",
    "codex-mid": "gpt-5.6-luna",
    "l": "gpt-5.6-sol",
    "large": "gpt-5.6-sol",
    "frontier": "gpt-5.6-sol",
    "codex": "gpt-5.6-sol",
    "codex-frontier": "gpt-5.6-sol",
}
_CODEX_CAPACITY_FALLBACKS = {
    "gpt-5.6-sol": "gpt-5.6-luna",
    "gpt-5.6-luna": "gpt-5.4-mini",
}


def _resolve_codex_model(model: str) -> str:
    """Resolve a logical S/M/L tier to the configured Codex model bucket."""
    requested = (model or "").strip().lower()
    if requested.startswith(("gpt-", "o1-", "o3-", "o4-")):
        return model
    return _CODEX_TIER_MODELS.get(requested, _CODEX_DEFAULT_MODEL)


def _is_capacity_failure(stderr: str) -> bool:
    """Allow one bounded model fallback for provider capacity failures only."""
    message = str(stderr or "").lower()
    return any(
        marker in message
        for marker in ("at capacity", "capacity unavailable", "overloaded")
    )


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
        description="Crush shim - bridges crush CLI interface to Codex backend",
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
        Path(state_file).write_text(json.dumps(state, indent=2), encoding="utf-8")
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
        Worker context string prepended to the Codex task prompt.
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
# Codex dispatch
# ---------------------------------------------------------------------------


def dispatch_to_codex(
    prompt: str,
    model: str,
    system_prompt: str,
    codex_binary: str = _CODEX_BINARY,
) -> Optional[str]:
    """Call ``codex exec`` with the given prompt and return the final message.

    Args:
        prompt: The user prompt to send.
        model: Logical worker model. Native Codex model names are passed
            through; SK model tiers use the configured Codex default.
        system_prompt: Worker context to prepend to the task.
        codex_binary: Path to the Codex CLI binary.

    Returns:
        Codex's final response text, or None on failure.
    """
    combined_prompt = f"WORKER CONTEXT:\n{system_prompt}\n\nTASK:\n{prompt}"
    output_path = None
    try:
        output_handle = tempfile.NamedTemporaryFile(
            prefix="skcapstone-codex-",
            suffix=".response",
            delete=False,
        )
        output_path = output_handle.name
        output_handle.close()
    except OSError as exc:
        logger.error("Failed to create Codex response file: %s", exc)
        return None

    native_model = _resolve_codex_model(model)
    try:
        attempted_models = [native_model]
        fallback = _CODEX_CAPACITY_FALLBACKS.get(native_model)
        if fallback:
            attempted_models.append(fallback)
        for attempt, attempted_model in enumerate(attempted_models):
            cmd = [
                codex_binary,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "--color",
                "never",
                "--output-last-message",
                output_path,
                "--model",
                attempted_model,
                combined_prompt,
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode == 0:
                response = (
                    Path(output_path).read_text(encoding="utf-8").strip()
                    if Path(output_path).exists()
                    else result.stdout.strip()
                )
                return response or result.stdout.strip()
            if attempt == 0 and fallback and _is_capacity_failure(result.stderr or ""):
                logger.warning(
                    "Codex model %s is at capacity; retrying bounded fallback %s",
                    attempted_model,
                    fallback,
                )
                continue
            logger.warning("Codex returned exit code %d: %s", result.returncode, result.stderr)
            return None
        return None
    except subprocess.TimeoutExpired:
        logger.warning("Codex call timed out for prompt: %s...", prompt[:80])
        return None
    except OSError as exc:
        logger.error("Failed to invoke Codex: %s", exc)
        return None
    finally:
        if output_path:
            try:
                Path(output_path).unlink()
            except OSError:
                pass


# Keep the old import name for callers outside the daemon. The daemon itself
# calls the Codex-named helper so Claude cannot be selected accidentally.
dispatch_to_claude = dispatch_to_codex


# ---------------------------------------------------------------------------
# Inbox / outbox
# ---------------------------------------------------------------------------


def _comms_root() -> Path:
    """Return the skcapstone comms root directory."""
    from . import SHARED_ROOT

    return Path(SHARED_ROOT).expanduser() / "comms"


def poll_inbox(team_name: str, agent_name: str) -> List[Path]:
    """Check the agent's inbox for pending message files.

    Args:
        team_name: Team name for comms routing.
        agent_name: Agent instance name.

    Returns:
        List of message file paths found in the inbox (sorted by name).
    """
    inbox = _agent_channel_root(team_name, agent_name) / "inbox"
    if not inbox.is_dir():
        return []
    return sorted(inbox.iterdir())


def _agent_channel_root(team_name: str, agent_name: str) -> Path:
    """Resolve the live channel for an agent.

    TeamEngine deployments use the deployment ID as the channel directory,
    while older callers use the human-readable team name. Support both so a
    worker cannot silently miss its mailbox.
    """
    root = _comms_root()
    named = root / team_name / agent_name
    if named.exists():
        return named
    matches = sorted(root.glob(f"*/{agent_name}"))
    return matches[0] if matches else named


def write_outbox(team_name: str, agent_name: str, message: Dict[str, Any]) -> None:
    """Write a response message to the agent's outbox.

    Args:
        team_name: Team name for comms routing.
        agent_name: Agent instance name.
        message: Message dict to write as JSON.
    """
    outbox = _agent_channel_root(team_name, agent_name) / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    filename = f"{_now_iso().replace(':', '-')}.json"
    (outbox / filename).write_text(json.dumps(message, indent=2), encoding="utf-8")


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
    """Main daemon loop: poll inbox, dispatch to Codex, write results.

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
                payload = msg_data.get("payload")
                prompt = (
                    msg_data.get("prompt")
                    or msg_data.get("task")
                    or (payload.get("content") if isinstance(payload, dict) else None)
                    or str(msg_data)
                )
            except (json.JSONDecodeError, OSError):
                prompt = None

            if prompt:
                response = dispatch_to_codex(prompt, model, system_prompt)
                if response:
                    write_outbox(
                        team_name,
                        agent_name,
                        {
                            "source": str(msg_path.name),
                            "response": response,
                            "timestamp": _now_iso(),
                        },
                    )

            # Remove processed message
            try:
                msg_path.unlink()
            except OSError:
                pass

        # Update heartbeat
        write_state(
            state_file,
            {
                "status": "running",
                "pid": os.getpid(),
                "agent_name": agent_name,
                "heartbeat": _now_iso(),
                "iteration": iteration,
                "binary": "crush-shim",
            },
        )

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
    write_state(
        args.state_file,
        {
            "status": "running",
            "pid": os.getpid(),
            "agent_name": agent_name,
            "started_at": _now_iso(),
            "binary": "crush-shim",
        },
    )

    # Register signal handlers
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        daemon_loop(session_config, crush_config, args.state_file)
    except Exception:
        logger.exception("Daemon loop crashed")
        write_state(
            args.state_file,
            {
                "status": "error",
                "pid": os.getpid(),
                "agent_name": agent_name,
                "error_at": _now_iso(),
                "binary": "crush-shim",
            },
        )
        sys.exit(1)

    # Clean shutdown
    write_state(
        args.state_file,
        {
            "status": "stopped",
            "agent_name": agent_name,
            "stopped_at": _now_iso(),
            "binary": "crush-shim",
        },
    )
    logger.info("Crush shim for %s stopped cleanly", agent_name)


if __name__ == "__main__":
    main()
