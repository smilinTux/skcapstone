"""Upgrade / update command — smart sovereign package management.

Checks installed sovereign packages, upgrades all that are present,
and interactively prompts about optional pillar components that are
not yet installed.

Commands:
    skcapstone upgrade   — full upgrade of installed sovereign packages
    skcapstone update    — alias for upgrade (friendlier name)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

from ._common import AGENT_HOME, console

# ── Package definitions ───────────────────────────────────────────────────────

# Core packages: always upgraded when installed; always offered for install.
CORE_PACKAGES: list[str] = [
    "skmemory",    # persistent memory layer
    "capauth",     # sovereign identity + PGP auth
    "skcapstone",  # main agent framework
]

# Optional pillar packages: upgraded only when already installed.
# If NOT installed, the user is prompted whether they want to add them.
OPTIONAL_PACKAGES: list[str] = [
    "skcomm",   # P2P transport layer
    "skchat",   # agent messaging daemon
]

# All sovereign packages in dependency order
SOVEREIGN_PACKAGES: list[str] = CORE_PACKAGES + OPTIONAL_PACKAGES


# ── Version helpers ───────────────────────────────────────────────────────────


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


def _get_latest_version(package: str) -> Optional[str]:
    """Fetch the latest available version from PyPI.

    Args:
        package: PyPI package name.

    Returns:
        Latest version string, or None if unreachable.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "index", "versions", package],
            capture_output=True,
            text=True,
            timeout=15,
        )
        # Output: "AVAILABLE VERSIONS: 0.3.0, 0.2.1, ..."
        for line in result.stdout.splitlines():
            if "AVAILABLE VERSIONS" in line.upper() or package in line.lower():
                parts = line.split(":")
                if len(parts) > 1:
                    versions = [v.strip() for v in parts[-1].split(",")]
                    if versions:
                        return versions[0]
    except Exception:
        pass
    return None


