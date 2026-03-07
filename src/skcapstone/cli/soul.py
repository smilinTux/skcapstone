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
from .. import SKCAPSTONE_AGENT

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

    def _agent_option():
        """Reusable --agent/-a option for soul subcommands."""
        return click.option(
            "--agent", "-a",
            default=SKCAPSTONE_AGENT or "lumina",
            envvar="SKCAPSTONE_AGENT",
            help="Agent profile name (default: SKCAPSTONE_AGENT or 'lumina').",
        )

    @main.group()
    @_agent_option()
    @click.pass_context
    def soul(ctx, agent):
        """Soul layering — hot-swappable personality overlays.

        Install soul blueprints, load overlays at runtime,
        and manage personality while preserving identity.
        """
        ctx.ensure_object(dict)
        ctx.obj["agent_name"] = agent

    @soul.command("list")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--installed-only", is_flag=True, help="Show only installed souls.")
    @click.option("--category", "filter_category", default=None, help="Filter by category.")
    @click.pass_context
    def soul_list(ctx, home, installed_only, filter_category):
        """List all available souls (installed and community repo)."""
        from ..soul import SoulManager

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path, agent_name=ctx.obj["agent_name"])
        all_souls = mgr.list_available()

        # Apply filters
        if installed_only:
            all_souls = [s for s in all_souls if s["source"] == "installed"]
        if filter_category:
            all_souls = [s for s in all_souls if s["category"].lower() == filter_category.lower()]

        if not all_souls:
            console.print("\n  [dim]No souls found.[/]")
            if installed_only:
                console.print("  [dim]Run: skcapstone soul install <path.md>[/]")
            console.print()
            return

        state = mgr.get_status()
        installed_count = sum(1 for s in all_souls if s["source"] == "installed")
        total = len(all_souls)

        console.print(f"\n  [bold]{total}[/] soul(s) available ({installed_count} installed)\n")

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Name", style="cyan", no_wrap=True)
        table.add_column("Category", style="yellow")
        table.add_column("Source", no_wrap=True)
        table.add_column("Description", style="dim")

        for s in all_souls:
            if s["source"] == "installed":
                source_tag = "[green][installed][/]"
            else:
                source_tag = "[dim][available][/]"

            name_display = s["name"]
            if s["name"] == state.active_soul:
                name_display = f"{s['name']} [green]<- ACTIVE[/]"

            table.add_row(name_display, s["category"], source_tag, s["description"])

        console.print(table)
        console.print()

    @soul.command("browse")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--category", "filter_category", default=None, help="Filter by category.")
    @click.option("--page", default=1, type=int, help="Page number (10 per page).")
    @click.option("--install", "install_name", default=None, help="Install a soul by name from browse results.")
    @click.pass_context
    def soul_browse(ctx, home, filter_category, page, install_name):
        """Browse available souls grouped by category with details.

        Shows a detailed view of all available souls from both installed
        and the community repo, grouped by category with full descriptions.

        Use --install <name> to install a soul directly from browse.
        """
        from ..soul import SoulManager

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path, agent_name=ctx.obj["agent_name"])

        # Handle --install flag: find and install from repo
        if install_name:
            slug = install_name.lower().replace(" ", "-")
            installed = mgr.list_installed()
            if slug in installed:
                console.print(f"\n  [yellow]Already installed:[/] {slug}\n")
                return

            found_path = _find_blueprint_in_repo(slug)
            if found_path is None:
                console.print(f"\n  [red]Blueprint not found in repo:[/] {install_name}")
                console.print("  Run [bold]skcapstone soul list[/] to see available souls.\n")
                sys.exit(1)

            try:
                bp = mgr.install(found_path)
                console.print(f"\n  [green]Installed:[/] [bold]{bp.display_name}[/] ({bp.name})")
                console.print(f"  Category: {bp.category}")
                if bp.vibe:
                    console.print(f"  Vibe: {bp.vibe[:80]}")
                audit_event(home_path, "SOUL_INSTALL", f"Soul '{bp.name}' installed from browse")
            except (ValueError, FileNotFoundError) as e:
                console.print(f"\n  [red]Failed to install:[/] {e}")
                sys.exit(1)
            console.print()
            return

        all_souls = mgr.list_available()

        if filter_category:
            all_souls = [s for s in all_souls if s["category"].lower() == filter_category.lower()]

        if not all_souls:
            console.print("\n  [dim]No souls found.[/]\n")
            return

        # Group by category
        by_category: dict[str, list[dict]] = {}
        for s in all_souls:
            by_category.setdefault(s["category"], []).append(s)

        # Pagination
        page_size = 10
        flat_entries = all_souls
        total_pages = (len(flat_entries) + page_size - 1) // page_size
        page = max(1, min(page, total_pages))
        start = (page - 1) * page_size
        end = start + page_size
        page_entries = flat_entries[start:end]

        # Group the page entries by category for display
        page_by_cat: dict[str, list[dict]] = {}
        for s in page_entries:
            page_by_cat.setdefault(s["category"], []).append(s)

        installed_count = sum(1 for s in all_souls if s["source"] == "installed")
        console.print(f"\n  [bold]{len(all_souls)}[/] soul(s) available ({installed_count} installed)")
        console.print(f"  Page {page}/{total_pages}\n")

        state = mgr.get_status()

        for category, souls in sorted(page_by_cat.items()):
            console.print(f"  [bold yellow]{category}[/] ({len(by_category.get(category, []))} total)")
            console.print()

            for s in souls:
                if s["source"] == "installed":
                    source_tag = "[green][installed][/]"
                else:
                    source_tag = "[dim][available][/]"

                active = " [green]<- ACTIVE[/]" if s["name"] == state.active_soul else ""
                console.print(f"    [bold cyan]{s['name']}[/] {source_tag}{active}")
                console.print(f"      [dim]{s['display_name']}[/]")
                if s["description"]:
                    console.print(f"      {s['description']}")
                console.print()

        if total_pages > 1:
            console.print(f"  [dim]Use --page N to navigate (1-{total_pages})[/]")
        console.print("  [dim]Use --install <name> to install a soul[/]\n")

    @soul.command("install")
    @click.argument("path", type=click.Path(exists=True))
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.pass_context
    def soul_install(ctx, path, home):
        """Install a soul from a blueprint markdown file."""
        from ..soul import SoulManager

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path, agent_name=ctx.obj["agent_name"])
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
    @click.pass_context
    def soul_install_all(ctx, directory, home):
        """Batch-install all blueprints from a directory."""
        from ..soul import SoulManager

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path, agent_name=ctx.obj["agent_name"])
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
    @click.pass_context
    def soul_load(ctx, name, home, reason):
        """Activate a soul overlay."""
        from ..soul import SoulManager

        validate_soul_name(name)

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path, agent_name=ctx.obj["agent_name"])
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
    @click.pass_context
    def soul_unload(ctx, home, reason):
        """Return to base soul."""
        from ..soul import SoulManager

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path, agent_name=ctx.obj["agent_name"])
        state = mgr.unload(reason=reason)
        if state.active_soul is None:
            console.print("\n  [green]Returned to base soul.[/]")
            audit_event(home_path, "SOUL_UNLOAD", "Returned to base soul", metadata={"reason": reason})
        else:
            console.print("\n  [dim]Already at base soul.[/]")
        console.print()

    @soul.command("status")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @_agent_option()
    @click.pass_context
    def soul_status(ctx, home, agent):
        """Show current soul state."""
        from ..soul import SoulManager

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path, agent_name=agent)
        state = mgr.get_status()
        installed = mgr.list_installed()

        active_display = state.active_soul or "[dim]base[/]"

        # Try to read vibe from base.json
        vibe = ""
        base_path = mgr.soul_dir / "base.json"
        if base_path.exists():
            try:
                import json
                base_data = json.loads(base_path.read_text(encoding="utf-8"))
                vibe = base_data.get("vibe", "")
            except Exception:
                pass

        lines = [
            f"Agent: [bold magenta]{agent}[/]",
            f"Base: [bold]{state.base_soul}[/]",
            f"Active: [bold cyan]{active_display}[/]",
            f"Installed: [bold]{len(installed)}[/] soul(s)",
            f"Activated at: {state.activated_at or '[dim]n/a[/]'}",
        ]
        if vibe:
            lines.insert(1, f"Vibe: [italic]{vibe}[/]")

        console.print()
        console.print(Panel(
            "\n".join(lines),
            title="Soul Layer", border_style="yellow",
        ))
        console.print()

    @soul.command("history")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--limit", "-n", default=20, help="Max entries to show.")
    @click.pass_context
    def soul_history(ctx, home, limit):
        """Show soul swap history."""
        from ..soul import SoulManager

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path, agent_name=ctx.obj["agent_name"])
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
    @click.pass_context
    def soul_info(ctx, name, home):
        """Show detailed info about an installed soul."""
        from ..soul import SoulManager

        validate_soul_name(name)

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path, agent_name=ctx.obj["agent_name"])
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
    @click.pass_context
    def soul_show(ctx, name, home):
        """Display the current soul identity or a named blueprint.

        With no argument, shows the active soul from skmemory's base.json.
        With a NAME, shows details of an installed soul overlay.
        """
        home_path = Path(home).expanduser()
        agent_name = ctx.obj["agent_name"]

        if name:
            # Show a specific installed soul overlay
            from ..soul import SoulManager
            validate_soul_name(name)
            mgr = SoulManager(home_path, agent_name=agent_name)
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
            if agent_name:
                soul_base = home_path / "agents" / agent_name / "soul"
            else:
                soul_base = home_path / "soul"
            soul_path = str(soul_base / "base.json")
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

    # -----------------------------------------------------------------------
    # soul registry — interact with the souls.skworld.io blueprint registry
    # -----------------------------------------------------------------------

    @soul.group("registry")
    def soul_registry():
        """Remote blueprint registry at souls.skworld.io.

        List, search, publish, and download community soul blueprints
        from the shared registry.
        """

    @soul_registry.command("list")
    @click.option("--url", default=None, help="Registry API base URL.")
    def registry_list(url):
        """List all blueprints in the remote registry.

        Pulls from the soul-blueprints GitHub repo. Falls back to
        the souls.skworld.io API if --url is set.
        """
        from ..blueprint_registry import (
            BlueprintRegistryClient,
            BlueprintRegistryError,
            _fetch_github_blueprints,
        )

        blueprints = None
        source = "github"

        # If custom URL, try the API server first
        if url:
            try:
                client = BlueprintRegistryClient(base_url=url)
                blueprints = client.list_blueprints()
                source = "registry"
            except BlueprintRegistryError:
                pass

        # Default: pull from GitHub repo
        if blueprints is None:
            blueprints = _fetch_github_blueprints()
            source = "github"

        if not blueprints:
            console.print("\n  [dim]No blueprints found.[/]\n")
            return

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Name", style="cyan", no_wrap=True)
        table.add_column("Display Name", style="bold")
        table.add_column("Category", style="yellow")

        for bp in blueprints:
            table.add_row(
                bp.get("name", "?"),
                bp.get("display_name", ""),
                bp.get("category", ""),
            )

        source_label = "" if source == "github" else "  [dim](registry)[/]"
        console.print()
        console.print(table)
        console.print(f"\n  [dim]{len(blueprints)} blueprint(s){source_label}[/]\n")

    @soul_registry.command("search")
    @click.argument("query")
    @click.option("--url", default=None, help="Registry API base URL.")
    def registry_search(query, url):
        """Search the remote registry for blueprints.

        Searches the soul-blueprints GitHub repo by name and category.
        """
        from ..blueprint_registry import (
            BlueprintRegistryClient,
            BlueprintRegistryError,
            _fetch_github_blueprints,
        )

        results = None

        # If custom URL, try the API server first
        if url:
            try:
                client = BlueprintRegistryClient(base_url=url)
                results = client.search_blueprints(query)
            except BlueprintRegistryError:
                pass

        # Default: search GitHub repo
        if results is None:
            results = _fetch_github_blueprints(query)

        if not results:
            console.print(f"\n  [dim]No blueprints matching '{query}'.[/]\n")
            return

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Name", style="cyan", no_wrap=True)
        table.add_column("Display Name", style="bold")
        table.add_column("Category", style="yellow")

        for bp in results:
            table.add_row(
                bp.get("name", "?"),
                bp.get("display_name", ""),
                bp.get("category", ""),
            )

        console.print()
        console.print(table)
        console.print(f"\n  [dim]{len(results)} result(s)[/]\n")

    @soul_registry.command("publish")
    @click.argument("name")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--url", default=None, help="Registry API base URL.")
    @click.pass_context
    def registry_publish(ctx, name, home, url):
        """Publish an installed soul blueprint to the registry.

        NAME is the slug of an installed soul (see `skcapstone soul list`).
        Requires a DID identity for authentication.
        """
        from ..blueprint_registry import BlueprintRegistryClient, BlueprintRegistryError
        from ..soul import SoulManager

        home_path = Path(home).expanduser()
        mgr = SoulManager(home_path, agent_name=ctx.obj["agent_name"])
        bp = mgr.get_info(name)

        if bp is None:
            console.print(f"\n  [red]Soul not found:[/] {name}")
            console.print("  Run [bold]skcapstone soul list[/] to see installed souls.\n")
            sys.exit(1)

        kwargs = {}
        if url:
            kwargs["base_url"] = url
        client = BlueprintRegistryClient(**kwargs)

        try:
            soul_data = json.loads(bp.model_dump_json())
            result = client.publish_blueprint(soul_data)
            console.print(f"\n  [green]Published:[/] [bold]{bp.display_name}[/] ({bp.name})")
            soul_id = result.get("soul_id", result.get("id", name))
            console.print(f"  Registry ID: {soul_id}")
            audit_event(home_path, "SOUL_REGISTRY_PUBLISH", f"Published '{name}' to registry")
        except BlueprintRegistryError as e:
            console.print(f"\n  [red]Publish failed:[/] {e}")
            sys.exit(1)
        console.print()

    @soul_registry.command("download")
    @click.argument("soul_id")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--url", default=None, help="Registry API base URL.")
    @click.option("--install", "do_install", is_flag=True, help="Also install after download.")
    def registry_download(soul_id, home, url, do_install):
        """Download a blueprint from the registry.

        SOUL_ID is the registry identifier of the blueprint.
        Use --install to also install it locally.
        """
        from ..blueprint_registry import BlueprintRegistryClient, BlueprintRegistryError

        home_path = Path(home).expanduser()

        kwargs = {}
        if url:
            kwargs["base_url"] = url
        client = BlueprintRegistryClient(**kwargs)

        try:
            if do_install:
                dest = client.download_and_install(soul_id, home=home_path)
                console.print(f"\n  [green]Downloaded and installed:[/] {soul_id}")
                console.print(f"  Saved to: {dest}")
                audit_event(home_path, "SOUL_REGISTRY_DOWNLOAD", f"Downloaded '{soul_id}' from registry")
            else:
                bp_data = client.download_blueprint(soul_id)
                console.print(f"\n  [green]Downloaded:[/] {soul_id}")
                console.print(json.dumps(bp_data, indent=2))
        except BlueprintRegistryError as e:
            console.print(f"\n  [red]Download failed:[/] {e}")
            sys.exit(1)
        console.print()

    @soul.command("swap")
    @click.argument("blueprint")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--reason", "-r", default="", help="Reason for the swap.")
    @click.pass_context
    def soul_swap(ctx, blueprint, home, reason):
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
        mgr = SoulManager(home_path, agent_name=ctx.obj["agent_name"])

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
