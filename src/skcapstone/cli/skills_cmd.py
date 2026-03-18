"""Skills commands: list, install."""

from __future__ import annotations

import json
import logging
import sys
import urllib.request
from typing import Optional

import click
import yaml

from ._common import AGENT_HOME, console
from ..registry_client import get_registry_client

from rich.panel import Panel
from rich.table import Table

logger = logging.getLogger(__name__)

# Raw catalog.yaml from the skskills GitHub repo (always fresh)
_GITHUB_CATALOG_URL = (
    "https://raw.githubusercontent.com/smilinTux/skskills/main/catalog.yaml"
)


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

        if q and not (
            q in name.lower()
            or q in desc.lower()
            or any(q in t.lower() for t in tags)
        ):
            continue

        entries.append({
            "name": name,
            "description": desc,
            "tags": tags,
            "category": item.get("category", ""),
            "pip": item.get("pip", ""),
            "git": item.get("git", ""),
        })

    return entries


def register_skills_commands(main: click.Group) -> None:
    """Register the skills command group."""

    @main.group()
    def skills():
        """Skills registry — discover and install agent skills.

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
            "local": "  [dim](local — offline)[/]",
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
            console.print(
                "[bold red]skskills not installed.[/] "
                "Run: pip install skskills"
            )
            sys.exit(1)

        ver_label = f" @{version}" if version else ""
        agent_label = f" (agent: {agent})" if agent != "global" else ""
        console.print(
            f"\n  Installing [cyan]{name}[/][dim]{ver_label}{agent_label}[/] ...\n"
        )

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

        console.print(
            f"  [green]Installed:[/] [bold]{result['name']}[/] v{result['version']}"
        )
        console.print(f"  [dim]Path:  {result['install_path']}[/]")
        console.print(f"  [dim]Agent: {result['agent']}[/]\n")