def _pip_install(package: str, upgrade: bool = True, force_reinstall: bool = False) -> tuple[bool, str]:
    """Run pip install [--upgrade] [--force-reinstall] for a package.

    Args:
        package: PyPI package name.
        upgrade: If True, pass --upgrade flag.
        force_reinstall: If True, pass --force-reinstall (overwrites current install).

    Returns:
        Tuple of (success, output_or_error).
    """
    cmd = [sys.executable, "-m", "pip", "install"]
    if force_reinstall:
        cmd.append("--force-reinstall")
    elif upgrade:
        cmd.append("--upgrade")
    cmd.append(package)

    try:
        result = subprocess.run(
            cmd,
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
    """Restart the skcapstone daemon after an upgrade.

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


# ── Package descriptions ──────────────────────────────────────────────────────

_PKG_DESCRIPTIONS: dict[str, str] = {
    "skmemory": "persistent memory layer (required by skcapstone)",
    "capauth": "sovereign PGP identity + authentication",
    "skcapstone": "main sovereign agent framework",
    "skcomm": "P2P transport layer for agent messaging",
    "skchat": "agent messaging daemon + MCP server",
}


def _describe(pkg: str) -> str:
    return _PKG_DESCRIPTIONS.get(pkg, pkg)


# ── Core upgrade logic ────────────────────────────────────────────────────────


def _run_upgrade(
    pkg_list: list[str],
    home_path: Path,
    restart: bool,
    yes: bool,
    force_reinstall: bool = False,
) -> bool:
    """Perform the upgrade, print results, return True if all succeeded.

    Args:
        pkg_list: Packages to upgrade/install.
        home_path: Agent home directory.
        restart: Whether to restart daemon after success.
        yes: Whether to skip interactive prompts.
        force_reinstall: Whether to force-reinstall regardless of current version.

    Returns:
        True if all upgrades succeeded.
    """
    if not pkg_list:
        console.print("  [dim]No packages selected.[/]")
        return True

    if force_reinstall:
        console.print("  [yellow]Force-reinstall mode:[/] all packages will be overwritten.\n")

    # Snapshot versions before
    before: dict[str, Optional[str]] = {pkg: _get_installed_version(pkg) for pkg in pkg_list}

    # Run upgrades
    results: dict[str, tuple[bool, Optional[str], Optional[str]]] = {}
    any_failed = False

    for pkg in pkg_list:
        already_installed = before.get(pkg) is not None
        if force_reinstall:
            action = "Reinstalling"
        elif already_installed:
            action = "Upgrading"
        else:
            action = "Installing"
        console.print(f"  {action} [cyan]{pkg}[/]...", end=" ")
        ok, _output = _pip_install(pkg, upgrade=True, force_reinstall=force_reinstall)
        after_ver = _get_installed_version(pkg)
        results[pkg] = (ok, before.get(pkg), after_ver)
        if ok:
            console.print("[green]ok[/]")
        else:
            console.print("[red]FAILED[/]")
            any_failed = True

    # Version diff table
    from rich.table import Table

    console.print()
    table = Table(
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 2),
    )
    table.add_column("Package", style="cyan")
    table.add_column("Description", style="dim")
    table.add_column("Before", style="dim")
    table.add_column("")
    table.add_column("After")
    table.add_column("Status")

    for pkg in pkg_list:
        ok, v_before, v_after = results[pkg]
        before_str = v_before or "[dim]—[/]"
        after_str = v_after or "[dim]unknown[/]"

        if not ok:
            status_str = "[red]FAILED[/]"
            arrow = "[red]✗[/]"
        elif v_before is None:
            status_str = "[green]installed[/]"
            arrow = "[green]NEW[/]"
        elif v_before == v_after:
            status_str = "[dim]up to date[/]"
            arrow = "[dim]=[/]"
        else:
            status_str = "[green]upgraded[/]"
            arrow = "[green]→[/]"

        table.add_row(pkg, _describe(pkg), before_str, arrow, after_str, status_str)

    console.print(table)

    # Restart if requested and all succeeded
    if restart:
        if any_failed:
            console.print()
            console.print("[yellow]Warning:[/] some packages failed — skipping daemon restart.")
        else:
            _restart_daemon(home_path)

    # Summary
    console.print()
    upgraded = [p for p in pkg_list if results[p][0] and results[p][1] != results[p][2]]
    up_to_date = [p for p in pkg_list if results[p][0] and results[p][1] == results[p][2]]
    failed = [p for p in pkg_list if not results[p][0]]

    parts: list[str] = []
    if upgraded:
        parts.append(f"[green]{len(upgraded)} upgraded[/]")
    if up_to_date:
        parts.append(f"[dim]{len(up_to_date)} up to date[/]")
    if failed:
        parts.append(f"[red]{len(failed)} failed[/]")

    console.print("  " + "  ".join(parts))

    # Auto-register skills and MCP servers after successful upgrade
    if not any_failed:
        _run_auto_register()

    return not any_failed


def _run_auto_register() -> None:
    """Run auto-registration of SK* skills and MCP servers.

    This is called as a final step after upgrading packages, ensuring
    all SKILL.md symlinks and MCP server entries are in place.
    """
    console.print()
    console.print("[bold]Registering skills & MCP servers...[/]")
    try:
        from skcapstone.register import register_all

        results = register_all()
        registered = 0
        for _name, pkg_result in results.get("packages", {}).items():
            skill = pkg_result.get("skill", {})
            if skill.get("action") == "created":
                registered += 1
            mcp = pkg_result.get("mcp", {})
            for _env, action in (mcp if isinstance(mcp, dict) else {}).items():
                if action == "created":
                    registered += 1

        if registered > 0:
            console.print(f"  [green]{registered} registration(s) applied[/]")
        else:
            console.print("  [dim]All registrations up to date[/]")
    except Exception as exc:
        console.print(f"  [yellow]Registration skipped[/] — {exc}")


# ── Click commands ────────────────────────────────────────────────────────────


def register_upgrade_commands(main: click.Group) -> None:
    """Register upgrade and update commands on the main CLI group."""

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
        help=(
            "Comma-separated list of packages to upgrade. "
            "Default: all installed sovereign packages."
        ),
    )
    @click.option(
        "--yes", "-y",
        is_flag=True,
        default=False,
        help="Skip interactive prompts — do not install optional packages that are missing.",
    )
    @click.option(
        "--all", "install_all",
        is_flag=True,
        default=False,
        help="Install all optional packages without prompting.",
    )
    @click.option(
        "--force-reinstall",
        is_flag=True,
        default=False,
        help=(
            "Force-reinstall ALL components, overwriting whatever is currently installed. "
            "Use this to fix broken installs or downgrade to a clean state."
        ),
    )
    def upgrade(
        home: str,
        restart: bool,
        packages: Optional[str],
        yes: bool,
        install_all: bool,
        force_reinstall: bool,
    ) -> None:
        """Upgrade sovereign packages to their latest versions.

        Upgrades all currently installed sovereign packages (skcapstone,
        skmemory, capauth) and any optional pillars already present.

        For optional packages that are NOT installed (skcomm, skchat),
        you will be prompted whether you want to add them — unless
        --yes (skip) or --all (install all) is passed.

        Use --force-reinstall to overwrite everything regardless of current
        version — useful for fixing broken installs.

        Examples:

          skcapstone upgrade                       # smart upgrade + prompts
          skcapstone upgrade --yes                 # upgrade only what's installed
          skcapstone upgrade --all                 # upgrade + install all pillars
          skcapstone upgrade --force-reinstall     # overwrite ALL components
          skcapstone upgrade --force-reinstall --all  # reinstall everything
          skcapstone upgrade --packages skcomm,skchat
          skcapstone upgrade --restart             # restart daemon after upgrade
        """
        home_path = Path(home).expanduser()

        console.print()
        console.print("[bold cyan]SKCapstone Upgrade[/]")
        console.print()

        # ── Force reinstall: use all sovereign packages ────────────────────────
        if force_reinstall:
            pkg_list = (
                [p.strip() for p in packages.split(",") if p.strip()]
                if packages
                else (SOVEREIGN_PACKAGES if install_all else CORE_PACKAGES + [
                    pkg for pkg in OPTIONAL_PACKAGES
                    if install_all or _get_installed_version(pkg) is not None
                ])
            )
            console.print(
                f"  [yellow bold]Force reinstall[/]: {', '.join(pkg_list)}"
            )
            console.print()
            ok = _run_upgrade(pkg_list, home_path, restart, yes, force_reinstall=True)
            if not ok:
                raise SystemExit(1)
            return

        # ── Explicit package list ──────────────────────────────────────────────
        if packages:
            pkg_list = [p.strip() for p in packages.split(",") if p.strip()]
            console.print(f"  Packages: {', '.join(pkg_list)}")
            console.print()
            ok = _run_upgrade(pkg_list, home_path, restart, yes)
            if not ok:
                raise SystemExit(1)
            return

        # ── Smart mode: scan installed packages ───────────────────────────────
        installed: list[str] = []
        not_installed: list[str] = []

        for pkg in SOVEREIGN_PACKAGES:
            ver = _get_installed_version(pkg)
            if ver:
                installed.append(pkg)
            else:
                not_installed.append(pkg)

        console.print("  [bold]Installed pillar packages:[/]")
        for pkg in installed:
            ver = _get_installed_version(pkg)
            console.print(f"    [green]●[/] [cyan]{pkg}[/] {ver}  [dim]{_describe(pkg)}[/]")

        if not_installed:
            console.print()
            console.print("  [bold]Not installed:[/]")
            for pkg in not_installed:
                console.print(f"    [dim]○[/] [dim]{pkg}[/]  [dim]{_describe(pkg)}[/]")

        console.print()

        # ── Prompt for optional packages ──────────────────────────────────────
        to_install_extra: list[str] = []

        if install_all:
            to_install_extra = not_installed
            if to_install_extra:
                console.print(
                    f"  [cyan]--all[/]: will also install: {', '.join(to_install_extra)}"
                )
                console.print()
        elif not yes and not_installed:
            console.print("  [bold]Optional components available:[/]")
            for pkg in not_installed:
                answer = click.confirm(
                    f"    Install [cyan]{pkg}[/] ({_describe(pkg)})?",
                    default=False,
                )
                if answer:
                    to_install_extra.append(pkg)
            if to_install_extra:
                console.print()

        # Combine: upgrade installed + install newly selected extras
        final_list = installed + to_install_extra

        if not final_list:
            console.print("  [dim]Nothing to do.[/]")
            return

        console.print(f"  Upgrading: [cyan]{', '.join(final_list)}[/]")
        console.print()

        ok = _run_upgrade(final_list, home_path, restart, yes)
        if not ok:
            raise SystemExit(1)

    # ── `update` alias ────────────────────────────────────────────────────────

    @main.command(
        name="update",
        context_settings={"ignore_unknown_options": False},
    )
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--restart", is_flag=True, default=False)
    @click.option("--packages", default=None)
    @click.option("--yes", "-y", is_flag=True, default=False)
    @click.option("--all", "install_all", is_flag=True, default=False)
    @click.option(
        "--force-reinstall",
        is_flag=True,
        default=False,
        help="Force-reinstall ALL components, overwriting current installs.",
    )
    @click.pass_context
    def update(
        ctx: click.Context,
        home: str,
        restart: bool,
        packages: Optional[str],
        yes: bool,
        install_all: bool,
        force_reinstall: bool,
    ) -> None:
        """Alias for 'upgrade' — update all sovereign packages.

        Checks installed pillar programs and upgrades them to the latest
        available versions. Prompts about optional components (skcomm,
        skchat) that are not yet installed.

        Examples:

          skcapstone update                       # smart update + prompts
          skcapstone update --yes                 # update only what's installed
          skcapstone update --all                 # update + install all pillars
          skcapstone update --force-reinstall     # overwrite ALL components
          skcapstone update --restart             # restart daemon after update
        """
        ctx.invoke(
            upgrade,
            home=home,
            restart=restart,
            packages=packages,
            yes=yes,
            install_all=install_all,
            force_reinstall=force_reinstall,
        )
