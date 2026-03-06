"""
Cross-platform MCP server launcher for SKCapstone.
Task: e5f81637

Detects the correct Python environment, sets required environment variables,
and launches the MCP server via stdio transport. Works on Linux, macOS, and
Windows without requiring shell-specific launcher scripts.

Usage:
    python -m skcapstone.mcp_launcher          # auto-detect everything
    python -m skcapstone.mcp_launcher --venv /path/to/venv
    python -m skcapstone.mcp_launcher --log-level DEBUG

Can also be imported and called programmatically:
    from skcapstone.mcp_launcher import launch
    launch()
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Python / venv detection
# ---------------------------------------------------------------------------

def _skenv_dir() -> Path:
    """Return the platform-appropriate skenv directory."""
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app) / "skenv"
        return Path.home() / ".skenv"
    return Path.home() / ".skenv"


def _python_in_venv(venv: Path) -> Path | None:
    """Return the python executable inside a venv, or None."""
    if sys.platform == "win32":
        candidate = venv / "Scripts" / "python.exe"
    else:
        candidate = venv / "bin" / "python"
    return candidate if candidate.is_file() else None


def _has_skcapstone(python: Path | str) -> bool:
    """Check if a Python interpreter has skcapstone importable."""
    try:
        result = subprocess.run(
            [str(python), "-c", "import skcapstone"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def find_python(explicit_venv: str | None = None) -> str:
    """Locate the best Python interpreter with skcapstone installed.

    Search order:
        1. Explicit venv path (parameter or SKCAPSTONE_VENV env var)
        2. ~/.skenv (or %LOCALAPPDATA%/skenv on Windows)
        3. Project-local .venv
        4. The currently running interpreter (sys.executable)
        5. System python3 / python

    Args:
        explicit_venv: Optional path to a virtualenv to use.

    Returns:
        Absolute path to a Python executable.

    Raises:
        RuntimeError: If no suitable Python is found.
    """
    # 1. Explicit override
    venv_path = explicit_venv or os.environ.get("SKCAPSTONE_VENV")
    if venv_path:
        py = _python_in_venv(Path(venv_path))
        if py and py.is_file():
            logger.info("Using explicit venv: %s", py)
            return str(py)
        logger.warning(
            "SKCAPSTONE_VENV=%s set but python not found there, falling back.",
            venv_path,
        )

    # 2. Standard skenv
    skenv = _skenv_dir()
    py = _python_in_venv(skenv)
    if py and _has_skcapstone(py):
        logger.info("Using skenv: %s", py)
        return str(py)

    # 3. Project-local .venv
    project_dir = Path(__file__).resolve().parent.parent.parent  # src/../..
    for venv_name in (".venv", "venv"):
        local_venv = project_dir / venv_name
        py = _python_in_venv(local_venv)
        if py and _has_skcapstone(py):
            logger.info("Using project venv: %s", py)
            return str(py)

    # 4. Current interpreter
    if _has_skcapstone(sys.executable):
        logger.info("Using current interpreter: %s", sys.executable)
        return sys.executable

    # 5. System python
    for cmd in ("python3", "python"):
        path = shutil.which(cmd)
        if path and _has_skcapstone(path):
            logger.info("Using system python: %s", path)
            return path

    raise RuntimeError(
        "Could not find a Python interpreter with skcapstone installed.\n"
        "Install with: bash scripts/install.sh (Linux/macOS) or "
        ".\\scripts\\install.ps1 (Windows)\n"
        "Or set SKCAPSTONE_VENV=/path/to/venv"
    )


# ---------------------------------------------------------------------------
# Environment setup
# ---------------------------------------------------------------------------

def _setup_environment() -> None:
    """Set required environment variables if not already present."""
    home = Path.home()
    skcapstone_home = home / ".skcapstone"

    os.environ.setdefault("SKCAPSTONE_HOME", str(skcapstone_home))
    os.environ.setdefault("SKMEMORY_HOME", str(skcapstone_home / "memory"))

    # Ensure src/ is on PYTHONPATH for importability
    src_dir = str(Path(__file__).resolve().parent.parent)
    python_path = os.environ.get("PYTHONPATH", "")
    if src_dir not in python_path.split(os.pathsep):
        os.environ["PYTHONPATH"] = (
            f"{src_dir}{os.pathsep}{python_path}" if python_path else src_dir
        )


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

def launch(
    venv: str | None = None,
    log_level: str = "WARNING",
    extra_args: list[str] | None = None,
) -> int:
    """Launch the MCP server, optionally in a subprocess.

    If the current interpreter already has skcapstone and is the best match,
    the server is launched in-process. Otherwise, a subprocess is spawned
    with the detected Python interpreter.

    Args:
        venv: Optional explicit venv path.
        log_level: Logging level for the MCP server.
        extra_args: Additional arguments to pass through.

    Returns:
        Exit code (0 on success).
    """
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.WARNING),
        format="%(name)s: %(message)s",
    )

    _setup_environment()

    python = find_python(explicit_venv=venv)

    # If the best Python IS us, run in-process
    if os.path.realpath(python) == os.path.realpath(sys.executable):
        logger.info("Launching MCP server in-process.")
        from skcapstone.mcp_server import main as mcp_main
        mcp_main()
        return 0

    # Otherwise, exec into the correct interpreter
    logger.info("Launching MCP server via: %s", python)
    cmd = [python, "-m", "skcapstone.mcp_server"]
    if extra_args:
        cmd.extend(extra_args)

    if sys.platform != "win32":
        # On Unix, replace the current process
        os.execv(python, cmd)
        # execv does not return
        return 1  # unreachable, but satisfies type checker
    else:
        # On Windows, os.execv has quirks; use subprocess
        result = subprocess.run(cmd)
        return result.returncode


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point with argument parsing."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Cross-platform SKCapstone MCP server launcher.",
        prog="skcapstone-mcp-launcher",
    )
    parser.add_argument(
        "--venv",
        help="Path to a virtualenv to use (overrides auto-detection).",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("SKCAPSTONE_LOG_LEVEL", "WARNING"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: WARNING).",
    )
    args, extra = parser.parse_known_args()

    rc = launch(venv=args.venv, log_level=args.log_level, extra_args=extra)
    sys.exit(rc)


if __name__ == "__main__":
    main()
