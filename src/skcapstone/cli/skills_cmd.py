"""Skills commands: list, install."""

from __future__ import annotations

import json
import logging
import sys
import urllib.request
from pathlib import Path
from typing import Optional

import click
import yaml
from rich.panel import Panel
from rich.table import Table

from ..registry_client import get_registry_client
from ._common import AGENT_HOME, console

logger = logging.getLogger(__name__)

# Raw catalog.yaml from the skskills GitHub repo (always fresh)
_GITHUB_CATALOG_URL = "https://raw.githubusercontent.com/smilinTux/skskills/main/catalog.yaml"


def _fetch_github_catalog(query: str = "") -> Optional[list[dict]]:
    """Fetch catalog.yaml from the skskills GitHub repo.

    Returns:
        List of skill entry dicts, or None on failure.
    """
    try:
        req = urllib.request.Request(_GITHUB_CATALOG_URL, headers={"User-Agent": "skcapstone"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = yaml.safe_load(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.debug("GitHub catalog fetch failed: %s", exc)
        return None

    entries = []
    q = query.lower()
    for item in raw.get("skills", []):
        name = item.get("name", "")
        desc = item.get("description", "").strip()
        tags = item.get("tags", [])

        if q and not (q in name.lower() or q in desc.lower() or any(q in t.lower() for t in tags)):
            continue

        entries.append(
            {
                "name": name,
                "description": desc,
                "tags": tags,
                "category": item.get("category", ""),
                "pip": item.get("pip", ""),
                "git": item.get("git", ""),
            }
        )

    return entries


def register_skills_commands(main: click.Group) -> None:
    """Register the skills command group."""

    @main.group()
    def skills():
        """Skills registry - discover and install agent skills.

        Fetches the latest skill catalog from GitHub. Falls back to the
        locally installed catalog if offline.

        Set SKSKILLS_REGISTRY_URL to override with a custom registry server.
        """

    @skills.command("list")
    @click.option("--query", "-q", default="", help="Filter by name, description, or tag.")
    @click.option(
        "--registry",
        default=None,
        envvar="SKSKILLS_REGISTRY_URL",
        help="Override the skills registry URL.",
    )
    @click.option("--json", "json_out", is_flag=True, help="Output raw JSON.")
    @click.option("--offline", is_flag=True, help="Use local catalog only (no network).")
    def skills_list(query: str, registry: str | None, json_out: bool, offline: bool) -> None:
        """List skills available in the catalog.

        Pulls the latest catalog from the skskills GitHub repo.
        Falls back to local catalog if offline or fetch fails.

        Examples:

            skcapstone skills list

            skcapstone skills list --query syncthing

            skcapstone skills list --query identity --json

            skcapstone skills list --offline
        """
        skill_entries = None
        source = "github"

        # 1. Try custom registry server if configured
        if registry:
            client = get_registry_client(registry)
            if client is not None:
                try:
                    skill_entries = client.search(query) if query else client.list_skills()
                    source = "remote"
                except Exception as exc:
                    logger.warning("Registry client query failed, falling back: %s", exc)

        # 2. Try GitHub raw catalog (always fresh, no server needed)
        if skill_entries is None and not offline:
            skill_entries = _fetch_github_catalog(query)
            source = "github"

        # 3. Fall back to local catalog (bundled with skskills package)
        if skill_entries is None:
            try:
                from skskills.catalog import SkillCatalog

                catalog = SkillCatalog()
                if query:
                    entries = catalog.search(query)
                else:
                    entries = catalog.list_all()
                skill_entries = [
                    {
                        "name": e.name,
                        "description": e.description,
                        "tags": e.tags,
                        "category": e.category,
                        "pip": e.pip,
                        "git": e.git,
                    }
                    for e in entries
                ]
                source = "local"
            except ImportError:
                console.print(
                    "[bold red]skskills not installed and GitHub unreachable.[/] "
                    "Run: pip install skskills"
                )
                sys.exit(1)
            except Exception as exc:
                console.print(f"[bold red]Catalog error:[/] {exc}")
                sys.exit(1)

        if json_out:
            click.echo(json.dumps(skill_entries, indent=2))
            return

        if not skill_entries:
            suffix = f" matching '{query}'" if query else ""
            console.print(f"\n  [dim]No skills found{suffix}.[/]\n")
            return

        source_labels = {
            "github": "",
            "remote": "  [dim](registry)[/]",
            "local": "  [dim](local - offline)[/]",
        }
        label = f"[bold]{len(skill_entries)}[/] skill(s)"
        if query:
            label += f" matching [cyan]'{query}'[/]"
        label += source_labels.get(source, "")

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Name", style="cyan")
        table.add_column("Category", style="dim")
        table.add_column("Description")
        table.add_column("Tags", style="dim")

        for s in skill_entries:
            table.add_row(
                s.get("name", ""),
                s.get("category", ""),
                s.get("description", ""),
                ", ".join(s.get("tags", [])),
            )

        console.print()
        console.print(Panel(label, title="Skills Catalog", border_style="bright_blue"))
        console.print(table)
        console.print()

    @skills.command("install")
    @click.argument("name")
    @click.option("--version", default=None, help="Specific version (default: latest).")
    @click.option(
        "--agent",
        default="global",
        help="Agent namespace for installation (default: global).",
    )
    @click.option("--force", is_flag=True, help="Overwrite an existing installation.")
    @click.option(
        "--registry",
        default=None,
        envvar="SKSKILLS_REGISTRY_URL",
        help="Override the skills registry URL.",
    )
    def skills_install(
        name: str,
        version: str | None,
        agent: str,
        force: bool,
        registry: str | None,
    ) -> None:
        """Download and install a skill from the remote registry.

        Fetches the skill package, verifies its checksum, and installs it
        into the local SKSkills directory for the specified agent namespace.

        Examples:

            skcapstone skills install syncthing-setup

            skcapstone skills install pgp-identity --version 0.2.0

            skcapstone skills install syncthing-setup --agent opus
        """
        client = get_registry_client(registry)
        if client is None:
            console.print("[bold red]skskills not installed.[/] " "Run: pip install skskills")
            sys.exit(1)

        ver_label = f" @{version}" if version else ""
        agent_label = f" (agent: {agent})" if agent != "global" else ""
        console.print(f"\n  Installing [cyan]{name}[/][dim]{ver_label}{agent_label}[/] ...\n")

        try:
            result = client.install(name, version=version, agent=agent, force=force)
        except FileNotFoundError:
            console.print(
                f"[bold red]Not found:[/] skill [cyan]{name}[/] is not in the registry.\n"
                f"  Run [dim]skcapstone skills list --query {name}[/] to search."
            )
            console.print()
            sys.exit(1)
        except ValueError as exc:
            console.print(f"[bold red]Install failed:[/] {exc}\n")
            sys.exit(1)
        except Exception as exc:
            console.print(f"[bold red]Error:[/] {exc}\n")
            sys.exit(1)

        console.print(f"  [green]Installed:[/] [bold]{result['name']}[/] v{result['version']}")
        console.print(f"  [dim]Path:  {result['install_path']}[/]")
        console.print(f"  [dim]Agent: {result['agent']}[/]\n")

    @skills.command("link")
    @click.argument("skill_name")
    @click.argument("agent_name")
    def skills_link(skill_name: str, agent_name: str) -> None:
        """Link a global skill into an agent's namespace.

        \b
        Example:
            skcapstone skills link syncthing-setup jarvis
        """
        try:
            from skskills.registry import SkillRegistry
        except ImportError:
            console.print("[red]skskills is not installed.[/red] Run: pip install skskills")
            return

        registry = SkillRegistry()
        try:
            path = registry.link_to_agent(skill_name, agent_name)
            console.print(f"[green]Linked:[/green] {skill_name} → {agent_name}")
            console.print(f"  Path: {path}")
        except FileNotFoundError as exc:
            console.print(f"[red]Link failed:[/red] {exc}")

    @skills.command("status")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory.")
    def skills_status(home: str) -> None:
        """Show skills pillar status for all agents.

        Reports per-agent skill counts from both the registry
        and the skcapstone synced skills directory.
        """
        import os

        home_path = Path(home).expanduser()
        skskills_home = Path(os.environ.get("SKSKILLS_HOME", "~/.skskills")).expanduser()

        console.print("\n[bold cyan]Skills Pillar Status[/]\n")
        console.print(f"  SKSkills home:    {skskills_home}")
        console.print(f"  SKCapstone home:  {home_path}")

        agents_dir = skskills_home / "agents"
        table = Table(title="Per-Agent Skill Counts")
        table.add_column("Agent", style="green")
        table.add_column("Registry Skills", style="yellow")
        table.add_column("Skcapstone Skills", style="cyan")
        table.add_column("Total")

        # Collect known agents from both registries
        known_agents: set[str] = set()
        if agents_dir.exists():
            for d in agents_dir.iterdir():
                if d.is_dir():
                    known_agents.add(d.name)

        skcap_agents_dir = home_path / "skills" / "agents"
        if skcap_agents_dir.exists():
            for d in skcap_agents_dir.iterdir():
                if d.is_dir():
                    known_agents.add(d.name)

        if not known_agents:
            console.print("\n[dim]No per-agent namespaces configured yet.[/dim]")
            console.print("  Create one with: skcapstone skills link syncthing-setup <agent>")
            return

        for agent_name in sorted(known_agents):
            # Count registry skills
            reg_count = 0
            if (agents_dir / agent_name).exists():
                reg_count = sum(
                    1
                    for d in (agents_dir / agent_name).iterdir()
                    if (d.is_dir() or d.is_symlink()) and (d / "skill.yaml").exists()
                )

            # Count skcapstone skills
            skcap_count = 0
            agent_skcap = skcap_agents_dir / agent_name if skcap_agents_dir.exists() else None
            if agent_skcap and agent_skcap.exists():
                skcap_count = sum(
                    1 for d in agent_skcap.iterdir() if d.is_dir() and (d / "skill.yaml").exists()
                )

            total = reg_count + skcap_count
            table.add_row(agent_name, str(reg_count), str(skcap_count), str(total))

        console.print()
        console.print(table)

        # Global registry
        installed_dir = skskills_home / "installed"
        global_count = 0
        if installed_dir.exists():
            global_count = sum(
                1 for d in installed_dir.iterdir() if d.is_dir() and (d / "skill.yaml").exists()
            )
        console.print(
            f"\n  Global registry: [yellow]{global_count}[/] skill(s) in {installed_dir}"
        )
        console.print(
            "\n  [dim]Tip: Use [white]skcapstone skills link <skill> <agent>[/white] "
            "to give an agent access to a global skill.[/dim]\n"
        )
