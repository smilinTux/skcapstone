"""Soul layering commands: list, install, install-all, load, unload, swap, show, status, history, info."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from ._common import AGENT_HOME, console
from ._validators import validate_soul_name
from ..pillars.security import audit_event

from rich.panel import Panel
from rich.table import Table

# Path to the soul-blueprints repository (community blueprints)
_BLUEPRINTS_REPO = Path.home() / "clawd" / "soul-blueprints" / "blueprints"


def _find_blueprint_in_repo(slug: str) -> Path | None:
    """Search the soul-blueprints repo for a blueprint matching the slug.

    Searches all category subdirectories for files matching:
      <SLUG>.md, <SLUG>.yaml, <SLUG>.yml (case-insensitive stem match).

    Args:
        slug: Lowercased, hyphenated blueprint name to search for.

    Returns:
        Path to the blueprint file, or None if not found.
    """
    if not _BLUEPRINTS_REPO.is_dir():
        return None

    # Normalize: try both hyphenated and underscored variants
    variants = {slug, slug.replace("-", "_"), slug.upper().replace("-", "_")}
    extensions = (".md", ".yaml", ".yml")

    for category_dir in sorted(_BLUEPRINTS_REPO.iterdir()):
        if not category_dir.is_dir():
            continue
        for bp_file in sorted(category_dir.iterdir()):
            if bp_file.suffix.lower() not in extensions:
                continue
            stem = bp_file.stem
            if stem.lower().replace("_", "-") == slug or stem in variants:
                return bp_file

    return None


def register_soul_commands(main: click.Group) -> None:
    """Register the soul command group."""

    @main.group()
    def soul():
        """Soul layering — hot-swappable personality overlays.

        Install soul blueprints, load overlays at runtime,
        and manage personality while preserving identity.
        """

    @soul.command("list")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def soul_list(home):
        """List all installed souls."""
        from ..soul import SoulManager

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path)
        names = mgr.list_installed()

        if not names:
            console.print("\n  [dim]No souls installed yet.[/]")
            console.print("  [dim]Run: skcapstone soul install <path.md>[/]\n")
            return

        state = mgr.get_status()
        console.print(f"\n  [bold]{len(names)}[/] soul(s) installed:\n")
        for n in names:
            active = " [green]<- ACTIVE[/]" if n == state.active_soul else ""
            info = mgr.get_info(n)
            cat = f" [{info.category}]" if info else ""
            console.print(f"    [cyan]{n}[/]{cat}{active}")
        console.print()

    @soul.command("install")
    @click.argument("path", type=click.Path(exists=True))
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def soul_install(path, home):
        """Install a soul from a blueprint markdown file."""
        from ..soul import SoulManager

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path)
        bp = mgr.install(Path(path))
        console.print(f"\n  [green]Installed:[/] [bold]{bp.display_name}[/] ({bp.name})")
        console.print(f"  Category: {bp.category}")
        if bp.vibe:
            console.print(f"  Vibe: {bp.vibe[:80]}")
        console.print(f"  Traits: {len(bp.core_traits)}")
        audit_event(home_path, "SOUL_INSTALL", f"Soul '{bp.name}' installed")
        console.print()

    @soul.command("install-all")
    @click.argument("directory", type=click.Path(exists=True))
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def soul_install_all(directory, home):
        """Batch-install all blueprints from a directory."""
        from ..soul import SoulManager

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path)
        installed = mgr.install_all(Path(directory))
        console.print(f"\n  [green]Installed {len(installed)} soul(s)[/]")
        for bp in installed:
            console.print(f"    [cyan]{bp.name}[/] — {bp.display_name}")
        audit_event(home_path, "SOUL_INSTALL_ALL", f"{len(installed)} souls installed")
        console.print()

    @soul.command("load")
    @click.argument("name")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--reason", "-r", default="", help="Reason for loading this soul.")
    def soul_load(name, home, reason):
        """Activate a soul overlay."""
        from ..soul import SoulManager

        validate_soul_name(name)

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path)
        try:
            state = mgr.load(name, reason=reason)
            console.print(f"\n  [green]Loaded:[/] [bold]{name}[/]")
            console.print(f"  Base: {state.base_soul}")
            audit_event(home_path, "SOUL_LOAD", f"Soul '{name}' loaded", metadata={"reason": reason})
        except ValueError as e:
            console.print(f"\n  [red]Error:[/] {e}")
            sys.exit(1)
        console.print()

    @soul.command("unload")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--reason", "-r", default="", help="Reason for unloading.")
    def soul_unload(home, reason):
        """Return to base soul."""
        from ..soul import SoulManager

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path)
        state = mgr.unload(reason=reason)
        if state.active_soul is None:
            console.print("\n  [green]Returned to base soul.[/]")
            audit_event(home_path, "SOUL_UNLOAD", "Returned to base soul", metadata={"reason": reason})
        else:
            console.print("\n  [dim]Already at base soul.[/]")
        console.print()

    @soul.command("status")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def soul_status(home):
        """Show current soul state."""
        from ..soul import SoulManager

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path)
        state = mgr.get_status()
        installed = mgr.list_installed()

        active_display = state.active_soul or "[dim]base[/]"
        console.print()
        console.print(Panel(
            f"Base: [bold]{state.base_soul}[/]\n"
            f"Active: [bold cyan]{active_display}[/]\n"
            f"Installed: [bold]{len(installed)}[/] soul(s)\n"
            f"Activated at: {state.activated_at or '[dim]n/a[/]'}",
            title="Soul Layer", border_style="yellow",
        ))
        console.print()

    @soul.command("history")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--limit", "-n", default=20, help="Max entries to show.")
    def soul_history(home, limit):
        """Show soul swap history."""
        from ..soul import SoulManager

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path)
        events = mgr.get_history()

        if not events:
            console.print("\n  [dim]No soul swap history yet.[/]\n")
            return

        events = events[-limit:]
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Time", style="dim", no_wrap=True)
        table.add_column("From", style="yellow")
        table.add_column("To", style="cyan")
        table.add_column("Duration", style="dim")
        table.add_column("Reason", style="dim")

        for e in events:
            ts = e.timestamp[:19].replace("T", " ") if "T" in e.timestamp else e.timestamp
            from_s = e.from_soul or "base"
            to_s = e.to_soul or "base"
            dur = f"{e.duration_minutes:.1f}m" if e.duration_minutes else ""
            table.add_row(ts, from_s, to_s, dur, e.reason)

        console.print()
        console.print(table)
        console.print(f"\n  [dim]{len(events)} swap(s)[/]\n")

    @soul.command("info")
    @click.argument("name")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def soul_info(name, home):
        """Show detailed info about an installed soul."""
        from ..soul import SoulManager

        validate_soul_name(name)

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path)
        bp = mgr.get_info(name)

        if bp is None:
            console.print(f"\n  [red]Soul not found:[/] {name}\n")
            sys.exit(1)

        emoji = f" {bp.emoji}" if bp.emoji else ""
        console.print()
        console.print(Panel(
            f"[bold]{bp.display_name}[/]{emoji}\n"
            f"Category: [cyan]{bp.category}[/]\n"
            f"Vibe: {bp.vibe}\n"
            + (f"Philosophy: [italic]{bp.philosophy}[/]\n" if bp.philosophy else "")
            + f"\n[bold]Core Traits ({len(bp.core_traits)}):[/]\n"
            + "\n".join(f"  \u2022 {t}" for t in bp.core_traits[:10])
            + (f"\n\n[bold]Communication:[/]\n"
               + ("  Patterns: " + ", ".join(bp.communication_style.patterns[:3]) if bp.communication_style.patterns else "")
               + ("\n  Phrases: " + ", ".join(bp.communication_style.signature_phrases[:3]) if bp.communication_style.signature_phrases else ""))
            + ("\n\n[bold]Emotional Topology:[/]\n"
               + "\n".join(f"  {k}: {v:.2f}" for k, v in bp.emotional_topology.items()) if bp.emotional_topology else ""),
            title=f"Soul: {name}", border_style="yellow",
        ))
        console.print()

    # -----------------------------------------------------------------------
    # soul show — display current active soul or a specific skmemory blueprint
    # -----------------------------------------------------------------------

    @soul.command("show")
    @click.argument("name", required=False, default=None)
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def soul_show(name, home):
        """Display the current soul identity or a named blueprint.

        With no argument, shows the active soul from skmemory's base.json.
        With a NAME, shows details of an installed soul overlay.
        """
        home_path = Path(home).expanduser()

        if name:
            # Show a specific installed soul overlay
            from ..soul import SoulManager
            validate_soul_name(name)
            mgr = SoulManager(home_path)
            bp = mgr.get_info(name)
            if bp is None:
                console.print(f"\n  [red]Soul not found:[/] {name}\n")
                sys.exit(1)
            console.print()
            console.print(Panel(
                f"[bold]{bp.display_name}[/]\n"
                f"Category: {bp.category}\n"
                f"Vibe: {bp.vibe}\n"
                f"Traits: {', '.join(bp.core_traits[:8])}\n",
                title=f"Soul: {name}", border_style="cyan",
            ))
            console.print()
            return

        # Show the current skmemory soul identity (base.json)
        try:
            from skmemory.soul import load_soul
            soul_path = str(home_path / "soul" / "base.json")
            blueprint = load_soul(path=soul_path)
            if blueprint is None:
                console.print("\n  [dim]No soul blueprint found.[/]\n")
                return

            lines = []
            if blueprint.name:
                lines.append(f"Name: [bold]{blueprint.name}[/]")
            if blueprint.title:
                lines.append(f"Title: [cyan]{blueprint.title}[/]")
            if blueprint.personality:
                lines.append(f"Traits: {', '.join(blueprint.personality)}")
            if blueprint.values:
                lines.append(f"Values: {', '.join(blueprint.values)}")
            if blueprint.community:
                lines.append(f"Community: {blueprint.community}")
            if blueprint.boot_message:
                lines.append(f"\nBoot: [italic]{blueprint.boot_message}[/]")

            console.print()
            console.print(Panel(
                "\n".join(lines),
                title="Active Soul Identity",
                border_style="green",
            ))
            console.print()
        except ImportError:
            console.print("\n  [red]skmemory not installed.[/] Run: pip install skmemory\n")
            sys.exit(1)

    # -----------------------------------------------------------------------
    # soul swap — search, install-if-needed, and activate a soul overlay
    # -----------------------------------------------------------------------

    @soul.command("swap")
    @click.argument("blueprint")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--reason", "-r", default="", help="Reason for the swap.")
    def soul_swap(blueprint, home, reason):
        """Swap to a different soul blueprint.

        Searches for BLUEPRINT in this order:
          1) Already installed souls
          2) ~/clawd/soul-blueprints/blueprints/*/<BLUEPRINT>.{md,yaml,yml}
          3) Defaults

        If found in the blueprints repo but not installed, installs it first.
        Backs up current state and activates the new soul overlay.
        """
        from ..soul import SoulManager, parse_blueprint

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path)

        # Get current state for the "swapped from" message
        state = mgr.get_status()
        old_name = state.active_soul or "base"

        # 1) Check if already installed
        installed = mgr.list_installed()
        slug = blueprint.lower().replace(" ", "-")

        if slug not in installed:
            # 2) Search the blueprints repo
            found_path = _find_blueprint_in_repo(slug)
            if found_path is None:
                console.print(f"\n  [red]Blueprint not found:[/] {blueprint}")
                console.print("  Searched installed souls and ~/clawd/soul-blueprints/blueprints/")
                console.print("  Run [bold]skcapstone soul list[/] to see available souls.\n")
                sys.exit(1)

            # Install it
            try:
                bp = mgr.install(found_path)
                console.print(f"  [green]Auto-installed:[/] {bp.display_name} ({bp.name})")
                slug = bp.name  # Use the parsed name
            except (ValueError, FileNotFoundError) as e:
                console.print(f"\n  [red]Failed to install blueprint:[/] {e}\n")
                sys.exit(1)

        # 3) Load/activate the soul
        try:
            mgr.load(slug, reason=reason or f"swap from {old_name}")
            audit_event(home_path, "SOUL_SWAP", f"Soul swapped: {old_name} -> {slug}")
            console.print(f"\n  Soul swapped: [yellow]{old_name}[/] -> [bold cyan]{slug}[/]\n")
        except ValueError as e:
            console.print(f"\n  [red]Error:[/] {e}\n")
            sys.exit(1)
