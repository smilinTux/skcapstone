"""Version command: detailed build and environment information."""

from __future__ import annotations

import importlib
import json
import platform
import sys
import urllib.request
from pathlib import Path
from typing import Optional

import click

from ._common import AGENT_HOME, console


def _check_optional_dep(name: str) -> Optional[str]:
    """Return installed version string or None if package is missing.

    Args:
        name: Python package name to probe.

    Returns:
        Version string (e.g. '1.2.3') or None if not importable.
    """
    try:
        mod = importlib.import_module(name)
        return getattr(mod, "__version__", "installed")
    except ImportError:
        return None


def _probe_ollama() -> dict:
    """Probe the local Ollama server.

    Hits /api/tags with a 2-second timeout and returns a dict with:
      - ``running``: bool — whether Ollama responded.
      - ``models``: list[str] — model names (empty on failure).
      - ``host``: str — the URL that was probed.

    Returns:
        Dict with keys ``running``, ``models``, ``host``.
    """
    import os
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    try:
        req = urllib.request.Request(f"{host}/api/tags")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
        models = [m.get("name", "") for m in data.get("models", [])]
        return {"running": True, "models": models, "host": host}
    except Exception:
        return {"running": False, "models": [], "host": host}


def _get_daemon_pid(home: Path) -> Optional[int]:
    """Return the daemon PID if it is currently running, else None.

    Args:
        home: Agent home directory.

    Returns:
        Integer PID or None.
    """
    try:
        from ..daemon import read_pid
        return read_pid(home)
    except Exception:
        return None


def gather_version_info(home: Path) -> dict:
    """Collect all version and environment data into a single dict.

    Args:
        home: Agent home directory.

    Returns:
        Dict with keys: package_version, python_version, platform,
        optional_deps, ollama, daemon_pid.
    """
    from .. import __version__

    optional_deps = {
        "watchdog": _check_optional_dep("watchdog"),
        "skcomm": _check_optional_dep("skcomm"),
        "skchat": _check_optional_dep("skchat"),
        "skseed": _check_optional_dep("skseed"),
    }

    return {
        "package_version": __version__,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "optional_deps": optional_deps,
        "ollama": _probe_ollama(),
        "daemon_pid": _get_daemon_pid(home),
    }


def register_version_commands(main: click.Group) -> None:
    """Register the ``version`` command on the main CLI group."""

    @main.command("version")
    @click.option("--home", default=AGENT_HOME, type=click.Path(),
                  help="Agent home directory.")
    @click.option("--json-out", is_flag=True, help="Output as machine-readable JSON.")
    def version_cmd(home: str, json_out: bool) -> None:
        """Show package version, runtime info, optional deps, Ollama status, and daemon PID."""
        home_path = Path(home).expanduser()
        info = gather_version_info(home_path)

        if json_out:
            click.echo(json.dumps(info, indent=2))
            return

        from .. import __version__

        console.print()
        console.print(f"  [bold cyan]skcapstone[/] [bold]{__version__}[/]")
        console.print(
            f"  [dim]Python {info['python_version']}  ·  "
            f"{info['platform']}[/]"
        )
        console.print()

        # ── Optional dependencies ──────────────────────────────────────────
        console.print("  [bold]Optional dependencies:[/]")
        for pkg, ver in info["optional_deps"].items():
            if ver is not None:
                console.print(f"    [green]✓[/] {pkg:<12} [dim]{ver}[/]")
            else:
                console.print(f"    [red]✗[/] {pkg:<12} [dim]not installed[/]")

        # ── Ollama status ──────────────────────────────────────────────────
        console.print()
        ollama = info["ollama"]
        if ollama["running"]:
            model_count = len(ollama["models"])
            model_str = (
                ", ".join(ollama["models"][:5])
                + (" …" if model_count > 5 else "")
            ) if ollama["models"] else "no models"
            console.print(
                f"  [bold]Ollama:[/] [green]running[/]  "
                f"[dim]{ollama['host']}  {model_count} model(s): {model_str}[/]"
            )
        else:
            console.print(
                f"  [bold]Ollama:[/] [red]not running[/]  "
                f"[dim]{ollama['host']}[/]"
            )

        # ── Daemon PID ─────────────────────────────────────────────────────
        pid = info["daemon_pid"]
        if pid is not None:
            console.print(f"  [bold]Daemon:[/] [green]running[/]  [dim]pid={pid}[/]")
        else:
            console.print("  [bold]Daemon:[/] [dim]not running[/]")

        console.print()
