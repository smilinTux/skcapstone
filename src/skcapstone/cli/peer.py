"""Peer management commands: add, list, remove, show."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console

from rich.panel import Panel
from rich.table import Table


def register_peer_commands(main: click.Group) -> None:
    """Register the peer command group."""

    @main.group()
    def peer():
        """Peer management — discover, add, and manage trusted contacts."""

    @peer.command("add")
    @click.option("--card", "card_path", type=click.Path(exists=True), help="Import from identity card.")
    @click.option("--name", default=None, help="Peer name (required if not using --card).")
    @click.option("--pubkey", default=None, type=click.Path(exists=True), help="Path to PGP public key.")
    @click.option("--email", default=None, help="Peer contact email.")
    @click.option("--home", "sk_home", default=AGENT_HOME, type=click.Path())
    def peer_add(card_path, name, pubkey, email, sk_home):
        """Add a peer from an identity card or manually."""
        from ..peers import add_peer_from_card, add_peer_manual

        sk_path = Path(sk_home).expanduser()

        if card_path:
            try:
                peer_record = add_peer_from_card(Path(card_path), skcapstone_home=sk_path)
                fp = peer_record.fingerprint[:16] + "..." if peer_record.fingerprint else "none"
                console.print(f"\n  [green]Added peer:[/] [cyan]{peer_record.name}[/]")
                console.print(f"  Fingerprint: [dim]{fp}[/]")
                console.print(f"  Type: {peer_record.entity_type}")
                console.print(f"  Trust: {peer_record.trust_level}")
                console.print(f"  Capabilities: {', '.join(peer_record.capabilities[:5])}")
                if peer_record.public_key:
                    console.print(f"  [green]Public key imported[/] — encrypted messaging enabled")
                console.print()
            except (FileNotFoundError, ValueError) as exc:
                console.print(f"\n  [red]Error:[/] {exc}\n")
                sys.exit(1)
        elif name:
            peer_record = add_peer_manual(
                name=name, public_key_path=Path(pubkey) if pubkey else None,
                email=email or "", skcapstone_home=sk_path,
            )
            console.print(f"\n  [green]Added peer:[/] [cyan]{peer_record.name}[/]")
            if peer_record.public_key:
                console.print(f"  [green]Public key imported[/]")
            console.print()
        else:
            console.print("\n  [yellow]Provide --card or --name.[/]")
            console.print("  skcapstone peer add --card card.json")
            console.print("  skcapstone peer add --name Lumina --pubkey lumina.pub.asc\n")
            sys.exit(1)

    @peer.command("list")
    @click.option("--home", "sk_home", default=AGENT_HOME, type=click.Path())
    @click.option("--json-out", is_flag=True, help="Output as JSON.")
    def peer_list(sk_home, json_out):
        """List all known peers."""
        from ..peers import list_peers

        peers = list_peers(skcapstone_home=Path(sk_home).expanduser())

        if json_out:
            click.echo(json.dumps([p.model_dump() for p in peers], indent=2, default=str))
            return

        console.print()
        if not peers:
            console.print("  [dim]No peers registered.[/]")
            console.print("  Add one: skcapstone peer add --card card.json")
            console.print()
            return

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2),
                      title=f"Known Peers ({len(peers)})")
        table.add_column("Name", style="cyan")
        table.add_column("Type", style="dim")
        table.add_column("Fingerprint", style="dim", max_width=20)
        table.add_column("Trust")
        table.add_column("Key")

        for p in peers:
            fp = p.fingerprint[:16] + "..." if p.fingerprint else ""
            has_key = "[green]yes[/]" if p.public_key else "[yellow]no[/]"
            table.add_row(p.name, p.entity_type, fp, p.trust_level, has_key)

        console.print(table)
        console.print()

    @peer.command("remove")
    @click.argument("name")
    @click.option("--home", "sk_home", default=AGENT_HOME, type=click.Path())
    def peer_remove(name, sk_home):
        """Remove a peer by name."""
        from ..peers import remove_peer

        removed = remove_peer(name, skcapstone_home=Path(sk_home).expanduser())
        if removed:
            console.print(f"\n  [green]Removed peer:[/] {name}\n")
        else:
            console.print(f"\n  [yellow]Peer '{name}' not found.[/]\n")

    @peer.command("show")
    @click.argument("name")
    @click.option("--home", "sk_home", default=AGENT_HOME, type=click.Path())
    def peer_show(name, sk_home):
        """Show detailed info about a peer."""
        from ..peers import get_peer

        p = get_peer(name, skcapstone_home=Path(sk_home).expanduser())
        if not p:
            console.print(f"\n  [yellow]Peer '{name}' not found.[/]\n")
            return

        fp = p.fingerprint[:20] + "..." if len(p.fingerprint) > 20 else p.fingerprint
        lines = [
            f"[bold]Name:[/]         [cyan]{p.name}[/]",
            f"[bold]Type:[/]         {p.entity_type}",
            f"[bold]Fingerprint:[/]  [dim]{fp}[/]",
            f"[bold]Trust:[/]        {p.trust_level}",
            f"[bold]Source:[/]       {p.source}",
            f"[bold]Added:[/]        {p.added_at[:19]}",
        ]
        if p.handle:
            lines.append(f"[bold]Handle:[/]       {p.handle}")
        if p.email:
            lines.append(f"[bold]Email:[/]        {p.email}")
        if p.capabilities:
            lines.append(f"[bold]Capabilities:[/] {', '.join(p.capabilities[:6])}")
        if p.contact_uris:
            for uri in p.contact_uris:
                lines.append(f"[bold]Contact:[/]      [cyan]{uri}[/]")
        lines.append(f"[bold]PGP Key:[/]      {'[green]present[/]' if p.public_key else '[yellow]missing[/]'}")

        console.print()
        console.print(Panel("\n".join(lines), title=f"Peer: {p.name}", border_style="cyan"))
        console.print()
