"""Upgrade command — pip-install latest sovereign packages with version diff."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

from ._common import AGENT_HOME, console

# Sovereign packages to upgrade, in dependency order
SOVEREIGN_PACKAGES = [
    "skmemory",
    "capauth",
    "skcomm",
    "skchat",
    "skcapstone",
]


def _get_installed_version(package: str) -> Optional[str]:
    """Return the installed version string for a package, or None if not installed.

    Args:
        package: PyPI package name.

    Returns:
        Version string like '1.2.3', or None.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", package],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None


def _pip_upgrade(package: str) -> tuple[bool, str]:
    """Run pip install --upgrade for a package.

    Args:
        package: PyPI package name.

    Returns:
        Tuple of (success, output_or_error).
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", package],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return True, result.stdout
        return False, result.stderr or result.stdout
    except subprocess.TimeoutExpired:
        return False, "pip install timed out after 120s"
    except Exception as exc:
        return False, str(exc)


def _restart_daemon(home: Path) -> None:
    """Restart the skcapstone daemon.

    Runs 'skcapstone daemon restart' and reports result.

    Args:
        home: Agent home directory.
    """
    console.print()
    console.print("[bold]Restarting daemon...[/]")
    try:
        result = subprocess.run(
            ["skcapstone", "daemon", "restart", "--home", str(home)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            console.print("  Daemon: [green]restarted[/]")
        else:
            err = result.stderr.strip() or result.stdout.strip()
            console.print(f"  Daemon: [yellow]restart failed[/] — {err}")
    except FileNotFoundError:
        console.print("  Daemon: [yellow]skcapstone not in PATH[/]")
    except subprocess.TimeoutExpired:
        console.print("  Daemon: [yellow]restart timed out[/]")
    except Exception as exc:
        console.print(f"  Daemon: [yellow]restart error[/] — {exc}")


def register_upgrade_commands(main: click.Group) -> None:
    """Register the upgrade command on the main CLI group."""

    @main.command()
    @click.option(
        "--home",
        default=AGENT_HOME,
        help="Agent home directory.",
        type=click.Path(),
    )
    @click.option(
        "--restart",
        is_flag=True,
        default=False,
        help="Restart the daemon after a successful upgrade.",
    )
    @click.option(
        "--packages",
        default=None,
        help="Comma-separated list of packages to upgrade (default: all sovereign packages).",
    )
    def upgrade(home: str, restart: bool, packages: Optional[str]) -> None:
        """Upgrade sovereign packages to their latest PyPI versions.

        Upgrades skcapstone, skmemory, skcomm, capauth, and skchat.
        Displays a version diff (before → after) for each package.
        Use --restart to automatically restart the daemon afterward.
        """
        home_path = Path(home).expanduser()

        pkg_list = (
            [p.strip() for p in packages.split(",") if p.strip()]
            if packages
            else list(SOVEREIGN_PACKAGES)
        )

        console.print()
        console.print("[bold cyan]SKCapstone Upgrade[/]")
        console.print(f"  Packages: {', '.join(pkg_list)}")
        console.print()

        # ── Snapshot versions before upgrade ─────────────────────────────────
        before: dict[str, Optional[str]] = {
            pkg: _get_installed_version(pkg) for pkg in pkg_list
        }

        # ── Run upgrades ──────────────────────────────────────────────────────
        results: dict[str, tuple[bool, Optional[str], Optional[str]]] = {}
        any_failed = False

        for pkg in pkg_list:
            console.print(f"  Upgrading [cyan]{pkg}[/]...", end=" ")
            ok, _output = _pip_upgrade(pkg)
            after_ver = _get_installed_version(pkg)
            results[pkg] = (ok, before.get(pkg), after_ver)
            if ok:
                console.print("[green]ok[/]")
            else:
                console.print("[red]FAILED[/]")
                any_failed = True

        # ── Version diff table ────────────────────────────────────────────────
        from rich.table import Table

        console.print()
        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 2),
        )
        table.add_column("Package", style="cyan")
        table.add_column("Before", style="dim")
        table.add_column("Arrow", no_wrap=True)
        table.add_column("After")
        table.add_column("Status")

        for pkg in pkg_list:
            ok, v_before, v_after = results[pkg]
            before_str = v_before or "[dim]not installed[/]"
            after_str = v_after or "[dim]unknown[/]"

            if not ok:
                status_str = "[red]FAILED[/]"
                arrow = "[red]✗[/]"
            elif v_before == v_after:
                status_str = "[dim]up to date[/]"
                arrow = "[dim]=[/]"
            else:
                status_str = "[green]upgraded[/]"
                arrow = "[green]→[/]"

            table.add_row(pkg, before_str, arrow, after_str, status_str)

        console.print(table)

        # ── Restart daemon if requested ───────────────────────────────────────
        if restart:
            if any_failed:
                console.print()
                console.print(
                    "[yellow]Warning:[/] some packages failed — "
                    "skipping daemon restart."
                )
            else:
                _restart_daemon(home_path)

        # ── Summary ───────────────────────────────────────────────────────────
        console.print()
        upgraded = [
            pkg for pkg in pkg_list
            if results[pkg][0] and results[pkg][1] != results[pkg][2]
        ]
        up_to_date = [
            pkg for pkg in pkg_list
            if results[pkg][0] and results[pkg][1] == results[pkg][2]
        ]
        failed = [pkg for pkg in pkg_list if not results[pkg][0]]

        parts = []
        if upgraded:
            parts.append(f"[green]{len(upgraded)} upgraded[/]")
        if up_to_date:
            parts.append(f"[dim]{len(up_to_date)} up to date[/]")
        if failed:
            parts.append(f"[red]{len(failed)} failed[/]")

        console.print("  " + "  ".join(parts))

        if any_failed:
            raise SystemExit(1)
